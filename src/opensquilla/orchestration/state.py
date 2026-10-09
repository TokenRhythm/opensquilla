"""Derive an agent's user-visible state from durable runtime facts."""

from __future__ import annotations

from opensquilla.orchestration.models import (
    ActivationPhase,
    AgentActivationRecord,
    AgentSessionRecord,
    AgentState,
)


def derive_agent_state(
    session: AgentSessionRecord,
    activation: AgentActivationRecord | None,
    *,
    inbox_count: int,
    live_descendants: int,
) -> AgentState:
    """Return derived state without trusting a writable aggregate status.

    ``session`` is intentionally part of the interface even though the first
    resolver version needs only activation, inbox, and descendant facts.  This
    keeps lifecycle facts explicit at call sites and leaves archive projection
    separate from execution health.
    """

    del session
    if inbox_count < 0 or live_descendants < 0:
        raise ValueError("state counters must be non-negative")

    if activation is not None and activation.phase is ActivationPhase.STOPPING:
        return AgentState.STOPPING

    if activation is not None and (
        activation.phase is ActivationPhase.STARTING
        or activation.live_model_call
        or activation.live_tool_call
    ):
        return AgentState.WORKING

    if live_descendants:
        return AgentState.WAITING_CHILDREN

    if (
        activation is not None
        and activation.phase is not ActivationPhase.RELEASED
        and activation.external_wait_id is not None
    ):
        return AgentState.WAITING_EXTERNAL

    if inbox_count or (activation is not None and activation.phase is ActivationPhase.QUEUED):
        return AgentState.QUEUED

    if activation is not None and activation.phase is ActivationPhase.RUNNING:
        return AgentState.INVALID_STATE

    return AgentState.IDLE
