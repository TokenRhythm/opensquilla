"""Canonical Gateway history projection for the OpenTUI host."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any

from opensquilla.cli.chat.session_state import ChatSessionState
from opensquilla.cli.chat.turn import UsageSummary
from opensquilla.cli.tui.opentui.messages import (
    ComposerState,
    HistoryMessage,
    HistoryReplace,
)

HISTORY_BOOTSTRAP_LIMIT = 200


def history_replace_from_bootstrap(
    snapshot: dict[str, Any],
    *,
    fallback_session_key: str,
) -> HistoryReplace:
    """Normalize a ``sessions.bootstrap`` result into the typed host frame."""

    raw_session = snapshot.get("session")
    session = raw_session if isinstance(raw_session, dict) else {}
    raw_history = snapshot.get("history")
    history = raw_history if isinstance(raw_history, dict) else {}
    raw_messages = history.get("messages")
    message_rows = raw_messages if isinstance(raw_messages, list) else []
    session_key = str(session.get("session_key") or fallback_session_key)

    messages = tuple(
        _history_message(row, ordinal=index)
        for index, row in enumerate(message_rows)
        if isinstance(row, dict)
    )
    summaries = tuple(
        dict(item) for item in history.get("compaction_summaries") or () if isinstance(item, dict)
    )
    scope = str(history.get("history_scope") or "complete")
    if scope not in {"complete", "latest_window", "compacted"}:
        scope = "latest_window" if history.get("has_more") else "complete"
    return HistoryReplace(
        session_key=session_key,
        history_scope=scope,
        has_more=bool(history.get("has_more")),
        loaded_count=int(history.get("loaded_count") or len(messages)),
        canonical_available=bool(history.get("canonical_available")),
        messages=messages,
        compaction_summaries=summaries,
    )


def apply_bootstrap_to_state(
    state: ChatSessionState,
    snapshot: dict[str, Any],
    history: HistoryReplace,
) -> None:
    """Atomically align mutable CLI state with a canonical bootstrap."""

    raw_session = snapshot.get("session")
    session = raw_session if isinstance(raw_session, dict) else {}
    state.session_key = history.session_key
    if "effective_model" in session or "model" in session:
        model = session.get("effective_model") or session.get("model")
        state.model = str(model) if model else None
    state.transcript.clear()
    state.usage.reset()
    for message in history.messages:
        if message.role in {"user", "assistant"}:
            state.transcript.add(message.role, message.text)
        # Summing a latest-window/compacted page would present a partial total
        # as the session cost. Only rebuild it when the bootstrap is complete.
        if history.history_scope == "complete" and message.usage:
            state.usage.add(UsageSummary.from_gateway_payload(message.usage))


async def set_tui_history_loading(output: object | None, *, loading: bool) -> None:
    """Toggle the structured composer while a session snapshot is loading."""

    send = getattr(output, "send_message", None)
    if not callable(send):
        return
    await send(
        "composer.set",
        asdict(
            ComposerState(
                placeholder="loading session history" if loading else "send a message",
                disabled=loading,
            )
        ),
    )


async def replace_tui_history(
    output: object | None,
    history: HistoryReplace,
    *,
    manage_composer: bool = True,
) -> None:
    """Disable input around one atomic host-side transcript replacement.

    Native/plain output handles intentionally have no structured message API;
    their in-memory state is still replaced by :func:`apply_bootstrap_to_state`.
    """

    send = getattr(output, "send_message", None)
    if not callable(send):
        return
    if manage_composer:
        await set_tui_history_loading(output, loading=True)
    try:
        await send("history.replace", asdict(history))
    finally:
        if manage_composer:
            await set_tui_history_loading(output, loading=False)


def _history_message(row: dict[str, Any], *, ordinal: int) -> HistoryMessage:
    role = str(row.get("role") or "message")
    text = _display_text(row)
    timestamp = row.get("timestamp") if row.get("timestamp") is not None else row.get("ts")
    raw_id = row.get("message_id") or row.get("messageId") or row.get("id")
    if raw_id is None:
        raw_id = row.get("transcript_id")
    message_id = (
        str(raw_id)
        if raw_id not in {None, ""}
        else _legacy_message_id(
            role=role,
            text=text,
            timestamp=timestamp,
            ordinal=ordinal,
        )
    )
    raw_usage = row.get("usage") or row.get("turn_usage")
    usage = dict(raw_usage) if isinstance(raw_usage, dict) else _flattened_usage(row)
    return HistoryMessage(
        id=message_id,
        role=role,
        text=text,
        timestamp=timestamp if isinstance(timestamp, str | int | float) else None,
        reasoning=str(row.get("reasoning_content") or row.get("reasoning") or ""),
        attachments=_dict_tuple(row.get("attachments")),
        artifacts=_dict_tuple(row.get("artifacts")),
        tool_calls=_dict_tuple(row.get("tool_calls")),
        usage=usage,
        turn_context=(
            dict(row["turn_context"])
            if isinstance(row.get("turn_context"), dict)
            else None
        ),
    )


def _display_text(row: dict[str, Any]) -> str:
    value = row.get("text")
    if value is None:
        value = row.get("content")
    return value if isinstance(value, str) else str(value or "")


def _dict_tuple(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, dict))


def _flattened_usage(row: dict[str, Any]) -> dict[str, Any] | None:
    keys = (
        "input_tokens",
        "inputTokens",
        "output_tokens",
        "outputTokens",
        "reasoning_tokens",
        "reasoningTokens",
        "cached_tokens",
        "cachedTokens",
        "cost_usd",
        "costUsd",
        "model",
    )
    usage = {key: row[key] for key in keys if row.get(key) is not None}
    return usage or None


def _legacy_message_id(
    *,
    role: str,
    text: str,
    timestamp: object,
    ordinal: int,
) -> str:
    digest = hashlib.blake2s(f"{role}\0{timestamp!s}\0{text}".encode(), digest_size=10).hexdigest()
    return f"legacy-{digest}-{ordinal}"


__all__ = [
    "HISTORY_BOOTSTRAP_LIMIT",
    "apply_bootstrap_to_state",
    "history_replace_from_bootstrap",
    "replace_tui_history",
    "set_tui_history_loading",
]
