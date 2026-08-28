"""Selection helpers for durable provider context state."""

from __future__ import annotations

from collections.abc import Sequence

from .models import SessionContextState


def _state_int_value(state: SessionContextState, field: str) -> int | None:
    value = getattr(state, field, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def context_state_order_key(index: int, state: SessionContextState) -> tuple[int, int, int]:
    """Return the canonical order for selecting the latest context state."""

    created_at = _state_int_value(state, "created_at")
    state_id = _state_int_value(state, "id")
    return (
        created_at if created_at is not None else -1,
        state_id if state_id is not None else -1,
        index,
    )


def ordered_context_states(
    context_states: Sequence[SessionContextState],
) -> list[SessionContextState]:
    indexed = list(enumerate(context_states))
    indexed.sort(key=lambda item: context_state_order_key(item[0], item[1]))
    return [state for _, state in indexed]


def latest_context_state(
    context_states: Sequence[SessionContextState],
    *,
    provider: str | None = None,
) -> SessionContextState | None:
    """Return the most recent context state, optionally provider-scoped.

    When ``provider`` is given, only states produced by that provider are
    considered, so provider-native state is never replayed against a
    different provider after a switch. ``None`` (default) preserves the
    previous global-newest behavior.
    """
    if provider is not None:
        context_states = [
            state for state in context_states if getattr(state, "provider", None) == provider
        ]
    ordered = ordered_context_states(context_states)
    return ordered[-1] if ordered else None


def latest_context_states_by_covered_through_id(
    context_states: Sequence[SessionContextState],
) -> list[SessionContextState]:
    ordered = ordered_context_states(context_states)
    latest_by_covered: dict[int, SessionContextState] = {}
    for state in ordered:
        latest_by_covered[state.covered_through_id] = state
    selected = {id(state) for state in latest_by_covered.values()}
    return [state for state in ordered if id(state) in selected]
