"""Shared dataclasses and protocols for the policy pipeline.

A :class:`DispatchInput` carries every piece of state a :class:`PolicyCheck`
needs to make a decision. Checks return :class:`PolicyDecision` instances; an
allowing decision sets ``allowed=True`` with no envelope, while a denying
decision sets ``allowed=False`` and provides the :class:`ToolResult`
envelope plus an optional structured log event the orchestrator must emit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from opensquilla.tool_boundary import ToolCall, ToolResult
from opensquilla.tools.types import ToolContext

if TYPE_CHECKING:  # pragma: no cover — type-only to avoid an import cycle.
    from opensquilla.tools.registry import ToolRegistry

@dataclass(frozen=True)
class DispatchInput:
    """Inputs available to every :class:`PolicyCheck`.

    Attributes
    ----------
    tool_call:
        The original :class:`ToolCall` requested by the agent loop.
    ctx:
        The effective :class:`ToolContext` (resolved from
        ``current_tool_context`` or the handler-build-time fallback).
    registered:
        The :class:`RegisteredTool` resolved from ``registry.get(...)``.
        Always present — registry-miss handling is performed by the
        orchestrator before the chain is consulted.
    registered_spec:
        Convenience alias for ``registered.spec`` to avoid attribute
        chains in the check bodies.
    registered_handler:
        Convenience alias for ``registered.handler``.
    known_skill_names:
        Frozen set of skill names recognised by the agent loop. Currently
        unused by the policy chain (the orchestrator handles the skill
        miss path), but threaded through so future checks can opt in.
    registry:
        The :class:`ToolRegistry` the handler was built from. Threaded
        through for completeness; currently unused by the chain.
    """

    tool_call: ToolCall
    ctx: ToolContext | None
    registered: Any
    known_skill_names: frozenset[str]
    registry: ToolRegistry

    @property
    def registered_spec(self) -> Any:
        return self.registered.spec

    @property
    def registered_handler(self) -> Any:
        return self.registered.handler

@dataclass(frozen=True)
class PolicyDecision:
    """Result of one :class:`PolicyCheck` evaluation.

    ``allowed=True`` means the check passes; ``envelope`` and ``log_event``
    must be ``None``. ``allowed=False`` means the chain must short-circuit
    with ``envelope`` as the returned :class:`ToolResult`. When ``log_event``
    is provided the orchestrator emits it at WARN level via the dispatch
    logger before returning.
    """

    allowed: bool
    envelope: ToolResult | None = None
    log_event: dict[str, Any] | None = None

@runtime_checkable
class PolicyCheck(Protocol):
    """Single-evaluation policy interface.

    Each implementation has a stable ``name`` for logging and an
    :meth:`evaluate` method that returns a :class:`PolicyDecision`. Checks
    must be pure with respect to ``DispatchInput``: no I/O, no global
    state, no exceptions. The orchestrator handles emission of any
    ``log_event`` returned by a denying decision.
    """

    name: str

    def evaluate(self, d: DispatchInput) -> PolicyDecision:
        ...
