"""Fail-closed backfill of one terminal AgentTask Activity snapshot v2."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from opensquilla.contracts.gateway_transport import TURN_COMMITTED_EVENT
from opensquilla.gateway.terminal_activity import (
    build_terminal_activity_snapshot,
    terminal_activity_snapshot,
)

_TERMINAL_EVENTS = frozenset({"session.event.done", "session.event.error"})
_TERMINAL_TASK_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "timeout", "abandoned"}
)


class BackfillError(ValueError):
    """Evidence is incomplete, ambiguous, or inconsistent."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackfillError(f"could not read JSON evidence: {path}") from exc


def _record(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _events_from_export(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        raw_events = value
    elif isinstance(value, Mapping):
        raw_events = value.get("events")
        if not isinstance(raw_events, list):
            replay = value.get("replay")
            raw_events = replay.get("events") if isinstance(replay, Mapping) else None
    else:
        raw_events = None
    if not isinstance(raw_events, list) or not raw_events:
        raise BackfillError("replay export has no events")

    events: list[dict[str, Any]] = []
    previous_seq = 0
    stream_generation: str | None = None
    for raw in raw_events:
        item = _record(raw)
        if item is None:
            raise BackfillError("replay contains a non-object event")
        event_name = item.get("event_name", item.get("event"))
        payload = item.get("payload")
        if not isinstance(event_name, str) or not isinstance(payload, Mapping):
            raise BackfillError(
                "replay events must retain the original event name and payload"
            )
        seq = payload.get("stream_seq", item.get("stream_seq"))
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= previous_seq:
            raise BackfillError("replay stream_seq is not strictly increasing")
        previous_seq = seq
        generation = payload.get("stream_generation", item.get("stream_generation"))
        if isinstance(generation, str) and generation:
            if stream_generation is not None and generation != stream_generation:
                raise BackfillError("replay mixes stream generations")
            stream_generation = generation
        events.append(
            {
                "event_name": event_name,
                "stream_seq": seq,
                "payload": dict(payload),
            }
        )
    return events


def _identity(payload: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _validate_event_evidence(
    events: Sequence[Mapping[str, Any]],
    *,
    task_id: str,
    turn_id: str,
) -> None:
    terminal_seq: int | None = None
    commit_seq: int | None = None
    for event in events:
        event_name = str(event["event_name"])
        payload = event["payload"]
        assert isinstance(payload, Mapping)
        event_task_id = _identity(payload, "task_id", "taskId")
        event_turn_id = _identity(payload, "turn_id", "turnId")
        if event_task_id is not None and event_task_id != task_id:
            raise BackfillError("replay contains an event for another task")
        if event_turn_id is not None and event_turn_id != turn_id:
            raise BackfillError("replay contains an event for another turn")
        if event_name in _TERMINAL_EVENTS:
            terminal_seq = int(event["stream_seq"])
        if event_name == TURN_COMMITTED_EVENT:
            commit_seq = int(event["stream_seq"])
    if terminal_seq is None:
        raise BackfillError("replay has no terminal done/error event")
    if commit_seq is None or commit_seq <= terminal_seq:
        raise BackfillError("replay has no terminal commit after done/error")


def _assistant_from_history(
    value: object,
    *,
    turn_id: str,
    assistant_message_id: str | None,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get("messages"), list):
        raise BackfillError("history export has no messages list")
    matches: list[Mapping[str, Any]] = []
    for raw in value["messages"]:
        message = _record(raw)
        if message is None or message.get("role") != "assistant":
            continue
        context = _record(message.get("turn_context")) or {}
        message_turn_id = context.get("turn_id", message.get("turn_id"))
        message_id = message.get("message_id", message.get("id"))
        if message_turn_id != turn_id:
            continue
        if assistant_message_id is not None and message_id != assistant_message_id:
            continue
        matches.append(message)
    if len(matches) != 1:
        raise BackfillError("history must contain exactly one matching assistant message")
    return matches[0]


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _transcript_segments(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for field in ("tool_calls", "timeline"):
        value = message.get(field)
        if isinstance(value, list) and value:
            records = [item for item in value if isinstance(item, Mapping)]
            if len(records) != len(value):
                raise BackfillError("assistant transcript contains a non-object segment")
            return records
    return []


def _validate_transcript_references(
    snapshot: Mapping[str, Any],
    message: Mapping[str, Any],
) -> None:
    entries = snapshot.get("entries")
    assert isinstance(entries, list)
    reasoning = message.get("reasoning_content")
    if reasoning is None:
        reasoning_record = _record(message.get("reasoning")) or {}
        reasoning = reasoning_record.get("text", "")
    if not isinstance(reasoning, str):
        raise BackfillError("assistant reasoning_content is not text")
    if snapshot.get("reasoning_utf16_length") != _utf16_length(reasoning):
        raise BackfillError("reasoning UTF-16 reference length does not match transcript")

    segments = _transcript_segments(message)
    text_segments = [
        str(segment.get("text", segment.get("raw", "")))
        for segment in segments
        if segment.get("type") == "text"
        and isinstance(segment.get("text", segment.get("raw", "")), str)
        and str(segment.get("text", segment.get("raw", "")))
    ]
    if not text_segments:
        answer = message.get("text")
        if isinstance(answer, str) and answer:
            text_segments = [answer]
    text_entries = sorted(
        (
            entry
            for entry in entries
            if isinstance(entry, Mapping)
            and entry.get("type") == "segment"
            and entry.get("segment_type") == "text"
        ),
        key=lambda entry: int(entry["text_index"]),
    )
    if len(text_entries) != len(text_segments):
        raise BackfillError("text segment count does not match transcript")
    for entry, text in zip(text_entries, text_segments, strict=True):
        if entry.get("text_utf16_length") != _utf16_length(text):
            raise BackfillError("text segment UTF-16 length does not match transcript")

    transcript_tools: dict[str, str] = {}
    for segment in segments:
        segment_type = str(segment.get("type") or "")
        if segment_type and segment_type not in {"tool_use", "tool_result"}:
            continue
        name = segment.get("name", segment.get("tool_name"))
        tool_use_id = segment.get(
            "tool_use_id",
            segment.get("toolUseId", segment.get("id")),
        )
        if name == "router_control":
            continue
        if not isinstance(name, str) or not name or not isinstance(tool_use_id, str):
            raise BackfillError("tool segment is missing stable identity")
        prior = transcript_tools.get(tool_use_id)
        if prior is not None and prior != name:
            raise BackfillError("tool identity maps to conflicting names")
        transcript_tools[tool_use_id] = name
    snapshot_tools = {
        str(entry["tool_use_id"]): str(entry["name"])
        for entry in entries
        if isinstance(entry, Mapping)
        and entry.get("type") == "segment"
        and entry.get("segment_type") == "tool"
    }
    if snapshot_tools != transcript_tools:
        raise BackfillError("tool references do not match transcript")


def build_validated_snapshot(
    replay_export: object,
    history_export: object,
    *,
    task_id: str,
    turn_id: str,
    assistant_message_id: str | None = None,
) -> dict[str, Any]:
    events = _events_from_export(replay_export)
    _validate_event_evidence(events, task_id=task_id, turn_id=turn_id)
    snapshot = build_terminal_activity_snapshot(
        events,
        task_id=task_id,
        turn_id=turn_id,
    )
    snapshot = terminal_activity_snapshot(
        snapshot,
        task_id=task_id,
        turn_id=turn_id,
    )
    if snapshot is None or snapshot.get("complete") is not True:
        raise BackfillError("replay cannot produce a complete Activity snapshot v2")
    message = _assistant_from_history(
        history_export,
        turn_id=turn_id,
        assistant_message_id=assistant_message_id,
    )
    _validate_transcript_references(snapshot, message)
    return snapshot


def _json_details(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BackfillError("target AgentTask details is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise BackfillError("target AgentTask details is not an object")
    return parsed


def _read_task(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    session_key: str,
) -> tuple[str, dict[str, Any]]:
    row = connection.execute(
        "SELECT status, details FROM agent_tasks "
        "WHERE task_id = ? AND session_key = ?",
        (task_id, session_key),
    ).fetchone()
    if row is None:
        raise BackfillError("target AgentTask identity was not found")
    status = str(row[0] or "").lower()
    if status not in _TERMINAL_TASK_STATUSES:
        raise BackfillError("target AgentTask is not terminal")
    return status, _json_details(row[1])


def _snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    payload = json.dumps(
        snapshot,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _backfill(
    *,
    db_path: Path,
    backup_path: Path | None,
    session_key: str,
    task_id: str,
    turn_id: str,
    snapshot: dict[str, Any],
    apply: bool,
    replace: bool,
) -> dict[str, Any]:
    if not db_path.is_file():
        raise BackfillError("database path does not exist")
    if not apply:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            status, details = _read_task(
                connection,
                task_id=task_id,
                session_key=session_key,
            )
        return {
            "applied": False,
            "status": status,
            "entry_count": len(snapshot["entries"]),
            "snapshot_sha256": _snapshot_digest(snapshot),
            "existing_snapshot": isinstance(details.get("activity_snapshot"), Mapping),
        }

    if backup_path is None:
        raise BackfillError("--backup is required with --apply")
    if backup_path.exists():
        raise BackfillError("backup path already exists")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as source, sqlite3.connect(backup_path) as backup:
        source.backup(backup)

    connection = sqlite3.connect(db_path, timeout=5)
    try:
        connection.execute("BEGIN IMMEDIATE")
        status, details = _read_task(
            connection,
            task_id=task_id,
            session_key=session_key,
        )
        existing = details.get("activity_snapshot")
        if isinstance(existing, Mapping) and existing != snapshot and not replace:
            raise BackfillError("target already has a different snapshot; use --replace")
        details["activity_snapshot"] = snapshot
        cursor = connection.execute(
            "UPDATE agent_tasks SET details = ? "
            "WHERE task_id = ? AND session_key = ?",
            (
                json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                task_id,
                session_key,
            ),
        )
        if cursor.rowcount != 1:
            raise BackfillError("target AgentTask changed during migration")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as verify:
        _, details = _read_task(
            verify,
            task_id=task_id,
            session_key=session_key,
        )
    readback = terminal_activity_snapshot(
        details.get("activity_snapshot"),
        task_id=task_id,
        turn_id=turn_id,
    )
    if readback is None or _snapshot_digest(readback) != _snapshot_digest(snapshot):
        raise BackfillError("readback hash does not match the requested snapshot")
    return {
        "applied": True,
        "status": status,
        "entry_count": len(snapshot["entries"]),
        "snapshot_sha256": _snapshot_digest(snapshot),
        "backup": str(backup_path),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--session-key", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--turn-id", required=True)
    parser.add_argument("--assistant-message-id")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--replace", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        snapshot = build_validated_snapshot(
            _read_json(args.replay),
            _read_json(args.history),
            task_id=args.task_id,
            turn_id=args.turn_id,
            assistant_message_id=args.assistant_message_id,
        )
        result = _backfill(
            db_path=args.db,
            backup_path=args.backup,
            session_key=args.session_key,
            task_id=args.task_id,
            turn_id=args.turn_id,
            snapshot=snapshot,
            apply=args.apply,
            replace=args.replace,
        )
    except BackfillError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
