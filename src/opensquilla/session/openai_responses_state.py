"""Session context-state helpers for OpenAI Responses compaction."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from opensquilla.provider.context_capabilities import (
    OPENAI_RESPONSES_COMPACTED_WINDOW_STATE_KIND,
)

from .context_state_selection import latest_context_state
from .keys import canonicalize_session_key
from .models import SessionContextState


def _valid_openai_responses_compacted_window_state(
    state: SessionContextState,
    *,
    now_ms: int | None = None,
) -> bool:
    if not state.valid:
        return False
    if state.provider != "openai_responses":
        return False
    if state.state_kind != OPENAI_RESPONSES_COMPACTED_WINDOW_STATE_KIND:
        return False
    if now_ms is not None and state.expires_at is not None and state.expires_at <= now_ms:
        return False
    return isinstance(state.payload.get("output"), list)


def build_openai_responses_input_items(
    *,
    context_states: list[SessionContextState],
    current_items: list[dict[str, Any]],
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Return Responses input with the latest compacted window replayed as-is."""

    valid_states = [
        state
        for state in context_states
        if _valid_openai_responses_compacted_window_state(state, now_ms=now_ms)
    ]
    if not valid_states:
        return deepcopy(current_items)

    latest_state = latest_context_state(valid_states, provider="openai_responses")
    if latest_state is None:
        return deepcopy(current_items)
    compacted_output = latest_state.payload["output"]
    return [*deepcopy(compacted_output), *deepcopy(current_items)]


def openai_responses_compacted_window_state(
    *,
    session_id: str,
    session_key: str,
    model: str,
    compact_response: dict[str, Any],
    covered_through_id: int,
) -> SessionContextState:
    """Build a provider-native state record from `/responses/compact` output.

    OpenAI documents the compact output as the canonical next context window.
    The `output` array can contain retained items plus opaque encrypted
    compaction items, so this helper validates only the envelope and stores the
    array unchanged.
    """

    output = compact_response.get("output")
    if not isinstance(output, list):
        raise ValueError("OpenAI Responses compact response must include output list")

    payload: dict[str, Any] = {
        "response_id": compact_response.get("id"),
        "output": deepcopy(output),
        "usage": deepcopy(compact_response.get("usage")),
        "opaque": True,
    }
    return SessionContextState(
        session_id=session_id,
        session_key=canonicalize_session_key(session_key),
        provider="openai_responses",
        model=model,
        state_kind=OPENAI_RESPONSES_COMPACTED_WINDOW_STATE_KIND,
        payload=payload,
        covered_through_id=covered_through_id,
        portable=False,
        cacheable=False,
        valid=True,
    )
