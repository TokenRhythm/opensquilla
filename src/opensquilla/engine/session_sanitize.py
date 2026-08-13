"""Provider-request sanitization for OpenSquilla session history.

This module builds a clean request view from in-memory history. It removes
OpenSquilla/provider bookkeeping that the model does not need while preserving
the user-visible content and persisted transcript state.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from opensquilla.provider import (
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
)
from opensquilla.provider.types import ContentBlockDocument, ContentBlockImage

_BLOCK_FIELDS: dict[str, set[str]] = {
    "text": {"type", "text"},
    "tool_use": {"type", "id", "name", "input"},
    "tool_result": {"type", "tool_use_id", "content", "is_error", "execution_status"},
    "image": {"type", "source_type", "media_type", "data"},
    "document": {"type", "source_type", "media_type", "data", "title"},
    "thinking": {"type", "thinking", "signature"},
}

_BLOCK_MODELS: dict[str, type[BaseModel]] = {
    "text": ContentBlockText,
    "tool_use": ContentBlockToolUse,
    "tool_result": ContentBlockToolResult,
    "image": ContentBlockImage,
    "document": ContentBlockDocument,
    "thinking": ContentBlockThinking,
}

_HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX = "[historical_tool_argument_omitted]\n"
_HISTORICAL_TOOL_ARGUMENT_FIELD_MAX_CHARS = 2048
_HISTORICAL_TOOL_ARGUMENT_TOTAL_MAX_CHARS = 4096
_HISTORICAL_TOOL_RESULT_MAX_CHARS = 4096
_HISTORICAL_TOOL_RESULT_PREVIEW_CHARS = 480
_TOOL_RESULT_PROJECTION_PREFIX = "[tool_result_projection]\n"
_AGGREGATE_TOOL_RESULT_PROJECTION_PREFIX = "[aggregate_tool_result_compacted]\n"
_HISTORICAL_TOOL_RESULT_COMPACTED_PREFIX = "[historical_tool_result_compacted]\n"
_TOOL_RESULT_PROJECTION_HEADER_PREFIXES = (
    "tool_result_handle:",
    "sha256:",
    "original_chars:",
    "preview_complete:",
    "retrieve_hint:",
    "search_hints:",
    "signal_scan:",
    "signal_next_call:",
)
_TOOL_RESULT_HANDLE_RE = re.compile(r"^tr-[0-9a-f]{32}$")
_TOOL_RESULT_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class HistoricalReplayProjectionResult:
    """Metrics for compacting raw persisted tool payloads out of model replay."""

    messages_in: int
    messages_out: int
    payload_chars_before: int
    payload_chars_after: int
    tool_uses_projected: int = 0
    tool_results_projected: int = 0
    reasoning_chars_removed: int = 0

    @property
    def changed(self) -> bool:
        return (
            self.tool_uses_projected > 0
            or self.tool_results_projected > 0
            or self.reasoning_chars_removed > 0
            or self.messages_in != self.messages_out
            or self.payload_chars_before != self.payload_chars_after
        )


@dataclass(frozen=True)
class SessionSanitizeResult:
    """Metrics for one sanitized request-view build."""

    messages_in: int
    messages_out: int
    payload_chars_before: int
    payload_chars_after: int
    metadata_keys_removed: int = 0

    @property
    def changed(self) -> bool:
        return (
            self.metadata_keys_removed > 0
            or self.messages_in != self.messages_out
            or self.payload_chars_before != self.payload_chars_after
        )


def session_payload_chars(messages: list[Message]) -> int:
    """Return a stable JSON character estimate for a provider message list."""

    return len(json.dumps(_to_jsonable(messages), ensure_ascii=False, sort_keys=True))


def project_historical_tool_payloads(
    messages: list[Message],
    *,
    preserve_reasoning_content: bool = False,
    recoverable_references: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[list[Message], HistoricalReplayProjectionResult]:
    """Return a compact provider-view for persisted historical tool payloads.

    Raw transcript rows keep full tool inputs/results for audit. This projection
    is only for model replay at the start of a later turn, where large stale
    write/code/tool-result payloads otherwise dominate the next provider request.
    """

    payload_chars_before = session_payload_chars(messages)
    projected: list[Message] = []
    tool_uses_projected = 0
    tool_results_projected = 0
    reasoning_chars_removed = 0
    touched = False

    for message in messages:
        content = message.content
        next_content = content
        content_changed = False
        if isinstance(content, list):
            next_blocks: list[Any] = []
            for block in content:
                if isinstance(block, ContentBlockToolUse):
                    next_tool_use, changed = _project_historical_tool_use(block)
                    if changed:
                        tool_uses_projected += 1
                        content_changed = True
                    next_blocks.append(next_tool_use)
                    continue
                if isinstance(block, ContentBlockToolResult):
                    next_tool_result, changed = _project_historical_tool_result(
                        block,
                        recoverable_references=recoverable_references,
                    )
                    if changed:
                        tool_results_projected += 1
                        content_changed = True
                    next_blocks.append(next_tool_result)
                    continue
                next_blocks.append(block)
            if content_changed:
                next_content = next_blocks

        next_reasoning = message.reasoning_content
        if (
            message.role == "assistant"
            and message.reasoning_content
            and not preserve_reasoning_content
        ):
            reasoning_chars_removed += len(message.reasoning_content)
            next_reasoning = None

        if content_changed or next_reasoning != message.reasoning_content:
            touched = True
            projected.append(
                Message(
                    role=message.role,
                    content=next_content,
                    reasoning_content=next_reasoning,
                )
            )
        else:
            projected.append(message)

    if not touched:
        projected = messages

    payload_chars_after = session_payload_chars(projected)
    return projected, HistoricalReplayProjectionResult(
        messages_in=len(messages),
        messages_out=len(projected),
        payload_chars_before=payload_chars_before,
        payload_chars_after=payload_chars_after,
        tool_uses_projected=tool_uses_projected,
        tool_results_projected=tool_results_projected,
        reasoning_chars_removed=reasoning_chars_removed,
    )


def sanitize_session_messages(
    messages: list[Message],
) -> tuple[list[Message], SessionSanitizeResult]:
    """Return a provider-safe request view without mutating stored history.

    The sanitizer only removes metadata from message/block envelopes. Tool
    result projection is handled separately by the agent request pipeline.
    """

    payload_chars_before = session_payload_chars(messages)
    sanitized: list[Message] = []
    metadata_keys_removed = 0
    touched = False

    for message in messages:
        content, removed, content_changed = _sanitize_content(message.content)
        metadata_keys_removed += removed
        if content_changed:
            touched = True
            sanitized.append(
                Message(
                    role=message.role,
                    content=content,
                    reasoning_content=message.reasoning_content,
                )
            )
        else:
            sanitized.append(message)

    if not touched:
        sanitized = messages

    payload_chars_after = session_payload_chars(sanitized)
    return sanitized, SessionSanitizeResult(
        messages_in=len(messages),
        messages_out=len(sanitized),
        payload_chars_before=payload_chars_before,
        payload_chars_after=payload_chars_after,
        metadata_keys_removed=metadata_keys_removed,
    )


def _project_historical_tool_use(
    block: ContentBlockToolUse,
) -> tuple[ContentBlockToolUse, bool]:
    input_chars = _json_chars(block.input)
    if input_chars <= _HISTORICAL_TOOL_ARGUMENT_TOTAL_MAX_CHARS and not any(
        isinstance(value, str) and len(value) > _HISTORICAL_TOOL_ARGUMENT_FIELD_MAX_CHARS
        for value in block.input.values()
    ):
        return block, False

    projected_input: dict[str, Any] = {}
    changed = False
    for key, value in block.input.items():
        value_text = value if isinstance(value, str) else _json_text(value)
        if len(value_text) > _HISTORICAL_TOOL_ARGUMENT_FIELD_MAX_CHARS:
            projected_input[key] = _tool_argument_projection(
                tool_name=block.name,
                tool_use_id=block.id,
                field=key,
                value=value_text,
                original_input_chars=input_chars,
            )
            changed = True
        else:
            projected_input[key] = value

    if not changed:
        projected_input["_opensquilla_compacted_tool_arguments"] = True
        projected_input["tool"] = block.name
        projected_input["reason"] = "historical_tool_arguments_omitted"
        projected_input["original_chars"] = input_chars
        projected_input["argument_keys"] = sorted(str(key) for key in block.input)
        changed = True

    return (
        ContentBlockToolUse(id=block.id, name=block.name, input=projected_input),
        changed,
    )


def _project_historical_tool_result(
    block: ContentBlockToolResult,
    *,
    recoverable_references: frozenset[tuple[str, str]],
) -> tuple[ContentBlockToolResult, bool]:
    content = block.content if isinstance(block.content, str) else _json_text(block.content)
    if len(content) <= _HISTORICAL_TOOL_RESULT_MAX_CHARS:
        return block, False
    reference = recoverable_tool_result_reference(content)
    if reference is None or reference not in recoverable_references:
        # A head/tail-only historical projection is irreversible for the
        # model. Keep visible evidence unless the caller has verified this
        # exact handle+digest in the current Store/session/agent scope.
        return block, False
    handle, raw_sha256 = reference
    if content.startswith(
        (_TOOL_RESULT_PROJECTION_PREFIX, _AGGREGATE_TOOL_RESULT_PROJECTION_PREFIX)
    ):
        compacted = _compact_historical_tool_result_projection(
            content,
            tool_use_id=block.tool_use_id,
        )
        return (
            ContentBlockToolResult(
                tool_use_id=block.tool_use_id,
                content=compacted,
                is_error=block.is_error,
                execution_status=block.execution_status,
            ),
            True,
        )
    compacted = _compact_historical_text(
        content,
        label="historical_tool_result",
        metadata={
            "retrieve_hint": (
                "Use retrieve_tool_result with this handle to inspect the original raw output."
            ),
            "tool_result_handle": handle,
            "tool_result_sha256": raw_sha256,
            "tool_use_id": block.tool_use_id,
        },
    )
    return (
        ContentBlockToolResult(
            tool_use_id=block.tool_use_id,
            content=compacted,
            is_error=block.is_error,
            execution_status=block.execution_status,
        ),
        True,
    )


def recoverable_tool_result_reference(content: str) -> tuple[str, str] | None:
    """Parse a strict Store reference from a known projection envelope.

    Parsing alone never proves recoverability.  Callers must also validate the
    returned handle and raw-content digest against the active Store scope.
    """

    if content.startswith(
        (_TOOL_RESULT_PROJECTION_PREFIX, _AGGREGATE_TOOL_RESULT_PROJECTION_PREFIX)
    ):
        fields: dict[str, str] = {}
        for line in content.splitlines()[1:]:
            key, separator, value = line.partition(":")
            if separator:
                fields.setdefault(key.strip(), value.strip())
        handle = fields.get("tool_result_handle", "")
        sha256 = fields.get("sha256", "")
        if not _TOOL_RESULT_HANDLE_RE.fullmatch(handle):
            return None
        if not _TOOL_RESULT_SHA256_RE.fullmatch(sha256):
            return None
        return handle, sha256
    if content.startswith(_HISTORICAL_TOOL_RESULT_COMPACTED_PREFIX):
        fields = {}
        for line in content.splitlines()[1:]:
            key, separator, value = line.partition(":")
            if separator:
                fields.setdefault(key.strip(), value.strip())
        handle = fields.get("tool_result_handle", "")
        sha256 = fields.get("tool_result_sha256", "")
        if not _TOOL_RESULT_HANDLE_RE.fullmatch(handle):
            return None
        if not _TOOL_RESULT_SHA256_RE.fullmatch(sha256):
            return None
        return handle, sha256

    # Structured truncation envelopes are always JSON objects emitted with
    # these literal keys.  Reject unrelated historical JSON before invoking
    # the recursive decoder: arbitrary tool output can be deeply nested even
    # though it cannot possibly contain a recoverable Store reference.
    object_start = 0
    object_end = len(content)
    while object_start < object_end and content[object_start] in " \t\r\n":
        object_start += 1
    while object_end > object_start and content[object_end - 1] in " \t\r\n":
        object_end -= 1
    if (
        object_start == object_end
        or content[object_start] != "{"
        or content[object_end - 1] != "}"
    ):
        return None
    if any(
        marker not in content
        for marker in (
            '"result_truncated"',
            '"retrieve_hint"',
            '"tool_result_handle"',
            '"tool_result_sha256"',
        )
    ):
        return None
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, RecursionError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("result_truncated") is not True or not payload.get("retrieve_hint"):
        return None
    payload_handle = payload.get("tool_result_handle")
    payload_sha256 = payload.get("tool_result_sha256")
    if not isinstance(payload_handle, str) or not isinstance(payload_sha256, str):
        return None
    handle = payload_handle.strip()
    sha256 = payload_sha256.strip()
    if not _TOOL_RESULT_HANDLE_RE.fullmatch(handle):
        return None
    if not _TOOL_RESULT_SHA256_RE.fullmatch(sha256):
        return None
    return handle, sha256


def _compact_historical_tool_result_projection(text: str, *, tool_use_id: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    lines = text.splitlines()
    preserved: list[str] = [text.splitlines()[0]]
    for line in lines[1:]:
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in _TOOL_RESULT_PROJECTION_HEADER_PREFIXES):
            preserved.append(line)
            continue
        if stripped.startswith("- L"):
            preserved.append(line)
            continue
    compacted_body = _compact_historical_text(
        text,
        label="historical_tool_result_projection_body",
        metadata={"tool_use_id": tool_use_id},
    )
    return "\n".join(
        [
            *preserved,
            (
                "historical_replay_projection: recoverability header preserved; "
                "projected body compacted."
            ),
            f"historical_replay_sha256: {digest}",
            compacted_body,
        ]
    )


def _tool_argument_projection(
    *,
    tool_name: str,
    tool_use_id: str,
    field: str,
    value: str,
    original_input_chars: int,
) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    head, tail, omitted = _head_tail(value, _HISTORICAL_TOOL_RESULT_PREVIEW_CHARS)
    lines = [
        _HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX.rstrip("\n"),
        f"tool: {tool_name}",
        f"tool_use_id: {tool_use_id}",
        f"field: {field}",
        f"original_chars: {len(value)}",
        f"original_input_chars: {original_input_chars}",
        f"sha256: {digest}",
        f"omitted_chars: {omitted}",
        "reason: historical tool argument omitted from later-turn provider replay.",
        "head:",
        head,
    ]
    if tail and tail != head:
        lines.extend(["...", "tail:", tail])
    return "\n".join(lines)


def _compact_historical_text(
    text: str,
    *,
    label: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    head, tail, omitted = _head_tail(text, _HISTORICAL_TOOL_RESULT_PREVIEW_CHARS)
    lines = [
        f"[{label}_compacted]",
        f"original_chars: {len(text)}",
        f"sha256: {digest}",
        f"omitted_chars: {omitted}",
        "reason: historical payload omitted from later-turn provider replay.",
    ]
    for key, value in sorted((metadata or {}).items()):
        lines.append(f"{key}: {value}")
    lines.extend(["head:", head])
    if tail and tail != head:
        lines.extend(["...", "tail:", tail])
    return "\n".join(lines)


def _head_tail(text: str, budget: int) -> tuple[str, str, int]:
    if len(text) <= budget:
        return text, "", 0
    head_chars = max(1, budget // 2)
    tail_chars = max(1, budget - head_chars)
    omitted = max(0, len(text) - head_chars - tail_chars)
    return text[:head_chars], text[-tail_chars:], omitted


def _json_chars(value: Any) -> int:
    return len(_json_text(value))


def _json_text(value: Any) -> str:
    return json.dumps(_to_jsonable(value), ensure_ascii=False, sort_keys=True)


def _sanitize_content(content: Any) -> tuple[Any, int, bool]:
    if not isinstance(content, list):
        return content, 0, False

    sanitized_blocks: list[Any] = []
    removed_total = 0
    touched = False
    for block in content:
        sanitized_block, removed, changed = _sanitize_block(block)
        removed_total += removed
        touched = touched or changed
        sanitized_blocks.append(sanitized_block)

    if not touched:
        return content, removed_total, False
    return sanitized_blocks, removed_total, True


def _sanitize_block(block: Any) -> tuple[Any, int, bool]:
    if isinstance(block, dict):
        return _sanitize_block_dict(block)

    model_dump = getattr(block, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(mode="python")
        sanitized, removed, changed = _sanitize_block_dict(payload)
        if not changed:
            return block, 0, False
        return sanitized, removed, True

    return block, 0, False


def _sanitize_block_dict(block: dict[str, Any]) -> tuple[Any, int, bool]:
    block_type = block.get("type")
    if not isinstance(block_type, str):
        return block, 0, False

    allowed = _BLOCK_FIELDS.get(block_type)
    if allowed is None:
        return block, 0, False

    cleaned = {key: block[key] for key in allowed if key in block}
    removed = len([key for key in block if key not in allowed])
    model_cls = _BLOCK_MODELS.get(block_type)
    if model_cls is None:
        return cleaned, removed, removed > 0

    try:
        return model_cls(**cleaned), removed, removed > 0
    except Exception:
        return cleaned, removed, removed > 0


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Message):
        payload: dict[str, Any] = {
            "role": value.role,
            "content": _to_jsonable(value.content),
        }
        if value.reasoning_content is not None:
            payload["reasoning_content"] = value.reasoning_content
        return payload
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, Path):
        return str(value)
    try:
        json.dumps(value)
    except TypeError:
        return repr(value)
    return value
