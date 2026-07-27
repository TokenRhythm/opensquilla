"""Stage object for before-turn compaction + transcript history load.

Owns the per-turn slice between the agent-bootstrap stage boundary (Agent
construction) and the stream-consumer stage boundary (the
``async for event in agent.run_turn(...)`` loop). The harness invokes
``CompactionAndHistoryStage.run`` once per turn, AFTER
AgentBootstrapStage and BEFORE the attachment-message build +
stream-consumer loop.

Side-effect contract:

- ``t3_upgrade.maybe_compact`` and ``preflight.maybe_compact`` MAY mutate
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

Compaction forward-compat: the four ports
(``T3UpgradeCompactionPort``, ``PreflightCompactionPort``,
``HistoryLoaderPort``, ``RequestContextPrependPort``) are shaped so
compaction can drop in replacement implementations (e.g. an incremental
cut-point compactor) without revising the stage interface. The IN-TURN
compaction refresh (``CompactionEvent`` -> ``persist_compaction_result``)
lives in StreamConsumerStage, NOT here, preserving the
cache-friendly system-prompt-rebuild contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.hooks.types import CompactionHook
    from opensquilla.engine.turn_runner.outcome import StageOutcome
    from opensquilla.provider.types import ProviderRequestCorrelation

# Internal sentinels mirroring the runtime.py module-level constants. The
# stage body branches on ``t3_status`` to decide whether to fall through
# to preflight; keeping a local copy avoids a runtime → stage import.
_T3_NOT_APPLICABLE: str = "not_applicable"
_T3_FLUSH_FAILED: str = "flush_failed"

# ---------------------------------------------------------------------------
# Ports — four narrow Protocols
# ---------------------------------------------------------------------------

@runtime_checkable
class T3UpgradeCompactionPort(Protocol):
    """Wraps ``TurnRunner._maybe_compact_on_t3_upgrade``.

    The helper performs router-state inspection, circuit-breaker
    checks, flush coordination, and the actual
    ``SessionManager.compact`` call. The port wraps the public contract:

    - Returns one of ``"not_applicable"`` / ``"handled"`` /
      ``"flush_failed"`` / ``"compact_failed"`` (the four ``_T3_*``
      sentinels). The CALLER branches on the return value to decide
      whether to fall through to generic preflight
      (``not_applicable`` and ``flush_failed`` fall through;
      ``handled`` and ``compact_failed`` do NOT).
    - All exception handling is internal; ``asyncio.CancelledError`` is
      re-raised, other exceptions are logged + swallowed.

    The port preserves the BEFORE-turn DB-mutation boundary: every successful
    compaction writes to DB via the SessionManager before this method
    returns. A replacement implementation MUST also
    write to DB before returning.
    """

    async def maybe_compact(
        self,
        *,
        session_key: str,
        turn: Any,
        context_window_tokens: int,
        compaction_provider: Any | None,
        compaction_model: str | None,
        provider_request_correlation: ProviderRequestCorrelation | None = None,
    ) -> str: ...

@runtime_checkable
class PreflightCompactionPort(Protocol):
    """Wraps ``TurnRunner._maybe_preflight_compact``.

    Fires ONLY when the t3-upgrade branch returned a status that allows
    fall-through (i.e., ``not_applicable`` or ``flush_failed``).

    Returns ``None``. The CALLER does NOT branch on the return; the side
    effect is implicit (DB rewrite + ``mark_compacted_this_turn`` if a
    compaction actually fired).

    Same exception model as T3 port: ``CancelledError`` re-raised,
    others logged + swallowed.
    """

    async def maybe_compact(
        self,
        *,
        session_key: str,
        context_window_tokens: int,
        compaction_provider: Any | None,
        compaction_model: str | None,
        provider_request_correlation: ProviderRequestCorrelation | None = None,
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
    ``context_window_tokens``, ``provider``, ``resolved_model``, and
    ``turn`` come from the upstream stages; ``session_key``,
    ``agent_id``, and ``history_has_persisted_user`` come from the
    ``_run_turn`` call site.
    """

    # From AgentBootstrapStage
    agent: Agent
    context_window_tokens: int
    # From PromptAssemblerStage
    provider: Any
    resolved_model: str
    turn: Any  # post-pipeline pipeline.TurnContext
    # From _run_turn locals (caller-provided)
    session_key: str
    agent_id: str
    history_has_persisted_user: bool
    compaction_context_window_tokens: int | None = None
    compaction_provider: Any | None = None
    compaction_model: str | None = None
    bound_user_message_id: str | None = None
    provider_request_correlation: ProviderRequestCorrelation | None = field(
        default=None,
        repr=False,
    )

@dataclass(frozen=True)
class CompactionAndHistoryStageOutput:
    """The pieces of state subsequent stages and the harness consume.

    - ``t3_upgrade_status``: one of the four ``_T3_*`` sentinels.
      Surfaced for observability + the equivalence harness snapshot;
      NOT consumed by downstream stages. It is used only
      to decide whether to call preflight (folded into the stage body).
    - ``preflight_invoked``: ``True`` if ``preflight.maybe_compact``
      was called (i.e. the t3 path returned a fall-through sentinel),
      ``False`` if skipped. Surfaced for observability + harness only.
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

    t3_upgrade_status: str
    preflight_invoked: bool
    compaction_summary_context: str | None
    final_request_context_prompt: str | None

# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class CompactionAndHistoryStage:
    """Compact (if needed), load history, prepend compaction context.

    Stable boundary: runs ONCE per turn, after ``AgentBootstrapStage``
    and before the attachment-build + stream-consumer steps. The four
    ports execute strictly sequentially:

    1. ``t3_upgrade.maybe_compact`` (always called).
    2. ``preflight.maybe_compact`` (called ONLY when t3 returned
       ``_T3_NOT_APPLICABLE`` or ``_T3_FLUSH_FAILED``).
    3. ``history_loader.load`` (always called).
    4. ``request_context_prepender.prepend`` (always called; pure).

    The stage fires ``CompactionHook.before_compact`` and
    ``CompactionHook.after_compact`` around BOTH t3 and preflight calls.
    Hook invocation discriminates t3 vs preflight via
    ``CompactionState.extra["phase"]``.

    Exception model: re-raises ``asyncio.CancelledError`` from any
    port. Other exceptions from t3/preflight ports are swallowed
    internally by the helpers (no surrounding
    try/except). History/prepender exceptions propagate to the outer
    terminal handler in ``_run_turn``.
    """

    name = "compaction_and_history_stage"

    def __init__(
        self,
        *,
        t3_upgrade: T3UpgradeCompactionPort,
        preflight: PreflightCompactionPort,
        history_loader: HistoryLoaderPort,
        request_context_prepender: RequestContextPrependPort,
        compaction_hooks: tuple[CompactionHook, ...] = (),
    ) -> None:
        self._t3_upgrade = t3_upgrade
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

        # 1. T3-upgrade compaction. Hook fires around the call so even a
        #    no-op path's observability is uniform.
        t3_state = CompactionState(
            session_key=inp.session_key,
            agent_id=inp.agent_id,
            total_tokens=0,
            threshold_tokens=compaction_context_window_tokens,
            extra={"phase": "t3_upgrade"},
        )
        await self._fire_before_compact(t3_state)
        t3_status = await self._t3_upgrade.maybe_compact(
            session_key=inp.session_key,
            turn=inp.turn,
            context_window_tokens=compaction_context_window_tokens,
            compaction_provider=compaction_provider,
            compaction_model=compaction_model,
            provider_request_correlation=inp.provider_request_correlation,
        )
        await self._fire_after_compact(t3_state, {"status": t3_status})

        # 2. Preflight compaction (fall-through cases only).
        preflight_invoked = False
        if t3_status in {_T3_NOT_APPLICABLE, _T3_FLUSH_FAILED}:
            preflight_invoked = True
            preflight_state = CompactionState(
                session_key=inp.session_key,
                agent_id=inp.agent_id,
                total_tokens=0,
                threshold_tokens=compaction_context_window_tokens,
                extra={"phase": "preflight"},
            )
            await self._fire_before_compact(preflight_state)
            await self._preflight.maybe_compact(
                session_key=inp.session_key,
                context_window_tokens=compaction_context_window_tokens,
                compaction_provider=compaction_provider,
                compaction_model=compaction_model,
                provider_request_correlation=inp.provider_request_correlation,
            )
            await self._fire_after_compact(preflight_state, {"status": "ran"})

        # 3. Load history (transcript + reconstructed messages + durable summary).
        compaction_summary_context = await self._history_loader.load(
            agent=inp.agent,
            session_key=inp.session_key,
            trim_last_user=inp.history_has_persisted_user,
            bound_user_message_id=inp.bound_user_message_id,
        )

        # 4. Prepend compaction summary context to request_context_prompt (pure).
        final_request_context_prompt = self._request_context_prepender.prepend(
            existing=inp.agent.config.request_context_prompt,
            prepended=compaction_summary_context,
        )

        return StageOutcome.success(
            CompactionAndHistoryStageOutput(
                t3_upgrade_status=t3_status,
                preflight_invoked=preflight_invoked,
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
