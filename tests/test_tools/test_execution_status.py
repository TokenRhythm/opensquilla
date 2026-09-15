"""Session status separates accepted selection, live requests and durable results."""

import json
from types import SimpleNamespace

from opensquilla.tools.builtin import sessions
from opensquilla.tools.types import ToolContext, current_tool_context


async def test_status_keeps_three_execution_sources_separate(monkeypatch) -> None:
    key = "agent:main:synthetic-status"
    row = SimpleNamespace(session_key=key, session_id="synthetic-session", model="legacy-model")

    class Manager:
        async def get_session(self, session_key):
            assert session_key == key
            return row

        async def get_transcript(self, session_key, **kwargs):
            assert session_key == key
            assert kwargs == {
                "limit": 50, "expected_session_id": row.session_id, "expected_session_epoch": 2,
            }
            return [
                SimpleNamespace(role="assistant", message_id="previous-answer", turn_usage={
                    "model": "reported-old-alias",
                    "execution_legs": [{"model": "previous-deployment", "provider": "synthetic"}],
                }),
                SimpleNamespace(role="assistant", message_id="partial-answer", turn_usage=None),
                SimpleNamespace(role="user", turn_usage={"model": "user-owned"}),
            ]

    monkeypatch.setattr(sessions, "_session_manager", Manager())
    snapshot = {
        "selection": {"model": "selected-model"},
        "current_request": {"model": "current-deployment"},
    }
    ctx = ToolContext(
        session_key=key, session_id=row.session_id, session_epoch=2,
        execution_status_snapshot=lambda: dict(snapshot),
    )
    token = current_tool_context.set(ctx)
    try:
        result = json.loads(await sessions.session_status())
    finally:
        current_tool_context.reset(token)

    assert result["model"] == "legacy-model"
    assert result["execution"]["selection"] == snapshot["selection"]
    assert result["execution"]["current_request"] == snapshot["current_request"]
    assert result["execution"]["last_completed"] == {
        "model": "previous-deployment", "provider": "synthetic",
        "reported_model": "reported-old-alias", "message_id": "previous-answer",
    }
    assert "last_completed" not in snapshot
