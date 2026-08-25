from __future__ import annotations

from opensquilla.engine.history import strip_historical_tool_pairs
from opensquilla.provider import (
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
)


def test_restricted_history_projection_strips_tool_pairs_without_mutating_history() -> None:
    messages = [
        Message(
            role="user",
            content=(
                "[Available skills for this turn]\n"
                "Load /secret/workspace/SKILL.md"
            ),
        ),
        Message(role="user", content="Inspect the workspace"),
        Message(
            role="assistant",
            content=[
                ContentBlockText(text="Inspection complete."),
                ContentBlockToolUse(
                    id="tool-1",
                    name="read_file",
                    input={"path": "/secret/workspace/private.txt"},
                ),
            ],
            reasoning_content="Read /secret/workspace/private.txt",
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="tool-1",
                    content="/secret/workspace/private.txt contents",
                    is_error=False,
                )
            ],
        ),
    ]

    projected, result = strip_historical_tool_pairs(messages)

    assert len(projected) == 2
    assert projected[0] is messages[1]
    assert projected[1].reasoning_content is None
    assert projected[1].content == [ContentBlockText(text="Inspection complete.")]
    assert result.tool_uses_removed == 1
    assert result.tool_results_removed == 1
    assert result.empty_messages_removed == 1
    assert result.synthetic_messages_removed == 1
    assert "/secret/workspace/private.txt" in str(messages)
    assert "/secret/workspace/private.txt" not in str(projected)
