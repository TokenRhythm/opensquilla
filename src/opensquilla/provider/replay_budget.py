"""Budget accepted replay without counting exact display aliases twice."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .types import Message, ProviderReplayState

_ANTHROPIC_BLOCK_FIELDS = {
    "text": ("type", "text"),
    "thinking": ("type", "thinking", "signature"),
    "redacted_thinking": ("type", "data"),
    "tool_use": ("type", "id", "name", "input"),
    "compaction": ("type", "content", "cache_control"),
}


def _same_json(left: Any, right: Any) -> bool:
    try:
        return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
            right, sort_keys=True, allow_nan=False,
        )
    except (OverflowError, RecursionError, TypeError, ValueError):
        return False


def project_message_replay_budget(message: Message | Mapping[str, Any]) -> dict[str, Any]:
    """Return a read-only budget view, retaining typed media and opaque data.

    Only proven aliases are removed. Unknown protocols, modified content, and
    unsupported native blocks keep the conservative complete representation.
    Source identity cannot prove a target route here, so it never removes state.
    """
    payload = {key: value for key, value in dict(message).items() if value is not None}
    raw_state = payload.get("provider_replay")
    if payload.get("role") != "assistant" or not isinstance(
        raw_state, ProviderReplayState | Mapping,
    ):
        return payload
    state = {key: value for key, value in dict(raw_state).items() if value is not None}
    reasoning = payload.get("reasoning_content")
    aliases: list[str] = []
    if state.get("protocol") == "anthropic_messages":
        native = state.get("native_content")
        content = payload.get("content")
        if not isinstance(native, list) or not isinstance(content, list):
            return payload
        if any(
            not isinstance(block, dict) or not isinstance(block.get("type"), str)
            or block["type"] not in _ANTHROPIC_BLOCK_FIELDS
            for block in native
        ):
            return payload
        current = []
        for block in content:
            value = block.model_dump(mode="json") if hasattr(block, "model_dump") else block
            if isinstance(value, dict):
                value = {
                    key: item for key, item in value.items()
                    if item is not None or key not in {"signature", "content", "cache_control"}
                }
            current.append(value)
        known_native = [
            {key: block[key] for key in _ANTHROPIC_BLOCK_FIELDS[block["type"]] if key in block}
            for block in native
        ]
        if not _same_json(current, known_native):
            return payload
        # Keep raw citations and other opaque metadata alongside the one copy
        # of each block. Typed media never enters this supported-block branch.
        payload["content"] = native
        state.pop("native_content")
        thinking = [block.get("thinking") for block in native if block["type"] == "thinking"]
        if thinking and all(isinstance(text, str) for text in thinking):
            aliases.append("".join(thinking))
    elif state.get("protocol") == "openai_chat_completions":
        native_reasoning = state.get("native_reasoning_content")
        if isinstance(native_reasoning, str):
            aliases.append(native_reasoning)
        details = state.get("reasoning_details")
        if isinstance(details, list):
            texts = [
                detail["text"] for detail in details
                if isinstance(detail, dict) and detail.get("type") == "reasoning.text"
                and isinstance(detail.get("text"), str)
            ]
            if texts:
                aliases.append("".join(texts))
    else:
        return payload
    if isinstance(reasoning, str) and reasoning in aliases:
        payload.pop("reasoning_content")
    payload["provider_replay"] = state
    return payload
