"""Chat transcript normalization shared by frontends."""

from __future__ import annotations

import json
import re
from typing import Any

from opensquilla.artifacts import artifact_payload, strip_artifact_markers_from_text
from opensquilla.chat.flattened_tool_markers import (
    flattened_used_tool_names,
    has_flattened_used_tool_line,
    is_flattened_tool_result_dump,
    parse_flattened_tool_result_dump,
    strip_confirmed_flattened_tool_result,
    strip_flattened_used_tool_lines,
)
from opensquilla.meta_preflight_protocol import (
    display_text_from_preflight_confirmation,
    strip_preflight_confirmation_protocol_text,
)
from opensquilla.silent_reply import sanitize_historical_silent_reply
from opensquilla.turn_outcome_projection import public_turn_context

_LEGACY_PLAN_IMPLEMENTATION_PROMPT = re.compile(
    r'Implement the approved plan “.+”\. '
    r"Work through its ordered steps and record truthful checkpoints\."
)


def _sanitize_display_protocol_payload(value: Any) -> Any:
    if isinstance(value, str):
        clean = strip_preflight_confirmation_protocol_text(value)
        return clean if clean is not None else value
    if isinstance(value, list):
        return [_sanitize_display_protocol_payload(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _sanitize_display_protocol_payload(item)
            for key, item in value.items()
        }
    return value


def _is_legacy_generated_plan_implementation(
    content: str,
    turn_context: Any,
) -> bool:
    """Recognize the exact pre-display_text PlanRun control prompt.

    Older gateways persisted the generated provider instruction as ordinary
    user-visible text. The positive PlanRun id plus the exact server template
    makes this a protocol compatibility check, not a guess based on user prose.
    Explicit implementation messages do not use this template and remain
    visible.
    """

    if not isinstance(turn_context, dict) or not turn_context.get("plan_run_id"):
        return False
    visible = str(content or "").strip()
    if visible.startswith("[") and "]\n" in visible:
        visible = visible.split("]\n", 1)[1].strip()
    return _LEGACY_PLAN_IMPLEMENTATION_PROMPT.fullmatch(visible) is not None


def _legacy_flattened_tool_result_pairs(entries: list[object]) -> dict[int, int]:
    """Map legacy result rows to their adjacent flattened assistant call row.

    Modern rows carry ``tool_call_id`` or role ``tool``. Older compaction
    projections sometimes persisted Anthropic-style tool results as role
    ``user`` with no structured identity, so recognize only the adjacent
    assistant-marker/result pair. An isolated user message that merely quotes
    the legacy syntax must remain ordinary conversation text.
    """

    pairs: dict[int, int] = {}
    previous_was_flattened_call = False
    for index, entry in enumerate(entries):
        role = str(getattr(entry, "role", "unknown") or "unknown").lower()
        content = str(getattr(entry, "content", "") or "")
        if (
            previous_was_flattened_call
            and role in {"tool", "user"}
            and is_flattened_tool_result_dump(content)
        ):
            pairs[index] = index - 1
        previous_was_flattened_call = (
            role == "assistant" and has_flattened_used_tool_line(content)
        )
    return pairs


def _legacy_tool_activity_segments(
    tool_entry: object,
    result_entry: object | None = None,
) -> list[dict[str, Any]]:
    """Project confirmed legacy text into the existing auditable tool timeline."""

    names = flattened_used_tool_names(str(getattr(tool_entry, "content", "") or ""))
    if not names:
        return []
    result = (
        parse_flattened_tool_result_dump(str(getattr(result_entry, "content", "") or ""))
        if result_entry is not None
        else None
    )
    stable_entry_id = str(
        getattr(result_entry or tool_entry, "message_id", None)
        or getattr(result_entry or tool_entry, "id", None)
        or "history"
    )
    segments: list[dict[str, Any]] = []
    for index, name in enumerate(names):
        is_result_owner = result is not None and index == len(names) - 1
        tool_use_id = f"legacy:{stable_entry_id}:{index + 1}"
        if result is not None and is_result_owner:
            tool_use_id = result.tool_use_id
        segments.append(
            {
                "type": "tool_use",
                "tool_use_id": tool_use_id,
                "name": name,
                "input": {},
                "legacy_projection": True,
            }
        )
        if result is not None and is_result_owner:
            segments.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "name": name,
                    "result": result.content,
                    "legacy_projection": True,
                }
            )
    return segments


def transcript_entries_to_chat_messages(
    entries: list[object],
    *,
    limit: int | None = None,
    previous_entry: object | None = None,
    next_entry: object | None = None,
) -> list[dict[str, Any]]:
    selected = entries[-limit:] if limit is not None else entries
    context_entries = [
        *([previous_entry] if previous_entry is not None else []),
        *selected,
        *([next_entry] if next_entry is not None else []),
    ]
    selected_offset = 1 if previous_entry is not None else 0
    legacy_tool_result_pairs = _legacy_flattened_tool_result_pairs(context_entries)
    legacy_tool_call_pairs = {
        tool_index: result_index
        for result_index, tool_index in legacy_tool_result_pairs.items()
    }
    messages: list[dict[str, Any]] = []
    for entry_index, entry in enumerate(selected):
        context_index = selected_offset + entry_index
        role = getattr(entry, "role", "unknown")
        turn_context = getattr(entry, "turn_context", None)
        silent_reply = sanitize_historical_silent_reply(
            getattr(entry, "content", "") or "",
            getattr(entry, "tool_calls", None),
            role=role,
            turn_context=turn_context if isinstance(turn_context, dict) else None,
        )
        content = silent_reply.content or ""
        legacy_segments: list[dict[str, Any]] = []
        projected_role = role
        paired_tool_index = legacy_tool_result_pairs.get(context_index)
        if paired_tool_index is not None:
            tool_entry = context_entries[paired_tool_index]
            # Structured persisted segments already retain the complete audit
            # trail. Synthesize only for metadata-poor legacy projections.
            if not getattr(tool_entry, "tool_calls", None):
                legacy_segments = _legacy_tool_activity_segments(tool_entry, entry)
                if legacy_segments:
                    projected_role = "assistant"
        elif (
            role == "assistant"
            and has_flattened_used_tool_line(content)
            and context_index not in legacy_tool_call_pairs
            and not silent_reply.segments
        ):
            # Preserve an orphaned legacy call as an expandable activity rather
            # than discarding the only surviving tool identity.
            legacy_segments = _legacy_tool_activity_segments(entry)
        attachments = None
        artifacts = None
        if content and content.startswith("{"):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict) and "text" in parsed:
                    display_text = parsed.get("display_text")
                    content = display_text if isinstance(display_text, str) else parsed["text"]
                    attachments = parsed.get("attachments")
                    parsed_artifacts = parsed.get("artifacts")
                    if isinstance(parsed_artifacts, list):
                        artifacts = [
                            artifact_payload(item)
                            for item in parsed_artifacts
                            if isinstance(item, dict)
                        ]
                        if artifacts:
                            content = strip_artifact_markers_from_text(content)
            except (ValueError, KeyError):
                pass
        if content and content.lstrip().startswith("[ContentBlock"):
            texts = re.findall(
                r"ContentBlockText\(type='text', text='(.*?)'\)",
                content,
            )
            content = "\n".join(t.replace("\\n", "\n") for t in texts) if texts else ""
            if not content.strip():
                continue
        if content:
            cleaned = content
            if role == "assistant" and has_flattened_used_tool_line(cleaned):
                cleaned = strip_flattened_used_tool_lines(cleaned)
            confirmed_tool_result = (
                role == "tool"
                or bool(getattr(entry, "tool_call_id", None))
                or paired_tool_index is not None
            )
            if confirmed_tool_result:
                cleaned = strip_confirmed_flattened_tool_result(cleaned)
            if cleaned != content:
                # The entry carried OpenSquilla's flattened tool serialization.
                # Drop it when nothing but internal tool transcript remains and
                # there is no structured tool timeline to render instead;
                # otherwise keep the narration that surrounded the markers.
                if not cleaned.strip() and not silent_reply.segments and not legacy_segments:
                    continue
                content = cleaned
        if projected_role == "user":
            display_text = display_text_from_preflight_confirmation(content)
            if display_text is not None:
                content = display_text
            elif _is_legacy_generated_plan_implementation(
                content,
                getattr(entry, "turn_context", None),
            ):
                content = ""
        msg: dict[str, Any] = {
            "id": getattr(entry, "message_id", None),
            "message_id": getattr(entry, "message_id", None),
            "role": projected_role,
            "text": content,
            "timestamp": getattr(entry, "created_at", None),
            "provenance_kind": getattr(entry, "provenance_kind", None),
            "provenance_source_session_key": getattr(entry, "provenance_source_session_key", None),
            "provenance_source_tool": getattr(entry, "provenance_source_tool", None),
        }
        transcript_id = getattr(entry, "id", None)
        if transcript_id is not None:
            msg["transcript_id"] = transcript_id
        reasoning = getattr(entry, "reasoning_content", None)
        if isinstance(reasoning, str) and reasoning.strip():
            msg["reasoning_content"] = reasoning
        if isinstance(turn_context, dict):
            if public_context := public_turn_context(turn_context):
                msg["turn_context"] = public_context
        if attachments:
            msg["attachments"] = attachments
        if artifacts:
            msg["artifacts"] = artifacts
        usage = getattr(entry, "turn_usage", None)
        if isinstance(usage, dict):
            msg["usage"] = usage
            model = usage.get("model") or usage.get("routed_model")
            if model:
                msg["model"] = model
            input_tokens = int(usage.get("input_tokens") or usage.get("inputTokens") or 0)
            output_tokens = int(usage.get("output_tokens") or usage.get("outputTokens") or 0)
            msg["input"] = input_tokens
            msg["output"] = output_tokens
            msg["input_tokens"] = input_tokens
            msg["output_tokens"] = output_tokens
            if usage.get("cost_usd") is not None:
                msg["cost_usd"] = float(usage.get("cost_usd") or 0.0)
        tool_calls = [*(silent_reply.segments or []), *legacy_segments]
        if tool_calls:
            msg["tool_calls"] = _sanitize_display_protocol_payload(tool_calls)
        if (
            silent_reply.suppressed
            and not content
            and not artifacts
            and not attachments
            and not tool_calls
        ):
            continue
        messages.append(msg)
    return messages
