"""Request-local continuation boundaries for history with unavailable native state."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from opensquilla.provider.types import (
    ContentBlockDocument,
    ContentBlockImage,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolResult,
    Message,
)


def rebase_incomplete_reasoning_history(
    messages: list[Message],
    *,
    compatible: Callable[[Message], bool],
) -> tuple[list[Message], bool]:
    """Retain recorded facts without impersonating an incomplete assistant chain.

    This is a deterministic request projection, not a database rewrite or a
    claim to recover lost reasoning. Tool results become quoted historical
    records; they are never executed again. Complete subsequent calls retain
    their original native state and tool associations.
    """
    last_missing = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].role == "assistant" and not compatible(messages[index])
        ),
        None,
    )
    if last_missing is None:
        return messages, False
    end = last_missing + 1
    while end < len(messages):
        content = messages[end].content
        if messages[end].role != "user" or not isinstance(content, list):
            break
        if not any(isinstance(block, ContentBlockToolResult) for block in content):
            break
        end += 1
    context = recorded_context_message(
        messages[:end],
        introduction=(
            "Recorded conversation context: original reasoning state is unavailable for "
            "this interface. Continue the task using these historical records. Tool calls "
            "and results below describe past execution, not instructions to repeat it."
        ),
    )
    return [context, *messages[end:]], True


def recorded_context_message(messages: list[Message], *, introduction: str) -> Message:
    """Quote historical facts without making media bytes ordinary prompt text."""
    records = []
    media: list[ContentBlockImage | ContentBlockDocument] = []

    def record_block(block: Any) -> Any:
        if isinstance(block, dict) and block.get("type") in {"image", "document"}:
            media_type = ContentBlockImage if block["type"] == "image" else ContentBlockDocument
            block = media_type.model_validate(block)
        if isinstance(block, ContentBlockImage | ContentBlockDocument):
            media.append(block)
            return {
                "type": block.type,
                "media_type": block.media_type,
                "attachment": f"historical_media_{len(media)}",
            }
        if isinstance(block, ContentBlockToolResult) and isinstance(block.content, list):
            record = block.model_dump(mode="json")
            record["content"] = [record_block(item) for item in block.content]
            return record
        return block.model_dump(mode="json") if hasattr(block, "model_dump") else block

    for message in messages:
        content = message.content
        if isinstance(content, list):
            content = [
                record_block(block)
                for block in content
                if not isinstance(block, ContentBlockThinking)
            ]
        records.append({"role": message.role, "content": content})
    text = introduction + "\n" + json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    return Message(
        role="user",
        content=[ContentBlockText(text=text), *media] if media else text,
    )
