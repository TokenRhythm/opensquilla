"""Structured compaction state helpers.

This module defines OpenSquilla-owned portable state. Provider-native
compaction blocks and cached-content references should live in provider context
state, not in this structured summary payload.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


class StructuredCompactionSummary(BaseModel):
    """Portable, inspectable task state produced by local compaction."""

    schema_version: int = 1
    user_goal: str = ""
    current_status: str = ""
    next_action: str | None = None
    completed_steps: list[str] = Field(default_factory=list)
    open_steps: list[str] = Field(default_factory=list)
    files_and_artifacts: list[dict[str, str]] = Field(default_factory=list)
    tool_results_to_remember: list[dict[str, str]] = Field(default_factory=list)
    decisions_and_rationale: list[dict[str, str]] = Field(default_factory=list)
    known_failures: list[dict[str, str]] = Field(default_factory=list)
    executed_commands_and_tests: list[str] = Field(default_factory=list)
    pending_tool_and_approval_ids: list[str] = Field(default_factory=list)
    important_identifiers: list[str] = Field(default_factory=list)
    constraints_and_preferences: list[str] = Field(default_factory=list)
    do_not_repeat: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    critical_carry_forward: list[str] = Field(default_factory=list)
    source_coverage: dict[str, Any] = Field(default_factory=dict)


class CompactionObligation(BaseModel):
    """Small continuity fact that should survive transcript compaction."""

    kind: str
    value: str
    source_role: str | None = None
    source_entry_id: int | None = None
    critical: bool = True


class CoverageResult(BaseModel):
    """Report-only coverage check for compacted portable state."""

    status: str = "unknown"
    checked_obligations: int = 0
    covered_obligations: int = 0
    missing_obligations: list[str] = Field(default_factory=list)
    critical_carry_forward: list[str] = Field(default_factory=list)
    blocked: bool = False


class CompactionReport(BaseModel):
    """Inspectable continuity report for destructive compaction."""

    session_id: str | None = None
    session_key: str | None = None
    compaction_id: str | None = None
    trigger_reason: str | None = None
    tokens_before: int | None = None
    tokens_after: int | None = None
    removed_count: int = 0
    kept_count: int = 0
    chunk_count: int = 0
    summary_source: str = "unknown"
    flush_receipt_status: str = "unknown"
    coverage_status: str = "unknown"
    missing_obligations: list[str] = Field(default_factory=list)
    state_kind: str = "structured_summary_v1"
    provider_state_valid: bool | None = None
    persisted_summary_id: int | None = None


_MAX_OBLIGATION_VALUE_CHARS = 240
_MAX_CRITICAL_CARRY_FORWARD = 32
_PATH_RE = re.compile(
    r"(?<![\w.-])(?:[A-Za-z]:[\\/]|\.{1,2}/|/|[A-Za-z0-9_.@()+-]+/)"
    r"(?:[A-Za-z0-9_.@()+-]+(?: [A-Za-z0-9_.@()+-]+)*/)*"
    r"[A-Za-z0-9_.@()+-]+(?: [A-Za-z0-9_.@()+-]+)*"
    r"(?:\.[A-Za-z0-9][A-Za-z0-9_.-]{0,15})?"
)
_COMMAND_RE = re.compile(
    r"\b(?:(?:uv run )?(?:pytest|ruff|python|mypy|pyright|npm|pnpm|yarn|git|bash|sh|make|cargo)"
    r"|go test)\b[^\n\r]{0,220}"
)
_ERROR_MARKERS = ("error", "failed", "failure", "traceback", "exit code", "exception")
_CONSTRAINT_PREFIXES = ("constraint:", "constraints:", "限制:", "要求:")
_GOAL_PREFIXES = ("goal:", "objective:", "目标:")
_NEXT_ACTION_MARKERS = ("next i will", "next step", "下一步", "i will ", "我会")
_DO_NOT_REPEAT_MARKERS = ("do not repeat", "don't repeat", "不要重复", "不要再")
_ARTIFACT_MARKERS = ("artifact", "generated artifact", "附件", "产物")
_DECISION_PREFIXES = ("decision:", "rationale:", "reason:", "decided:", "决定:", "原因:")
_IDENTIFIER_RE = re.compile(
    r"\b(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{12,64})\b"
)
_ARTIFACT_NAME_RE = re.compile(
    r"\b[A-Za-z0-9_.@()+-]+(?: [A-Za-z0-9_.@()+-]+)*"
    r"\.(?:pdf|png|jpe?g|gif|csv|json|md|txt|xlsx?|pptx?|docx?|html?|zip)\b",
    re.IGNORECASE,
)


def _entry_value(entry: Any, key: str, default: Any = None) -> Any:
    if isinstance(entry, Mapping):
        return entry.get(key, default)
    return getattr(entry, key, default)


def _clean_obligation_text(value: Any, *, max_chars: int = _MAX_OBLIGATION_VALUE_CHARS) -> str:
    text = _string_value(value)
    text = re.sub(r"\s+", " ", text).strip(" `\t\r\n,;)]")
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _after_label(line: str) -> str:
    if ":" not in line:
        return line
    return line.split(":", 1)[1]


def _obligation_label(obligation: CompactionObligation) -> str:
    return f"{obligation.kind}: {obligation.value}"


_OBLIGATION_CONTINUITY_PRIORITY: dict[str, int] = {
    "user_goal": 0,
    "user_constraint_or_preference": 1,
    "pending_tool_or_approval_id": 2,
    "current_plan_or_next_action": 3,
    "failed_command_or_error": 4,
    "do_not_repeat_action": 5,
    "unresolved_question": 6,
    "decision_or_rationale": 7,
    "command": 8,
    "tool_result_fact": 9,
    "tool_result_id": 10,
    "important_identifier": 11,
    "artifact_path_or_name": 12,
    "file_path": 13,
}
_DEFAULT_OBLIGATION_CONTINUITY_PRIORITY = 14


@dataclass(slots=True)
class _RankedObligation:
    obligation: CompactionObligation
    last_seen_order: int


class _BoundedObligationBuffer:
    """Collect bounded per-kind candidates and reserve critical continuity categories."""

    def __init__(self, max_obligations: int) -> None:
        self._max_obligations = max(0, max_obligations)
        self._candidates: dict[tuple[str, str], _RankedObligation] = {}
        self._next_seen_order = 0

    @staticmethod
    def _rank(candidate: _RankedObligation) -> tuple[int, int, str, str]:
        obligation = candidate.obligation
        return (
            _OBLIGATION_CONTINUITY_PRIORITY.get(
                obligation.kind,
                _DEFAULT_OBLIGATION_CONTINUITY_PRIORITY,
            ),
            -candidate.last_seen_order,
            obligation.kind,
            obligation.value.casefold(),
        )

    def add(
        self,
        *,
        key: tuple[str, str],
        obligation: CompactionObligation,
        seen: set[tuple[str, str]],
    ) -> None:
        self._next_seen_order += 1
        if self._max_obligations == 0:
            return

        candidate = _RankedObligation(
            obligation=obligation,
            last_seen_order=self._next_seen_order,
        )
        if key in self._candidates:
            # A repeated fact belongs to its freshest source so current task state
            # wins over an identical mention in an older checkpoint.
            self._candidates[key] = candidate
            return

        self._candidates[key] = candidate
        seen.add(key)
        same_kind = [
            candidate_key
            for candidate_key, ranked in self._candidates.items()
            if ranked.obligation.kind == obligation.kind
        ]
        if len(same_kind) <= self._max_obligations:
            return

        evicted_key = max(
            same_kind,
            key=lambda candidate_key: self._rank(self._candidates[candidate_key]),
        )
        del self._candidates[evicted_key]
        seen.discard(evicted_key)

    def values(self) -> list[CompactionObligation]:
        selected: dict[tuple[str, str], _RankedObligation] = {}

        def _select(candidate: _RankedObligation) -> None:
            if len(selected) >= self._max_obligations:
                return
            obligation = candidate.obligation
            selected[(obligation.kind, obligation.value.casefold())] = candidate

        ranked_candidates = sorted(self._candidates.values(), key=self._rank)
        for reserved_kind in (
            "user_goal",
            "user_constraint_or_preference",
        ):
            candidate = next(
                (
                    item
                    for item in ranked_candidates
                    if item.obligation.kind == reserved_kind
                ),
                None,
            )
            if candidate is not None:
                _select(candidate)

        # Pending operations are structural continuity, not optional historical
        # detail. Keep every pending ID that fits after the current task anchors.
        for candidate in ranked_candidates:
            if candidate.obligation.kind == "pending_tool_or_approval_id":
                _select(candidate)

        # Preserve balanced kind coverage before repeated facts of any one kind
        # consume the remaining capacity.
        represented_kinds = sorted(
            {candidate.obligation.kind for candidate in ranked_candidates},
            key=lambda kind: (
                _OBLIGATION_CONTINUITY_PRIORITY.get(
                    kind,
                    _DEFAULT_OBLIGATION_CONTINUITY_PRIORITY,
                ),
                kind,
            ),
        )
        for kind in represented_kinds:
            candidate = next(
                (item for item in ranked_candidates if item.obligation.kind == kind),
                None,
            )
            if candidate is not None:
                _select(candidate)

        for candidate in ranked_candidates:
            _select(candidate)

        return [
            candidate.obligation
            for candidate in sorted(selected.values(), key=self._rank)
        ]


def _add_obligation(
    obligations: _BoundedObligationBuffer,
    seen: set[tuple[str, str]],
    *,
    kind: str,
    value: Any,
    source_role: str | None,
    source_entry_id: int | None,
    max_obligations: int,
) -> None:
    if max_obligations <= 0:
        return
    cleaned = _clean_obligation_text(value)
    if not cleaned:
        return
    key = (kind, cleaned.casefold())
    obligations.add(
        key=key,
        obligation=CompactionObligation(
            kind=kind,
            value=cleaned,
            source_role=source_role,
            source_entry_id=source_entry_id,
        ),
        seen=seen,
    )


_STRUCTURED_SECTION_KINDS: dict[str, str] = {
    "Goal": "user_goal",
    "Next Action": "current_plan_or_next_action",
    "Open Steps": "current_plan_or_next_action",
    "Files and Artifacts": "file_path",
    "Tool Results To Remember": "tool_result_fact",
    "Decisions and Rationale": "decision_or_rationale",
    "Known Failures": "failed_command_or_error",
    "Executed Commands and Tests": "command",
    "Pending Tool and Approval IDs": "pending_tool_or_approval_id",
    "Important Identifiers": "important_identifier",
    "Constraints and Preferences": "user_constraint_or_preference",
    "Do Not Repeat": "do_not_repeat_action",
    "Unresolved Questions": "unresolved_question",
}


def _extract_rendered_structured_obligations(
    content: str,
    *,
    obligations: _BoundedObligationBuffer,
    seen: set[tuple[str, str]],
    source_role: str | None,
    source_entry_id: int | None,
    max_obligations: int,
) -> None:
    if "[Structured Compaction Summary]" not in content:
        return
    section_kind: str | None = None
    critical_section = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line == "[Structured Compaction Summary]":
            continue
        if line.endswith(":") and not line.startswith("-"):
            title = line[:-1]
            section_kind = _STRUCTURED_SECTION_KINDS.get(title)
            critical_section = title == "Critical Carry Forward"
            continue
        value = line[2:].strip() if line.startswith("- ") else line
        if critical_section and ":" in value:
            raw_kind, raw_value = value.split(":", 1)
            kind = raw_kind.strip()
            if kind in {
                "user_goal",
                "current_plan_or_next_action",
                "file_path",
                "artifact_path_or_name",
                "tool_result_id",
                "tool_result_fact",
                "failed_command_or_error",
                "command",
                "pending_tool_or_approval_id",
                "decision_or_rationale",
                "important_identifier",
                "user_constraint_or_preference",
                "do_not_repeat_action",
                "unresolved_question",
            }:
                _add_obligation(
                    obligations,
                    seen,
                    kind=kind,
                    value=raw_value,
                    source_role=source_role,
                    source_entry_id=source_entry_id,
                    max_obligations=max_obligations,
                )
            continue
        if section_kind is None:
            continue
        # Mapping-list renderings use "- key: value"; retain the value rather
        # than the presentation key.
        if ":" in value and section_kind in {
            "file_path",
            "tool_result_fact",
            "decision_or_rationale",
            "failed_command_or_error",
        }:
            _key, value = value.split(":", 1)
        _add_obligation(
            obligations,
            seen,
            kind=section_kind,
            value=value,
            source_role=source_role,
            source_entry_id=source_entry_id,
            max_obligations=max_obligations,
        )


def extract_compaction_obligations(
    entries: Sequence[Any],
    *,
    max_obligations: int = 64,
) -> list[CompactionObligation]:
    """Extract bounded high-signal continuity facts before entries are removed."""

    obligations = _BoundedObligationBuffer(max_obligations)
    seen: set[tuple[str, str]] = set()
    issued_tool_call_ids: dict[str, int] = {}
    completed_tool_call_ids: set[str] = set()
    tool_call_order = 0
    for entry in entries:
        role = _string_value(_entry_value(entry, "role")) or None
        entry_id = _entry_value(entry, "id")
        source_entry_id = entry_id if isinstance(entry_id, int) else None
        content = _string_value(_entry_value(entry, "content"))
        _extract_rendered_structured_obligations(
            content,
            obligations=obligations,
            seen=seen,
            source_role=role,
            source_entry_id=source_entry_id,
            max_obligations=max_obligations,
        )

        tool_call_id = _entry_value(entry, "tool_call_id")
        cleaned_tool_result_id = _clean_obligation_text(tool_call_id)
        if cleaned_tool_result_id:
            completed_tool_call_ids.add(cleaned_tool_result_id)
        _add_obligation(
            obligations,
            seen,
            kind="tool_result_id",
            value=tool_call_id,
            source_role=role,
            source_entry_id=source_entry_id,
            max_obligations=max_obligations,
        )
        lines = [_clean_obligation_text(line) for line in content.splitlines()]
        for line in [line for line in lines if line]:
            lower = line.casefold()
            if role == "user" and lower.startswith(_GOAL_PREFIXES):
                _add_obligation(
                    obligations,
                    seen,
                    kind="user_goal",
                    value=_after_label(line),
                    source_role=role,
                    source_entry_id=source_entry_id,
                    max_obligations=max_obligations,
                )
            if role == "user" and lower.startswith(_CONSTRAINT_PREFIXES):
                _add_obligation(
                    obligations,
                    seen,
                    kind="user_constraint_or_preference",
                    value=_after_label(line),
                    source_role=role,
                    source_entry_id=source_entry_id,
                    max_obligations=max_obligations,
                )
            if lower.startswith(_DECISION_PREFIXES):
                _add_obligation(
                    obligations,
                    seen,
                    kind="decision_or_rationale",
                    value=_after_label(line),
                    source_role=role,
                    source_entry_id=source_entry_id,
                    max_obligations=max_obligations,
                )
            if role == "assistant" and any(marker in lower for marker in _NEXT_ACTION_MARKERS):
                _add_obligation(
                    obligations,
                    seen,
                    kind="current_plan_or_next_action",
                    value=line,
                    source_role=role,
                    source_entry_id=source_entry_id,
                    max_obligations=max_obligations,
                )
            if any(marker in lower for marker in _DO_NOT_REPEAT_MARKERS):
                _add_obligation(
                    obligations,
                    seen,
                    kind="do_not_repeat_action",
                    value=line,
                    source_role=role,
                    source_entry_id=source_entry_id,
                    max_obligations=max_obligations,
                )
            if any(marker in lower for marker in _ARTIFACT_MARKERS):
                for match in _ARTIFACT_NAME_RE.finditer(line):
                    if "/" in match.group(0) or "\\" in match.group(0):
                        continue
                    _add_obligation(
                        obligations,
                        seen,
                        kind="artifact_path_or_name",
                        value=match.group(0).rstrip("."),
                        source_role=role,
                        source_entry_id=source_entry_id,
                        max_obligations=max_obligations,
                    )
            if "?" in line or "？" in line:
                _add_obligation(
                    obligations,
                    seen,
                    kind="unresolved_question",
                    value=line,
                    source_role=role,
                    source_entry_id=source_entry_id,
                    max_obligations=max_obligations,
                )
            if any(marker in lower for marker in _ERROR_MARKERS):
                _add_obligation(
                    obligations,
                    seen,
                    kind="failed_command_or_error",
                    value=line,
                    source_role=role,
                    source_entry_id=source_entry_id,
                    max_obligations=max_obligations,
                )

        tool_calls = _entry_value(entry, "tool_calls") or []
        if isinstance(tool_calls, Sequence) and not isinstance(tool_calls, (str, bytes)):
            for call in tool_calls:
                if isinstance(call, Mapping):
                    call_id = call.get("id") or call.get("tool_use_id")
                    cleaned_call_id = _clean_obligation_text(call_id)
                    is_result = call.get("type") == "tool_result" or "result" in call
                    execution_status = call.get("execution_status")
                    if isinstance(execution_status, Mapping):
                        status_name = _string_value(
                            execution_status.get("status")
                        ).casefold()
                        status_reason = _string_value(
                            execution_status.get("reason")
                        ).casefold()
                        preservation_class = _string_value(
                            execution_status.get("preservation_class")
                        ).casefold()
                    else:
                        status_name = _string_value(
                            call.get("status")
                        ).casefold()
                        status_reason = ""
                        preservation_class = ""
                    pending_result = bool(
                        status_name
                        in {
                            "pending",
                            "queued",
                            "running",
                            "in_progress",
                            "requires_action",
                            "awaiting_approval",
                            "unresolved",
                            "waiting",
                        }
                        or (
                            status_name == "unknown"
                            and (
                                status_reason
                                not in {"", "legacy_missing_status"}
                                or preservation_class == "ephemeral"
                            )
                        )
                    )
                    terminal_result = bool(
                        status_name
                        in {
                            "success",
                            "error",
                            "failed",
                            "failure",
                            "timeout",
                            "timed_out",
                            "cancelled",
                        }
                        or (is_result and not status_name and not pending_result)
                    )
                    if cleaned_call_id:
                        tool_call_order += 1
                        if is_result and terminal_result:
                            completed_tool_call_ids.add(cleaned_call_id)
                        else:
                            issued_tool_call_ids[cleaned_call_id] = tool_call_order
                    _add_obligation(
                        obligations,
                        seen,
                        kind="tool_result_id",
                        value=call.get("id") or call.get("tool_use_id"),
                        source_role=role,
                        source_entry_id=source_entry_id,
                        max_obligations=max_obligations,
                    )
                    if is_result:
                        result_text = _string_value(
                            call.get("result") or call.get("content")
                        )
                        _add_obligation(
                            obligations,
                            seen,
                            kind="tool_result_fact",
                            value=result_text,
                            source_role=role,
                            source_entry_id=source_entry_id,
                            max_obligations=max_obligations,
                        )
                        if (
                            bool(call.get("is_error"))
                            or status_name
                            in {
                                "error",
                                "failed",
                                "failure",
                                "timeout",
                                "timed_out",
                                "cancelled",
                            }
                        ):
                            _add_obligation(
                                obligations,
                                seen,
                                kind="failed_command_or_error",
                                value=(
                                    result_text
                                    or f"{cleaned_call_id}: {status_name}"
                                ),
                                source_role=role,
                                source_entry_id=source_entry_id,
                                max_obligations=max_obligations,
                            )
                        for match in _COMMAND_RE.finditer(result_text):
                            _add_obligation(
                                obligations,
                                seen,
                                kind="command",
                                value=match.group(0).rstrip("."),
                                source_role=role,
                                source_entry_id=source_entry_id,
                                max_obligations=max_obligations,
                            )
                        for match in _PATH_RE.finditer(result_text):
                            _add_obligation(
                                obligations,
                                seen,
                                kind="file_path",
                                value=match.group(0).rstrip("."),
                                source_role=role,
                                source_entry_id=source_entry_id,
                                max_obligations=max_obligations,
                            )
                        if any(
                            marker in result_text.casefold()
                            for marker in _ERROR_MARKERS
                        ):
                            _add_obligation(
                                obligations,
                                seen,
                                kind="failed_command_or_error",
                                value=result_text,
                                source_role=role,
                                source_entry_id=source_entry_id,
                                max_obligations=max_obligations,
                            )

        for match in _IDENTIFIER_RE.finditer(content):
            _add_obligation(
                obligations,
                seen,
                kind="important_identifier",
                value=match.group(0).rstrip("."),
                source_role=role,
                source_entry_id=source_entry_id,
                max_obligations=max_obligations,
            )
        for match in _PATH_RE.finditer(content):
            _add_obligation(
                obligations,
                seen,
                kind="file_path",
                value=match.group(0).rstrip("."),
                source_role=role,
                source_entry_id=source_entry_id,
                max_obligations=max_obligations,
            )
        for match in _COMMAND_RE.finditer(content):
            _add_obligation(
                obligations,
                seen,
                kind="command",
                value=match.group(0).rstrip("."),
                source_role=role,
                source_entry_id=source_entry_id,
                max_obligations=max_obligations,
            )
    pending_ids = issued_tool_call_ids.keys() - completed_tool_call_ids
    for pending_id in sorted(
        pending_ids,
        key=lambda candidate_id: (issued_tool_call_ids[candidate_id], candidate_id),
    ):
        _add_obligation(
            obligations,
            seen,
            kind="pending_tool_or_approval_id",
            value=pending_id,
            source_role="assistant",
            source_entry_id=None,
            max_obligations=max_obligations,
        )
    return obligations.values()


def verify_summary_coverage(
    summary_text: str,
    obligations: Sequence[CompactionObligation],
    *,
    backfill_missing: bool = True,
    block_missing_critical: bool = False,
) -> CoverageResult:
    """Compare obligations with summary text without blocking by default."""

    search_text = summary_text.casefold()
    missing_obligations = [
        obligation for obligation in obligations if obligation.value.casefold() not in search_text
    ]
    missing = [_obligation_label(obligation) for obligation in missing_obligations]
    blocked = block_missing_critical and any(
        obligation.critical for obligation in missing_obligations
    )
    if not obligations:
        status = "unknown"
    elif blocked:
        status = "fail_blocked"
    elif not missing:
        status = "pass"
    elif backfill_missing:
        status = "pass_with_backfill"
    else:
        status = "fail_reported"
    carry_forward = missing[:_MAX_CRITICAL_CARRY_FORWARD] if backfill_missing or blocked else []
    return CoverageResult(
        status=status,
        checked_obligations=len(obligations),
        covered_obligations=len(obligations) - len(missing),
        missing_obligations=missing,
        critical_carry_forward=carry_forward,
        blocked=blocked,
    )


def build_structured_summary_from_text(
    summary_text: str,
    obligations: Sequence[CompactionObligation],
    *,
    block_missing_critical: bool = False,
) -> tuple[StructuredCompactionSummary, CoverageResult]:
    """Build portable structured state from existing summary text plus obligations."""

    initial_coverage = verify_summary_coverage(
        summary_text,
        obligations,
        backfill_missing=True,
        # Missing facts are materialized into the OpenSquilla-owned sidecar
        # below. Blocking against the model's prose here would reject the
        # very backfill that makes deterministic recovery safe.
        block_missing_critical=False,
    )
    first_by_kind: dict[str, str] = {}
    values_by_kind: dict[str, list[str]] = {}
    for obligation in obligations:
        first_by_kind.setdefault(obligation.kind, obligation.value)
        values_by_kind.setdefault(obligation.kind, []).append(obligation.value)

    summary = StructuredCompactionSummary(
        user_goal=first_by_kind.get("user_goal", ""),
        current_status=summary_text,
        next_action=first_by_kind.get("current_plan_or_next_action"),
        open_steps=values_by_kind.get("current_plan_or_next_action", []),
        files_and_artifacts=[{"path": value} for value in values_by_kind.get("file_path", [])]
        + [{"artifact": value} for value in values_by_kind.get("artifact_path_or_name", [])],
        tool_results_to_remember=[
            {"id": value} for value in values_by_kind.get("tool_result_id", [])
        ]
        + [{"fact": value} for value in values_by_kind.get("tool_result_fact", [])],
        known_failures=[
            {"detail": value} for value in values_by_kind.get("failed_command_or_error", [])
        ],
        executed_commands_and_tests=values_by_kind.get("command", []),
        pending_tool_and_approval_ids=values_by_kind.get(
            "pending_tool_or_approval_id",
            [],
        ),
        decisions_and_rationale=[
            {"detail": value} for value in values_by_kind.get("decision_or_rationale", [])
        ],
        important_identifiers=values_by_kind.get("important_identifier", []),
        constraints_and_preferences=values_by_kind.get("user_constraint_or_preference", []),
        do_not_repeat=values_by_kind.get("do_not_repeat_action", []),
        unresolved_questions=values_by_kind.get("unresolved_question", []),
        critical_carry_forward=initial_coverage.critical_carry_forward,
        source_coverage={
            "status": initial_coverage.status,
            "checked_obligations": initial_coverage.checked_obligations,
            "covered_obligations": initial_coverage.covered_obligations,
        },
    )
    coverage = initial_coverage
    if block_missing_critical:
        # The durable artifact is the rendered structured checkpoint, not the
        # free-form model prose. Re-check after deterministic sidecar backfill;
        # only obligations still absent from the actual replay may block.
        coverage = verify_summary_coverage(
            render_structured_summary(summary),
            obligations,
            backfill_missing=False,
            block_missing_critical=True,
        )
        summary.source_coverage = {
            "status": coverage.status,
            "checked_obligations": coverage.checked_obligations,
            "covered_obligations": coverage.covered_obligations,
        }
    return summary, coverage


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _append_scalar_section(lines: list[str], title: str, value: Any) -> None:
    text = _string_value(value)
    if not text:
        return
    lines.append(f"{title}:")
    lines.append(text)
    lines.append("")


def _append_list_section(lines: list[str], title: str, values: Sequence[Any]) -> None:
    rendered = [_string_value(value) for value in values]
    rendered = [value for value in rendered if value]
    if not rendered:
        return
    lines.append(f"{title}:")
    lines.extend(f"- {value}" for value in rendered)
    lines.append("")


def _append_mapping_list_section(
    lines: list[str],
    title: str,
    values: Sequence[Mapping[str, Any]],
) -> None:
    items: list[list[tuple[str, str]]] = []
    for value in values:
        pairs = [
            (str(key), _string_value(raw_value))
            for key, raw_value in value.items()
            if _string_value(raw_value)
        ]
        if pairs:
            items.append(pairs)
    if not items:
        return

    lines.append(f"{title}:")
    for pairs in items:
        first_key, first_value = pairs[0]
        lines.append(f"- {first_key}: {first_value}")
        for key, rendered_value in pairs[1:]:
            lines.append(f"  {key}: {rendered_value}")
    lines.append("")


def render_structured_summary(summary: StructuredCompactionSummary | Mapping[str, Any]) -> str:
    """Render structured compaction state as stable model-readable text."""

    if isinstance(summary, Mapping):
        summary = StructuredCompactionSummary.model_validate(summary)

    lines: list[str] = ["[Structured Compaction Summary]", ""]
    _append_scalar_section(lines, "Goal", summary.user_goal)
    _append_scalar_section(lines, "Current Status", summary.current_status)
    _append_scalar_section(lines, "Next Action", summary.next_action)
    _append_list_section(lines, "Completed Steps", summary.completed_steps)
    _append_list_section(lines, "Open Steps", summary.open_steps)
    _append_mapping_list_section(lines, "Files and Artifacts", summary.files_and_artifacts)
    _append_mapping_list_section(
        lines,
        "Tool Results To Remember",
        summary.tool_results_to_remember,
    )
    _append_mapping_list_section(
        lines,
        "Decisions and Rationale",
        summary.decisions_and_rationale,
    )
    _append_mapping_list_section(lines, "Known Failures", summary.known_failures)
    _append_list_section(
        lines,
        "Executed Commands and Tests",
        summary.executed_commands_and_tests,
    )
    _append_list_section(
        lines,
        "Pending Tool and Approval IDs",
        summary.pending_tool_and_approval_ids,
    )
    _append_list_section(lines, "Important Identifiers", summary.important_identifiers)
    _append_list_section(
        lines,
        "Constraints and Preferences",
        summary.constraints_and_preferences,
    )
    _append_list_section(lines, "Do Not Repeat", summary.do_not_repeat)
    _append_list_section(lines, "Unresolved Questions", summary.unresolved_questions)
    _append_list_section(lines, "Critical Carry Forward", summary.critical_carry_forward)

    return "\n".join(lines).rstrip() + "\n"
