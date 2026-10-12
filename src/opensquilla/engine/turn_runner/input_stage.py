"""Stage object for the input-normalize + input-persist slice of _run_turn.

Owns the per-turn input slice. The harness invokes ``InputStage.run``
once per turn, before any provider/tool/prompt resolution.

Side-effect contract: re-raises any exception from
``SessionAppendPort.append_message`` exactly as the inline body did. The
harness catches it through the existing CancelledError / Exception
terminal handlers in ``_run_turn``. ``InputStage`` does NOT call any
``TurnHook`` — those fire once per turn at the harness level.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from opensquilla.engine.turn_runner.outcome import StageOutcome
    from opensquilla.session.models import TranscriptEntry
    from opensquilla.tools.types import ToolContext

# ---------------------------------------------------------------------------
# Ports — narrow protocols so the stage is unit-testable without the full
# SessionManager. Production SessionManager satisfies SessionAppendPort
# structurally; the stage never touches non-append SessionManager methods.
# ---------------------------------------------------------------------------

@runtime_checkable
class SessionAppendPort(Protocol):
    """The single SessionManager method InputStage needs.

    Mirrors ``SessionManager.append_message`` structurally. The stage relies
    on the ``role`` / ``content`` / ``provenance`` keyword args and the
    returned ``TranscriptEntry.content`` for stamp pickup.
    """

    async def append_message(
        self,
        session_key: str,
        role: str,
        content: str,
        *,
        provenance: dict[str, Any] | None = None,
    ) -> TranscriptEntry: ...

@runtime_checkable
class ExtraContextResolver(Protocol):
    """Adapter around the static helpers ``_extra_context_for_tool_context``
    and ``_merge_extra_prompt_context`` on ``TurnRunner``.

    Both helpers are static methods today; the harness binds a tiny
    adapter at runtime so ``InputStage`` does not import the runtime.
    """

    def extra_context_for(self, ctx: ToolContext | None) -> dict[str, str]: ...

    def merge(
        self,
        base: dict[str, str] | None,
        extra: dict[str, str],
    ) -> dict[str, str] | None: ...

# ---------------------------------------------------------------------------
# Stage I/O dataclasses (frozen — stage outputs are immutable values
# the Harness accumulates onto TurnContext)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InputStageInput:
    """Inputs the InputStage needs at the boundary it owns.

    Mirrors the locals visible to the original inline slice today: the raw
    ``message``, the optional ``semantic_message``, the ``input_mode``, the
    persistence flag + provenance, the session key, and the tool context
    (for the subagent extra-prompt-context branch). The session-append port
    is injected so the stage does not import SessionManager directly.
    """

    message: str
    semantic_message: str | None
    input_mode: str
    persist_input: bool
    input_provenance: dict[str, Any] | None
    session_key: str
    tool_context: ToolContext | None
    session_append: SessionAppendPort | None

@dataclass(frozen=True)
class InputStageOutput:
    """The four pieces of state subsequent stages consume.

    - ``runtime_message``: what the agent loop sees as the user-input string
      (may be the SessionManager-stamped variant).
    - ``semantic_input``: what prompt-assembly uses for the semantic-input
      slot in ``prompt_report`` (NEVER the wrapped form).
    - ``extra_prompt_context``: the Internal-Event-Mode + subagent-protocol
      merged dict that prompt-assembly later threads into the system prompt.
    - ``persisted_entry``: the TranscriptEntry returned by SessionManager
      when ``persist_input=True``. ``None`` when the persist branch was
      skipped (no SessionManager, empty message, or persist_input=False).
      Carried so tests can assert the call shape; downstream stages do NOT
      consume it.
    """

    runtime_message: str
    semantic_input: str
    extra_prompt_context: dict[str, str] | None
    persisted_entry: TranscriptEntry | None = None
    normalization_metadata: dict[str, Any] | None = None

    def to_outcome(self) -> StageOutcome[InputStageOutput]:
        """Wrap this output as a non-terminal ``StageOutcome``.

        Cosmetic shim — ``InputStage`` never produces an early-yield, so
        the harness can equivalently wrap externally. Provided so all
        TurnRunner stages document the contract at the producer site.
        """

        from opensquilla.engine.turn_runner.outcome import StageOutcome

        return StageOutcome.success(self)

# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class InputStage:
    """Normalize the user/system input and (optionally) persist it.

    Stable boundary: runs ONCE per turn, before provider resolution. Pure
    with respect to its inputs except for the optional
    ``session_append.append_message`` call.
    """

    name = "input_stage"

    def __init__(self, extra_ctx: ExtraContextResolver) -> None:
        self._extra_ctx = extra_ctx

    async def run(self, inp: InputStageInput) -> InputStageOutput:
        runtime_message = inp.message
        semantic_input = (
            inp.semantic_message if inp.semantic_message is not None else inp.message
        )
        extra_prompt_context: dict[str, str] | None = None

        if inp.input_mode == "system_event":
            runtime_message = f"[INTERNAL SYSTEM EVENT]\n{inp.message}"
            semantic_input = inp.message
            extra_prompt_context = {
                "Internal Event Mode": (
                    "The next input is an internal scheduler event, not a human user"
                    " message. Treat it as system-originated context."
                )
            }

        extra_prompt_context = self._extra_ctx.merge(
            extra_prompt_context,
            self._extra_ctx.extra_context_for(inp.tool_context),
        )
        normalization_metadata = None
        if isinstance(inp.input_provenance, dict):
            input_normalization = inp.input_provenance.get("input_normalization")
            if isinstance(input_normalization, dict):
                normalization_metadata = dict(input_normalization)

        persisted_entry = None
        if inp.persist_input and inp.session_append is not None and inp.message:
            input_role = "system" if inp.input_mode == "system_event" else "user"
            persisted_entry = await inp.session_append.append_message(
                inp.session_key,
                role=input_role,
                content=inp.message,
                provenance=inp.input_provenance,
            )
            # Pick up any stamp SessionManager applied (user role only).
            if (
                inp.input_mode != "system_event"
                and persisted_entry is not None
                and isinstance(persisted_entry.content, str)
                and persisted_entry.content != inp.message
            ):
                runtime_message = persisted_entry.content

        return InputStageOutput(
            runtime_message=runtime_message,
            semantic_input=semantic_input,
            extra_prompt_context=extra_prompt_context,
            persisted_entry=persisted_entry,
            normalization_metadata=normalization_metadata,
        )
