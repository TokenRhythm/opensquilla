"""Pure, turn-local execution contracts.

This module is deliberately a leaf.  It contains only standard-library types
so the identity and publication state can be imported by engine, provider, and
gateway boundaries without creating an import cycle.

The context is intentionally not copyable.  A turn has one owner and one
mutable ledger; copying it would create a second, indistinguishable authority
for attempts, generation epochs, or publication.
"""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class TurnExecutionError(RuntimeError):
    """Base error for invalid turn-local state transitions."""


class ProviderAdmissionError(TurnExecutionError):
    """Raised when a provider call cannot be admitted by the turn ledger."""


class StickyExecutionRole(StrEnum):
    """Roles that may own provider calls during one turn."""

    PROPOSER = "proposer"
    PRIMARY_AGGREGATOR = "primary_aggregator"
    FIXED_AGGREGATOR = "fixed_aggregator"
    FIXED_DIRECT = "fixed_direct"
    TERMINAL = "terminal"


def _role(value: StickyExecutionRole | str) -> StickyExecutionRole:
    if isinstance(value, StickyExecutionRole):
        return value
    try:
        return StickyExecutionRole(str(value))
    except ValueError as exc:
        raise ValueError(f"unknown execution role: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    """Immutable causal identity shared by every event and durable write."""

    turn_id: str
    assistant_message_id: str
    session_key: str
    channel_id: str | None = None
    turn_start_sequence: int = 0

    def __post_init__(self) -> None:
        for name in ("turn_id", "assistant_message_id", "session_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.channel_id is not None and not isinstance(self.channel_id, str):
            raise TypeError("channel_id must be a string or None")
        if self.turn_start_sequence < 0:
            raise ValueError("turn_start_sequence must be non-negative")


@dataclass(frozen=True, slots=True)
class AssistantMessageReservation:
    """Turn-local reservation; creating it never writes a transcript row."""

    turn_id: str
    assistant_message_id: str
    session_key: str
    channel_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderCallLease:
    """Single-use admission proof for one logical-call attempt."""

    lease_id: str
    turn_id: str
    role: StickyExecutionRole
    logical_call_index: int
    attempt_index: int
    owner: str
    adapter_request_budget: int = 1


@dataclass(slots=True)
class CallAttemptLedger:
    """Mutable accounting for one role/logical-call pair."""

    role: StickyExecutionRole
    logical_call_index: int
    attempt_indices: list[int] = field(default_factory=list)
    request_starts: int = 0
    request_start_evidence: list[Any] = field(default_factory=list)
    outcomes: list[Any] = field(default_factory=list)


def _freeze(value: Any) -> Any:
    """Recursively freeze common JSON-shaped recovery snapshots."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    # Dataclass/provider message objects are not JSON-shaped, but recovery must
    # still be isolated from later mutation of the caller-owned object graph.
    # Keep their concrete type for replay while severing the original identity.
    try:
        return copy.deepcopy(value)
    except Exception as exc:  # pragma: no cover - defensive contract boundary
        raise TypeError(
            f"recovery snapshot value is not safely copyable: {type(value).__name__}"
        ) from exc


@dataclass(frozen=True, slots=True)
class RecoveryContext:
    """Immutable successful-draft/context snapshot for fixed-role takeover."""

    successful_drafts: tuple[Any, ...] = ()
    tool_schemas: tuple[Any, ...] = ()
    attachment_boundary: Any = None
    conversation: tuple[Any, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "successful_drafts",
            tuple(_freeze(item) for item in self.successful_drafts),
        )
        object.__setattr__(
            self,
            "tool_schemas",
            tuple(_freeze(item) for item in self.tool_schemas),
        )
        object.__setattr__(self, "attachment_boundary", _freeze(self.attachment_boundary))
        object.__setattr__(
            self,
            "conversation",
            tuple(_freeze(item) for item in self.conversation),
        )
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(
                {str(key): _freeze(item) for key, item in self.metadata.items()}
            ),
        )


@dataclass(frozen=True, slots=True)
class CompletedToolRecord:
    """One tool round that crossed the legal Done boundary."""

    call_id: str
    events: tuple[Any, ...]
    done_event: Any
    sequence: int


@dataclass(frozen=True, slots=True)
class UsageExecutionLeg:
    """Immutable usage identity for one execution leg."""

    execution_id: str
    role: StickyExecutionRole
    logical_call_index: int
    attempt_index: int
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class SurfaceCapabilities:
    """Capabilities relevant to same-message publication."""

    supports_streaming: bool = True
    supports_edit: bool = True
    supports_generation_reset: bool = True


@dataclass(slots=True)
class GenerationState:
    """Current public generation for a turn."""

    epoch: int = 0
    owner: StickyExecutionRole = StickyExecutionRole.PRIMARY_AGGREGATOR
    text_snapshot: str = ""
    reasoning_snapshot: str = ""
    last_sequence: int = 0
    terminal: bool = False


@dataclass(slots=True)
class TurnPublicationLedger:
    """Same-message publication state with monotonic event de-duplication."""

    turn_id: str
    channel_id: str | None
    assistant_message_id: str
    generation_epoch: int = 0
    last_sequence: int = -1
    reserved: bool = True
    published: bool = False
    visible_output: bool = False
    terminal: bool = False
    released: bool = False
    text_snapshot: str = ""
    reasoning_snapshot: str = ""
    _seen_sequences: set[tuple[int, int]] = field(default_factory=set, repr=False)

    def accept(
        self,
        *,
        generation_epoch: int,
        sequence: int,
        text: str = "",
        reasoning: str = "",
        terminal: bool = False,
    ) -> bool:
        if self.released or generation_epoch < self.generation_epoch:
            return False
        if sequence <= self.last_sequence:
            return False
        marker = (generation_epoch, sequence)
        if marker in self._seen_sequences:
            return False
        self._seen_sequences.add(marker)
        self.generation_epoch = generation_epoch
        self.last_sequence = sequence
        self.text_snapshot = text
        self.reasoning_snapshot = reasoning
        self.published = True
        self.visible_output = True
        self.terminal = self.terminal or terminal
        return True


@dataclass(slots=True)
class PendingToolBuffer:
    """Per-provider-call tool events hidden until a legal Done is received."""

    call_id: str
    events: list[Any] = field(default_factory=list)
    closed: bool = False

    def append(self, event: Any) -> None:
        if self.closed:
            raise TurnExecutionError(f"tool buffer is closed: {self.call_id}")
        self.events.append(event)

    def close(self) -> None:
        self.closed = True


@dataclass(frozen=True, slots=True)
class RoleActivationResult:
    """Result of the single fixed-role activation transaction."""

    activated: bool
    role: StickyExecutionRole
    reason: str
    fallback_activation_count: int
    generation_epoch: int
    sequence: int | None = None


@dataclass(frozen=True, slots=True)
class CleanupHandle:
    """Opaque handle for a task owned by the turn cleanup registry."""

    handle_id: str
    owner: str


class TurnExecutionContext:
    """Single-owner mutable ledger for one logical turn.

    The public identity and snapshots are immutable.  Mutable transitions are
    serialized by an internal asyncio lock; callers must pass this same object
    explicitly to downstream stages/providers.
    """

    def __init__(
        self,
        identity: TurnIdentity,
        *,
        control: Any = None,
        deadline: float | None = None,
        surface: SurfaceCapabilities | Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(identity, TurnIdentity):
            raise TypeError("identity must be a TurnIdentity")
        if surface is None:
            resolved_surface = SurfaceCapabilities()
        elif isinstance(surface, SurfaceCapabilities):
            resolved_surface = surface
        else:
            resolved_surface = SurfaceCapabilities(
                **{
                    name: bool(value)
                    for name, value in surface.items()
                    if name in {
                        "supports_streaming",
                        "supports_edit",
                        "supports_generation_reset",
                    }
                }
            )
        self._identity = identity
        self._control = control
        self._deadline = deadline
        self._surface = resolved_surface
        self._lock = asyncio.Lock()
        self._closed = False
        # A terminal generation is a one-way authority boundary. Keep this
        # separate from ``current_role`` so a terminal fixed reset can retain
        # its diagnostic role while still rejecting every late provider call
        # and event.
        self._terminalized = False
        self._sequence = identity.turn_start_sequence
        self._last_event_sequence = identity.turn_start_sequence - 1
        self._last_meaningful_progress_at: float | None = None
        self._current_role = StickyExecutionRole.PRIMARY_AGGREGATOR
        self._fallback_activation_count = 0
        self._fallback_started = False
        self._fixed_started = False
        self._recovery_terminal = False
        self._suppress_selector_fallback = False
        self._successful_drafts: tuple[Any, ...] = ()
        self._recovery_context: RecoveryContext | None = None
        self._usage_legs: list[UsageExecutionLeg] = []
        self._generation_state = GenerationState(
            last_sequence=identity.turn_start_sequence
        )
        self._publication_ledger = TurnPublicationLedger(
            turn_id=identity.turn_id,
            channel_id=identity.channel_id,
            assistant_message_id=identity.assistant_message_id,
            last_sequence=identity.turn_start_sequence - 1,
        )
        self._attempt_ledgers: dict[tuple[StickyExecutionRole, int], CallAttemptLedger] = {}
        self._active_leases: dict[str, ProviderCallLease] = {}
        self._lease_started: set[str] = set()
        self._lease_finished: dict[str, Any] = {}
        self._tool_buffers: dict[str, PendingToolBuffer] = {}
        self._completed_tools: list[CompletedToolRecord] = []
        self._cleanup_tasks: dict[str, asyncio.Future[Any] | asyncio.Task[Any]] = {}
        self._cleanup_handles: dict[str, CleanupHandle] = {}
        self._proposer_logical_tasks_created = 0
        self._proposer_admissions = 0
        self._proposer_request_starts = 0
        self._primary_logical_call_index = -1
        self._fixed_logical_call_index = -1
        self._selector_hops_after_fixed = 0

    @classmethod
    def create(
        cls,
        identity: TurnIdentity,
        control: Any = None,
        deadline: float | None = None,
        surface: SurfaceCapabilities | Mapping[str, Any] | None = None,
    ) -> TurnExecutionContext:
        """Create a fresh context; each logical turn must call this once."""

        return cls(identity, control=control, deadline=deadline, surface=surface)

    def __copy__(self) -> TurnExecutionContext:
        raise TypeError("TurnExecutionContext is single-owner and cannot be copied")

    def __deepcopy__(self, memo: dict[int, Any]) -> TurnExecutionContext:
        del memo
        raise TypeError("TurnExecutionContext is single-owner and cannot be copied")

    @property
    def identity(self) -> TurnIdentity:
        return self._identity

    @property
    def control(self) -> Any:
        return self._control

    @property
    def deadline(self) -> float | None:
        return self._deadline

    def set_deadline_if_missing(self, deadline: float | None) -> bool:
        """Install one absolute monotonic deadline before provider admission."""

        if deadline is None or self._closed or self._deadline is not None:
            return False
        if isinstance(deadline, bool) or not isinstance(deadline, int | float):
            raise TypeError("deadline must be a monotonic timestamp")
        self._deadline = float(deadline)
        return True

    @property
    def surface(self) -> SurfaceCapabilities:
        return self._surface

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def terminal(self) -> bool:
        """Whether this turn has crossed its terminal publication boundary."""

        return self._terminalized or self._generation_state.terminal

    @property
    def generation_epoch(self) -> int:
        return self._generation_state.epoch

    @property
    def generation(self) -> GenerationState:
        return self._generation_state

    @property
    def assistant_message_id(self) -> str:
        return self._identity.assistant_message_id

    @property
    def publication_ledger(self) -> TurnPublicationLedger:
        return self._publication_ledger

    @property
    def recovery_context(self) -> RecoveryContext | None:
        return self._recovery_context

    @property
    def successful_drafts(self) -> tuple[Any, ...]:
        return self._successful_drafts

    @property
    def usage_execution_legs(self) -> tuple[UsageExecutionLeg, ...]:
        return tuple(self._usage_legs)

    @property
    def completed_tools(self) -> tuple[CompletedToolRecord, ...]:
        return tuple(self._completed_tools)

    @property
    def attempt_ledgers(self) -> Mapping[tuple[StickyExecutionRole, int], CallAttemptLedger]:
        return MappingProxyType(dict(self._attempt_ledgers))

    @property
    def pending_tool_buffers(self) -> Mapping[str, PendingToolBuffer]:
        return MappingProxyType(dict(self._tool_buffers))

    @property
    def proposer_logical_tasks_created(self) -> int:
        return self._proposer_logical_tasks_created

    @property
    def proposer_admissions(self) -> int:
        return self._proposer_admissions

    @property
    def proposer_request_starts(self) -> int:
        return self._proposer_request_starts

    @property
    def primary_logical_call_index(self) -> int:
        return self._primary_logical_call_index

    @property
    def fixed_logical_call_index(self) -> int:
        return self._fixed_logical_call_index

    @property
    def last_meaningful_progress_at(self) -> float | None:
        return self._last_meaningful_progress_at

    @property
    def fallback_activation_count(self) -> int:
        return self._fallback_activation_count

    @property
    def selector_hops_after_fixed(self) -> int:
        return self._selector_hops_after_fixed

    @property
    def recovery_markers(self) -> Mapping[str, bool]:
        """Canonical role-transaction markers for diagnostics and tests."""

        return MappingProxyType(
            {
                "ensemble_fallback_consumed": bool(
                    self._fallback_activation_count
                ),
                "fallback_started": self._fallback_started,
                "fixed_started": self._fixed_started,
                "recovery_terminal": self._recovery_terminal,
                "suppress_selector_fallback": self._suppress_selector_fallback,
            }
        )

    @property
    def counters(self) -> Mapping[str, int]:
        return MappingProxyType(
            {
                "proposer_logical_tasks_created": self._proposer_logical_tasks_created,
                "proposer_admissions": self._proposer_admissions,
                "proposer_request_starts": self._proposer_request_starts,
                "primary_logical_call_index": self._primary_logical_call_index,
                "fixed_logical_call_index": self._fixed_logical_call_index,
                "fallback_activation_count": self._fallback_activation_count,
                "selector_hops_after_fixed": self._selector_hops_after_fixed,
                "completed_tool_rounds": len(self._completed_tools),
            }
        )

    def current_role(self) -> StickyExecutionRole:
        return self._current_role

    def next_sequence(self) -> int:
        """Allocate a turn-local sequence for context-owned events."""

        self._sequence += 1
        return self._sequence

    async def record_proposer_logical_task(self) -> int:
        async with self._lock:
            self._ensure_open()
            self._proposer_logical_tasks_created += 1
            return self._proposer_logical_tasks_created

    def record_successful_drafts(self, drafts: Any) -> None:
        """Replace the immutable successful-draft snapshot before takeover."""

        self._ensure_open()
        if isinstance(drafts, (str, bytes)):
            raise TypeError("successful drafts must be a collection")
        self._successful_drafts = tuple(_freeze(item) for item in drafts)

    def record_usage_leg(self, leg: UsageExecutionLeg) -> None:
        self._ensure_open()
        if not isinstance(leg, UsageExecutionLeg):
            raise TypeError("leg must be a UsageExecutionLeg")
        self._usage_legs.append(leg)

    async def admit_provider_call(
        self,
        role: StickyExecutionRole | str,
        logical_call_index: int,
        attempt_index: int,
        owner: str,
    ) -> ProviderCallLease:
        """Admit exactly one physical attempt for a logical provider call."""

        normalized_role = _role(role)
        if logical_call_index < 0:
            raise ValueError("logical_call_index must be non-negative")
        if attempt_index not in (0, 1):
            raise ValueError("attempt_index must be 0 or 1")
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner must be a non-empty string")

        async with self._lock:
            self._ensure_open()
            self._ensure_admission_allowed()
            if self._terminalized or self._generation_state.terminal:
                raise ProviderAdmissionError(
                    "terminal turns cannot admit provider calls"
                )
            if normalized_role == StickyExecutionRole.TERMINAL:
                raise ProviderAdmissionError("terminal turns cannot admit provider calls")
            if self._current_role == StickyExecutionRole.TERMINAL:
                raise ProviderAdmissionError("turn is terminal")
            if normalized_role == StickyExecutionRole.PROPOSER:
                if self._current_role in {
                    StickyExecutionRole.FIXED_AGGREGATOR,
                    StickyExecutionRole.FIXED_DIRECT,
                }:
                    raise ProviderAdmissionError(
                        "proposer admission is closed after fixed activation"
                    )
            elif normalized_role != self._current_role:
                raise ProviderAdmissionError(
                    f"role {normalized_role.value} is not sticky role "
                    f"{self._current_role.value}"
                )

            key = (normalized_role, logical_call_index)
            ledger = self._attempt_ledgers.setdefault(
                key,
                CallAttemptLedger(normalized_role, logical_call_index),
            )
            if attempt_index in ledger.attempt_indices:
                raise ProviderAdmissionError(
                    "the logical call attempt was already admitted"
                )
            if attempt_index == 1:
                if 0 not in ledger.attempt_indices:
                    raise ProviderAdmissionError(
                        "attempt 1 requires an admitted attempt 0"
                    )
                if not self._attempt_finished_locked(
                    normalized_role,
                    logical_call_index,
                    0,
                ):
                    raise ProviderAdmissionError(
                        "attempt 1 requires a finished attempt 0"
                    )
            if len(ledger.attempt_indices) >= 2:
                raise ProviderAdmissionError("logical call has exhausted its attempts")

            lease = ProviderCallLease(
                lease_id=uuid.uuid4().hex,
                turn_id=self._identity.turn_id,
                role=normalized_role,
                logical_call_index=logical_call_index,
                attempt_index=attempt_index,
                owner=owner,
            )
            ledger.attempt_indices.append(attempt_index)
            self._active_leases[lease.lease_id] = lease
            if normalized_role == StickyExecutionRole.PROPOSER:
                self._proposer_admissions += 1
            elif normalized_role == StickyExecutionRole.PRIMARY_AGGREGATOR:
                self._primary_logical_call_index = max(
                    self._primary_logical_call_index,
                    logical_call_index,
                )
            elif normalized_role in {
                StickyExecutionRole.FIXED_AGGREGATOR,
                StickyExecutionRole.FIXED_DIRECT,
            }:
                self._fixed_logical_call_index = max(
                    self._fixed_logical_call_index,
                    logical_call_index,
                )
            return lease

    async def record_request_start(self, lease: ProviderCallLease, evidence: Any) -> None:
        """Record a request only after the adapter has a real request lease."""

        async with self._lock:
            self._ensure_open()
            active = self._require_active_lease(lease)
            if lease.lease_id in self._lease_started:
                raise ProviderAdmissionError(
                    "provider lease already recorded a request start"
                )
            self._lease_started.add(lease.lease_id)
            ledger = self._attempt_ledgers[(active.role, active.logical_call_index)]
            ledger.request_starts += 1
            ledger.request_start_evidence.append(evidence)
            if active.role == StickyExecutionRole.PROPOSER:
                self._proposer_request_starts += 1

    async def finish_provider_call(self, lease: ProviderCallLease, outcome: Any) -> None:
        """Close a lease; a closed lease cannot be reused."""

        async with self._lock:
            if lease.lease_id not in self._active_leases:
                return
            active = self._active_leases.pop(lease.lease_id)
            self._lease_finished[active.lease_id] = outcome
            self._attempt_ledgers[(active.role, active.logical_call_index)].outcomes.append(
                outcome
            )

    async def activate_fixed(
        self,
        role: StickyExecutionRole | str,
        reason: str,
        recovery_context: RecoveryContext | None = None,
    ) -> RoleActivationResult:
        """Consume the one fallback activation and make the role sticky."""

        normalized_role = _role(role)
        if normalized_role not in {
            StickyExecutionRole.FIXED_AGGREGATOR,
            StickyExecutionRole.FIXED_DIRECT,
        }:
            raise ValueError("fixed activation requires a fixed role")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")

        async with self._lock:
            self._ensure_open()
            self._ensure_admission_allowed()
            if self._current_role == normalized_role:
                return RoleActivationResult(
                    activated=False,
                    role=normalized_role,
                    reason=reason,
                    fallback_activation_count=self._fallback_activation_count,
                    generation_epoch=self._generation_state.epoch,
                )
            if self._fallback_activation_count:
                raise ProviderAdmissionError("fixed activation was already consumed")
            if self._current_role == StickyExecutionRole.TERMINAL:
                raise ProviderAdmissionError("terminal turn cannot activate fixed role")
            self._fallback_activation_count = 1
            self._fallback_started = True
            self._fixed_started = True
            self._suppress_selector_fallback = True
            self._current_role = normalized_role
            self._recovery_context = recovery_context or RecoveryContext(
                successful_drafts=self._successful_drafts
            )
            self._successful_drafts = self._recovery_context.successful_drafts
            sequence = self._allocate_sequence_locked()
            return RoleActivationResult(
                activated=True,
                role=normalized_role,
                reason=reason,
                fallback_activation_count=1,
                generation_epoch=self._generation_state.epoch,
                sequence=sequence,
            )

    async def begin_failed_fixed_recovery(
        self,
        role: StickyExecutionRole | str,
        reason: str,
        recovery_context: RecoveryContext | None = None,
    ) -> None:
        """Consume fallback authority when no fixed role can be committed.

        Missing deployment/readiness/request-fit failures are still one fixed
        recovery transaction.  They set ``fallback_started`` and
        ``recovery_terminal`` while deliberately leaving ``fixed_started``
        false, then the caller publishes the sole terminal generation reset.
        """

        normalized_role = _role(role)
        if normalized_role not in {
            StickyExecutionRole.FIXED_AGGREGATOR,
            StickyExecutionRole.FIXED_DIRECT,
        }:
            raise ValueError("failed fixed recovery requires a fixed role")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        async with self._lock:
            self._ensure_open()
            self._ensure_admission_allowed()
            if self._fallback_activation_count:
                raise ProviderAdmissionError("fixed activation was already consumed")
            if self._terminalized or self._generation_state.terminal:
                raise ProviderAdmissionError("terminal turn cannot start fixed recovery")
            self._fallback_activation_count = 1
            self._fallback_started = True
            self._fixed_started = False
            self._recovery_terminal = True
            self._suppress_selector_fallback = True
            self._recovery_context = recovery_context or RecoveryContext(
                successful_drafts=self._successful_drafts,
                metadata={"reason": reason, "fixed_role": normalized_role.value},
            )

    def accept_event(
        self,
        generation_epoch: int,
        sequence: int,
        meaningful: bool,
        provenance: Any,
    ) -> bool:
        """Accept only current-generation, strictly-new events.

        ``meaningful`` refreshes activity only for genuine upstream
        provenance.  Heartbeats, polling, reset events, and UI repaint do not
        count as provider progress.
        """

        if self._closed:
            return False
        if self._terminalized or self._generation_state.terminal:
            return False
        if generation_epoch != self._generation_state.epoch:
            return False
        if sequence <= self._last_event_sequence:
            return False
        self._last_event_sequence = sequence
        self._sequence = max(self._sequence, sequence)
        self._generation_state.last_sequence = sequence
        if meaningful and self._is_real_upstream(provenance):
            self._last_meaningful_progress_at = time.monotonic()
        return True

    def open_tool_buffer(self, call_id: str) -> PendingToolBuffer:
        if not isinstance(call_id, str) or not call_id.strip():
            raise ValueError("call_id must be a non-empty string")
        self._ensure_open()
        return self._tool_buffers.setdefault(call_id, PendingToolBuffer(call_id))

    async def commit_tool_round(self, call_id: str, done_event: Any) -> CompletedToolRecord:
        if not isinstance(call_id, str) or not call_id.strip():
            raise ValueError("call_id must be a non-empty string")
        if done_event is None:
            raise ValueError("done_event is required")
        async with self._lock:
            self._ensure_open()
            buffer = self._tool_buffers.pop(call_id, None)
            if buffer is None:
                raise TurnExecutionError(f"no pending tool buffer: {call_id}")
            buffer.close()
            record = CompletedToolRecord(
                call_id=call_id,
                events=tuple(buffer.events),
                done_event=done_event,
                sequence=self._allocate_sequence_locked(),
            )
            self._completed_tools.append(record)
            return record

    def drop_tool_buffer(self, call_id: str, reason: str) -> None:
        del reason
        self._tool_buffers.pop(call_id, None)

    def drop_pending_tool_buffers(self, reason: str) -> int:
        """Discard every uncommitted tool buffer for a generation reset."""

        del reason
        buffers = tuple(self._tool_buffers.values())
        for buffer in buffers:
            buffer.close()
        self._tool_buffers.clear()
        return len(buffers)

    def begin_generation_reset(
        self,
        from_role: StickyExecutionRole | str,
        to_role: StickyExecutionRole | str,
        safe_reason: str,
        *,
        terminal: bool = False,
        terminal_text_snapshot: str | None = None,
        terminal_error_message: str = "",
        terminal_error_code: str = "",
        terminal_failure_kind: str = "",
    ) -> AnswerGenerationResetEvent:
        """Advance the generation epoch while retaining the same message id."""

        if not isinstance(safe_reason, str) or not safe_reason.strip():
            raise ValueError("safe_reason must be a non-empty string")
        if self._closed:
            raise TurnExecutionError("turn context is closed")
        if self._terminalized or self._generation_state.terminal:
            raise TurnExecutionError("terminal generation is already closed")
        old_epoch = self._generation_state.epoch
        new_epoch = old_epoch + 1
        sequence = self.next_sequence()
        normalized_to_role = _role(to_role)
        self.drop_pending_tool_buffers("generation_reset")
        self._generation_state = GenerationState(
            epoch=new_epoch,
            owner=normalized_to_role,
            last_sequence=sequence,
            terminal=terminal,
        )
        if terminal:
            self._terminalized = True
            self._suppress_selector_fallback = True
            if self._fallback_started:
                self._recovery_terminal = True
        self._last_event_sequence = sequence
        self._publication_ledger.generation_epoch = new_epoch
        self._publication_ledger.last_sequence = sequence
        self._publication_ledger.text_snapshot = ""
        self._publication_ledger.reasoning_snapshot = ""
        self._publication_ledger.terminal = terminal
        if terminal and terminal_text_snapshot:
            self._publication_ledger.text_snapshot = terminal_text_snapshot
            self._publication_ledger.published = True
            self._publication_ledger.visible_output = True
        return AnswerGenerationResetEvent(
            turn_id=self._identity.turn_id,
            assistant_message_id=self._identity.assistant_message_id,
            old_generation_epoch=old_epoch,
            new_generation_epoch=new_epoch,
            from_role=_role(from_role),
            to_role=normalized_to_role,
            safe_reason=safe_reason,
            sequence=sequence,
            terminal=terminal,
            terminal_text_snapshot=terminal_text_snapshot,
            terminal_error_message=terminal_error_message,
            terminal_error_code=terminal_error_code,
            terminal_failure_kind=terminal_failure_kind,
        )

    def begin_control_terminal(self, reason: str) -> int | None:
        """Cross the sole control-terminal boundary and reject late events."""

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        if self._closed or self._terminalized or self._generation_state.terminal:
            return None
        self.drop_pending_tool_buffers("control_terminal")
        sequence = self.next_sequence()
        self._terminalized = True
        self._suppress_selector_fallback = True
        self._last_event_sequence = sequence
        self._generation_state.last_sequence = sequence
        self._generation_state.terminal = True
        self._publication_ledger.last_sequence = sequence
        self._publication_ledger.terminal = True
        if not self._publication_ledger.visible_output:
            self._publication_ledger.released = True
            self._publication_ledger.reserved = False
        return sequence

    def publish_visible(
        self,
        *,
        text: str = "",
        reasoning: str = "",
        generation_epoch: int | None = None,
        sequence: int | None = None,
        terminal: bool = False,
    ) -> bool:
        """Record visible content against the reserved caller-supplied id."""

        if not text and not reasoning:
            return False
        epoch = self._generation_state.epoch if generation_epoch is None else generation_epoch
        if epoch != self._generation_state.epoch or self._closed:
            return False
        event_sequence = self.next_sequence() if sequence is None else sequence
        if not self.accept_event(epoch, event_sequence, True, "upstream"):
            return False
        return self._publication_ledger.accept(
            generation_epoch=epoch,
            sequence=event_sequence,
            text=text,
            reasoning=reasoning,
            terminal=terminal,
        )

    def release_reserved_unpublished(self, reason: str) -> bool:
        """Release a reservation without materializing an empty row."""

        del reason
        if self._publication_ledger.visible_output:
            return False
        if self._publication_ledger.released:
            return False
        self._publication_ledger.released = True
        self._publication_ledger.reserved = False
        return True

    def reserve_cleanup(
        self,
        task: Awaitable[Any] | asyncio.Future[Any] | asyncio.Task[Any],
        owner: str,
    ) -> CleanupHandle:
        """Register a bounded cleanup task that the context owner joins."""

        self._ensure_open()
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner must be a non-empty string")
        future = (
            task
            if isinstance(task, (asyncio.Future, asyncio.Task))
            else asyncio.ensure_future(task)
        )
        handle = CleanupHandle(uuid.uuid4().hex, owner)
        self._cleanup_tasks[handle.handle_id] = future
        self._cleanup_handles[handle.handle_id] = handle
        return handle

    def release_cleanup(self, handle: CleanupHandle) -> bool:
        removed = self._cleanup_tasks.pop(handle.handle_id, None)
        self._cleanup_handles.pop(handle.handle_id, None)
        return removed is not None

    async def close(self) -> None:
        """Close once and join every registered cleanup task."""

        async with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = tuple(self._cleanup_tasks.values())
            self._cleanup_tasks.clear()
            self._cleanup_handles.clear()
            if not self._publication_ledger.visible_output:
                self._publication_ledger.released = True
                self._publication_ledger.reserved = False
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _ensure_open(self) -> None:
        if self._closed:
            raise TurnExecutionError("turn context is closed")

    def _ensure_admission_allowed(self) -> None:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise ProviderAdmissionError("turn deadline has elapsed")
        control = self._control
        if control is None:
            return
        if callable(control):
            blocked = bool(control())
        else:
            blocked = any(
                bool(getattr(control, name, False))
                for name in ("cancelled", "is_cancelled", "shutdown", "closed")
            )
        if blocked:
            raise ProviderAdmissionError("turn control state rejects provider admission")

    def _require_active_lease(self, lease: ProviderCallLease) -> ProviderCallLease:
        if not isinstance(lease, ProviderCallLease):
            raise TypeError("lease must be a ProviderCallLease")
        active = self._active_leases.get(lease.lease_id)
        if active is None:
            raise ProviderAdmissionError("provider lease is not active")
        if active != lease:
            raise ProviderAdmissionError("provider lease identity mismatch")
        return active

    def _attempt_finished_locked(
        self,
        role: StickyExecutionRole,
        logical_call_index: int,
        attempt_index: int,
    ) -> bool:
        """Return whether one admitted attempt crossed its finish boundary."""

        ledger = self._attempt_ledgers.get((role, logical_call_index))
        if ledger is None or attempt_index < 0:
            return False
        # Outcomes are appended in the same order as admitted attempts.  A
        # later attempt cannot exist until the earlier one has been finished,
        # so this positional check is sufficient and keeps the finish proof
        # local to the logical-call ledger.
        return len(ledger.outcomes) > attempt_index

    def _allocate_sequence_locked(self) -> int:
        self._sequence += 1
        return self._sequence

    @staticmethod
    def _is_real_upstream(provenance: Any) -> bool:
        if provenance is True:
            return True
        if isinstance(provenance, str):
            return provenance.strip().lower() in {
                "upstream",
                "upstream_activity",
                "provider",
                "provider_event",
            }
        if isinstance(provenance, Mapping):
            return bool(provenance.get("upstream_activity")) and not bool(
                provenance.get("synthetic")
            )
        return False


@dataclass(frozen=True, slots=True)
class AnswerGenerationResetEvent:
    """Typed same-message generation replacement event."""

    kind: str = "answer_generation_reset"
    turn_id: str = ""
    assistant_message_id: str = ""
    old_generation_epoch: int = 0
    new_generation_epoch: int = 0
    from_role: StickyExecutionRole = StickyExecutionRole.PRIMARY_AGGREGATOR
    to_role: StickyExecutionRole = StickyExecutionRole.PRIMARY_AGGREGATOR
    safe_reason: str = ""
    preserve_completed_tools: bool = True
    authoritative_text_snapshot: str = ""
    authoritative_reasoning_snapshot: str = ""
    sequence: int = 0
    terminal: bool = False
    terminal_text_snapshot: str | None = None
    # Safe, typed failure metadata for terminal replacements. These fields
    # stay on the in-process event so the shared turn finalizer can persist a
    # failed outcome without emitting a second public ErrorEvent. The public
    # wire model deliberately ignores them; ``terminal_text_snapshot`` remains
    # the sole user-visible failure payload.
    terminal_error_message: str = ""
    terminal_error_code: str = ""
    terminal_failure_kind: str = ""
