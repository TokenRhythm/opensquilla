"""Structured compaction state helpers.

This module defines OpenSquilla-owned portable state. Provider-native
compaction blocks and cached-content references should live in provider context
state, not in this structured summary payload.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
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
_OBLIGATION_KIND_PRIORITY = {
    "user_goal": 0,
    "user_constraint_or_preference": 1,
    "current_plan_or_next_action": 2,
    "do_not_repeat_action": 3,
    "failed_command_or_error": 4,
    "unresolved_question": 5,
    "decision_or_rationale": 6,
    "tool_result_fact": 7,
    "tool_result_id": 8,
    "file_path": 9,
    "artifact_path_or_name": 10,
    "command": 11,
    "important_identifier": 12,
}


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


def _add_obligation(
    obligations: list[CompactionObligation],
    seen: set[tuple[str, str]],
    *,
    kind: str,
    value: Any,
    source_role: str | None,
    source_entry_id: int | None,
    max_obligations: int,
) -> None:
    # The final cap is global and priority-aware. Keep at most one full cap per
    # kind while collecting so adversarial path/id-heavy input cannot make the
    # candidate set grow without bound.
    if sum(item.kind == kind for item in obligations) >= max_obligations:
        return
    cleaned = _clean_obligation_text(value)
    if not cleaned:
        return
    key = (kind, cleaned.casefold())
    if key in seen:
        return
    seen.add(key)
    obligations.append(
        CompactionObligation(
            kind=kind,
            value=cleaned,
            source_role=source_role,
            source_entry_id=source_entry_id,
        )
    )


def extract_compaction_obligations(
    entries: Sequence[Any],
    *,
    max_obligations: int = 64,
) -> list[CompactionObligation]:
    """Extract bounded high-signal continuity facts before entries are removed."""

    if max_obligations <= 0:
        return []

    obligations: list[CompactionObligation] = []
    seen: set[tuple[str, str]] = set()
    # Inspect newest entries first so duplicate facts keep their most recent
    # provenance.  Apply the hard cap only after extraction; otherwise early
    # path/id noise can consume the quota before a later goal or constraint.
    for entry in reversed(entries):
        role = _string_value(_entry_value(entry, "role")) or None
        entry_id = _entry_value(entry, "id")
        source_entry_id = entry_id if isinstance(entry_id, int) else None
        content = _string_value(_entry_value(entry, "content"))

        tool_call_id = _entry_value(entry, "tool_call_id")
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
                    _add_obligation(
                        obligations,
                        seen,
                        kind="tool_result_id",
                        value=call.get("id") or call.get("tool_use_id"),
                        source_role=role,
                        source_entry_id=source_entry_id,
                        max_obligations=max_obligations,
                    )
                    if call.get("type") == "tool_result" or "result" in call:
                        _add_obligation(
                            obligations,
                            seen,
                            kind="tool_result_fact",
                            value=call.get("result") or call.get("content"),
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
    obligations.sort(key=lambda item: _OBLIGATION_KIND_PRIORITY.get(item.kind, 100))
    return obligations[:max_obligations]


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

    coverage = verify_summary_coverage(
        summary_text,
        obligations,
        backfill_missing=True,
        block_missing_critical=block_missing_critical,
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
        files_and_artifacts=[{"path": value} for value in values_by_kind.get("file_path", [])]
        + [{"artifact": value} for value in values_by_kind.get("artifact_path_or_name", [])],
        tool_results_to_remember=[
            {"id": value} for value in values_by_kind.get("tool_result_id", [])
        ]
        + [{"fact": value} for value in values_by_kind.get("tool_result_fact", [])],
        known_failures=[
            {"detail": value} for value in values_by_kind.get("failed_command_or_error", [])
        ],
        decisions_and_rationale=[
            {"detail": value} for value in values_by_kind.get("decision_or_rationale", [])
        ],
        important_identifiers=values_by_kind.get("important_identifier", []),
        constraints_and_preferences=values_by_kind.get("user_constraint_or_preference", []),
        do_not_repeat=values_by_kind.get("do_not_repeat_action", []),
        unresolved_questions=values_by_kind.get("unresolved_question", []),
        critical_carry_forward=coverage.critical_carry_forward,
        source_coverage={
            "status": coverage.status,
            "checked_obligations": coverage.checked_obligations,
            "covered_obligations": coverage.covered_obligations,
        },
    )
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
