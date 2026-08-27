"""TurnContext — mutable accumulator owned by the TurnRunner harness.

Cross-cutting state threaded across ordered TurnRunner stages. Owned exclusively
by the harness; stage classes read it through typed StageInput dataclasses and
write it via StageOutput return values the harness applies.

Direct mutation of TurnContext from inside a stage is forbidden — a stage
that needs to mutate cross-cutting state returns it via its Output.

The dataclass starts with InputStage and ProviderAndToolsStage output fields and
can grow as later stages move behind the harness boundary.

Note: distinct from ``opensquilla.engine.pipeline.TurnContext`` which is
the pre-turn pipeline value object. The two coexist while the pipeline
TurnContext stays in place. Import this one as::

    from opensquilla.engine.turn_runner.context import TurnContext as HarnessTurnContext

when both names are needed in the same module.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from opensquilla.contracts.turn_execution import (
    TurnExecutionContext,
    TurnPublicationLedger,
)
from opensquilla.engine.types import ControlTerminalEvent, ControlTerminalReason

if TYPE_CHECKING:
    from opensquilla.engine.agent import Agent, ToolHandler
    from opensquilla.engine.types import AgentConfig
    from opensquilla.observability.prompt_report import PromptReport
    from opensquilla.provider.types import ModelCapabilities
    from opensquilla.tools.types import ToolContext


def set_execution_deadline_if_missing(
    context: TurnExecutionContext | None,
    timeout_seconds: float | int | None,
) -> None:
    """Install the resolved whole-turn deadline without replacing an explicit one."""

    if context is None or context.deadline is not None or context.closed:
        return
    if isinstance(timeout_seconds, bool):
        return
    try:
        duration = float(timeout_seconds) if timeout_seconds is not None else 0.0
    except (TypeError, ValueError):
        return
    if duration <= 0:
        return
    context.set_deadline_if_missing(time.monotonic() + duration)


def control_terminal_event_for_context(
    context: TurnExecutionContext | None,
    reason: ControlTerminalReason | str,
) -> ControlTerminalEvent | None:
    """Create one control terminal event and close the context boundary.

    The additive event type already lives in ``engine.types`` while the
    immutable contract is intentionally frozen for this lane.  The small
    bridge updates the context's private terminal markers in the same owner
    task, making ``accept_event`` reject every late provider event without
    introducing a second terminal reset.
    """

    if context is None:
        return None
    if context.closed or context.terminal:
        return None

    normalized_reason = ControlTerminalReason(reason)
    sequence = context.begin_control_terminal(normalized_reason.value)
    if sequence is None:
        return None

    return ControlTerminalEvent(
        turn_id=context.identity.turn_id,
        assistant_message_id=context.identity.assistant_message_id,
        sequence=sequence,
        reason=normalized_reason,
    )

@dataclass
class TurnContext:
    """Cross-cutting state accumulated across stage classes."""

    # Created by the TurnRunner owner and explicitly threaded through stages.
    # ``None`` keeps older embedding/test callers source-compatible while the
    # identity-aware path is migrated one boundary at a time.
    execution_context: TurnExecutionContext | None = None
    assistant_message_id: str | None = None
    publication_ledger: TurnPublicationLedger | None = None

    # Populated by InputStage
    runtime_message: str = ""
    semantic_input: str = ""
    extra_prompt_context: dict[str, str] | None = None

    # Populated by ProviderAndToolsStage
    provider: Any = None
    cloned_selector: Any = None
    tool_defs: list[Any] = field(default_factory=list)
    tool_handler: ToolHandler | None = None
    effective_tool_context: ToolContext | None = None
    tool_metadata: dict[str, Any] = field(default_factory=dict)

    # Populated by PromptAssemblerStage. The ``provider`` field
    # above is OVERWRITTEN by this stage's output (the stage may have
    # wrapped it in ``_SelectorFallbackProvider``).
    turn: Any = None  # post-pipeline pipeline.TurnContext
    effective_runtime_message: str = ""
    final_prompt: str = ""
    cache_breakpoints: list[Any] | None = None
    request_context_prompt: str | None = None
    resolved_model: str = ""
    provider_name: str = ""
    session_id_for_log: str | None = None
    prompt_report: PromptReport | None = None
    selector_model: str = ""
    squilla_router_tier: Any = None

    # Populated by AgentBootstrapStage
    agent: Agent | None = None
    agent_config: AgentConfig | None = None
    effective_runtime_timeout: float = 0.0
    effective_max_iterations: int = 0
    effective_iteration_timeout: float = 0.0
    effective_tool_timeout: float = 0.0
    effective_request_timeout: float = 0.0
    effective_max_provider_retries: int = 0
    model_capabilities: ModelCapabilities | None = None
    private_memory_allowed: bool = False
    sync_manager: Any = None

    # Populated by CompactionAndHistoryStage
    t3_upgrade_status: str = ""
    preflight_invoked: bool = False
    loaded_compaction_summary_context: str | None = None
    final_request_context_prompt: str | None = None

    # Populated by AttachmentStage
    extra_attachment_messages: list[Any] | None = None
    turn_input: str = ""

    # Populated by StreamConsumerStage. Written by the harness
    # from the _StreamState passed into the stage after the stream
    # generator exhausts./consume these fields.
    stream_final_text_parts: list[str] = field(default_factory=list)
    stream_turn_segments: list[dict] = field(default_factory=list)
    stream_turn_artifacts: list[dict[str, Any]] = field(default_factory=list)
    stream_error_message: str | None = None
    stream_pending_error_event: Any | None = None  # ErrorEvent | None
    stream_done_event: Any | None = None  # DoneEvent | None

    # Populated by TurnFinalizerStage. Written by the harness
    # from TurnFinalizerStageOutput.
    # Consumed by (TurnHook.after_turn fan-out): the seven
    # finalized_* fields here carry the post-stage state that the
    # after_turn hook payload needs (final_text, turn_segments,
    # turn_artifacts, error_message, pending_error_event, done_event,
    # cost_rollup). keeps them populated unconditionally so
    # can read them without re-deriving from local scope.
    finalized_final_text: str = ""
    finalized_turn_segments: list[dict] = field(default_factory=list)
    finalized_turn_artifacts: list[dict[str, Any]] = field(default_factory=list)
    finalized_error_message: str | None = None
    finalized_pending_error_event: Any | None = None  # ErrorEvent | None
    finalized_done_event: Any | None = None  # DoneEvent | None
    finalized_cost_rollup: Any | None = None  # CostRollupResult | None
