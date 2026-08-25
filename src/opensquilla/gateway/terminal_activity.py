"""Bounded, non-sensitive terminal activity snapshots.

Version 1 contains only the small provider-phase proof used by retryable usage
barriers. Version 2 is a compact presentation trace for every terminal turn.
It stores ordering and references only: arbitrary labels, tool arguments,
results, and reasoning text never enter the task details row.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

USAGE_ACCOUNTING_BARRIER_CODES = frozenset(
    {
        "usage_accounting_busy",
        "usage_accounting_unavailable",
    }
)

_ACTIVITY_PHASES = {
    "router": frozenset({"decided"}),
    "state": frozenset({"thinking", "streaming", "tool_calling"}),
    "provider": frozenset(
        {"requesting", "reasoning", "retry_wait", "retrying", "fallback"}
    ),
}
_MAX_ACTIVITY_PHASES = 32
_MAX_ACTIVITY_ENTRIES = 2048
_MAX_RETRY_AFTER_MS = 900_000
_MAX_TIMESTAMP_MS = 10_000_000_000_000
_SAFE_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ACTIVITY_ENTRY_TYPES = frozenset(
    {"phase", "reasoning", "segment", "interrupt", "maintenance"}
)
_MAINTENANCE_STATES = frozenset(
    {"running", "completed", "skipped", "stale", "cancelled", "failed"}
)
_MAINTENANCE_REASONS = frozenset({"within_budget", "within_compaction_budget"})


def is_usage_accounting_barrier(code: object) -> bool:
    return isinstance(code, str) and code.strip().lower() in USAGE_ACCOUNTING_BARRIER_CODES


def safe_retry_after_ms(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, str | int | float):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed <= 0:
        return None
    return min(parsed, _MAX_RETRY_AFTER_MS)


def safe_primary_user_message_id(value: object) -> str | None:
    """Return one non-empty authoritative transcript message id."""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def usage_barrier_replay_proof(
    *,
    usage_call_index: object,
    no_prior_provider_dispatch: object,
    replay_safe: object,
) -> dict[str, Any]:
    """Normalize the closed proof required before offering whole-turn replay."""

    call_index = (
        usage_call_index
        if isinstance(usage_call_index, int)
        and not isinstance(usage_call_index, bool)
        and usage_call_index > 0
        else None
    )
    no_prior = call_index == 1 and no_prior_provider_dispatch is True
    return {
        **({"usage_call_index": call_index} if call_index is not None else {}),
        "no_prior_provider_dispatch": no_prior,
        "replay_safe": no_prior and replay_safe is True,
    }


def append_activity_phase(
    phases: list[dict[str, Any]],
    *,
    event_kind: object,
    payload: Mapping[str, Any],
    observed_at_ms: int,
) -> None:
    """Append one allowlisted phase without copying arbitrary event fields."""

    kind = str(event_kind or "").strip().lower()
    phase: str
    if kind == "router_decision":
        snapshot_kind = "router"
        phase = "decided"
    elif kind == "state_change":
        snapshot_kind = "state"
        phase = str(payload.get("to_state") or "").strip().lower()
    elif kind == "provider_activity":
        snapshot_kind = "provider"
        phase = str(payload.get("phase") or "").strip().lower()
    else:
        return
    if phase not in _ACTIVITY_PHASES[snapshot_kind]:
        return

    raw_at = payload.get("started_at")
    try:
        event_at = int(raw_at) if raw_at is not None and not isinstance(raw_at, bool) else 0
    except (TypeError, ValueError, OverflowError):
        event_at = 0
    at = event_at if event_at > 0 else max(0, int(observed_at_ms))
    entry = {"kind": snapshot_kind, "phase": phase, "at": at}
    if phases and phases[-1] == entry:
        return
    if len(phases) >= _MAX_ACTIVITY_PHASES:
        phases.pop(0)
    phases.append(entry)


def _safe_positive_int(value: object, *, maximum: int = _MAX_TIMESTAMP_MS) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed <= 0 or parsed > maximum:
        return None
    return parsed


def _safe_nonnegative_int(value: object, *, maximum: int = _MAX_TIMESTAMP_MS) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed < 0 or parsed > maximum:
        return None
    return parsed


def _safe_text(value: object, *, maximum: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        return None
    return normalized


def _event_parts(value: object) -> tuple[str, Mapping[str, Any], int] | None:
    if isinstance(value, Mapping):
        event_name = value.get("event_name", value.get("event"))
        payload = value.get("payload", value)
        stream_seq = value.get("stream_seq")
    else:
        event_name = getattr(value, "event_name", None)
        payload = getattr(value, "payload", None)
        stream_seq = getattr(value, "stream_seq", None)
    if not isinstance(event_name, str) or not isinstance(payload, Mapping):
        return None
    order = _safe_positive_int(payload.get("stream_seq", stream_seq))
    if order is None:
        return None
    return event_name, payload, order


def _event_timestamp(payload: Mapping[str, Any], *preferred: str) -> int:
    for field in (*preferred, "emitted_at", "emittedAt"):
        parsed = _safe_positive_int(payload.get(field))
        if parsed is not None:
            return parsed
    return 1


def _event_at(payload: Mapping[str, Any]) -> int:
    return _event_timestamp(payload, "started_at", "startedAt")


def _event_end_at(payload: Mapping[str, Any]) -> int:
    return _event_timestamp(payload, "ended_at", "endedAt")


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _entry_checksum(entries: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        entries,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_record(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("{") or len(text) > 2_000_000:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def build_terminal_activity_snapshot(
    events: object,
    *,
    task_id: str,
    turn_id: str,
    terminal_at: int | None = None,
) -> dict[str, Any] | None:
    """Build one compact v2 snapshot from stream-registry events."""

    if not isinstance(events, list | tuple):
        return None
    ordered_events: list[tuple[str, Mapping[str, Any], int]] = []
    for raw in events:
        parts = _event_parts(raw)
        if parts is not None:
            ordered_events.append(parts)
    ordered_events.sort(key=lambda item: item[2])
    if not ordered_events:
        return None

    entries: list[dict[str, Any]] = []
    complete = True
    text_index = 0
    write_round = 0
    reasoning: dict[str, dict[str, Any]] = {}
    reasoning_order: list[str] = []
    tools: dict[str, dict[str, Any]] = {}
    maintenance: dict[str, int] = {}
    interrupts: dict[str, dict[str, Any]] = {}
    terminal_boundary = _safe_positive_int(terminal_at)

    def append(entry: dict[str, Any]) -> None:
        nonlocal complete
        if len(entries) >= _MAX_ACTIVITY_ENTRIES:
            complete = False
            return
        entries.append(entry)

    for event_name, payload, order in ordered_events:
        suffix = event_name.removeprefix("session.event.")
        at = _event_at(payload)

        if suffix in {"done", "error"}:
            terminal_boundary = _event_end_at(payload)
            continue

        if suffix == "answer_generation_reset":
            reasoning_snapshot = payload.get(
                "authoritative_reasoning_snapshot",
                payload.get("authoritativeReasoningSnapshot"),
            )
            if isinstance(reasoning_snapshot, str) and reasoning_snapshot:
                block_id = f"reset-reasoning:{order}"
                reasoning[block_id] = {
                    "type": "reasoning",
                    "id": block_id,
                    "order": order,
                    "block_index": len(reasoning_order),
                    "started_at": at,
                    "ended_at": at,
                    "status": "completed",
                    "content_kind": "reasoning",
                    "_text_utf16_length": _utf16_length(reasoning_snapshot),
                }
                reasoning_order.append(block_id)
            authoritative_text = payload.get(
                "authoritative_text_snapshot",
                payload.get("authoritativeTextSnapshot"),
            )
            # A following text_delta normally repeats the authoritative snapshot
            # and the live reducer de-duplicates it. Without that delta there is
            # no transcript-segment boundary to reference safely.
            if isinstance(authoritative_text, str) and authoritative_text:
                complete = False
            continue

        if suffix == "router_decision":
            append(
                {
                    "type": "phase",
                    "id": f"router:decided:{order}",
                    "order": order,
                    "kind": "router",
                    "phase": "decided",
                    "at": at,
                }
            )
            continue

        if suffix == "state_change":
            phase = str(payload.get("to_state", payload.get("toState")) or "").lower()
            if phase == "tool_use":
                phase = "tool_calling"
            if phase in _ACTIVITY_PHASES["state"]:
                append(
                    {
                        "type": "phase",
                        "id": f"state:{phase}:{order}",
                        "order": order,
                        "kind": "state",
                        "phase": phase,
                        "at": at,
                    }
                )
            continue

        if suffix == "provider_activity":
            phase = str(payload.get("phase") or "").lower()
            if phase not in _ACTIVITY_PHASES["provider"]:
                continue
            entry: dict[str, Any] = {
                "type": "phase",
                "id": f"provider:{phase}:{order}",
                "order": order,
                "kind": "provider",
                "phase": phase,
                "at": at,
            }
            reason = _safe_text(payload.get("reason"), maximum=64)
            if reason == "rate_limited":
                entry["reason"] = reason
            retry_after = _safe_nonnegative_int(
                payload.get("retry_after_ms", payload.get("retryAfterMs")),
                maximum=_MAX_RETRY_AFTER_MS,
            )
            if retry_after is not None:
                entry["retry_after_ms"] = retry_after
            for source, target in (
                ("retry_attempt", "retry_attempt"),
                ("retry_limit", "retry_limit"),
            ):
                value = _safe_nonnegative_int(payload.get(source), maximum=10_000)
                if value is not None:
                    entry[target] = value
            append(entry)
            continue

        if suffix in {"thinking_start", "thinking", "thinking_end"}:
            block_id = _safe_text(
                payload.get("block_id", payload.get("blockId")), maximum=160
            ) or "legacy-reasoning"
            block = reasoning.get(block_id)
            if block is None:
                raw_index = _safe_nonnegative_int(
                    payload.get("block_index", payload.get("blockIndex")),
                    maximum=100_000,
                )
                block = {
                    "type": "reasoning",
                    "id": block_id,
                    "order": order,
                    "block_index": raw_index if raw_index is not None else len(reasoning_order),
                    "started_at": at,
                    "ended_at": at,
                    "status": "streaming",
                    "content_kind": (
                        "summary"
                        if payload.get("content_kind", payload.get("contentKind"))
                        == "summary"
                        else "reasoning"
                    ),
                    "_text_utf16_length": 0,
                }
                reasoning[block_id] = block
                reasoning_order.append(block_id)
            if suffix == "thinking":
                text = payload.get("text")
                if isinstance(text, str):
                    block["_text_utf16_length"] = int(
                        block["_text_utf16_length"]
                    ) + _utf16_length(text)
            if suffix == "thinking_end":
                status = str(payload.get("status") or "completed").lower()
                block["status"] = (
                    status
                    if status in {"completed", "interrupted", "error"}
                    else "completed"
                )
                block["ended_at"] = _event_end_at(payload)
            continue

        if suffix == "text_delta":
            text = payload.get("text")
            if not isinstance(text, str) or not text:
                continue
            write_round += 1
            append(
                {
                    "type": "phase",
                    "id": f"write:{write_round}:{order}",
                    "order": order,
                    "kind": "write",
                    "phase": "writing",
                    "round": write_round,
                    "at": at,
                }
            )
            append(
                {
                    "type": "segment",
                    "id": f"text:{text_index}",
                    "order": order,
                    "segment_type": "text",
                    "text_index": text_index,
                    "text_utf16_length": _utf16_length(text),
                    "at": at,
                    "ended_at": max(at, _event_end_at(payload)),
                }
            )
            text_index += 1
            continue

        if suffix == "tool_use_start":
            tool_id = _safe_text(
                payload.get("tool_use_id", payload.get("toolUseId", payload.get("id"))),
                maximum=200,
            )
            name = _safe_text(
                payload.get("tool_name", payload.get("toolName", payload.get("name"))),
                maximum=128,
            )
            if name == "router_control":
                continue
            if tool_id is None or name is None or _SAFE_TOOL_NAME.fullmatch(name) is None:
                complete = False
                continue
            entry = {
                "type": "segment",
                "id": f"tool:{tool_id}",
                "order": order,
                "segment_type": "tool",
                "tool_use_id": tool_id,
                "name": name,
                "started_at": at,
            }
            tools[tool_id] = entry
            append(entry)
            continue

        if suffix == "tool_result":
            tool_id = _safe_text(
                payload.get("tool_use_id", payload.get("toolUseId", payload.get("id"))),
                maximum=200,
            )
            if tool_id is None:
                complete = False
                continue
            existing_entry: dict[str, Any] | None = tools.get(tool_id)
            if existing_entry is None:
                name = _safe_text(
                    payload.get("tool_name", payload.get("toolName", payload.get("name"))),
                    maximum=128,
                )
                if name == "router_control":
                    continue
                if name is None or _SAFE_TOOL_NAME.fullmatch(name) is None:
                    complete = False
                    continue
                existing_entry = {
                    "type": "segment",
                    "id": f"tool:{tool_id}",
                    "order": order,
                    "segment_type": "tool",
                    "tool_use_id": tool_id,
                    "name": name,
                    "started_at": at,
                }
                tools[tool_id] = existing_entry
                append(existing_entry)
                complete = False
            existing_entry["ended_at"] = max(
                int(existing_entry["started_at"]),
                _event_end_at(payload),
            )
            existing_entry["is_error"] = payload.get("is_error", payload.get("isError")) is True

            result_record = _json_record(
                payload.get("result", payload.get("content", payload.get("output")))
            )
            if result_record is not None and result_record.get("kind") == "user_input":
                request_id = _safe_text(
                    result_record.get("request_id", result_record.get("requestId")),
                    maximum=200,
                )
                if request_id and result_record.get("paused") is True:
                    interrupts.setdefault(
                        request_id,
                        {
                            "type": "interrupt",
                            "id": f"clarify:{request_id}",
                            "order": order,
                            "interrupt_type": "clarify",
                            "reference_id": request_id,
                            "tool_use_id": tool_id,
                            "started_at": at,
                        },
                    )
                elif request_id and result_record.get("paused") is False:
                    interrupt = interrupts.get(request_id)
                    if interrupt is None:
                        complete = False
                    elif str(result_record.get("status") or "") == "answered":
                        interrupt["resolution"] = "replied"
                        interrupt["ended_at"] = _event_end_at(payload)
                    else:
                        complete = False
            continue

        if event_name.endswith(".approval.requested"):
            approval_id = _safe_text(
                payload.get("approval_id", payload.get("approvalId")), maximum=200
            )
            tool_name = _safe_text(
                payload.get("tool_name", payload.get("toolName")), maximum=128
            )
            approval_kind = _safe_text(
                payload.get("approval_kind", payload.get("approvalKind")), maximum=128
            )
            namespace = _safe_text(payload.get("namespace"), maximum=64) or "exec"
            if approval_id is None or (
                tool_name is not None and _SAFE_TOOL_NAME.fullmatch(tool_name) is None
            ):
                complete = False
                continue
            interrupts.setdefault(
                approval_id,
                {
                    "type": "interrupt",
                    "id": f"approval:{approval_id}",
                    "order": order,
                    "interrupt_type": "approval",
                    "reference_id": approval_id,
                    "namespace": namespace,
                    **({"tool_name": tool_name} if tool_name is not None else {}),
                    **(
                        {"approval_kind": approval_kind}
                        if approval_kind is not None
                        else {}
                    ),
                    "started_at": at,
                },
            )
            continue

        if event_name.endswith(".approval.resolved"):
            approval_id = _safe_text(
                payload.get("approval_id", payload.get("approvalId")), maximum=200
            )
            if approval_id is None:
                complete = False
                continue
            interrupt = interrupts.get(approval_id)
            if interrupt is None:
                complete = False
                continue
            resolution = str(payload.get("resolution") or "").lower()
            if resolution != "expired":
                resolution = "denied" if payload.get("approved") is False else "approved"
            interrupt["resolution"] = resolution
            interrupt["ended_at"] = _event_end_at(payload)
            continue

        if suffix == "compaction":
            compaction_id = _safe_text(
                payload.get("compaction_id", payload.get("compactionId")), maximum=200
            )
            raw_state = str(payload.get("status") or "started").lower()
            if raw_state in {"started", "observed", "running"}:
                state = "running"
            elif raw_state in {"completed", "emergency_ephemeral"}:
                state = "completed"
            elif raw_state in {"failed", "error", "timed_out"}:
                state = "failed"
            else:
                state = raw_state
            if compaction_id is None or state not in _MAINTENANCE_STATES:
                continue
            entry = {
                "type": "maintenance",
                "id": compaction_id,
                "order": order,
                "maintenance_type": "context_compaction",
                "state": state,
                "at": at,
                "ended_at": _event_end_at(payload),
            }
            for field in ("source", "durability"):
                field_value = _safe_text(payload.get(field), maximum=64)
                if field_value is not None:
                    entry[field] = field_value
            reason = _safe_text(
                payload.get("reason", payload.get("skip_reason")),
                maximum=64,
            )
            if reason in _MAINTENANCE_REASONS:
                entry["reason"] = reason
            prior_index = maintenance.get(compaction_id)
            if prior_index is None:
                maintenance[compaction_id] = len(entries)
                append(entry)
            elif prior_index < len(entries):
                prior = entries[prior_index]
                entries[prior_index] = {
                    **prior,
                    **entry,
                    "order": prior["order"],
                    "at": prior["at"],
                    "ended_at": entry["ended_at"],
                }

    reasoning_offset = 0
    persisted_reasoning_blocks = 0
    for block_id in reasoning_order:
        block = reasoning[block_id]
        text_length = int(block.pop("_text_utf16_length", 0))
        if text_length <= 0:
            continue
        # Agent.done persists physical provider-call reasoning with a single
        # newline between non-empty blocks. Keep these offsets aligned to that
        # canonical transcript string rather than to the delta-only stream.
        if persisted_reasoning_blocks:
            reasoning_offset += 1
        block["text_start_utf16"] = reasoning_offset
        reasoning_offset += text_length
        block["text_end_utf16"] = reasoning_offset
        if block["status"] == "streaming":
            block["status"] = "completed"
        append(block)
        persisted_reasoning_blocks += 1

    for interrupt in interrupts.values():
        if "resolution" not in interrupt or "ended_at" not in interrupt:
            complete = False
            continue
        append(interrupt)

    entries.sort(
        key=lambda entry: (
            int(entry["order"]),
            0 if entry["type"] == "phase" else 1,
            str(entry["id"]),
        )
    )
    phases: list[dict[str, Any]] = [
        entry for entry in entries if entry["type"] == "phase"
    ]
    effective_terminal_at = terminal_boundary
    if effective_terminal_at is None and ordered_events:
        effective_terminal_at = max(_event_end_at(payload) for _, payload, _ in ordered_events)
        complete = False
    for index in range(len(phases)):
        phase_entry = phases[index]
        started_at = int(phase_entry["at"])
        next_started_at = (
            int(phases[index + 1]["at"])
            if index + 1 < len(phases)
            else effective_terminal_at
        )
        phase_entry["ended_at"] = max(
            started_at,
            next_started_at if next_started_at is not None else started_at,
        )
    entries.sort(
        key=lambda entry: (
            int(entry["order"]),
            0 if entry["type"] == "phase" else 1,
            str(entry["id"]),
        )
    )
    if not entries:
        return None
    return {
        "version": 2,
        "task_id": task_id,
        "turn_id": turn_id,
        "complete": complete,
        "reasoning_utf16_length": reasoning_offset,
        "entries": entries,
        "checksum": _entry_checksum(entries),
    }


def _terminal_activity_snapshot_v2(
    value: Mapping[str, Any],
    *,
    task_id: str,
    turn_id: str,
) -> dict[str, Any] | None:
    if set(value) - {
        "version",
        "task_id",
        "turn_id",
        "complete",
        "reasoning_utf16_length",
        "entries",
        "checksum",
    }:
        return None
    if value.get("task_id") != task_id or value.get("turn_id") != turn_id:
        return None
    if not isinstance(value.get("complete"), bool):
        return None
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) > _MAX_ACTIVITY_ENTRIES:
        return None
    entries: list[dict[str, Any]] = []
    previous_order = 0
    entry_ids: set[str] = set()
    text_indices: list[int] = []
    reasoning_spans: list[tuple[int, int]] = []
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            return None
        entry_type = str(raw.get("type") or "")
        entry_id = _safe_text(raw.get("id"), maximum=240)
        order = _safe_positive_int(raw.get("order"))
        if (
            entry_type not in _ACTIVITY_ENTRY_TYPES
            or entry_id is None
            or order is None
            or order < previous_order
            or entry_id in entry_ids
        ):
            return None
        previous_order = order
        entry_ids.add(entry_id)
        common = {"type": entry_type, "id": entry_id, "order": order}

        if entry_type == "phase":
            allowed = {
                "type", "id", "order", "kind", "phase", "at", "reason",
                "ended_at", "retry_after_ms", "retry_attempt", "retry_limit", "round",
            }
            if set(raw) - allowed:
                return None
            kind = str(raw.get("kind") or "")
            phase = str(raw.get("phase") or "")
            valid_phase = (
                (kind in _ACTIVITY_PHASES and phase in _ACTIVITY_PHASES[kind])
                or (kind == "write" and phase == "writing")
            )
            at = _safe_positive_int(raw.get("at"))
            ended_at = _safe_positive_int(raw.get("ended_at"))
            if (
                not valid_phase
                or at is None
                or ended_at is None
                or ended_at < at
            ):
                return None
            entry = {
                **common,
                "kind": kind,
                "phase": phase,
                "at": at,
                "ended_at": ended_at,
            }
            if kind == "provider":
                if raw.get("reason") is not None:
                    if raw.get("reason") != "rate_limited":
                        return None
                    entry["reason"] = "rate_limited"
                retry_after = _safe_nonnegative_int(
                    raw.get("retry_after_ms"), maximum=_MAX_RETRY_AFTER_MS
                )
                retry_attempt = _safe_nonnegative_int(
                    raw.get("retry_attempt"), maximum=10_000
                )
                retry_limit = _safe_nonnegative_int(
                    raw.get("retry_limit"), maximum=10_000
                )
                if raw.get("retry_after_ms") is not None and retry_after is None:
                    return None
                if raw.get("retry_attempt") is not None and retry_attempt is None:
                    return None
                if raw.get("retry_limit") is not None and retry_limit is None:
                    return None
                if retry_after is not None:
                    entry["retry_after_ms"] = retry_after
                if retry_attempt is not None:
                    entry["retry_attempt"] = retry_attempt
                if retry_limit is not None:
                    entry["retry_limit"] = retry_limit
            elif kind == "write":
                round_index = _safe_positive_int(raw.get("round"), maximum=100_000)
                if round_index is None:
                    return None
                entry["round"] = round_index
            elif any(
                raw.get(field) is not None
                for field in (
                    "reason", "retry_after_ms", "retry_attempt", "retry_limit", "round"
                )
            ):
                return None

        elif entry_type == "reasoning":
            allowed = {
                "type", "id", "order", "block_index", "started_at", "ended_at",
                "status", "content_kind", "text_start_utf16", "text_end_utf16",
            }
            if set(raw) - allowed:
                return None
            block_index = _safe_nonnegative_int(raw.get("block_index"), maximum=100_000)
            started_at = _safe_positive_int(raw.get("started_at"))
            ended_at = _safe_positive_int(raw.get("ended_at"))
            start = _safe_nonnegative_int(
                raw.get("text_start_utf16"), maximum=100_000_000
            )
            end = _safe_nonnegative_int(
                raw.get("text_end_utf16"), maximum=100_000_000
            )
            status = str(raw.get("status") or "")
            content_kind = str(raw.get("content_kind") or "")
            if (
                block_index is None
                or started_at is None
                or ended_at is None
                or ended_at < started_at
                or start is None
                or end is None
                or end < start
                or status not in {"completed", "interrupted", "error"}
                or content_kind not in {"reasoning", "summary"}
            ):
                return None
            reasoning_spans.append((start, end))
            entry = {
                **common,
                "block_index": block_index,
                "started_at": started_at,
                "ended_at": ended_at,
                "status": status,
                "content_kind": content_kind,
                "text_start_utf16": start,
                "text_end_utf16": end,
            }

        elif entry_type == "segment":
            segment_type = str(raw.get("segment_type") or "")
            if segment_type == "text":
                allowed = {
                    "type", "id", "order", "segment_type", "text_index",
                    "text_utf16_length", "at", "ended_at",
                }
                if set(raw) - allowed:
                    return None
                text_index = _safe_nonnegative_int(raw.get("text_index"), maximum=100_000)
                text_length = _safe_positive_int(
                    raw.get("text_utf16_length"), maximum=100_000_000
                )
                at = _safe_positive_int(raw.get("at"))
                ended_at = _safe_positive_int(raw.get("ended_at"))
                if (
                    text_index is None
                    or text_length is None
                    or at is None
                    or ended_at is None
                    or ended_at < at
                ):
                    return None
                text_indices.append(text_index)
                entry = {
                    **common,
                    "segment_type": "text",
                    "text_index": text_index,
                    "text_utf16_length": text_length,
                    "at": at,
                    "ended_at": ended_at,
                }
            elif segment_type == "tool":
                allowed = {
                    "type", "id", "order", "segment_type", "tool_use_id", "name",
                    "started_at", "ended_at", "is_error",
                }
                if set(raw) - allowed:
                    return None
                tool_id = _safe_text(raw.get("tool_use_id"), maximum=200)
                name = _safe_text(raw.get("name"), maximum=128)
                started_at = _safe_positive_int(raw.get("started_at"))
                ended_at = (
                    _safe_positive_int(raw.get("ended_at"))
                    if raw.get("ended_at") is not None
                    else None
                )
                if (
                    tool_id is None
                    or name is None
                    or _SAFE_TOOL_NAME.fullmatch(name) is None
                    or started_at is None
                    or (raw.get("ended_at") is not None and ended_at is None)
                    or (ended_at is not None and ended_at < started_at)
                    or (
                        raw.get("is_error") is not None
                        and not isinstance(raw.get("is_error"), bool)
                    )
                ):
                    return None
                entry = {
                    **common,
                    "segment_type": "tool",
                    "tool_use_id": tool_id,
                    "name": name,
                    "started_at": started_at,
                    **({"ended_at": ended_at} if ended_at is not None else {}),
                    **(
                        {"is_error": raw["is_error"]}
                        if isinstance(raw.get("is_error"), bool)
                        else {}
                    ),
                }
            else:
                return None

        elif entry_type == "maintenance":
            allowed = {
                "type", "id", "order", "maintenance_type", "state", "at",
                "ended_at", "source", "durability", "reason",
            }
            if set(raw) - allowed:
                return None
            state = str(raw.get("state") or "")
            at = _safe_positive_int(raw.get("at"))
            ended_at = _safe_positive_int(raw.get("ended_at"))
            if (
                raw.get("maintenance_type") != "context_compaction"
                or state not in _MAINTENANCE_STATES
                or at is None
                or ended_at is None
                or ended_at < at
            ):
                return None
            entry = {
                **common,
                "maintenance_type": "context_compaction",
                "state": state,
                "at": at,
                "ended_at": ended_at,
            }
            for field in ("source", "durability"):
                if raw.get(field) is None:
                    continue
                value_text = _safe_text(raw.get(field), maximum=64)
                if value_text is None:
                    return None
                entry[field] = value_text
            if raw.get("reason") is not None:
                reason = _safe_text(raw.get("reason"), maximum=64)
                if reason not in _MAINTENANCE_REASONS:
                    return None
                entry["reason"] = reason

        elif entry_type == "interrupt":
            allowed = {
                "type", "id", "order", "interrupt_type", "reference_id",
                "tool_use_id", "namespace", "tool_name", "approval_kind",
                "resolution", "started_at", "ended_at",
            }
            if set(raw) - allowed:
                return None
            interrupt_type = str(raw.get("interrupt_type") or "")
            reference_id = _safe_text(raw.get("reference_id"), maximum=200)
            resolution = str(raw.get("resolution") or "")
            started_at = _safe_positive_int(raw.get("started_at"))
            ended_at = _safe_positive_int(raw.get("ended_at"))
            if (
                interrupt_type not in {"approval", "clarify"}
                or reference_id is None
                or started_at is None
                or ended_at is None
                or ended_at < started_at
            ):
                return None
            entry = {
                **common,
                "interrupt_type": interrupt_type,
                "reference_id": reference_id,
                "started_at": started_at,
                "ended_at": ended_at,
            }
            if interrupt_type == "approval":
                tool_name = (
                    _safe_text(raw.get("tool_name"), maximum=128)
                    if raw.get("tool_name") is not None
                    else None
                )
                namespace = _safe_text(raw.get("namespace"), maximum=64)
                approval_kind = (
                    _safe_text(raw.get("approval_kind"), maximum=128)
                    if raw.get("approval_kind") is not None
                    else None
                )
                if (
                    resolution not in {"approved", "denied", "expired", "unavailable"}
                    or (
                        raw.get("tool_name") is not None
                        and (
                            tool_name is None
                            or _SAFE_TOOL_NAME.fullmatch(tool_name) is None
                        )
                    )
                    or namespace is None
                    or (raw.get("approval_kind") is not None and approval_kind is None)
                    or raw.get("tool_use_id") is not None
                ):
                    return None
                entry.update(
                    {
                        "namespace": namespace,
                        "resolution": resolution,
                    }
                )
                if tool_name is not None:
                    entry["tool_name"] = tool_name
                if approval_kind is not None:
                    entry["approval_kind"] = approval_kind
            else:
                tool_use_id = _safe_text(raw.get("tool_use_id"), maximum=200)
                if (
                    resolution != "replied"
                    or tool_use_id is None
                    or any(
                        raw.get(field) is not None
                        for field in ("namespace", "tool_name", "approval_kind")
                    )
                ):
                    return None
                entry.update(
                    {
                        "tool_use_id": tool_use_id,
                        "resolution": "replied",
                    }
                )

        else:
            return None
        entries.append(entry)
    if text_indices and sorted(text_indices) != list(range(len(text_indices))):
        return None
    if not entries:
        return None
    checksum = _entry_checksum(entries)
    supplied_checksum = value.get("checksum")
    if supplied_checksum is not None and supplied_checksum != checksum:
        return None
    reasoning_length = _safe_nonnegative_int(
        value.get("reasoning_utf16_length"), maximum=100_000_000
    )
    if reasoning_length is None:
        return None
    if reasoning_spans:
        cursor = 0
        for index, (start, end) in enumerate(sorted(reasoning_spans)):
            expected_start = cursor if index == 0 else cursor + 1
            if start != expected_start:
                return None
            cursor = end
        if cursor != reasoning_length:
            return None
    elif reasoning_length != 0:
        return None
    return {
        "version": 2,
        "task_id": task_id,
        "turn_id": turn_id,
        "complete": value["complete"],
        "reasoning_utf16_length": reasoning_length,
        "entries": entries,
        "checksum": checksum,
    }


def terminal_activity_snapshot(
    value: object,
    *,
    task_id: str,
    turn_id: str,
) -> dict[str, Any] | None:
    """Return an identity-bound snapshot containing only closed phase enums."""

    if isinstance(value, Mapping) and value.get("version") == 2:
        return _terminal_activity_snapshot_v2(
            value,
            task_id=task_id,
            turn_id=turn_id,
        )

    raw_phases: object
    if isinstance(value, Mapping):
        raw_phases = value.get("phases")
    else:
        raw_phases = value
    if not isinstance(raw_phases, list):
        return None

    phases: list[dict[str, Any]] = []
    for raw in raw_phases[:_MAX_ACTIVITY_PHASES]:
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("kind") or "").strip().lower()
        phase = str(raw.get("phase") or "").strip().lower()
        if phase not in _ACTIVITY_PHASES.get(kind, frozenset()):
            continue
        raw_at = raw.get("at")
        if isinstance(raw_at, bool):
            continue
        try:
            at = int(raw_at)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            continue
        if at <= 0 or at > _MAX_TIMESTAMP_MS:
            continue
        entry = {"kind": kind, "phase": phase, "at": at}
        if not phases or phases[-1] != entry:
            phases.append(entry)
    if not phases:
        return None
    return {
        "version": 1,
        "task_id": task_id,
        "turn_id": turn_id,
        "phases": phases,
    }


__all__ = [
    "USAGE_ACCOUNTING_BARRIER_CODES",
    "append_activity_phase",
    "build_terminal_activity_snapshot",
    "is_usage_accounting_barrier",
    "safe_primary_user_message_id",
    "safe_retry_after_ms",
    "terminal_activity_snapshot",
    "usage_barrier_replay_proof",
]
