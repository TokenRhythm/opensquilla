"""Thinking/reasoning cleanup for cross-provider compatibility."""

from __future__ import annotations

from opensquilla.provider import ContentBlockText, ContentBlockThinking, Message


def _has_tool_use(content: object) -> bool:
    if not isinstance(content, list):
        return False
    return any(getattr(block, "type", None) == "tool_use" for block in content)


def drop_reasoning(
    messages: list[Message],
    *,
    preserve_tool_call_reasoning: bool = False,
    preserve_reasoning_content: bool = False,
) -> list[Message]:
    """Strip thinking blocks AND reasoning_content from assistant messages.

    - Removes ContentBlockThinking blocks from content lists (Anthropic)
    - Clears reasoning_content field (DeepSeek/OpenRouter)
    - Optionally preserves reasoning_content on assistant messages
    - Inserts placeholder text block if content becomes empty
    - Returns original list reference if nothing changed
    """
    touched = False
    out: list[Message] = []

    for msg in messages:
        # Clear reasoning_content on any assistant message that has it
        if msg.role == "assistant" and msg.reasoning_content is not None:
            keep_tool_reasoning = preserve_tool_call_reasoning and _has_tool_use(msg.content)
            keep_reasoning_content = preserve_reasoning_content or keep_tool_reasoning
            touched = True
            if isinstance(msg.content, list):
                filtered = [
                    b
                    for b in msg.content
                    if keep_tool_reasoning or not isinstance(b, ContentBlockThinking)
                ]
                if not filtered:
                    filtered = [ContentBlockText(text="")]
                out.append(
                    Message(
                        role="assistant",
                        content=filtered,
                        reasoning_content=(
                            msg.reasoning_content if keep_reasoning_content else None
                        ),
                    )
                )
            else:
                out.append(
                    Message(
                        role="assistant",
                        content=msg.content,
                        reasoning_content=(
                            msg.reasoning_content if keep_reasoning_content else None
                        ),
                    )
                )
            continue

        if msg.role != "assistant" or not isinstance(msg.content, list):
            out.append(msg)
            continue

        if preserve_tool_call_reasoning and _has_tool_use(msg.content):
            out.append(msg)
            continue

        # Check for thinking blocks in content (no reasoning_content to clear)
        filtered = []
        changed = False
        for block in msg.content:
            if isinstance(block, ContentBlockThinking):
                touched = True
                changed = True
                continue
            filtered.append(block)

        if not changed:
            out.append(msg)
            continue

        if not filtered:
            filtered = [ContentBlockText(text="")]

        out.append(Message(role="assistant", content=filtered))

    return out if touched else messages


# Backward compatibility alias
drop_thinking_blocks = drop_reasoning
