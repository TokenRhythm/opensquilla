"""Generic StageOutcome value: the universal stage return shape.

Every TurnRunner stage that owns a terminal-event branch returns
``StageOutcome[StageOutputT]`` so the harness can sequence stages
uniformly without per-stage adapters.

Most stages return ``StageOutcome.success(output)``. Stages that own a
terminal-event branch (provider-resolution failure today; future
preflight-compaction failure, etc.) return
``StageOutcome.terminate_with(early_yield=event)``.

Three invariants the dataclass enforces in ``__post_init__``:

- ``terminate=True`` requires ``early_yield`` to be a non-None
  ``AgentEvent`` AND ``output`` to be ``None``.
- ``terminate=False`` requires ``output`` to be a non-None stage-specific
  value AND ``early_yield`` to be ``None``.
- ``StageOutcome`` is frozen; the harness consumes it at the call site
  and never mutates it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opensquilla.engine.types import AgentEvent

@dataclass(frozen=True)
class StageOutcome[StageOutputT]:
    """Universal return value for a TurnRunner stage.

    Either carries a stage-specific ``output`` (success path) or an
    ``early_yield`` ``AgentEvent`` plus ``terminate=True`` (the stage
    decided the turn must end early; the harness yields the event and
    returns from the generator).
    """

    output: StageOutputT | None = None
    early_yield: AgentEvent | None = None
    terminate: bool = False

    def __post_init__(self) -> None:
        if self.terminate:
            if self.early_yield is None:
                raise ValueError(
                    "StageOutcome(terminate=True) requires early_yield"
                )
            if self.output is not None:
                raise ValueError(
                    "StageOutcome(terminate=True) must have output=None"
                )
        else:
            if self.early_yield is not None:
                raise ValueError(
                    "StageOutcome(terminate=False) must have early_yield=None"
                )
            if self.output is None:
                raise ValueError(
                    "StageOutcome(terminate=False) requires output"
                )

    def require_output(self) -> StageOutputT:
        """Return the success output, preserving the runtime invariant for type checkers."""

        if self.terminate or self.output is None:
            raise ValueError("StageOutcome does not carry output")
        return self.output

    def require_early_yield(self) -> AgentEvent:
        """Return the terminal event, preserving the runtime invariant for type checkers."""

        if not self.terminate or self.early_yield is None:
            raise ValueError("StageOutcome does not carry early_yield")
        return self.early_yield

    @classmethod
    def success(cls, output: StageOutputT) -> StageOutcome[StageOutputT]:
        """Construct a non-terminal outcome carrying a stage output."""

        return cls(output=output, early_yield=None, terminate=False)

    @classmethod
    def terminate_with(
        cls, early_yield: AgentEvent
    ) -> StageOutcome[StageOutputT]:
        """Construct a terminal outcome carrying an early-yield event."""

        return cls(output=None, early_yield=early_yield, terminate=True)
