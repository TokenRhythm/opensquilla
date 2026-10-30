"""Durable model-policy inputs anchored to accepted transcript rows.

Policy state lives on the input that produced it, before provider execution.
Canonical transcript copies therefore preserve fork prefixes, while session
identity rotation and compaction retain their existing ownership semantics.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

MODEL_DECISION_KEY = "router_model_decision"


def canonical_previous_usage(value: object) -> dict[str, Any]:
    """Match the frozen feature snapshot's complete optional usage bundle."""
    if not isinstance(value, Mapping):
        return {}
    required = {
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_write_tokens",
    }
    if not required.issubset(value) or any(
        isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0
        for key in required
    ):
        return {}
    execution = {
        "route_id",
        "execution_status",
        "error_code",
        "response_id",
        "attempt_ids",
        "retry_count",
    }
    return {key: item for key, item in value.items() if key not in execution}


def visible_router_text(role: str, content: object) -> str:
    from opensquilla.engine.steps.inject_time_prefix import TIME_PREFIX_RE

    text = str(content or "")
    if text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, Mapping):
            visible = payload.get("text")
            envelope = (role == "user" and "attachments" in payload) or (
                role == "assistant" and "artifacts" in payload
            )
            if isinstance(visible, str) and envelope:
                text = visible
    if role == "user":
        text = TIME_PREFIX_RE.sub("", text, count=1)
    return text.strip()


def normalized_router_attachments(attachments: Sequence[object]) -> list[dict[str, Any]]:
    """Source all-or-nothing modality projection; never include attachment bytes."""
    normalized = []
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            return []
        raw = next(
            (
                value.strip().casefold()
                for key in ("mime", "mime_type", "content_type", "type")
                if isinstance((value := attachment.get(key)), str) and value.strip()
            ),
            None,
        )
        if raw is None:
            return []
        media = raw.split(";", 1)[0].strip()
        if media.startswith("image/") or media == "image":
            modality = "image"
        elif media.startswith("audio/") or media == "audio":
            modality = "audio"
        elif media.startswith("video/") or media == "video":
            modality = "video"
        elif (
            media.startswith("text/")
            or media
            in {
                "application/json",
                "application/pdf",
                "application/rtf",
                "application/xml",
                "document",
            }
            or media.startswith("application/vnd.openxmlformats-officedocument")
            or media.startswith("application/vnd.oasis.opendocument")
        ):
            modality = "document"
        elif media in {
            "application/gzip",
            "application/x-7z-compressed",
            "application/x-rar-compressed",
            "application/x-tar",
            "application/zip",
            "archive",
        } or media.endswith("+zip"):
            modality = "archive"
        else:
            modality = "other"
        safe: dict[str, Any] = {"type": modality}
        for key in (
            "mime_type",
            "media_type",
            "parse_status",
            "status",
            "summary",
            "truncated",
            "token_count",
        ):
            value = attachment.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe[key] = value
        normalized.append(safe)
    return normalized


def prepare_model_router_metadata(
    context: Mapping[str, Any] | None,
    *,
    current_request: str,
    input_message_id: str | None,
    history_user: Sequence[str] | None = None,
    previous_answer: str | None = None,
    previous_usage: Mapping[str, Any] | None = None,
    tool_defs: Sequence[Any] = (),
    attachments: Sequence[object] = (),
    channel_kind: str = "",
) -> dict[str, Any]:
    """Complete the frozen RouterInput contract with this turn's raw request facts."""
    context = context or {}
    model_input = deepcopy(context.get("router_model_input") or {})
    model_input["current_request"] = current_request
    model_input.setdefault("task_anchor", "")
    model_input.setdefault("history_user", list(history_user or [])[-4:])
    model_input.setdefault("previous_answer", previous_answer or "")
    model_input.setdefault("previous_usage", dict(previous_usage or {}))
    model_input.setdefault("previous_outcome", "unknown")
    model_input.setdefault("active_route_tier", None)
    model_input.setdefault("route_history", [])
    history_chars = context.get("router_model_history_user_chars")
    if history_chars is None:
        history_chars = sum(len(text) for text in model_input["history_user"])
    model_input["context"] = {
        "turn_index": len(model_input["route_history"]),
        "context_tokens_est": (
            len(current_request) + history_chars + len(model_input["previous_answer"])
        ) // 4,
    }
    available_tools = sorted({name for tool in tool_defs if (
        name := str(tool.get("name") if isinstance(tool, Mapping)
                    else getattr(tool, "name", "")).strip()
    )})
    model_input["tool_state"] = {"available_tools": available_tools} if available_tools else {}
    model_input["attachments"] = normalized_router_attachments(attachments)
    model_input["previous_usage"] = canonical_previous_usage(model_input["previous_usage"])
    if channel_kind:
        model_input["context"]["platform"] = channel_kind
    control_context = dict(context.get("router_model_control_context") or {})
    control_context["context_tokens_est"] = (
        int(control_context.get("context_tokens_est") or 0)
        + max(1, (len(current_request) + 3) // 4)
    )
    metadata = {
        "router_model_control_context": control_context,
        "router_model_input": model_input,
        "router_model_state": deepcopy(context.get("router_model_state")),
        "router_model_input_message_id": input_message_id,
    }
    if context.get("router_model_decision"):
        metadata["router_model_decision"] = deepcopy(context["router_model_decision"])
    if context.get("router_model_control_event") is not None:
        metadata["router_model_control_event"] = context["router_model_control_event"]
    return metadata


def model_generation_transcript(
    entries: Sequence[Any],
    bound_user_message_id: str | None,
    *,
    include_current_staged: bool = False,
) -> list[Any] | None:
    """Return the explicit-reset generation, or None for ordinary history replay."""
    rows = list(entries)
    boundary = (
        next(
            (
                i
                for i, row in enumerate(rows)
                if getattr(row, "message_id", None) == bound_user_message_id
            ),
            None,
        )
        if bound_user_message_id
        else None
    )
    state_rows = [
        row
        for i, row in enumerate(rows)
        if getattr(row, "role", None) == "user"
        and (boundary is None or i <= boundary)
        and _decision(row).get("state")
        and (
            _decision(row).get("state_committed") is True
            or (
                include_current_staged and getattr(row, "message_id", None) == bound_user_message_id
            )
        )
    ]
    state = _mapping(_decision(state_rows[-1]).get("state")) if state_rows else {}
    generation_start = state.get("generation_start_input_message_id")
    if not generation_start:
        return None
    start = next(
        (
            i
            for i, row in enumerate(rows)
            if generation_start
            in {getattr(row, "message_id", None), _decision(row).get("input_message_id")}
        ),
        None,
    )
    if start is None:
        raise RuntimeError("Model router generation boundary is absent from canonical history")
    allowed_turn_ids = {
        _turn_id(row)
        for i, row in enumerate(rows)
        if i >= start
        and (boundary is None or i <= boundary)
        and getattr(row, "role", None) == "user"
    } - {""}
    return [
        row
        for i, row in enumerate(rows)
        if i >= start
        and (
            (getattr(row, "role", None) == "user" and (boundary is None or i <= boundary))
            or (
                getattr(row, "role", None) != "user"
                and (
                    _turn_id(row) in allowed_turn_ids
                    or (not _turn_id(row) and (boundary is None or i <= boundary))
                )
            )
        )
    ]


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _decision(entry: Any) -> Mapping[str, Any]:
    return _mapping(_mapping(getattr(entry, "turn_context", None)).get(MODEL_DECISION_KEY))


def _turn_id(entry: Any) -> str:
    from opensquilla.turn_outcome_projection import turn_id_from_context

    return turn_id_from_context(getattr(entry, "turn_context", None)) or ""


def build_model_router_context(
    entries: Sequence[Any],
    *,
    bound_user_message_id: str | None,
    exclude_last_user: bool,
    feature_entries: Sequence[Any] | None = None,
    canonical_complete: bool = True,
) -> dict[str, Any]:
    """Build raw user history and the last five admitted routes within 30 minutes."""
    rows = list(entries)
    bound_index = (
        next(
            (
                i
                for i, row in enumerate(rows)
                if getattr(row, "message_id", None) == bound_user_message_id
            ),
            None,
        )
        if bound_user_message_id
        else None
    )
    if bound_user_message_id and bound_index is None:
        raise RuntimeError("Model router input is absent from its canonical transcript")
    if bound_index is None and exclude_last_user:
        bound_index = next(
            (i for i in reversed(range(len(rows))) if getattr(rows[i], "role", None) == "user"),
            None,
        )
    prior_users = [
        row
        for i, row in enumerate(rows)
        if getattr(row, "role", None) == "user" and (bound_index is None or i < bound_index)
    ]
    current = rows[bound_index] if bound_index is not None else None
    current_id = getattr(current, "message_id", None)
    state_rows = [
        row
        for row in prior_users
        if _decision(row).get("state_committed") is True and _decision(row).get("state")
    ]
    previous_record = _decision(state_rows[-1]) if state_rows else {}
    state = _mapping(previous_record.get("state")) or None
    replay_record = _decision(current) if current is not None else {}
    active_feature_rows = list(feature_entries) if feature_entries is not None else rows
    feature_rows = active_feature_rows
    anchor_id = (state or {}).get("task_start_input_message_id")

    def has_id(candidates: Sequence[Any], message_id: str | None) -> bool:
        return message_id is not None and any(
            message_id
            in {
                getattr(row, "message_id", None),
                _decision(row).get("input_message_id"),
            }
            for row in candidates
        )

    if anchor_id and not has_id(feature_rows, anchor_id):
        if canonical_complete and has_id(rows, anchor_id) and has_id(rows, current_id):
            feature_rows = rows
        else:
            raise RuntimeError("Model router task feature boundary is unavailable")
    feature_bound_index = next(
        (
            i
            for i, row in enumerate(feature_rows)
            if row is current or getattr(row, "message_id", None) == current_id
        ),
        None,
    )
    if current_id is not None and feature_bound_index is None:
        raise RuntimeError("Model router current feature boundary is unavailable")
    feature_prior_users = [
        row
        for i, row in enumerate(feature_rows)
        if getattr(row, "role", None) == "user"
        and (feature_bound_index is None or i < feature_bound_index)
    ]
    excluded_turns = {
        _turn_id(row)
        for i, row in enumerate(feature_rows)
        if feature_bound_index is not None
        and i >= feature_bound_index
        and getattr(row, "role", None) == "user"
    } - {""}

    def text(row: Any) -> str:
        return visible_router_text(str(getattr(row, "role", "")), getattr(row, "content", None))

    anchor = ""
    if state:
        task_anchor = state.get("task_start_input_message_id")
        for row in feature_prior_users:
            record = _decision(row)
            aliases = {getattr(row, "message_id", None), record.get("input_message_id")}
            if task_anchor in aliases and task_anchor is not None:
                anchor = text(row)
                break
        if not anchor:
            # Forks reassign message IDs; task identity also survives the copy.
            anchor_row = next(
                (
                    row
                    for row in state_rows
                    if _mapping(_decision(row).get("state")).get("task_id") == state.get("task_id")
                ),
                None,
            )
            if anchor_row is not None:
                anchor = text(anchor_row)

    if state_rows:
        previous_input = state_rows[-1]
        previous_turn_id = _turn_id(previous_input)
        previous_index = next(
            (
                i
                for i, row in enumerate(feature_rows)
                if getattr(row, "message_id", None) == getattr(previous_input, "message_id", None)
            ),
            -1,
        )
    # The preceding reply may land after this input was queued. Causal IDs,
    # rather than its physical row position, distinguish it from future turns.
    answer = None
    for i in reversed(range(len(feature_rows))):
        row = feature_rows[i]
        turn_id = _turn_id(row)
        if (
            getattr(row, "role", None) == "assistant"
            and turn_id not in excluded_turns
            and (turn_id or feature_bound_index is None or i < feature_bound_index)
            and (not state_rows or i > previous_index and turn_id in {"", previous_turn_id})
        ):
            answer = row
            break
    usage = _mapping(getattr(answer, "turn_usage", None)) if answer is not None else {}
    outcome = "unknown"
    execution_succeeded = False
    if state_rows:
        previous_outcome = _mapping(
            _mapping(getattr(state_rows[-1], "turn_context", None)).get(
                "router_model_previous_outcome"
            )
        )
        outcome_kind = previous_outcome.get("kind")
        if outcome_kind == "completed":
            execution_succeeded = True
        elif outcome_kind in {"failed", "interrupted", "blocked", "budgetLimited", "partial"}:
            outcome = "failure"
    if outcome != "failure":
        signals = []
        for container in (
            getattr(answer, "turn_context", None),
            getattr(answer, "turn_usage", None),
        ):
            for key in (
                "agent_loop_stop_reason",
                "stop_reason",
                "outcome",
                "execution_status",
                "error_code",
            ):
                value = _mapping(container).get(key)
                if isinstance(value, str) and value.strip():
                    signals.append(value.strip())
        tool_names = []
        for raw in getattr(answer, "tool_calls", None) or []:
            if isinstance(raw, Mapping):
                name = (
                    raw.get("name")
                    or raw.get("tool_name")
                    or _mapping(raw.get("function")).get("name")
                )
                if isinstance(name, str):
                    tool_names.append(name)
        if re.search(
            r"(?:\b(?:clarif(?:y|ication)|ask[_ -]?user|need[_ -]?input)\b|"
            r"could you (?:clarify|provide)|please (?:specify|provide)|clarify which|"
            r"能否|请\s*提供|需要(?:更多|具体).{0,8}信息)",
            " ".join([*signals, *tool_names, text(answer) if answer is not None else ""]),
            re.IGNORECASE,
        ):
            outcome = "clarification"
        elif execution_succeeded:
            outcome = "success"
        elif any(
            re.search(
                r"(?:\b(?:error|fail(?:ed|ure)?|timeout|abort(?:ed)?|cancel(?:led|ed)?)\b|"
                r"错误|失败|超时|取消)",
                signal,
                re.IGNORECASE,
            )
            for signal in signals
        ):
            outcome = "failure"
        elif signals:
            outcome = "success"
    route_history = []
    history_before = int(time.time() * 1000)
    history_cutoff = history_before - 30 * 60 * 1000
    recent_rows = [
        row
        for row in state_rows
        if int(_decision(row).get("decided_at_ms") or getattr(row, "created_at", 0))
        >= history_cutoff
        and int(_decision(row).get("decided_at_ms") or getattr(row, "created_at", 0))
        <= history_before
    ]
    for row in recent_rows[-5:]:
        record = _decision(row)
        row_state = _mapping(record.get("state"))
        tier = str(row_state.get("tier") or "").upper()
        if tier in {"C0", "C1", "C2", "C3"}:
            history_entry: dict[str, Any] = {"tier_id": tier}
            audit = _mapping(_mapping(record.get("decision")).get("tier_audit"))
            probabilities = audit.get("probabilities")
            if isinstance(probabilities, Mapping):
                try:
                    vector = {
                        str(key).upper(): float(value) for key, value in probabilities.items()
                    }
                except (TypeError, ValueError):
                    vector = {}
                if set(vector) == {"C0", "C1", "C2", "C3"} and all(
                    math.isfinite(p) for p in vector.values()
                ):
                    ordered = sorted(vector.values(), reverse=True)
                    history_entry.update(
                        difficulty=sum(i * vector[f"C{i}"] for i in range(4)),
                        margin=ordered[0] - ordered[1],
                    )
            route_history.append(history_entry)
    user_history = [visible for row in feature_prior_users if (visible := text(row))]
    result: dict[str, Any] = {
        "router_model_state": deepcopy(state),
        "router_model_history_user_chars": sum(len(value) for value in user_history),
        "router_model_input": {
            "task_anchor": anchor,
            "history_user": user_history[-4:],
            "previous_answer": text(answer) if answer is not None else "",
            "previous_usage": canonical_previous_usage(usage),
            "previous_outcome": outcome,
            "active_route_tier": str(state["tier"]).upper() if state else None,
            "route_history": route_history,
        },
    }
    from opensquilla.session.compaction import estimate_entry_model_replay_tokens

    generation_rows = model_generation_transcript(rows, bound_user_message_id)
    if generation_rows is None:
        generation_rows = active_feature_rows
    generation_current = next(
        (
            i
            for i, row in enumerate(generation_rows)
            if row is current or getattr(row, "message_id", None) == current_id
        ),
        None,
    )
    generation_history = (
        generation_rows[:generation_current] if generation_current is not None else generation_rows
    )
    current_context = (
        _mapping(getattr(current, "turn_context", None)) if current is not None else {}
    )
    control = current_context.get("router_model_control_event")
    if control in {"new", "new_task", "new-task", "create_task", "create-task"}:
        generation_history = []
    result["router_model_control_context"] = {
        "context_tokens_est": sum(
            estimate_entry_model_replay_tokens(row) for row in generation_history
        ),
    }
    if replay_record:
        result["router_model_decision"] = deepcopy(replay_record)
    if control in {"new", "new_task", "new-task", "create_task", "create-task", "continue", "redo"}:
        result["router_model_control_event"] = control
    return result
