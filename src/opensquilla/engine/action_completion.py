"""Evidence contract for turns that execute action-capable tools."""

from __future__ import annotations

from opensquilla.provider import ToolDefinition, ToolInputSchema

ACTION_COMPLETION_TOOL_NAME = "complete_action_task"
ACTION_COMPLETION_RECOVERY_LIMIT = 1
ACTION_COMPLETION_INCOMPLETE_CODE = "action_completion_incomplete"

ACTION_COMPLETION_CONTRACT_MESSAGE = (
    "[Action completion contract]\n"
    "This turn executed an action-capable tool. Do not end the turn with prose "
    "alone. If the user's requested action is complete, call "
    "complete_action_task with a concise user-visible summary. If work remains, "
    "call the next necessary tool. Do not repeat a successful tool merely to "
    "satisfy this contract."
)

ACTION_COMPLETION_RECOVERY_MESSAGE = (
    "[Action completion recovery]\n"
    "The previous response contained text but no completion evidence. Make one "
    "final decision now: call complete_action_task with the final user-visible "
    "summary if the requested action is complete, or call the next necessary "
    "tool if it is not. Do not repeat or replay a successful action."
)

ACTION_COMPLETION_INCOMPLETE_MESSAGE = (
    "The action task stopped without verifiable completion evidence after one "
    "recovery attempt. No tool was replayed automatically."
)


def action_completion_tool_definition() -> ToolDefinition:
    """Return the internal, side-effect-free completion evidence tool."""

    return ToolDefinition(
        name=ACTION_COMPLETION_TOOL_NAME,
        description=(
            "Declare that the user's requested action is complete. This tool has "
            "no side effects and must only be called after all requested actions "
            "and required verification are finished. Include the same final "
            "user-visible answer as ordinary text in this response."
        ),
        input_schema=ToolInputSchema(
            properties={
                "summary": {
                    "type": "string",
                    "description": "Concise user-visible summary of the completed action.",
                }
            },
            required=["summary"],
        ),
        completion_effect="control",
    )


class ActionCompletionIncompleteError(RuntimeError):
    """Durable terminal signal for an action turn lacking completion evidence."""

    code = ACTION_COMPLETION_INCOMPLETE_CODE
    terminal_reason = ACTION_COMPLETION_INCOMPLETE_CODE

    def __init__(self, message: str = ACTION_COMPLETION_INCOMPLETE_MESSAGE) -> None:
        super().__init__(message)


__all__ = [
    "ACTION_COMPLETION_CONTRACT_MESSAGE",
    "ACTION_COMPLETION_INCOMPLETE_CODE",
    "ACTION_COMPLETION_INCOMPLETE_MESSAGE",
    "ACTION_COMPLETION_RECOVERY_LIMIT",
    "ACTION_COMPLETION_RECOVERY_MESSAGE",
    "ACTION_COMPLETION_TOOL_NAME",
    "ActionCompletionIncompleteError",
    "action_completion_tool_definition",
]
