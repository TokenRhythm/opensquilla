"""Bounded feedback for rejected, never-executed provider tool batches."""

from __future__ import annotations

import json

from opensquilla.provider.types import Message, ToolArgumentRejection

MAX_TOOL_ARGUMENT_CORRECTIONS = 2


def valid_tool_argument_rejection(
    rejection: ToolArgumentRejection, *, tool_names: set[str],
) -> bool:
    """Reject incomplete or ambiguous proofs before allowing continuation."""
    calls = rejection.calls
    return (
        bool(calls)
        and bool(rejection.terminal_reason)
        and len({call.tool_call_id for call in calls}) == len(calls)
        and all(
            call.tool_call_id
            and call.tool_name in tool_names
            and call.reason in {"invalid_json", "schema_invalid", "batch_not_executed"}
            for call in calls
        )
        and any(call.reason != "batch_not_executed" for call in calls)
    )


def append_tool_argument_feedback(
    messages: list[Message],
    rejection: ToolArgumentRejection,
    *,
    visible_text: str,
    reasoning_content: str | None = None,
) -> None:
    """Preserve visible prose, never forge executable tool calls or results."""
    # An empty assistant response is intentional: replay v1 starts with the
    # assistant, even when this completed generation emitted only a rejected
    # call. Never synthesize a tool call with guessed arguments for that role.
    messages.append(Message(
        role="assistant", content=visible_text, reasoning_content=reasoning_content,
    ))
    details = json.dumps(
        [{"tool": call.tool_name, "reason": call.reason} for call in rejection.calls],
        ensure_ascii=False,
    )
    messages.append(Message(
        role="user",
        content=(
            "[Runtime tool argument feedback]\n"
            "Your response completed, but its tool batch was rejected before execution. "
            "No tools in THIS batch ran, including any valid sibling calls. "
            "Earlier completed tool results remain valid; do not repeat those operations. "
            "Rejected batch: " + details + "\n"
            "Continue the original task by regenerating complete JSON object arguments "
            "matching the tool schemas. Do not repair or guess a truncated string locally. "
            "Do not repeat the explanation already shown, and do not claim any rejected "
            "operation succeeded."
        ),
    ))
