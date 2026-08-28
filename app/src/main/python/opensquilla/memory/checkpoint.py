from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from time import time
from typing import Any, Literal

CheckpointRole = Literal[
    "user",
    "assistant",
    "tool_call",
    "tool_result",
    "system_notice",
    "error",
]
CheckpointContentType = Literal["text", "json", "binary_ref", "redacted"]
CheckpointStatus = Literal["ok", "error", "truncated", "redacted"]


@dataclass(frozen=True)
class CheckpointEvent:
    schema_version: int
    event_id: str
    session_key: str
    session_id: str
    turn_id: str
    sequence: int
    timestamp_ms: int
    role: CheckpointRole
    content_type: CheckpointContentType
    content: str
    summary: str | None
    tool_name: str | None
    tool_call_id: str | None
    status: CheckpointStatus
    token_estimate: int
    source: str
    attachments: list[dict]
    content_hash: str

    def to_json_dict(self) -> dict:
        payload = asdict(self)
        if not payload["content_hash"]:
            payload["content_hash"] = checkpoint_event_hash(self.content)
        return payload


@dataclass(frozen=True)
class CheckpointWriteResult:
    relative_path: str
    event_count: int
    content_hash: str


def checkpoint_event_hash(content: str) -> str:
    normalized = str(content or "").strip().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _json_ready(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    return value


def _serialize_checkpoint_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(_json_ready(value), ensure_ascii=False, sort_keys=True, default=str)


def _entry_checkpoint_content(entry: Any) -> str:
    content = getattr(entry, "content", None)
    reasoning_content = getattr(entry, "reasoning_content", None)
    if content is not None and reasoning_content:
        return _serialize_checkpoint_content(
            {
                "content": _json_ready(content),
                "reasoning_content": reasoning_content,
            }
        )
    if content is not None:
        return _serialize_checkpoint_content(content)
    return _serialize_checkpoint_content(reasoning_content)


def checkpoint_turn_id(entries: list[Any]) -> str:
    entry_ids = [
        int(entry_id)
        for entry in entries
        if (entry_id := getattr(entry, "id", None)) is not None
    ]
    if entry_ids:
        return f"through-{max(entry_ids)}"
    seed = json.dumps(
        [
            {
                "role": getattr(entry, "role", ""),
                "content": getattr(entry, "content", "") or "",
                "tool_call_id": getattr(entry, "tool_call_id", None),
                "tool_calls": getattr(entry, "tool_calls", None),
                "reasoning_content": getattr(entry, "reasoning_content", None),
            }
            for entry in entries
        ],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return f"turn-{checkpoint_event_hash(seed)[:16]}"


def checkpoint_coverage_hash(entries: list[Any]) -> str:
    seed = json.dumps(
        [
            {
                "id": getattr(entry, "id", None),
                "role": getattr(entry, "role", ""),
                "content": getattr(entry, "content", "") or "",
                "tool_call_id": getattr(entry, "tool_call_id", None),
                "tool_calls": getattr(entry, "tool_calls", None),
                "reasoning_content": getattr(entry, "reasoning_content", None),
            }
            for entry in entries
        ],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return checkpoint_event_hash(seed)


def _content_type_for(content: str) -> CheckpointContentType:
    stripped = str(content or "").strip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            return "text"
        return "json"
    return "text"


def _checkpoint_role_for(role: str) -> CheckpointRole:
    if role == "tool":
        return "tool_result"
    if role in {"user", "assistant"}:
        return role  # type: ignore[return-value]
    if role == "system":
        return "system_notice"
    return "system_notice"


def _tool_call_name(tool_call: dict[str, Any]) -> str | None:
    function = tool_call.get("function")
    if isinstance(function, dict):
        return function.get("name")
    return tool_call.get("name")


def _tool_call_content(tool_call: dict[str, Any]) -> str:
    function = tool_call.get("function")
    if isinstance(function, dict) and function.get("arguments") is not None:
        return str(function.get("arguments") or "")
    return json.dumps(tool_call, ensure_ascii=False, sort_keys=True, default=str)


def build_checkpoint_events(
    *,
    session_key: str,
    session_id: str,
    entries: list[Any],
    source: str,
    turn_id: str | None = None,
) -> list[CheckpointEvent]:
    if not entries:
        raise ValueError("checkpoint entries cannot be empty")
    resolved_turn_id = turn_id or checkpoint_turn_id(entries)
    events: list[CheckpointEvent] = []
    timestamp_ms = int(time() * 1000)
    for entry in entries:
        sequence = len(events) + 1
        content = _entry_checkpoint_content(entry)
        event_seed = (
            f"{session_key}:{resolved_turn_id}:{sequence}:"
            f"{getattr(entry, 'role', '')}:{checkpoint_event_hash(content)}"
        )
        events.append(
            CheckpointEvent(
                schema_version=1,
                event_id=f"checkpoint-{checkpoint_event_hash(event_seed)[:16]}",
                session_key=session_key,
                session_id=session_id,
                turn_id=resolved_turn_id,
                sequence=sequence,
                timestamp_ms=int(getattr(entry, "created_at", None) or timestamp_ms),
                role=_checkpoint_role_for(str(getattr(entry, "role", ""))),
                content_type=_content_type_for(content),
                content=content,
                summary=None,
                tool_name=None,
                tool_call_id=getattr(entry, "tool_call_id", None),
                status="ok",
                token_estimate=int(getattr(entry, "token_count", None) or 0),
                source=source,
                attachments=[],
                content_hash="",
            )
        )
        tool_calls = getattr(entry, "tool_calls", None) or []
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                sequence = len(events) + 1
                tool_content = _tool_call_content(tool_call)
                tool_call_id = tool_call.get("id")
                event_seed = (
                    f"{session_key}:{resolved_turn_id}:{sequence}:"
                    f"tool_call:{checkpoint_event_hash(tool_content)}"
                )
                events.append(
                    CheckpointEvent(
                        schema_version=1,
                        event_id=f"checkpoint-{checkpoint_event_hash(event_seed)[:16]}",
                        session_key=session_key,
                        session_id=session_id,
                        turn_id=resolved_turn_id,
                        sequence=sequence,
                        timestamp_ms=int(
                            getattr(entry, "created_at", None) or timestamp_ms
                        ),
                        role="tool_call",
                        content_type=_content_type_for(tool_content),
                        content=tool_content,
                        summary=None,
                        tool_name=_tool_call_name(tool_call),
                        tool_call_id=str(tool_call_id) if tool_call_id else None,
                        status="ok",
                        token_estimate=0,
                        source=source,
                        attachments=[],
                        content_hash="",
                    )
                )
    return events


def _safe_path_component(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    if safe in {"", ".", ".."}:
        return "unknown"
    return safe


def checkpoint_relative_path(*, session_key: str, turn_id: str) -> Path:
    return (
        Path("memory")
        / ".checkpoints"
        / _safe_path_component(session_key)
        / f"{_safe_path_component(turn_id)}.jsonl"
    )


def serialize_checkpoint_event(event: CheckpointEvent) -> str:
    return json.dumps(event.to_json_dict(), ensure_ascii=False, sort_keys=True)


def append_checkpoint_events(
    workspace: Path,
    events: list[CheckpointEvent],
) -> CheckpointWriteResult:
    """Write one complete turn checkpoint JSONL snapshot.

    Rewriting the same serialized body is idempotent: if the target already has
    the same body hash, this returns the existing result without duplicating
    lines.
    """
    if not events:
        raise ValueError("checkpoint events cannot be empty")

    first_event = events[0]
    if any(
        event.session_key != first_event.session_key
        or event.turn_id != first_event.turn_id
        for event in events
    ):
        raise ValueError("checkpoint events must share session_key and turn_id")

    relative_path = checkpoint_relative_path(
        session_key=first_event.session_key,
        turn_id=first_event.turn_id,
    )
    body = "".join(f"{serialize_checkpoint_event(event)}\n" for event in events)
    body_bytes = body.encode("utf-8")
    content_hash = hashlib.sha256(body_bytes).hexdigest()
    result = CheckpointWriteResult(
        relative_path=relative_path.as_posix(),
        event_count=len(events),
        content_hash=content_hash,
    )

    target_path = workspace / relative_path
    if (
        target_path.exists()
        and hashlib.sha256(target_path.read_bytes()).hexdigest() == content_hash
    ):
        return result

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False,
            dir=target_path.parent,
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            mode="w",
            encoding="utf-8",
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(body)
        os.replace(temp_path, target_path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    return result
