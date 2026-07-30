from __future__ import annotations

from typing import Any

import pytest

from opensquilla.cli.chat.session_state import ChatSessionState
from opensquilla.cli.tui.opentui.history import (
    apply_bootstrap_to_state,
    history_replace_from_bootstrap,
    replace_tui_history,
)


def _snapshot() -> dict[str, Any]:
    return {
        "session": {
            "session_key": "agent:main:canonical",
            "model": "openai/test",
        },
        "history": {
            "history_scope": "compacted",
            "has_more": False,
            "loaded_count": 2,
            "canonical_available": True,
            "compaction_summaries": [{"id": "summary-1", "summary_text": "earlier"}],
            "messages": [
                {
                    "message_id": "m1",
                    "role": "user",
                    "text": "persisted question",
                    "attachments": [{"name": "brief.pdf"}],
                },
                {
                    "message_id": "m2",
                    "role": "assistant",
                    "text": "persisted answer",
                    "reasoning_content": "checked",
                    "artifacts": [{"id": "artifact-1"}],
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                },
            ],
        },
    }


def test_bootstrap_projection_preserves_scope_content_and_durable_ids() -> None:
    history = history_replace_from_bootstrap(
        _snapshot(),
        fallback_session_key="agent:main:alias",
    )

    assert history.session_key == "agent:main:canonical"
    assert history.history_scope == "compacted"
    assert history.canonical_available is True
    assert [message.id for message in history.messages] == ["m1", "m2"]
    assert history.messages[0].attachments == ({"name": "brief.pdf"},)
    assert history.messages[1].reasoning == "checked"
    assert history.messages[1].artifacts == ({"id": "artifact-1"},)


def test_bootstrap_replaces_local_transcript_without_partial_usage_total() -> None:
    state = ChatSessionState(session_key="agent:main:old", model="old")
    state.transcript.add("user", "stale")
    history = history_replace_from_bootstrap(
        _snapshot(),
        fallback_session_key=state.session_key,
    )

    apply_bootstrap_to_state(state, _snapshot(), history)

    assert state.session_key == "agent:main:canonical"
    assert state.model == "openai/test"
    assert [turn.content for turn in state.transcript.turns] == [
        "persisted question",
        "persisted answer",
    ]
    assert state.usage.input_tokens == 0
    assert state.usage.output_tokens == 0


class _Output:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send_message(self, message_type: str, payload: dict[str, Any]) -> None:
        self.sent.append((message_type, payload))


@pytest.mark.asyncio
async def test_tui_history_replace_disables_composer_around_atomic_frame() -> None:
    output = _Output()
    history = history_replace_from_bootstrap(
        _snapshot(),
        fallback_session_key="agent:main:alias",
    )

    await replace_tui_history(output, history)

    assert [message_type for message_type, _payload in output.sent] == [
        "composer.set",
        "history.replace",
        "composer.set",
    ]
    assert output.sent[0][1]["disabled"] is True
    assert output.sent[1][1]["messages"][0]["id"] == "m1"
    assert output.sent[2][1]["disabled"] is False
