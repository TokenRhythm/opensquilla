"""Stage object for before-turn compaction + transcript history load.

Owns the per-turn slice between the agent-bootstrap stage boundary (Agent
construction) and the stream-consumer stage boundary (the
``async for event in agent.run_turn(...)`` loop). The harness invokes
``CompactionAndHistoryStage.run`` once per turn, AFTER
AgentBootstrapStage and BEFORE the attachment-message build +
stream-consumer loop.

Side-effect contract:

- Unless ``skip_compaction`` is requested, ``preflight.maybe_compact`` MAY mutate
  the DB transcript via ``SessionManager.compact``. They MAY also mutate
  the runner's per-turn ``has_compacted_this_turn`` flag and the
  per-session compaction-failure circuit state. All exceptions other
  than ``asyncio.CancelledError`` are swallowed internally.
- ``history_loader.load`` mutates ``agent._history`` via
  ``agent.set_history`` and returns a (possibly ``None``) durable
  compaction-summary context string.
- ``request_context_prepender.prepend`` is a pure string function whose
  return value the harness applies as
  ``agent.config.request_context_prompt = ...``.

``CompactionAndHistoryStage`` IS the first consumer of
``CompactionHook.before_compact`` / ``CompactionHook.after_compact`` when hooks
are supplied. Hook invocations are isolated with ``except Exception: pass`` so
observer failures do not break the turn.

NEVER terminates. Always returns ``StageOutcome.success(...)``. The
``StageOutcome`` shape is preserved for forward-compatibility with a
future ``ErrorEvent`` early-yield branch.

Compaction forward-compat: the three ports
(``PreflightCompactionPort``, ``HistoryLoaderPort``,
``RequestContextPrependPort``) are shaped so
compaction can drop in replacement implementations (e.g. an incremental
cut-point compactor) without revising the stage interface. The IN-TURN
compaction refresh (``CompactionEvent`` -> ``persist_compaction_result``)
lives in StreamConsumerStage, NOT here, preserving the
cache-friendly system-prompt-rebuild contract.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.hooks.types import CompactionHook
    from opensquilla.engine.turn_runner.outcome import StageOutcome
    from opensquilla.engine.turn_runner.transcript_snapshot import (
        TurnTranscriptSnapshot,
    )
    from opensquilla.provider.types import ProviderRequestCorrelation
    from opensquilla.session.compaction import CompactionRequestContext
    from opensquilla.session.compaction_budget import CompactionBudget
    from opensquilla.session.compaction_deployment import CompactionExecutionPlan

# ---------------------------------------------------------------------------
# Ports — three narrow Protocols
# ---------------------------------------------------------------------------

@runtime_checkable
class PreflightCompactionPort(Protocol):
    """Wraps ``TurnRunner._maybe_preflight_compact``.

    Returns ``None``. The CALLER does NOT branch on the return; the side
    effect is implicit (DB rewrite + ``mark_compacted_this_turn`` if a
    compaction actually fired).

    Exceptions other than ``CancelledError`` are logged and swallowed by the
    runtime helper; cancellation is re-raised.
    """

    async def maybe_compact(
        self,
        *,
        session_key: str,
        context_window_tokens: int,
        compaction_provider: Any | None,
        compaction_model: str | None,
        compaction_plan: CompactionExecutionPlan | None = None,
        compaction_request_context: CompactionRequestContext | None = None,
        compaction_budget: CompactionBudget | None = None,
        history_capacity_tokens: int | None = None,
        history_capacity_chars: int | None = None,
        history_has_persisted_user: bool = False,
        bound_user_message_id: str | None = None,
        provider_request_correlation: ProviderRequestCorrelation | None = None,
        consumer_admission: Any | None = None,
        consumer_admission_fingerprint: str = "",
        attachment_path_resolver: Callable[[dict[str, Any], str], str | None] | None = None,
        transcript_snapshot: TurnTranscriptSnapshot[Any] | None = None,
        expected_session_id: str | None = None,
        expected_session_epoch: int | None = None,
    ) -> None: ...

@runtime_checkable
class HistoryLoaderPort(Protocol):
    """Wraps ``TurnRunner._load_history``.

    Reads the persisted transcript, reconstructs per-entry messages,
    and mutates ``agent._history`` via ``agent.set_history(history)``
    when any history was reconstructed.

    Returns ``str | None`` — the formatted compaction-summary context
    if durable summaries existed, else ``None``. The stage body feeds
    the return value into the prepender port.

    NOT a compaction port; does NOT fire ``CompactionHook``. History
    load is a read of already-persisted (possibly already-compacted)
    state.
    """

    async def load(
        self,
        *,
        agent: Agent,
        session_key: str,
        trim_last_user: bool,
        bound_user_message_id: str | None = None,
        transcript_snapshot: TurnTranscriptSnapshot[Any] | None = None,
        expected_session_id: str | None = None,
        expected_session_epoch: int | None = None,
    ) -> str | None: ...

@runtime_checkable
class RequestContextPrependPort(Protocol):
    """Wraps ``_prepend_request_context_prompt`` (module-level pure helper).

    Pure string function. Returns the prepended context (or the existing
    value unchanged when there is nothing to prepend). The harness
    applies the result with ``agent.config.request_context_prompt =
    ...`` after the stage returns; the stage itself does NOT mutate the
    agent.

    Reason for promoting a small pure function to a port: testability.
    A recording fake lets us assert "prepender was called with exactly
    these two arguments" without parsing log lines.
    """

    def prepend(
        self,
        *,
        existing: str | None,
        prepended: str | None,
    ) -> str | None: ...

# ---------------------------------------------------------------------------
# Stage I/O dataclasses (frozen)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompactionAndHistoryStageInput:
    """Inputs the ``CompactionAndHistoryStage`` needs at its boundary.

    Mirrors the locals visible to the original inline slice at the point
    ``AgentBootstrapStage`` has finished. ``agent``,
    ``context_window_tokens``, ``provider``, and ``resolved_model`` come from
    the upstream stages; ``session_key``,
    ``agent_id``, and ``history_has_persisted_user`` come from the
    ``_run_turn`` call site.
    """

    # From AgentBootstrapStage
    agent: Agent
    context_window_tokens: int
    # From PromptAssemblerStage
    provider: Any
    resolved_model: str
    # From _run_turn locals (caller-provided)
    session_key: str
    agent_id: str
    history_has_persisted_user: bool
    expected_session_id: str | None = None
    expected_session_epoch: int | None = None
    compaction_context_window_tokens: int | None = None
    compaction_provider: Any | None = None
    compaction_model: str | None = None
    compaction_plan: CompactionExecutionPlan | None = field(
        default=None,
        repr=False,
    )
    compaction_request_context: CompactionRequestContext | None = field(
        default=None, repr=False
    )
    compaction_budget: CompactionBudget | None = field(default=None, repr=False)
    history_capacity_tokens: int | None = None
    history_capacity_chars: int | None = None
    bound_user_message_id: str | None = None
    provider_request_correlation: ProviderRequestCorrelation | None = field(
        default=None,
        repr=False,
    )
    consumer_admission: Any | None = field(default=None, repr=False)
    consumer_admission_fingerprint: str = ""
    attachment_path_resolver: Callable[[dict[str, Any], str], str | None] | None = field(
        default=None, repr=False,
    )
    # Explicit authority boundary supplied by the runtime. Restricted turns
    # load canonical history for the primary provider projection, but may not
    # invoke before-turn compaction or replay durable summaries because those
    # paths can make auxiliary provider calls over unprojected history.
    # An upstream terminal preflight may suppress auxiliary compaction while
    # retaining the ordinary history-loading path.
    skip_compaction: bool = False
    transcript_snapshot: TurnTranscriptSnapshot[Any] | None = field(
        default=None,
        repr=False,
    )

@dataclass(frozen=True)
class CompactionAndHistoryStageOutput:
    """The pieces of state subsequent stages and the harness consume.

    - ``compaction_summary_context``: the string passed to the
      prepender. ``None`` when no durable summaries exist. The
      equivalence harness asserts this is bit-identical between
      previous and current.
    - ``final_request_context_prompt``: the result of the prepend.
      The harness applies this to
      ``agent.config.request_context_prompt`` after the stage returns.
      Surfaced (not applied inside the stage) so the harness remains
      the only place that mutates ``agent.config``.
    """

    compaction_summary_context: str | None
    final_request_context_prompt: str | None

# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class CompactionAndHistoryStage:
    """Compact (if needed), load history, prepend compaction context.

    Stable boundary: runs ONCE per turn, after ``AgentBootstrapStage``
    and before the attachment-build + stream-consumer steps. The three
    ports execute strictly sequentially:

    1. ``preflight.maybe_compact`` (unless compaction is suppressed).
    2. ``history_loader.load`` (always called).
    3. ``request_context_prepender.prepend`` (always called; pure).

    The stage fires ``CompactionHook.before_compact`` and
    ``CompactionHook.after_compact`` around the preflight call with
    ``CompactionState.extra["phase"] == "preflight"``.

    Exception model: re-raises ``asyncio.CancelledError`` from any
    port. Other exceptions from the preflight port are swallowed
    internally by the helpers (no surrounding
    try/except). History/prepender exceptions propagate to the outer
    terminal handler in ``_run_turn``.
    """

    name = "compaction_and_history_stage"

    def __init__(
        self,
        *,
        preflight: PreflightCompactionPort,
        history_loader: HistoryLoaderPort,
        request_context_prepender: RequestContextPrependPort,
        compaction_hooks: tuple[CompactionHook, ...] = (),
    ) -> None:
        self._preflight = preflight
        self._history_loader = history_loader
        self._request_context_prepender = request_context_prepender
        self._compaction_hooks = compaction_hooks

    async def run(
        self,
        inp: CompactionAndHistoryStageInput,
    ) -> StageOutcome[CompactionAndHistoryStageOutput]:
        # Local imports keep the module import-cycle-free.
        from opensquilla.engine.hooks.types import CompactionState
        from opensquilla.engine.turn_runner.outcome import StageOutcome

        compaction_context_window_tokens = (
            inp.compaction_context_window_tokens or inp.context_window_tokens
        )
        compaction_provider = (
            inp.compaction_provider if inp.compaction_provider is not None else inp.provider
        )
        compaction_model = inp.compaction_model or inp.resolved_model

        if not inp.skip_compaction:
            preflight_state = CompactionState(
                session_key=inp.session_key,
                agent_id=inp.agent_id,
                total_tokens=0,
                threshold_tokens=compaction_context_window_tokens,
                extra={"phase": "preflight"},
            )
            await self._fire_before_compact(preflight_state)
            preflight_kwargs: dict[str, Any] = {}
            if inp.compaction_budget is not None:
                preflight_kwargs["compaction_budget"] = inp.compaction_budget
            if inp.compaction_request_context is not None:
                preflight_kwargs["compaction_request_context"] = inp.compaction_request_context
            if inp.attachment_path_resolver is not None:
                preflight_kwargs["attachment_path_resolver"] = inp.attachment_path_resolver
            if inp.transcript_snapshot is not None:
                preflight_kwargs["transcript_snapshot"] = inp.transcript_snapshot
            if inp.expected_session_id is not None or inp.expected_session_epoch is not None:
                preflight_kwargs["expected_session_id"] = inp.expected_session_id
                preflight_kwargs["expected_session_epoch"] = inp.expected_session_epoch
            await self._preflight.maybe_compact(
                session_key=inp.session_key,
                context_window_tokens=compaction_context_window_tokens,
                compaction_provider=compaction_provider,
                compaction_model=compaction_model,
                compaction_plan=inp.compaction_plan,
                history_capacity_tokens=inp.history_capacity_tokens,
                history_capacity_chars=inp.history_capacity_chars,
                history_has_persisted_user=inp.history_has_persisted_user,
                bound_user_message_id=inp.bound_user_message_id,
                provider_request_correlation=inp.provider_request_correlation,
                consumer_admission=inp.consumer_admission,
                consumer_admission_fingerprint=inp.consumer_admission_fingerprint,
                **preflight_kwargs,
            )
            await self._fire_after_compact(preflight_state, {"status": "ran"})

        # 3. Load history (transcript + reconstructed messages + durable summary).
        history_kwargs: dict[str, Any] = {}
        if inp.transcript_snapshot is not None:
            history_kwargs["transcript_snapshot"] = inp.transcript_snapshot
        if inp.expected_session_id is not None or inp.expected_session_epoch is not None:
            history_kwargs["expected_session_id"] = inp.expected_session_id
            history_kwargs["expected_session_epoch"] = inp.expected_session_epoch
        compaction_summary_context = await self._history_loader.load(
            agent=inp.agent,
            session_key=inp.session_key,
            trim_last_user=inp.history_has_persisted_user,
            bound_user_message_id=inp.bound_user_message_id,
            **history_kwargs,
        )

        # 4. Prepend compaction summary context to request_context_prompt (pure).
        final_request_context_prompt = self._request_context_prepender.prepend(
            existing=inp.agent.config.request_context_prompt,
            prepended=compaction_summary_context,
        )

        return StageOutcome.success(
            CompactionAndHistoryStageOutput(
                compaction_summary_context=compaction_summary_context,
                final_request_context_prompt=final_request_context_prompt,
            )
        )

    async def _fire_before_compact(self, state: Any) -> None:
        for hook in self._compaction_hooks:
            try:
                await hook.before_compact(state)
            except Exception:  # noqa: BLE001 — hook isolation contract
                # A buggy hook MUST NOT break the turn. Swallow per
                # protocol; observability is the runtime's concern.
                pass

    async def _fire_after_compact(self, state: Any, outcome: Any) -> None:
        for hook in self._compaction_hooks:
            try:
                await hook.after_compact(state, outcome)
            except Exception:  # noqa: BLE001 — hook isolation contract
                pass
