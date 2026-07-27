"""Agent core — explicit state machine + tool loop.

Core loop is under 500 lines. No recursive calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import structlog

from opensquilla.artifacts import artifact_payload
from opensquilla.context_budget import ContextBudgetClass, ContextBudgetGovernor
from opensquilla.engine.agent_injection import PendingInputProvider
from opensquilla.engine.cache_break_monitor import (
    check_response_for_cache_break,
    notify_compaction,
    record_prompt_state,
)
from opensquilla.engine.elevation_triage import RuleAssessment, local_elevation_assessment
from opensquilla.engine.fallback import FallbackPolicy, backoff_sleep
from opensquilla.engine.final_diff_contract import (
    FinalDiffContractObservation,
    build_final_diff_contract_observation,
    final_diff_contract_recovery_message,
)
from opensquilla.engine.finalize_evidence_gate import (
    EXECUTION_TOOL_NAMES as _GATE_EXECUTION_TOOL_NAMES,
)
from opensquilla.engine.finalize_evidence_gate import (
    FINALIZE_EVIDENCE_GATE_CHALLENGE_LIMIT,
    FinalizeEvidenceTracker,
    execution_signals_from_result,
    finalize_evidence_challenge_message,
    finalize_evidence_gate_key,
    is_repro_script_path,
)
from opensquilla.engine.finalize_evidence_gate import (
    WRITE_TOOL_NAMES as _GATE_WRITE_TOOL_NAMES,
)
from opensquilla.engine.history import limit_turns, repair_tool_pairing
from opensquilla.engine.patch_evidence_ledger import PatchEvidenceLedger
from opensquilla.engine.post_write_convergence import (
    PostWriteConvergenceDecision,
    PostWriteConvergenceObservation,
    PostWriteConvergenceTracker,
)
from opensquilla.engine.progress_watchdog import ProgressObservation, ProgressWatchdog
from opensquilla.engine.runtime_diagnostics import RuntimeDiagnosticsObserver
from opensquilla.engine.runtime_events import append_runtime_event
from opensquilla.engine.runtime_recovery import (
    RuntimeRecoveryDecision,
    RuntimeRecoveryMode,
    post_tool_empty_decision,
    reasoning_continuation_decision,
    reasoning_prefill_decision,
    source_loop_recovery_decision,
    supports_reasoning_prefill_replay,
)
from opensquilla.engine.runtime_state_capsule import (
    build_runtime_state_capsule,
    runtime_state_capsule_message,
)
from opensquilla.engine.session_sanitize import (
    SessionSanitizeResult,
    project_historical_tool_payloads,
    sanitize_session_messages,
    session_payload_chars,
)
from opensquilla.engine.submit_review import (
    SubmitAction,
    SubmitReviewState,
    build_submit_review_message,
    evaluate_explicit_submit,
)
from opensquilla.engine.submit_review import (
    confirmation_message as submit_review_confirmation_message,
)
from opensquilla.engine.submit_review import (
    diff_is_truncated as submit_review_diff_is_truncated,
)
from opensquilla.engine.submit_review import (
    empty_diff_note as submit_review_empty_diff_note,
)
from opensquilla.engine.submit_review import (
    nudge_message as submit_review_nudge_message,
)
from opensquilla.engine.submit_review import (
    observe_tool_activity as submit_review_observe_tool_activity,
)
from opensquilla.engine.submit_review import (
    should_fire_implicit as submit_review_should_fire_implicit,
)
from opensquilla.engine.thinking import drop_reasoning
from opensquilla.engine.tokenjuice_adapter import reduce_tool_result_with_tokenjuice
from opensquilla.engine.tool_result_store import (
    TOOL_RESULT_META_NAME,
    ToolResultRecord,
    ToolResultStore,
    ToolResultStoreBudgetError,
)
from opensquilla.engine.tool_token_estimate import estimate_tokens as get_approx_tokens
from opensquilla.engine.usage import model_usage_cost_fields
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageCallStart,
    UsageEventSink,
    UsageExecutionContext,
    bind_usage_accounting_scope,
    current_usage_accounting_scope,
    has_known_provider_usage_receipt,
    normalize_provider_usage,
    provider_accounts_physical_usage,
    start_usage_call,
)
from opensquilla.execution_status import (
    mark_execution_status_truncated,
    runtime_execution_status,
)
from opensquilla.observability.turn_call_log import TurnCallLogger
from opensquilla.provider import (
    ChatConfig,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolResult,
    ContentBlockToolUse,
    LLMProvider,
    Message,
    ProviderHeartbeatEvent,
    ToolDefinition,
    ToolUseEndEvent,
)
from opensquilla.provider import (
    DoneEvent as ProviderDoneEvent,
)
from opensquilla.provider import (
    ErrorEvent as ProviderErrorEvent,
)
from opensquilla.provider import (
    ReasoningDeltaEvent as ProviderReasoningDelta,
)
from opensquilla.provider import (
    TextDeltaEvent as ProviderTextDelta,
)
from opensquilla.provider import (
    ToolUseDeltaEvent as ProviderToolUseDelta,
)
from opensquilla.provider import (
    ToolUseStartEvent as ProviderToolUseStart,
)
from opensquilla.provider.correlation_context import bind_provider_request_correlation
from opensquilla.provider.failures import ProviderFailureKind, classify_provider_error
from opensquilla.provider.protocol import (
    project_provider_message_count,
    validate_provider_chat_request,
)
from opensquilla.provider.types import (
    ContentBlockImage,
    FailureInjector,
    ProviderMessageCountProjection,
    ProviderMessageLimitProof,
    ProviderRequestCorrelation,
    derive_provider_request_correlation,
)
from opensquilla.provider.types import (
    EnsembleProgressEvent as ProviderEnsembleProgressEvent,
)
from opensquilla.result_budget import (
    ToolResultBudgetClass,
    ToolResultBudgetPolicy,
    compact_tool_result_content,
    exec_command_invokes_git_diff,
    exec_command_invokes_source_context_read,
    resolve_budget_class,
)
from opensquilla.router_control import router_control_replay_event_from_payload
from opensquilla.safety.secret_redaction import redact_secret_value
from opensquilla.sandbox.approval_runtime import ApprovalAction, SuspendedToolRequest
from opensquilla.sandbox.elevation import ElevationAction
from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    build_compaction_config_from_provider,
    compact_context,
)
from opensquilla.session.compaction_lifecycle import (
    COMPACTION_CHUNK_SUMMARIZED_EVENT,
    COMPACTION_SUMMARY_VERIFIED_EVENT,
    COMPACTION_TRIGGERED_EVENT,
    compaction_effect_payload,
    compaction_lifecycle_payload,
    compaction_result_payload,
    flush_receipt_allows_destructive_compaction,
    flush_receipt_is_successful_flush,
    flush_trigger_enabled,
    new_compaction_id,
    pre_compaction_flush_requires_safe_receipt,
)
from opensquilla.session.terminal_reply import build_terminal_reply
from opensquilla.tool_boundary import AgentToolHandler as ToolHandler
from opensquilla.tools.patch_classification import is_instrumentation_only_patch
from opensquilla.tools.projected_arguments import find_projected_tool_argument
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext, current_tool_context
from opensquilla.tools.write_policy import match_workspace_write_deny
from opensquilla.tools.write_tracking import classify_workspace_path
from opensquilla.usage_reasons import (
    normalize_usage_unknown_reason,
    provider_error_usage_reason,
)

from .context import ContextAssembly
from .subagent import SubagentManager, SubagentSpec
from .types import (
    _THINKING_BUDGET_DEFAULT,
    AgentConfig,
    AgentEvent,
    AgentState,
    ArtifactEvent,
    CompactionEvent,
    CompactionOutcome,
    DoneEvent,
    EnsembleProgressEvent,
    ErrorEvent,
    RunHeartbeatEvent,
    StateChangeEvent,
    TextDeltaEvent,
    ThinkingEvent,
    ThinkingLevel,
    ToolCall,
    ToolResult,
    ToolResultEvent,
    ToolUseDeltaEvent,
    ToolUseStartEvent,
    WarningEvent,
)

logger = structlog.get_logger("opensquilla.engine.agent")

_TURN_OBJECTIVE_REMINDER_MAX_CHARS = 2000

_TURN_OBJECTIVE_REMINDER_ENV = "OPENSQUILLA_TURN_OBJECTIVE_REMINDER"
_TURN_OBJECTIVE_REMINDER_ON = {"on", "1", "true", "yes"}
_TURN_OBJECTIVE_REMINDER_OFF = {"off", "0", "false", "no"}
_TURN_OBJECTIVE_REMINDER_TRIM_PREFIX = "trim:"


def _resolve_turn_objective_reminder() -> tuple[bool, int]:
    """Resolve the turn-objective reminder override.

    ``OPENSQUILLA_TURN_OBJECTIVE_REMINDER`` accepts "on"/"off" or
    "trim:<chars>" (a positive integer replacing the default truncation cap).
    Unset or "off" suppresses the per-turn "[Current user request reminder]"
    message; "on" restores it with the shipped truncation cap.
    Unrecognized values raise instead of being silently ignored so a run
    manifest cannot record an override the run did not actually apply.
    """
    env_value = os.environ.get(_TURN_OBJECTIVE_REMINDER_ENV, "").strip().lower()
    if not env_value or env_value in _TURN_OBJECTIVE_REMINDER_OFF:
        return False, _TURN_OBJECTIVE_REMINDER_MAX_CHARS
    if env_value in _TURN_OBJECTIVE_REMINDER_ON:
        return True, _TURN_OBJECTIVE_REMINDER_MAX_CHARS
    if env_value.startswith(_TURN_OBJECTIVE_REMINDER_TRIM_PREFIX):
        raw_chars = env_value[len(_TURN_OBJECTIVE_REMINDER_TRIM_PREFIX) :]
        if raw_chars.isdigit() and int(raw_chars) > 0:
            return True, int(raw_chars)
    raise ValueError(
        f"{_TURN_OBJECTIVE_REMINDER_ENV} must be one of: "
        + ", ".join(sorted(_TURN_OBJECTIVE_REMINDER_ON | _TURN_OBJECTIVE_REMINDER_OFF))
        + ", or trim:<positive integer>"
    )

_PROVIDER_OUTPUT_TRUNCATED_REPLY = build_terminal_reply(
    {
        "status": "failed",
        "terminal_reason": "output_truncated",
        "error_class": "provider_output_truncated",
        "error_message": "Provider output limit reached before completion",
    }
)
_PROVIDER_OUTPUT_CONTINUE_PROMPT = (
    "The previous provider response reached its output limit before the task finished. "
    "Continue from the exact point where it stopped. Do not repeat text that has already "
    "been written. If a tool call was interrupted or incomplete, regenerate a complete "
    "tool call from scratch."
)
_TEXT_ONLY_TOOL_RECOVERY_LIMIT = 2
_TEXT_ONLY_TOOL_RECOVERY_MESSAGE = (
    "[Runtime recovery]\n"
    "Previous assistant turn had text only and no tool calls. If the task still "
    "requires repo inspection, editing, or verification, call the appropriate tool "
    "now; if complete, answer briefly."
)

_SOURCE_CONTEXT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "glob_search",
        "grep_search",
        "list_dir",
        "read_file",
        "git_diff",
        "git_log",
        "git_show",
        "git_status",
    }
)
_REPEATED_TOOL_CALL_RECOVERY_TOOL_NAMES: frozenset[str] = frozenset(
    {"exec_command", "glob_search", "grep_search", "list_dir"}
)
_EXECUTION_TOOL_NAMES: frozenset[str] = frozenset(
    {"background_process", "exec_command", "execute_code"}
)
_FOCUSED_VERIFICATION_MARKERS: tuple[str, ...] = (
    "pytest",
    " unittest",
    "python -m unittest",
    "ruff check",
    "cargo test",
    "cargo build",
    "cargo check",
    "go test",
    "npm test",
    "pnpm test",
    "yarn test",
    "mvn test",
    "gradle test",
    "ctest",
    "rspec",
    "tox",
    " make check",
    " make test",
    " run-tests.py",
    " ./run-tests.py",
    " tests/jqtest",
)
_CLEAN_TEST_SUMMARY_RE = re.compile(
    r"\btests run:\s*\d+,\s*failures:\s*0,\s*errors:\s*0"
    r"(?:,\s*skipped:\s*\d+)?\b",
    re.IGNORECASE,
)
_CLEAN_PASSED_FAILED_SUMMARY_RE = re.compile(
    r"\b\d+\s+passed\b[^\n\r;]*(?:;|,)?[^\n\r]*\b0\s+failed\b",
    re.IGNORECASE,
)
_PLAIN_PASSED_SUMMARY_RE = re.compile(r"\b\d+\s+passed\b", re.IGNORECASE)
_CLEAN_ERROR_COUNT_RE = re.compile(r"\b0\s+error\(s\)(?:\W|$)", re.IGNORECASE)
_FAILED_FINALIZATION_RECOVERY_LIMIT = 3
_PATCH_HYGIENE_BLOCK_CHALLENGE_LIMIT = 2
# Scratch verify-mirror (OPENSQUILLA_SCRATCH_VERIFY_MIRROR): directory name
# under the scratch dir, and the fail-closed cap on mirror files the hash
# guard will inspect per execution.
_VERIFY_MIRROR_DIR_NAME = "verify-mirror"
_VERIFY_MIRROR_MAX_FILES = 200


def _patch_hygiene_block_key(offending_paths: list[str]) -> str:
    """Dedup key: the same set of offending paths never re-fires."""

    encoded = json.dumps(sorted(offending_paths), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _patch_hygiene_block_message(test_paths: list[str]) -> str:
    rendered = ", ".join(test_paths[:5])
    if len(test_paths) > 5:
        rendered += f" (and {len(test_paths) - 5} more)"
    return (
        "[Patch hygiene check]\n"
        "You are about to finish, but the workspace diff still changes test "
        f"files: {rendered}. The final change must live in the project source; "
        "the repository's test suite is managed separately and modifications "
        "to it do not count as part of the fix. Do not finalize yet. Revert "
        "the listed test-file changes (restore modified or deleted test files "
        "to their original content and remove newly added ones) so the diff "
        "contains only non-test changes. If editing a test was your only "
        "change, implement the actual fix in the source code instead. Keeping "
        "a copy of any reproduction script under the scratch directory is "
        "fine; test directories are not."
    )


def _patch_hygiene_block_protected_message(protected_paths: list[str]) -> str:
    rendered = ", ".join(protected_paths[:5])
    if len(protected_paths) > 5:
        rendered += f" (and {len(protected_paths) - 5} more)"
    return (
        "[Patch hygiene check]\n"
        "You are about to finish, but the workspace diff still changes files "
        f"that this deployment's write policy protects: {rendered}. Protected "
        "paths must stay unchanged in the final diff; edits to them do not "
        "count as part of the fix. Do not finalize yet. Revert the listed "
        "changes (restore modified or deleted files to their original content "
        "and remove newly added ones) so the diff no longer touches protected "
        "paths. If a protected file was the only thing you changed, implement "
        "the actual fix in unprotected project source instead. Keeping copies "
        "or new files under the scratch directory is fine."
    )


def _finalize_variant_challenge_message() -> str:
    """Uniform one-shot variant-sweep challenge (finalize_variant_challenge).

    Same text for every task and every fire: the wording names failure
    classes in the abstract (alternate spellings of a construct, boundary
    values, sibling shapes handled by the same logic, and reworks that
    change contracts callers observe) and never any task-specific content.
    """

    return (
        "[Variant sweep check]\n"
        "Before you finish: enumerate the distinct input or construct classes "
        "that can reach the code paths you changed (for example alternate "
        "syntaxes or spellings of the same construct, boundary or edge-case "
        "values, and sibling types or code shapes handled by the same logic). "
        "Then run your verification against each class you listed, not only "
        "the case from the task description. If any class fails, fix your "
        "change and re-run until green. If this leads you to rework your "
        "approach, preserve the behavior contracts callers can observe "
        "unless the task itself asks to change them: the types of raised or "
        "propagated errors, public signatures and return types, and output "
        "formats. If every class passes, finish and briefly note which "
        "classes you checked."
    )


_CODE_CHANGE_TASK_MARKERS: tuple[str, ...] = (
    "bug",
    "fix",
    "failing",
    "failure",
    "implement",
    "patch",
    "traceback",
    "error",
    "exception",
    "regression",
    "test",
)
_NO_CHANGE_FINAL_MARKERS: tuple[str, ...] = (
    "no code change",
    "no file change",
    "no changes are required",
    "no changes needed",
    "diff should remain empty",
    "repository diff should remain empty",
)
_ROOT_SCRATCH_ARTIFACT_NAMES: frozenset[str] = frozenset(
    {
        "actual.json",
        "bug.py",
        "bug_test.py",
        "check.py",
        "data.json",
        "debug.py",
        "expected.json",
        "input.json",
        "fix.patch",
        "minimal.py",
        "minimal_bug.py",
        "output.json",
        "repro.json",
        "repro.py",
        "reproduction.py",
        "sample.json",
        "sample2.json",
        "scratch.py",
        "test_case.py",
        "test_issue.py",
        "tmp.py",
        "verify.py",
        "works.py",
    }
)
_ROOT_SCRATCH_ARTIFACT_PREFIXES: tuple[str, ...] = (
    "actual_",
    "data_",
    "debug_",
    "expected_",
    "input_",
    "minimal_",
    "output_",
    "repro_",
    "sample_",
    "scratch_",
    "test_",
    "tmp_",
    "verify_",
)
_ROOT_SCRATCH_ARTIFACT_SUFFIXES: frozenset[str] = frozenset(
    {".json", ".js", ".log", ".out", ".py", ".sh", ".ts", ".txt"}
)
_SUSPICIOUS_NEW_WORKSPACE_WRITE_PREFIXES: tuple[str, ...] = (
    "debug_marker",
    "guard_unlock",
    "runtime_guard",
    "temp_marker",
)
_SUSPICIOUS_NEW_WORKSPACE_WRITE_CONTENT_MARKERS: tuple[str, ...] = (
    "debug marker",
    "guard unlock",
    "placeholder for runtime guard",
    "runtime guard unlock",
    "satisfy the runtime guard",
    "temp marker",
)
_NO_WORKSPACE_WRITE_REASONS: frozenset[str] = frozenset(
    {
        "source_context_without_workspace_write",
        "source_context_exploration_without_workspace_write",
        "repeated_failure_anchor_without_workspace_write",
        "tool_activity_without_workspace_write",
    }
)
_WORKSPACE_EDIT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "apply_patch",
        "edit_file",
        "write_file",
    }
)
_DIAGNOSTIC_RETRIEVAL_GATED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        *_WORKSPACE_EDIT_TOOL_NAMES,
        "finalize",
    }
)

_meta_invoke_depth: ContextVar[int] = ContextVar("opensquilla_meta_invoke_depth", default=0)
_meta_invoke_turn_count: ContextVar[int] = ContextVar(
    "opensquilla_meta_invoke_turn_count", default=0
)


def _normalize_workspace_relative_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _progress_watchdog_guidance_message(reason: str, details: Mapping[str, Any]) -> str:
    no_workspace_write_reason = reason in _NO_WORKSPACE_WRITE_REASONS
    if reason == "repeated_provider_failure":
        signal = "repeated provider failures"
    elif reason == "repeated_tool_error":
        signal = "repeated tool errors"
    elif reason == "repeated_failure_anchor_without_workspace_write":
        signal = "the same failure anchor repeating without a new workspace edit"
    elif reason in {
        "source_context_without_workspace_write",
        "source_context_exploration_without_workspace_write",
        "source_context_after_workspace_write",
    }:
        signal = (
            "source-context exploration continuing after repository edits"
            if reason == "source_context_after_workspace_write"
            else "source-context exploration continuing without clear patch progress"
        )
    elif reason == "tool_activity_without_workspace_write":
        signal = "tool activity continuing without a real workspace edit"
    elif reason == "verified_workspace_diff_continued_tool_activity":
        signal = "continued tool activity after a workspace diff and focused verification"
    else:
        signal = "repeated no-progress activity"

    count = details.get("count")
    count_text = f" Count: {count}." if isinstance(count, int) and count > 0 else ""
    workspace_change_likely_required = bool(
        details.get("workspace_change_likely_required")
    )
    failure_summary = str(details.get("failure_anchor_summary") or "").strip()
    if len(failure_summary) > 700:
        failure_summary = failure_summary[:697].rstrip() + "..."
    failure_text = f" Recent failure anchor(s): {failure_summary}." if failure_summary else ""
    if no_workspace_write_reason and workspace_change_likely_required:
        next_step_text = (
            "This task appears to require a repository patch, but no tracked "
            "workspace source file has been changed yet. Avoid repeating broad "
            "exploration or writing more scratch notes. If the exact edit is not "
            "localized yet, use targeted source reads/searches; once localized, use "
            "an available source-edit tool on the real project source file allowed "
            "by the workspace write policy, then run one focused validation command."
        )
    elif reason in {
        "source_context_after_workspace_write",
        "verified_workspace_diff_continued_tool_activity",
    }:
        if isinstance(count, int) and count >= 6:
            next_step_text = (
                "You already have repository edits and have received this warning "
                "again. Do not call read_file, grep_search, glob_search, list_dir, "
                "or write more scratch files next. Use the current context: make a "
                "source edit, run one focused validation command, or finalize if "
                "validation is clean."
            )
        else:
            next_step_text = (
                "You already have repository edits. Stop broad source exploration. "
                "Use the current diff and latest verification result: either fix the "
                "patch, run one focused validation command, or finalize if validation "
                "is clean."
            )
    else:
        next_step_text = (
            "Do not repeat the same action unchanged. Change approach, inspect the "
            "current workspace diff and the latest failure signal, make the smallest "
            "justified source edit if one is available, or explain the concrete blocker."
        )
    return (
        "[Runtime progress warning]\n"
        f"The runtime observed {signal}.{count_text}{failure_text} "
        f"{next_step_text}"
    )


def _post_write_convergence_message(
    decision: PostWriteConvergenceDecision,
) -> str:
    details = decision.details
    stable_count = details.get("stable_count")
    count_text = (
        f" for {stable_count} post-verification tool turn(s)"
        if isinstance(stable_count, int) and stable_count > 0
        else ""
    )
    paths = details.get("diff_paths")
    if isinstance(paths, list) and paths:
        path_text = ", ".join(str(path) for path in paths[:5])
        if len(paths) > 5:
            path_text += ", ..."
        path_text = f" Current diff paths: {path_text}."
    else:
        path_text = ""
    if decision.action == "finalize":
        next_step = (
            "Do not call tools. Provide the final answer from the current patch and "
            "latest clean validation result. Only mention a blocker if the current "
            "diff is known to be incomplete."
        )
    else:
        next_step = (
            "Stop broad source exploration. Use the current diff and latest clean "
            "validation result: finalize if the patch is ready, or make one small "
            "source edit only if the validation evidence requires it."
        )
    return (
        "[Runtime post-write convergence]\n"
        f"The current diff has stayed unchanged{count_text} after a successful "
        f"focused validation.{path_text} {next_step}"
    )


def _cost_source_for_usage(
    cost_usd: float,
    billed_cost: float,
    explicit_source: str | None = None,
) -> str:
    source = str(explicit_source or "").strip().lower()
    if source in {"provider_billed", "mixed", "opensquilla_estimate", "unavailable"}:
        return source
    if billed_cost > 0.0 and abs(cost_usd - billed_cost) <= 1e-9:
        return "provider_billed"
    if billed_cost > 0.0:
        return "mixed"
    if cost_usd > 0.0:
        return "opensquilla_estimate"
    return "unavailable"


def _usage_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _usage_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _model_usage_row_cost_source(sources: list[str], *, cost_usd: float) -> str:
    meaningful = [source for source in sources if source not in {"", "none"}]
    if not meaningful:
        return "unavailable" if cost_usd <= 0.0 else "opensquilla_estimate"
    unique = set(meaningful)
    if unique == {"provider_billed"}:
        return "provider_billed"
    if unique <= {"opensquilla_estimate", "unavailable"}:
        return "opensquilla_estimate" if cost_usd > 0.0 else "unavailable"
    return "mixed"


def _with_model_usage_cost_fields(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        model_id = str(item.get("model") or "")
        if model_id:
            cache_read = (
                item.get("cache_read_tokens")
                if "cache_read_tokens" in item
                else item.get("cached_tokens")
            )
            item.update(
                model_usage_cost_fields(
                    model_id=model_id,
                    provider=str(item.get("provider") or ""),
                    input_tokens=_usage_int(item.get("input_tokens") or item.get("inputTokens")),
                    output_tokens=_usage_int(
                        item.get("output_tokens") or item.get("outputTokens")
                    ),
                    billed_cost=_usage_float(
                        item.get("billed_cost")
                        or item.get("billedCost")
                        or item.get("billed_cost_usd")
                        or item.get("billedCostUsd")
                    ),
                    # Unbilled rows must be priced with their own cache counts,
                    # not cache-blind — otherwise the legacy-inference path in
                    # model_usage_cost_fields treats every cache token as fresh
                    # input while still labeling the estimate "cache_aware".
                    cache_read_tokens=_usage_int(cache_read or 0),
                    cache_write_tokens=_usage_int(item.get("cache_write_tokens") or 0),
                    has_billed_receipt=(
                        True
                        if str(
                            item.get("cost_source") or item.get("costSource") or ""
                        ).strip().lower()
                        in {"provider_billed", "openrouter_usage"}
                        else None
                    ),
                )
            )
        enriched.append(item)
    return enriched


def _summarize_model_usage_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregated: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    sources_by_key: dict[tuple[str, str, str, str], list[str]] = {}
    for row in _with_model_usage_cost_fields(rows):
        model_id = str(row.get("model") or "").strip()
        if not model_id:
            continue
        role = str(row.get("role") or "").strip() or "member"
        label = str(row.get("label") or role).strip() or role
        provider = str(row.get("provider") or "").strip()
        key = (role, label, provider, model_id)
        if key not in aggregated:
            aggregated[key] = {
                "role": role,
                "profile": row.get("profile"),
                "label": label,
                "provider": provider,
                "model": model_id,
                "sample_index": row.get("sample_index", 0),
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "billed_cost": 0.0,
                "cost_usd": 0.0,
                "billed_cost_usd": 0.0,
                "estimated_cost_usd": 0.0,
                "request_count": 0,
            }
            sources_by_key[key] = []
        target = aggregated[key]
        for usage_field in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "cache_write_tokens",
        ):
            target[usage_field] += _usage_int(
                row.get(usage_field) or row.get(_camel_usage_key(usage_field))
            )
        target["billed_cost"] += _usage_float(row.get("billed_cost") or row.get("billedCost"))
        target["cost_usd"] += _usage_float(row.get("cost_usd") or row.get("costUsd"))
        target["billed_cost_usd"] += _usage_float(
            row.get("billed_cost_usd") or row.get("billedCostUsd")
        )
        target["estimated_cost_usd"] += _usage_float(
            row.get("estimated_cost_usd") or row.get("estimatedCostUsd")
        )
        target["request_count"] += max(1, _usage_int(row.get("request_count") or 1))
        sources_by_key[key].append(str(row.get("cost_source") or row.get("costSource") or "none"))

    summarized: list[dict[str, Any]] = []
    for key, row in aggregated.items():
        row["cost_usd"] = round(float(row["cost_usd"] or 0.0), 6)
        row["billed_cost"] = round(float(row["billed_cost"] or 0.0), 6)
        row["billed_cost_usd"] = round(float(row["billed_cost_usd"] or 0.0), 6)
        row["estimated_cost_usd"] = round(float(row["estimated_cost_usd"] or 0.0), 6)
        row["cost_source"] = _model_usage_row_cost_source(
            sources_by_key.get(key, []),
            cost_usd=float(row["cost_usd"] or 0.0),
        )
        row["costUsd"] = row["cost_usd"]
        row["billedCostUsd"] = row["billed_cost_usd"]
        row["estimatedCostUsd"] = row["estimated_cost_usd"]
        row["costSource"] = row["cost_source"]
        summarized.append(row)
    return summarized


def _camel_usage_key(field: str) -> str:
    parts = field.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


MAX_META_INVOKE_DEPTH = 3
MAX_META_INVOKE_PER_TURN = 8


def _meta_empty_final_text_fallback(skill_name: str, inputs: Mapping[str, Any]) -> str:
    language = str(inputs.get("user_language") or "").lower()
    instruction = str(inputs.get("language_instruction") or "").lower()
    if language.startswith("en") or (not language and "english" in instruction):
        return (
            f"Meta skill `{skill_name}` completed, but this run did not produce "
            "a user-visible final answer. Review the step results above, or "
            "rerun with more specific output requirements if needed."
        )
    return (
        f"Meta skill `{skill_name}` 已完成，但这次流程没有生成可展示的最终回答。"
        "请查看上方步骤结果和产物；如果需要，可以补充更明确的输出要求后重新运行。"
    )


def _is_deepseek_model_id(model_id: str | None) -> bool:
    normalized = (model_id or "").strip().lower()
    return normalized.startswith("deepseek") or "/deepseek" in normalized


def _is_direct_deepseek_v4_model_id(model_id: str | None) -> bool:
    normalized = (model_id or "").strip().lower()
    return normalized in {"deepseek-v4-flash", "deepseek-v4-pro"}


_LARGE_JSON_TOOL_FIELD_KEYS: frozenset[str] = frozenset({"body", "body_base64"})
_LARGE_JSON_TOOL_FIELD_CHARS = 20_000
_TOOL_ARGUMENT_PROJECTION_PREFIX = "[tool_use_argument_projection]\n"
_HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX = "[historical_tool_argument_omitted]\n"
_INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX = "[invalid_provider_context_projection:"
_INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY = "_invalid_provider_context_arguments"
_AGGREGATE_TOOL_RESULT_MAX_SHARE = 0.25
# Below this size a duplicate tool result is not worth eliding: the dedup stub
# itself costs ~200 chars, so tiny repeated payloads would grow, not shrink.
_PROVIDER_HISTORY_DEDUP_MIN_CHARS = 400
_TOOL_ARGUMENT_HEARTBEAT_CHARS = 4096
_PROVIDER_CONTEXT_PROJECTION_REUSED_REASON = "provider_context_projection_reused"
_SEMANTIC_TOOL_RESULT_PROJECTION_SKIP_TOOLS = frozenset({"read_file", "git_diff"})
_TOOL_RESULT_RETRIEVE_HINT = (
    "retrieve_hint: this result is incomplete. If the next diagnosis, patch, "
    "or validation step depends on omitted details, first call "
    "retrieve_tool_result with this tool_result_handle. Prefer mode=query with "
    "an L<num> from search_hints, a failing test name, file path, or error "
    "phrase; use mode=head_tail for orientation, and mode=raw_slice with "
    "offset/limit only when focused query retrieval is insufficient. If "
    "retrieve_tool_result returns continuation.next_call, prefer that exact "
    "follow-up. Do not infer omitted diagnostics from this projection.\n"
)
_TOOL_RESULT_HINT_LINE_MAX_CHARS = 180
_TOOL_RESULT_HINT_MAX_LINES = 8
_TOOL_RESULT_HINT_MAX_CHARS = 900
_TOOL_RESULT_HINT_SCAN_MAX_CHARS = 2048
_TOOL_PROJECTION_EVENT_ARGUMENT_KEYS = frozenset(
    {"command", "cmd", "workdir", "cwd", "path", "paths"}
)
_TOOL_PROJECTION_EVENT_ARGUMENT_MAX_CHARS = 4096
_TOOL_RESULT_HINT_PATTERN = re.compile(
    r"\b("
    r"assert(?:ion)?s?|"
    r"error|errors|exception|fatal|"
    r"fail(?:ed|ing|ure|ures|s)?|"
    r"mismatch|panic(?:ked|king|s)?|"
    r"traceback|expected|actual"
    r")\b",
    re.IGNORECASE,
)
_TOOL_RESULT_HINT_PATH_PATTERN = re.compile(
    r"(?:[A-Za-z]:)?[./\\]?[A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+(?::\d+)?"
)
_PROJECTION_SIGNAL_HINTS_ENV = "OPENSQUILLA_PROJECTION_SIGNAL_HINTS"
_PROJECTION_SIGNAL_PATTERNS_ENV = "OPENSQUILLA_PROJECTION_SIGNAL_PATTERNS"
_PROJECTION_SIGNAL_HINTS_ON = frozenset({"on", "1", "true", "yes"})
_PROJECTION_SIGNAL_HINTS_OFF = frozenset({"off", "0", "false", "no"})
# Default failure-signal pattern for the projection signal scan. Kept separate
# from _TOOL_RESULT_HINT_PATTERN so the env override below can never perturb
# search_hints selection. Case-sensitive on purpose: the anchors target the
# capitalized/tool-emitted forms (FAILED, Traceback, AssertionError, ...).
_PROJECTION_SIGNAL_DEFAULT_PATTERN = re.compile(
    r"(?:\bFAILED\b|\bFAIL:|\bError\b|\bException\b|\bTraceback\b"
    r"|\bAssertionError\b|\berror:|\bwarnings? summary\b"
    r"|\bpanic(?:ked)?\b|\bfatal\b)"
)
_PROJECTION_SIGNAL_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _projection_signal_hints_enabled(config_value: bool = False) -> bool:
    """Resolve the projection signal-scan gate.

    Unset defers to ``config_value`` (the AgentConfig field threaded from the
    same env by the bootstrap stage; off by default). Recognized on/off values
    override it; unrecognized values raise instead of being silently ignored
    so a run manifest cannot record an override the run did not actually
    apply.
    """
    raw = os.environ.get(_PROJECTION_SIGNAL_HINTS_ENV, "").strip().lower()
    if not raw:
        return bool(config_value)
    if raw in _PROJECTION_SIGNAL_HINTS_ON:
        return True
    if raw in _PROJECTION_SIGNAL_HINTS_OFF:
        return False
    raise ValueError(
        f"{_PROJECTION_SIGNAL_HINTS_ENV} must be one of: "
        + ", ".join(sorted(_PROJECTION_SIGNAL_HINTS_ON | _PROJECTION_SIGNAL_HINTS_OFF))
    )


def _projection_signal_pattern() -> re.Pattern[str]:
    """Return the failure-signal regex, honoring the env override.

    A non-blank ``OPENSQUILLA_PROJECTION_SIGNAL_PATTERNS`` value replaces the
    default pattern wholesale (write alternations into one regex). Compiled
    overrides are cached by raw string; invalid regexes raise ValueError per
    the manifest-honesty convention rather than silently falling back.
    """
    raw = os.environ.get(_PROJECTION_SIGNAL_PATTERNS_ENV, "").strip()
    if not raw:
        return _PROJECTION_SIGNAL_DEFAULT_PATTERN
    cached = _PROJECTION_SIGNAL_PATTERN_CACHE.get(raw)
    if cached is not None:
        return cached
    try:
        compiled = re.compile(raw)
    except re.error as exc:
        raise ValueError(
            f"{_PROJECTION_SIGNAL_PATTERNS_ENV} must be a valid regular "
            f"expression: {exc}"
        ) from exc
    _PROJECTION_SIGNAL_PATTERN_CACHE[raw] = compiled
    return compiled
_PROVIDER_CONTEXT_REPAIR_PROMPT = (
    "A previous tool call was rejected because it reused provider-only compacted "
    "tool arguments. Regenerate the complete tool arguments from the available "
    "source context and retry the tool call. Do not copy compacted placeholders."
)
_IDENTICAL_REQUEST_LOOP_NUDGE = (
    "The last several requests were identical: the conversation is stuck "
    "repeating the same rejected or failed action. Do not repeat the previous "
    "tool call. Change approach now: re-read the relevant files or re-run the "
    "command to rebuild tool arguments from real content, try a different tool "
    "or target, or finalize with your best current answer."
)
_PLACEHOLDER_ESCALATION_DIRECTIVE = (
    "STOP: multiple tool calls this turn reused compacted placeholder text and "
    "were rejected without running. Reissuing that call will never work. Before "
    "your next tool call, re-open the target file or re-run the underlying "
    "command to get its real current content, then rebuild the tool arguments "
    "from that fresh output. Never retype or paraphrase placeholder text."
)
_DEADLINE_WRAPUP_DIRECTIVE_TEMPLATE = (
    "Time check: roughly {minutes} minute(s) of wall-clock budget remain for "
    "this task. Stop exploring and converge now: apply your best current "
    "changes, verify them quickly if you can, and finish with a complete "
    "final answer. Finishing your best-supported work now is better than "
    "further investigation that the clock will cut off."
)
_MID_BUDGET_NO_DIFF_NUDGE_FRACTIONS: tuple[float, ...] = (0.5, 0.75)
_MID_BUDGET_NO_DIFF_NUDGE_TEMPLATE = (
    "Progress check: about {percent}% of the wall-clock budget for this task "
    "is spent and the workspace has no source change yet. If you already "
    "know the fix, start implementing it now and verify it against the "
    "existing tests. If you are still investigating, pick the most likely "
    "file and make the smallest reasonable edit now, then refine it with the "
    "remaining time instead of leaving the whole budget to analysis."
)
_MID_BUDGET_NO_DIFF_NUDGE_PREFIX = _MID_BUDGET_NO_DIFF_NUDGE_TEMPLATE.split(
    "{percent}", 1
)[0]
_REASONING_ONLY_ACT_NOW_DIRECTIVE = (
    "Your previous response was internal reasoning only, so nothing was "
    "delivered or executed. Act now: issue the tool call that carries out "
    "your current best next step, or state your final answer directly. "
    "Decide with the analysis you already have instead of reasoning further."
)
_PLAN_ONLY_ACT_NOW_DIRECTIVE = (
    "The last several responses only updated the plan without acting on it. "
    "Planning further will not move the task forward. Act now: issue the "
    "tool call that carries out the current in-progress step, and update "
    "the plan again only after real progress."
)
# One-shot endgame fix directive (OPENSQUILLA_ENDGAME_FIX_DIRECTIVE_MARGIN_
# SECONDS). The prefix is distinct from the wrap-up's "Time check: roughly "
# so the nudge-identity predicates can tell them apart.
_ENDGAME_FIX_DIRECTIVE_TEMPLATE = (
    "Time check: about {minutes} minute(s) remain and the workspace contains "
    "no source fix yet beyond diagnostic instrumentation. Stop investigating "
    "now. Decide on the most likely root cause from the evidence you already "
    "have, remove leftover debug output, apply your best-supported fix to "
    "the source code, and verify it directly. An imperfect fix you can "
    "defend beats no fix."
)
_ENDGAME_FIX_DIRECTIVE_PREFIX = "Time check: about "
_LARGE_CONTEXT_INVALID_RESPONSE_INPUT_TOKENS = 30_000
_COMPACTED_TOOL_ARGUMENT_MARKERS = frozenset(
    {
        "_opensquilla_compacted_tool_arguments",
        "_opensquilla_compacted_tool_input",
    }
)


def _tool_result_search_hints(content: str) -> str:
    lines: list[str] = []
    candidates: list[tuple[int, int, str]] = []
    used_chars = 0
    seen: set[str] = set()
    for line_number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        scan_line = line[:_TOOL_RESULT_HINT_SCAN_MAX_CHARS]
        has_diagnostic = bool(_TOOL_RESULT_HINT_PATTERN.search(scan_line))
        has_path = bool(_TOOL_RESULT_HINT_PATH_PATTERN.search(scan_line))
        if not has_diagnostic and not has_path:
            continue
        snippet = scan_line[:_TOOL_RESULT_HINT_LINE_MAX_CHARS]
        normalized = snippet.casefold()
        if normalized in seen:
            continue
        rendered = f"- L{line_number}: {snippet}"
        score = (10 if has_diagnostic else 0) + (1 if has_path else 0)
        if used_chars + len(rendered) > _TOOL_RESULT_HINT_MAX_CHARS:
            continue
        seen.add(normalized)
        candidates.append((-score, line_number, rendered))
    for _score, _line_number, rendered in sorted(candidates):
        if used_chars + len(rendered) > _TOOL_RESULT_HINT_MAX_CHARS:
            continue
        lines.append(rendered)
        used_chars += len(rendered)
        if len(lines) >= _TOOL_RESULT_HINT_MAX_LINES:
            break
    if not lines:
        return ""
    return "search_hints:\n" + "\n".join(lines) + "\n"


def _tool_result_signal_scan(
    content: str,
    *,
    handle: str | None,
    head_chars: int | None = None,
    tail_chars: int | None = None,
    preview_lines: frozenset[str] | None = None,
) -> tuple[str, int, int | None]:
    """Scan the omitted region of a projected tool result for failure signals.

    Returns ``(rendered_lines, match_count, first_line_number)``. Line numbers
    are 1-based over the FULL original ``content`` (the same coordinates
    search_hints renders and retrieve_tool_result's ``L<num>`` query resolves
    against the byte-identical stored record).

    Omission model: with ``head_chars``/``tail_chars`` the omitted region is
    the contiguous char span between the preserved head and tail; otherwise a
    line counts as omitted when its exact text is absent from
    ``preview_lines`` (the reducer-summarized preview). The membership check
    is an approximation — a reducer that rewrites a matching line makes it
    count as omitted even though a variant survives — but the rendered line
    number still points at a real failure line in the original.

    Returns ``("", 0, None)`` when there is nothing to report or no handle
    exists to retrieve against.
    """
    if handle is None or not content:
        return "", 0, None
    pattern = _projection_signal_pattern()
    omitted_start: int | None = None
    omitted_end: int | None = None
    if head_chars is not None:
        omitted_start = max(0, int(head_chars))
        omitted_end = len(content) - max(0, int(tail_chars or 0))
        if omitted_end <= omitted_start:
            return "", 0, None
    match_count = 0
    first_line_number: int | None = None
    offset = 0
    for line_number, line in enumerate(content.splitlines(), start=1):
        line_start = offset
        offset += len(line) + 1
        if omitted_start is not None and omitted_end is not None:
            if line_start + len(line) <= omitted_start or line_start >= omitted_end:
                continue
        elif preview_lines is not None and line in preview_lines:
            continue
        if not pattern.search(line[:_TOOL_RESULT_HINT_SCAN_MAX_CHARS]):
            continue
        match_count += 1
        if first_line_number is None:
            first_line_number = line_number
    if match_count == 0 or first_line_number is None:
        return "", 0, None
    rendered = _render_projection_signal_lines(
        handle=handle,
        match_count=match_count,
        first_line_number=first_line_number,
    )
    return rendered, match_count, first_line_number


def _render_projection_signal_lines(
    *,
    handle: str | None,
    match_count: int,
    first_line_number: int | None,
) -> str:
    """Render the signal_scan notice lines for an already-computed scan.

    Kept separate from the scan so the fresh-result path can scan once with
    the size-gate probe's placeholder handle and re-render with the real
    stored handle (both handle forms have identical length, so the probe
    measures the true envelope size).
    """
    if handle is None or match_count <= 0 or first_line_number is None:
        return ""
    next_call_arguments = json.dumps(
        {"handle": handle, "mode": "query", "query": f"L{first_line_number}"},
        ensure_ascii=False,
        sort_keys=True,
    )
    return (
        f"signal_scan: {match_count} lines matching failure patterns in the "
        f"omitted region (first at L{first_line_number})\n"
        f"signal_next_call: retrieve_tool_result {next_call_arguments}\n"
    )


def _projection_event_argument_value(value: Any, *, key: str) -> Any:
    redacted = redact_secret_value(value, key=key)
    if isinstance(redacted, str) and len(redacted) > _TOOL_PROJECTION_EVENT_ARGUMENT_MAX_CHARS:
        omitted = len(redacted) - _TOOL_PROJECTION_EVENT_ARGUMENT_MAX_CHARS
        prefix = redacted[:_TOOL_PROJECTION_EVENT_ARGUMENT_MAX_CHARS]
        return f"{prefix}...[truncated {omitted} chars]"
    return redacted


def _projection_event_arguments(arguments: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(arguments, dict):
        return None
    selected: dict[str, Any] = {}
    for key in sorted(_TOOL_PROJECTION_EVENT_ARGUMENT_KEYS):
        if key in arguments:
            selected[key] = _projection_event_argument_value(arguments[key], key=key)
    return selected or None


def _large_json_field_replacement(value: str) -> dict[str, object]:
    return {
        "omitted": True,
        "omitted_chars": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "reason": "large_tool_result_field",
    }


def _omit_large_json_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        sanitized_dict: dict[str, Any] = {}
        for key, item in value.items():
            if (
                key in _LARGE_JSON_TOOL_FIELD_KEYS
                and isinstance(item, str)
                and len(item) > _LARGE_JSON_TOOL_FIELD_CHARS
            ):
                sanitized_dict[key] = _large_json_field_replacement(item)
                changed = True
                continue
            sanitized, child_changed = _omit_large_json_value(item)
            sanitized_dict[key] = sanitized
            changed = changed or child_changed
        return sanitized_dict, changed
    if isinstance(value, list):
        changed = False
        sanitized_list: list[Any] = []
        for item in value:
            sanitized, child_changed = _omit_large_json_value(item)
            sanitized_list.append(sanitized)
            changed = changed or child_changed
        return sanitized_list, changed
    return value, False


def _omit_large_json_tool_fields(content: str) -> tuple[str, bool]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content, False
    sanitized, changed = _omit_large_json_value(parsed)
    if not changed:
        return content, False
    return json.dumps(sanitized, ensure_ascii=False, indent=2), True


def _is_threshold_denial(result: ToolResult) -> bool:
    try:
        payload = json.loads(result.content)
    except Exception:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") == "denied"
        and payload.get("reason") == "threshold_exceeded"
    )


_PENDING_APPROVAL_STATUSES: frozenset[str] = frozenset({"approval_required", "approval_pending"})


def _pending_approval_payload(content: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("status") not in _PENDING_APPROVAL_STATUSES:
        return None
    approval_id = payload.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        return None
    return payload


def _suspend_tool_request(
    tool_call: ToolCall,
    payload: dict[str, Any],
) -> SuspendedToolRequest:
    """Capture the original provider request plus its full model arguments."""

    from opensquilla.gateway.approval_queue import get_approval_queue

    raw_action: dict[str, Any] = {}
    retry_context: dict[str, Any] = {}
    approval_session_key = ""
    try:
        entry = get_approval_queue().get(str(payload["approval_id"]))
        if isinstance(entry.params.get("action"), dict):
            raw_action = dict(entry.params["action"])
        for key in (
            "backendRetry",
            "sandboxOriginalOutput",
            "sandboxBackend",
            "sandboxBackendNotes",
            "retryReason",
        ):
            if key in entry.params:
                retry_context[key] = entry.params[key]
        approval_session_key = str(entry.params.get("sessionKey") or "")
    except (KeyError, TypeError):
        pass
    if tool_call.tool_name == "apply_patch":
        kind = "apply_patch"
    elif tool_call.tool_name == "execute_code":
        kind = "code"
    elif payload.get("approvalKind") == "sandbox_network":
        kind = "network"
    elif tool_call.tool_name in {"write_file", "edit_file", "delete_file"}:
        kind = "filesystem"
    elif tool_call.tool_name in {"image", "pdf", "audio"}:
        kind = "media"
    else:
        kind = "exec_command"
    action = ApprovalAction(
        kind=kind,  # type: ignore[arg-type]
        call_id=tool_call.tool_use_id,
        tool_name=tool_call.tool_name,
        cwd=Path(str(raw_action.get("cwd") or ".")).expanduser().resolve(strict=False),
        payload={
            "arguments": dict(tool_call.arguments),
            "elevation": raw_action,
            "retry_context": retry_context,
        },
        justification=str(raw_action.get("justification") or ""),
    )
    return SuspendedToolRequest(
        tool_call=tool_call,
        action=action,
        metadata={"session_key": approval_session_key},
    )


async def _wait_for_pending_approval_resolution(
    payload: dict[str, Any],
    *,
    timeout: float,
) -> None:
    approval_id = payload.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        return
    try:
        from opensquilla.gateway.approval_queue import get_approval_queue

        queue = get_approval_queue()
        await queue.wait(approval_id, timeout=max(0.0, timeout))
    except KeyError:
        return


async def _review_pending_elevation_if_configured(
    payload: dict[str, Any],
    *,
    transcript: list[Message],
    runtime_events_path: str | None,
    suspended_action: ApprovalAction | None = None,
) -> RuleAssessment | None:
    """Resolve one internal elevation record through deterministic local rules."""

    approval_id = payload.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        return None

    from opensquilla.gateway.approval_queue import get_approval_queue
    queue = get_approval_queue()
    try:
        entry = queue.get(approval_id)
    except KeyError:
        return None
    params = entry.params
    approval_kind = str(params.get("approvalKind") or "")
    if (
        entry.namespace != "exec"
        or approval_kind not in {"sandbox_elevation", "sandbox_network"}
        or params.get("reviewer") != "auto_review"
        or params.get("humanActionable") is not False
    ):
        return None
    if entry.resolved:
        return None

    fingerprint = str(
        (
            params.get("reviewFingerprint")
            if approval_kind == "sandbox_network"
            else params.get("fingerprint")
        )
        or ""
    )
    append_runtime_event(
        runtime_events_path,
        {
            "event_type": "sandbox_elevation_review",
            "phase": "started",
            "review_id": approval_id,
            "approval_id": approval_id,
            "fingerprint": fingerprint,
            "humanActionable": False,
            "reviewer": "deterministic_rules",
            "action": (
                suspended_action.audit_payload() if suspended_action is not None else None
            ),
        },
    )

    review_source = "rules"
    try:
        raw_action = params.get("action")
        if not isinstance(raw_action, dict):
            raise ValueError("missing_canonical_elevation_action")
        action = ElevationAction.from_canonical_payload(raw_action)
        if action.fingerprint() != fingerprint:
            raise ValueError("elevation_action_fingerprint_mismatch")
        assessment = local_elevation_assessment(action, transcript)
    except Exception as exc:
        review_source = "rules_integrity_failure"
        assessment = RuleAssessment(
            risk_level="critical",
            user_authorization="unknown",
            outcome="deny",
            rationale=(
                "The exact approval action failed canonical integrity validation: "
                f"{str(exc) or type(exc).__name__}"
            ),
            human_confirmation_allowed=False,
        )

    updated_params = dict(params)
    requires_human_confirmation = (
        assessment.risk_level == "critical" and assessment.human_confirmation_allowed
    )
    updated_params.update(
        {
            "reviewRiskLevel": assessment.risk_level,
            "reviewAuthorization": assessment.user_authorization,
            "reviewOutcome": assessment.outcome,
            "reviewStatus": assessment.status,
            "reviewRationale": assessment.rationale,
            "reviewSource": review_source,
        }
    )
    if requires_human_confirmation:
        updated_params.update(
            {
                "reviewer": "user",
                "humanActionable": True,
                "ruleReviewOutcome": assessment.outcome,
                "reviewStatus": "human_confirmation_required",
            }
        )
    try:
        queue.update_params(approval_id, updated_params)
        if not requires_human_confirmation:
            queue.resolve(approval_id, assessment.outcome == "allow")
    except ValueError:
        # Another resolver won the race. Never override an existing decision.
        return None

    if assessment.outcome == "allow" and approval_kind == "sandbox_network":
        from opensquilla.sandbox.escalation import grant_auto_review_network_once

        grant_auto_review_network_once(updated_params)

    append_runtime_event(
        runtime_events_path,
        {
            "event_type": "sandbox_elevation_review",
            "phase": "completed",
            "review_id": approval_id,
            "approval_id": approval_id,
            "fingerprint": fingerprint,
            "humanActionable": False,
            "risk_level": assessment.risk_level,
            "authorization": assessment.user_authorization,
            "outcome": assessment.outcome,
            "status": assessment.status,
            "rationale": assessment.rationale,
            "attempt": assessment.attempt_count,
            "latency_ms": assessment.latency_ms,
            "human_confirmation_required": requires_human_confirmation,
            "review_source": review_source,
        },
    )
    logger.info(
        "sandbox_elevation.review_completed",
        approval_id=approval_id,
        fingerprint=fingerprint,
        risk_level=assessment.risk_level,
        authorization=assessment.user_authorization,
        outcome=assessment.outcome,
        source=review_source,
        status=(
            "human_confirmation_required"
            if requires_human_confirmation
            else assessment.status
        ),
    )
    return None if requires_human_confirmation else assessment


@functools.lru_cache(maxsize=4096)
def _tool_result_content_has_artifact(content: str) -> bool:
    try:
        payload = json.loads(content)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("artifact"), dict) or isinstance(payload.get("artifacts"), list):
        return True
    return payload.get("status") in {"published", "already_published"}


def _tool_result_content_is_provider_projection(content: str) -> bool:
    return content.startswith(
        (
            "[tool_result_projection]\n",
            "[aggregate_tool_result_compacted]\n",
            "[duplicate_tool_result_elided]\n",
        )
    )


def _tool_result_budget_tokens(content: str) -> int:
    return max(get_approx_tokens(content), len(content) // 4)


def _artifact_event_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "kind",
        "id",
        "sha256",
        "name",
        "mime",
        "size",
        "session_id",
        "session_key",
        "source",
        "created_at",
        "download_url",
        "store",
        "has_thumbnail",
    }
    normalized = artifact_payload(payload)
    kwargs = {key: value for key, value in normalized.items() if key in allowed}
    # artifact_payload exposes the public thumbnail_url; carry the boolean signal onto
    # the event dataclass so downstream serializers can rebuild the variant URL.
    kwargs["has_thumbnail"] = bool(
        payload.get("has_thumbnail") or normalized.get("thumbnail_url")
    )
    return kwargs


def _flatten_content_blocks(blocks: list[Any]) -> str:
    """Convert a list of content-block Pydantic models to a plain string for compaction.

    Extracts text from ContentBlockText, summarises tool_use/tool_result blocks,
    and drops thinking/image blocks to avoid leaking Python repr strings.
    """
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, ContentBlockText):
            parts.append(b.text)
        elif isinstance(b, ContentBlockToolUse):
            parts.append(f"[Used tool: {b.name}]")
        elif isinstance(b, ContentBlockToolResult):
            snippet = b.content if isinstance(b.content, str) else str(b.content)
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            parts.append(f"[Tool result ({b.tool_use_id}): {snippet}]")
        # Skip thinking / image blocks — not useful for compaction
    return "\n".join(parts)


def _message_has_tool_result(message: Message | None) -> bool:
    if message is None or not isinstance(message.content, list):
        return False
    return any(getattr(block, "type", None) == "tool_result" for block in message.content)


def _tail_has_tool_result(messages: list[Message], *, lookback: int = 2) -> bool:
    if not messages:
        return False
    return any(_message_has_tool_result(message) for message in messages[-lookback:])


def _is_mid_budget_nudge_message(message: Message) -> bool:
    return (
        message.role == "user"
        and isinstance(message.content, str)
        and message.content.startswith(_MID_BUDGET_NO_DIFF_NUDGE_PREFIX)
    )


def _is_runtime_nudge_message(message: Message) -> bool:
    """Whether a message is a runtime-injected nudge, not conversation history.

    Covers the mid-budget progress nudge, the plan-only act-now directive,
    and the endgame fix directive — everything the engine appends after tool
    results that the post-tool shape predicates must see through.
    """

    if message.role != "user" or not isinstance(message.content, str):
        return False
    return (
        message.content.startswith(_MID_BUDGET_NO_DIFF_NUDGE_PREFIX)
        or message.content == _PLAN_ONLY_ACT_NOW_DIRECTIVE
        or message.content.startswith(_ENDGAME_FIX_DIRECTIVE_PREFIX)
    )


def _tail_has_tool_result_ignoring_nudges(messages: list[Message]) -> bool:
    """Post-tool shape of the turn with runtime-injected nudges removed.

    A nudge stacked after watchdog or pending-input messages pushes the tool
    results out of the plain lookback window; the nudge is not conversation
    history, so the shape is judged as if it were absent.
    """

    return _tail_has_tool_result(
        [message for message in messages[-4:] if not _is_runtime_nudge_message(message)]
    )


def _message_has_visible_text(message: Message) -> bool:
    if isinstance(message.content, str):
        return bool(message.content.strip())
    if not isinstance(message.content, list):
        return False
    return any(
        isinstance(block, ContentBlockText) and bool(block.text.strip())
        for block in message.content
    )


def _message_has_tool_use(message: Message) -> bool:
    if not isinstance(message.content, list):
        return False
    return any(isinstance(block, ContentBlockToolUse) for block in message.content)


def _build_reasoning_prefill_message(
    *,
    reasoning_content: str,
    thinking_signature: str | None,
) -> Message:
    content: list[Any] = []
    if thinking_signature:
        content.append(
            ContentBlockThinking(
                thinking=reasoning_content,
                signature=thinking_signature,
            )
        )
    else:
        content.append(ContentBlockText(text=""))
    return Message(
        role="assistant",
        content=content,
        reasoning_content=reasoning_content,
    )


def _drop_runtime_recovery_scaffolding(messages: list[Message]) -> list[Message]:
    cleaned = list(messages)
    while cleaned:
        last = cleaned[-1]
        if (
            last.role == "user"
            and isinstance(last.content, str)
            and last.content.startswith("[Runtime recovery]")
        ):
            cleaned.pop()
            if cleaned:
                previous = cleaned[-1]
                if (
                    previous.role == "assistant"
                    and not _message_has_visible_text(previous)
                    and not _message_has_tool_use(previous)
                ):
                    cleaned.pop()
            continue
        if (
            last.role == "assistant"
            and last.reasoning_content
            and not _message_has_visible_text(last)
            and not _message_has_tool_use(last)
        ):
            cleaned.pop()
            continue
        break
    return cleaned


def _append_length_capped_continuation(
    turn_messages: list[Message],
    *,
    response_text: str,
    tool_calls: list[ToolCall],
) -> str:
    visible_text = response_text
    if visible_text:
        turn_messages.append(
            Message(role="assistant", content=[ContentBlockText(text=visible_text)])
        )
    turn_messages.append(Message(role="user", content=_PROVIDER_OUTPUT_CONTINUE_PROMPT))
    return visible_text


class _ProviderAttemptKind(StrEnum):
    OK = "ok"
    REASONING_ONLY = "reasoning_only"
    MALFORMED_EMPTY = "malformed_empty"
    INCOMPLETE_TOOLS = "incomplete_tools"
    STREAM_INCOMPLETE = "stream_incomplete"
    LENGTH_CAPPED = "length_capped"


class _IterationStreamTimeoutError(TimeoutError):
    """Raised when provider streaming exceeds the active Agent iteration budget."""


def _is_large_context_invalid_response(
    kind: _ProviderAttemptKind,
    *,
    input_tokens: int,
) -> bool:
    return (
        kind
        in {
            _ProviderAttemptKind.REASONING_ONLY,
            _ProviderAttemptKind.MALFORMED_EMPTY,
        }
        and input_tokens >= _LARGE_CONTEXT_INVALID_RESPONSE_INPUT_TOKENS
    )


@dataclass(frozen=True)
class _ProviderAttemptClassification:
    kind: _ProviderAttemptKind
    stop_reason: str | None = None
    user_visible_emitted: bool = False


@dataclass(frozen=True)
class _ProviderRetryPolicy:
    max_provider_retries: int
    attempt_budgets: dict[_ProviderAttemptKind, int]
    provider_failure_budgets: dict[ProviderFailureKind, int]

    @classmethod
    def from_provider_budget(
        cls,
        max_provider_retries: int,
        *,
        length_capped_continuations: int = 3,
        reasoning_only_retries: int = 1,
    ) -> _ProviderRetryPolicy:
        length_capped_continuations = max(1, length_capped_continuations)
        return cls(
            max_provider_retries=max_provider_retries,
            attempt_budgets={
                _ProviderAttemptKind.REASONING_ONLY: max(1, reasoning_only_retries),
                _ProviderAttemptKind.MALFORMED_EMPTY: 1,
                _ProviderAttemptKind.STREAM_INCOMPLETE: 1,
                _ProviderAttemptKind.LENGTH_CAPPED: length_capped_continuations,
            },
            provider_failure_budgets={ProviderFailureKind.EMPTY_RESPONSE: 1},
        )

    def used_attempts(self) -> dict[_ProviderAttemptKind, int]:
        return {kind: 0 for kind in self.attempt_budgets}

    def can_retry_attempt(
        self,
        kind: _ProviderAttemptKind,
        used: dict[_ProviderAttemptKind, int],
    ) -> bool:
        return self.max_provider_retries > 0 and used.get(kind, 0) < self.attempt_budgets.get(
            kind, 0
        )

    def can_retry_provider_failure(
        self,
        failure_kind: ProviderFailureKind,
        *,
        post_tool_turn: bool,
        provider_retry_attempt: int,
    ) -> bool:
        if failure_kind is ProviderFailureKind.EMPTY_RESPONSE:
            return (
                post_tool_turn
                and self.max_provider_retries > 0
                and provider_retry_attempt
                < self.provider_failure_budgets.get(failure_kind, self.max_provider_retries)
            )
        return provider_retry_attempt < self.max_provider_retries


@dataclass(frozen=True)
class _MessageCountRecoveryOutcome:
    """Ephemeral provider-view rewrite produced by count-aware compaction."""

    messages: list[Message]
    request_context_insert_index: int
    runtime_context_insert_index: int
    protected_turn_start_index: int
    projected_wire_messages: int
    removed_count: int


@dataclass(frozen=True)
class _MessageCountRequestView:
    """Compacted request-only prefix plus later canonical messages.

    Count recovery must not rewrite ``turn_messages`` because that list becomes
    the transcript at turn completion.  This view records the canonical length
    it covered so later assistant/tool messages can be spliced into requests.
    """

    messages: list[Message]
    canonical_tail_start: int
    request_context_insert_index: int
    runtime_context_insert_index: int
    protected_turn_start_index: int

    def materialize(self, canonical_messages: list[Message]) -> list[Message]:
        tail_start = max(0, min(self.canonical_tail_start, len(canonical_messages)))
        return [*self.messages, *canonical_messages[tail_start:]]

    def rebase_after_canonical_suffix_cleanup(
        self,
        canonical_before: list[Message],
        canonical_after: list[Message],
    ) -> _MessageCountRequestView:
        """Keep an ephemeral request view aligned after a canonical suffix drop.

        Runtime-recovery scaffolding is removed from the canonical transcript
        before the next tool exchange.  Count recovery may already have
        snapshotted that suffix, so advance the append boundary and apply the
        same cleanup to the request-only view instead of letting it skip the
        next canonical message.
        """

        if canonical_after != canonical_before[: len(canonical_after)]:
            raise ValueError("canonical cleanup must remove a suffix")
        removed_count = len(canonical_before) - len(canonical_after)
        materialized = self.materialize(canonical_before)
        if removed_count > len(materialized):
            raise ValueError("canonical cleanup exceeds the request view")
        rebased_messages = (
            materialized[:-removed_count] if removed_count else materialized
        )
        return _MessageCountRequestView(
            messages=rebased_messages,
            canonical_tail_start=len(canonical_after),
            request_context_insert_index=self.request_context_insert_index,
            runtime_context_insert_index=self.runtime_context_insert_index,
            protected_turn_start_index=self.protected_turn_start_index,
        )


def _classify_provider_attempt(
    *,
    text: str,
    tool_calls: list[ToolCall],
    pending_tools: dict[str, _StreamAccumulator],
    got_done_event: bool,
    stop_reason: str | None,
    reasoning_content: str | None,
    reasoning_tokens: int,
    user_visible_emitted: bool,
) -> _ProviderAttemptClassification:
    visible_text = bool(text.strip())
    if pending_tools:
        return _ProviderAttemptClassification(
            _ProviderAttemptKind.INCOMPLETE_TOOLS,
            stop_reason=stop_reason,
            user_visible_emitted=user_visible_emitted,
        )
    if not got_done_event:
        return _ProviderAttemptClassification(
            _ProviderAttemptKind.STREAM_INCOMPLETE,
            stop_reason=stop_reason,
            user_visible_emitted=user_visible_emitted,
        )
    if (stop_reason or "").lower() == "length" and not visible_text and not tool_calls:
        if (reasoning_content and reasoning_content.strip()) or reasoning_tokens > 0:
            return _ProviderAttemptClassification(
                _ProviderAttemptKind.REASONING_ONLY,
                stop_reason=stop_reason,
                user_visible_emitted=user_visible_emitted,
            )
        return _ProviderAttemptClassification(
            _ProviderAttemptKind.MALFORMED_EMPTY,
            stop_reason=stop_reason,
            user_visible_emitted=user_visible_emitted,
        )
    if (stop_reason or "").lower() == "length":
        return _ProviderAttemptClassification(
            _ProviderAttemptKind.LENGTH_CAPPED,
            stop_reason=stop_reason,
            user_visible_emitted=user_visible_emitted,
        )
    if visible_text or tool_calls:
        return _ProviderAttemptClassification(
            _ProviderAttemptKind.OK,
            stop_reason=stop_reason,
            user_visible_emitted=user_visible_emitted,
        )
    if (reasoning_content and reasoning_content.strip()) or reasoning_tokens > 0:
        return _ProviderAttemptClassification(
            _ProviderAttemptKind.REASONING_ONLY,
            stop_reason=stop_reason,
            user_visible_emitted=user_visible_emitted,
        )
    return _ProviderAttemptClassification(
        _ProviderAttemptKind.MALFORMED_EMPTY,
        stop_reason=stop_reason,
        user_visible_emitted=user_visible_emitted,
    )


def _chat_config_with_thinking_disabled(chat_cfg: ChatConfig) -> ChatConfig:
    return ChatConfig(
        max_tokens=chat_cfg.max_tokens,
        temperature=chat_cfg.temperature,
        top_p=chat_cfg.top_p,
        system=chat_cfg.system,
        thinking=False,
        thinking_budget_tokens=0,
        thinking_budget_explicit=False,
        timeout=chat_cfg.timeout,
        stop_sequences=chat_cfg.stop_sequences,
        cache_breakpoints=chat_cfg.cache_breakpoints,
        cache_mode=chat_cfg.cache_mode,
        output_json_schema=chat_cfg.output_json_schema,
        output_json_schema_strict=chat_cfg.output_json_schema_strict,
        model_capabilities=chat_cfg.model_capabilities,
        thinking_level=None,
        provider_request_max_chars=chat_cfg.provider_request_max_chars,
        tool_choice=chat_cfg.tool_choice,
    )


def _strip_historical_image_blocks(
    messages: list[Message],
    *,
    preserve_images: bool = False,
) -> list[Message]:
    """Remove image payload blocks from history before provider calls.

    Current-turn uploads are passed through ``extra_messages`` and are not part
    of the history list sanitized here. This prevents a later text follow-up
    from replaying stale image input to a text-only route.
    """
    if preserve_images:
        return messages

    sanitized: list[Message] = []
    for msg in messages:
        content = msg.content
        if not isinstance(content, list):
            sanitized.append(msg)
            continue

        kept: list[Any] = []
        omitted: list[str] = []
        for block in content:
            if isinstance(block, ContentBlockImage):
                media_type = block.media_type or "image"
                omitted.append(f"[historical image omitted: {media_type}]")
                continue
            kept.append(block)

        if not omitted:
            sanitized.append(msg)
            continue

        kept.extend(ContentBlockText(text=marker) for marker in omitted)
        sanitized.append(Message(role=msg.role, content=kept))
    return sanitized


@dataclass
class _StreamAccumulator:
    """Tracks streamed tool-argument progress until the provider closes it.

    Argument semantics belong to the provider adapter.  The accumulated raw
    fragments are useful for progress heartbeats and diagnostics, but must not
    override the canonical object carried by ``ToolUseEndEvent``: adapters may
    repair provider-specific wire formats or replace provisional deltas with an
    authoritative terminal payload.
    """

    tool_use_id: str
    tool_name: str
    synthetic_from_text: bool = False
    json_buf: list[str] = field(default_factory=list)
    json_chars: int = 0


class Agent:
    """Explicit state-machine agent.

    Lifecycle per turn:
      IDLE -> THINKING -> STREAMING -> [TOOL_CALLING -> THINKING -> ...] -> DONE
      Any step can transition to ERROR.
    """

    def __init__(
        self,
        provider: LLMProvider,
        config: AgentConfig | None = None,
        tool_definitions: list[ToolDefinition] | None = None,
        tool_handler: ToolHandler | None = None,
        subagent_manager: SubagentManager | None = None,
        usage_tracker: Any | None = None,
        session_key: str | None = None,
        turn_call_logger: TurnCallLogger | None = None,
        memory_sync_manager: Any | None = None,
        session_flush_service: Any | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_context: ToolContext | None = None,
        failure_injector: FailureInjector | None = None,
        usage_event_sink: UsageEventSink | None = None,
        usage_execution_context: UsageExecutionContext | None = None,
        provider_request_correlation: ProviderRequestCorrelation | None = None,
    ) -> None:
        self.provider = provider
        self.config = config or AgentConfig()
        self.tool_definitions = tool_definitions or []
        self._tool_definition_by_name = {tool.name: tool for tool in self.tool_definitions}
        self._raw_tool_handler = tool_handler
        self.tool_handler = tool_handler
        self.subagent_manager = subagent_manager or SubagentManager()
        self._usage_tracker = usage_tracker
        self._session_key = session_key
        self._turn_call_logger = turn_call_logger
        self._tool_registry: ToolRegistry | None = tool_registry
        if (
            tool_context is not None
            and self.config.runtime_events_path
            and tool_context.on_runtime_event is None
        ):
            # The tool handler may already have closed over this ToolContext
            # before Agent construction. Preserve object identity so tool
            # internals that read current_tool_context can emit events.
            tool_context.on_runtime_event = self._record_tool_context_runtime_event
        if tool_context is not None and self.config.tool_result_store_dir:
            tool_context = replace(
                tool_context,
                tool_result_store_dir=self.config.tool_result_store_dir,
                tool_result_store_session_id=(
                    self.config.tool_result_store_session_id
                    or tool_context.tool_result_store_session_id
                    or tool_context.artifact_session_id
                    or self._session_key
                ),
            )
        if tool_context is not None and (
            tool_context.source_diff_preservation_mode
            != self.config.source_diff_preservation_mode
            or tool_context.source_diff_candidate_mode
            != self.config.source_diff_candidate_mode
        ):
            tool_context = replace(
                tool_context,
                source_diff_preservation_mode=self.config.source_diff_preservation_mode,
                source_diff_candidate_mode=self.config.source_diff_candidate_mode,
            )
        if tool_context is not None:
            tool_context = self._apply_configured_tool_result_budget(tool_context)
            tool_context.validate_path_roots()
        self._tool_context: ToolContext | None = tool_context
        # Test-only offline failure seam. ``None`` on every production path,
        # so the provider chat call below stays byte-identical to before when
        # it is unset; a test passes an explicit FailureInjector to script the
        # retry/rotate/fallback chain without a network or a real provider.
        self._failure_injector: FailureInjector | None = failure_injector
        # Optional provider-call ledger boundary.  Keeping both values out of
        # AgentConfig prevents persistence concerns from leaking into provider
        # request configuration.  With no sink, every accounting branch below
        # is skipped and the historical runtime path is unchanged.
        self._usage_event_sink = usage_event_sink
        self._usage_execution_context = usage_execution_context
        self._provider_request_correlation = provider_request_correlation
        if self.tool_handler is not None and self._tool_context is not None:
            self.tool_handler = self._bind_tool_handler_context(
                self.tool_handler,
                self._tool_context,
            )
        self._meta_run_writer = (self.config.metadata or {}).get("meta_run_writer")
        self._pending_warnings: list[WarningEvent] = []
        (
            self._turn_objective_reminder_enabled,
            self._turn_objective_reminder_max_chars,
        ) = _resolve_turn_objective_reminder()

        self._state: AgentState = AgentState.IDLE
        self._history: list[Message] = []
        self._context: ContextAssembly | None = None
        # Typed dependency surface. Either constructor injection or legacy
        # attribute assignment from the runtime is accepted; both reach the same
        # internal slot.
        self._memory_sync_manager: Any | None = memory_sync_manager

        # Memory flush state (sub-agent based, re-entrant per compaction cycle)
        self._flush_done_this_cycle: bool = False
        self._active_flush_task: asyncio.Task | None = None
        self._flush_wait_timed_out_task: asyncio.Task | None = None
        self._flush_backoff_until: float = 0.0
        self._flush_backoff_seconds: float = 0.0
        self._session_flush_service = session_flush_service
        self._last_compaction_refusal_reason: str | None = None
        self._tool_failure_loop_counts: dict[tuple[str, str], int] = {}
        self._identical_request_last_sha: str | None = None
        self._identical_request_streak: int = 0
        self._provider_tool_result_overrides: dict[str, ContentBlockToolResult] = {}
        self._provider_tool_result_frozen_overrides: dict[str, ContentBlockToolResult] = {}
        self._provider_tool_result_frozen_full_ids: set[str] = set()
        self._provider_history_dedup_survivor_ids: set[str] = set()
        self._projected_diagnostic_evidence: dict[str, dict[str, Any]] = {}
        self._focused_retrieved_tool_result_handles: set[str] = set()
        self._tool_result_snapshot_cache: dict[
            tuple[str, str, str, str, str, str], ToolResultRecord
        ] = {}
        self._patch_evidence_ledger: PatchEvidenceLedger | None = None
        if self.config.patch_evidence_ledger_path:
            self._patch_evidence_ledger = PatchEvidenceLedger(
                path=self.config.patch_evidence_ledger_path,
                workspace_dir=self.config.workspace_dir,
                session_key=session_key,
                agent_id=getattr(tool_context, "agent_id", None) if tool_context else None,
            )

    def _context_overflow_error(self) -> ErrorEvent:
        reason = self._last_compaction_refusal_reason
        if reason == "memory_flush_timeout_before_compaction":
            return ErrorEvent(
                message=(
                    "Context compaction could not run because the pre-compaction "
                    "memory flush timed out."
                ),
                code="compaction_refused_flush_timeout",
            )
        if reason == "memory_flush_degraded_before_compaction":
            return ErrorEvent(
                message=(
                    "Context compaction could not run because the pre-compaction "
                    "memory flush did not produce a verified summary."
                ),
                code="compaction_refused_memory_flush",
            )
        if reason == "empty_summary_rejected":
            return ErrorEvent(
                message="Context compaction produced no replacement summary.",
                code="compaction_refused_empty_summary",
            )
        if reason == "compaction_failed":
            return ErrorEvent(
                message="Context compaction failed before the provider request could be retried.",
                code="compaction_failed",
            )
        if reason == "compaction_not_smaller":
            return ErrorEvent(
                message="Context compaction did not reduce the provider request.",
                code="compaction_not_smaller",
            )
        if reason == "provider_recent_tail_too_large":
            return ErrorEvent(
                message=(
                    "The request is too large for the provider context window after "
                    "automatic context compaction and payload reduction. OpenSquilla "
                    "preserved the recoverable state; retry with a narrower request "
                    "or a larger-context model."
                ),
                code="provider_request_too_large",
            )
        if reason == "provider_request_budget_exhausted":
            return ErrorEvent(
                message=(
                    "The request is too large for the provider context window after "
                    "automatic context compaction and payload reduction. OpenSquilla "
                    "preserved the recoverable state; retry with a narrower request "
                    "or a larger-context model."
                ),
                code="provider_request_too_large",
            )
        return ErrorEvent(
            message="Context overflow persists after compaction",
            code="compaction_exhausted",
        )

    def _record_provider_context_overflow_reason(
        self,
        provider_error: ProviderErrorEvent,
    ) -> None:
        if provider_error.code != "provider_request_budget_exhausted":
            return
        proof = self._provider_request_budget_proof(provider_error)
        if proof is None:
            self._last_compaction_refusal_reason = "provider_request_budget_exhausted"
            return
        if proof.get("recent_tail_too_large") is True:
            self._last_compaction_refusal_reason = "provider_recent_tail_too_large"
            return
        if proof.get("compaction_not_smaller") is True:
            self._last_compaction_refusal_reason = "compaction_not_smaller"
            return
        fallback_reason = proof.get("fallback_reason")
        if fallback_reason == "provider_request_budget_exhausted":
            self._last_compaction_refusal_reason = "provider_request_budget_exhausted"

    @staticmethod
    def _provider_request_budget_proof(
        provider_error: ProviderErrorEvent,
    ) -> dict[str, Any] | None:
        if provider_error.code != "provider_request_budget_exhausted":
            return None
        try:
            proof = json.loads(provider_error.message)
        except (TypeError, ValueError):
            return None
        return proof if isinstance(proof, dict) else None

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _configured_tool_result_budget_policy(self) -> ToolResultBudgetPolicy | None:
        single_limit = self._positive_int(
            getattr(self.config, "tool_result_dispatch_max_chars", 0)
        )
        turn_limit = self._positive_int(
            getattr(self.config, "tool_result_dispatch_turn_max_chars", 0)
        )
        if single_limit is None and turn_limit is None:
            return None
        return ToolResultBudgetPolicy(
            max_single_execution_result_chars=single_limit,
            max_execution_tool_result_chars_per_turn=turn_limit,
        )

    def _apply_configured_tool_result_budget(
        self,
        tool_context: ToolContext,
    ) -> ToolContext:
        policy = self._configured_tool_result_budget_policy()
        if policy is None:
            return tool_context
        return replace(
            tool_context,
            tool_result_budget_policy=policy,
        )

    def _bind_tool_handler_context(
        self,
        tool_handler: ToolHandler,
        tool_context: ToolContext,
    ) -> ToolHandler:
        async def _handler(tc: ToolCall) -> ToolResult:
            with bind_provider_request_correlation(
                self._provider_request_correlation,
            ):
                active = current_tool_context.get()
                if (
                    active is not None
                    and getattr(active, "on_runtime_event", None) is not None
                ):
                    return await tool_handler(tc)
                token = current_tool_context.set(tool_context)
                try:
                    return await tool_handler(tc)
                finally:
                    current_tool_context.reset(token)

        return _handler

    def _provider_budget_compaction_window_tokens(
        self,
        provider_error: ProviderErrorEvent,
    ) -> int | None:
        proof = self._provider_request_budget_proof(provider_error)
        if proof is None:
            return None
        proof_budget = self._positive_int(
            proof.get("effective_proof_budget") or proof.get("proof_budget")
        )
        if proof_budget is None:
            return None
        estimated_chars = self._positive_int(proof.get("estimated_chars"))
        estimated_tokens = self._positive_int(proof.get("estimated_tokens"))
        if estimated_chars and estimated_tokens:
            window_tokens = int(proof_budget * (estimated_tokens / estimated_chars))
        else:
            window_tokens = proof_budget // 4
        if window_tokens <= 0:
            return None
        return min(self.config.context_window_tokens, window_tokens)

    def _provider_budget_estimated_tokens(
        self,
        provider_error: ProviderErrorEvent,
    ) -> int | None:
        proof = self._provider_request_budget_proof(provider_error)
        if proof is None:
            return None
        return self._positive_int(proof.get("estimated_tokens"))

    def _provider_request_proof_max_chars(self) -> int:
        return self._context_budget_governor().snapshot().provider_request_max_chars

    def _context_budget_governor(self) -> ContextBudgetGovernor:
        return ContextBudgetGovernor.from_config(self.config)

    @staticmethod
    def _context_budget_class(
        budget_class: ToolResultBudgetClass | None,
    ) -> ContextBudgetClass:
        if budget_class is ToolResultBudgetClass.EXTERNAL:
            return ContextBudgetClass.EXTERNAL
        if budget_class is ToolResultBudgetClass.ARTIFACT:
            return ContextBudgetClass.ARTIFACT
        if budget_class is ToolResultBudgetClass.ERROR:
            return ContextBudgetClass.ERROR
        if budget_class is ToolResultBudgetClass.CONTROL:
            return ContextBudgetClass.CONTROL
        return ContextBudgetClass.LOCAL

    def _tool_use_argument_provider_request_max_chars(self, tool_name: str) -> int:
        budget_class = self._context_budget_class(resolve_budget_class(tool_name))
        return self._context_budget_governor().tool_argument_chars_for(budget_class)

    def _tool_result_provider_request_max_chars(
        self,
        budget_class: ToolResultBudgetClass | None = None,
    ) -> int:
        return self._context_budget_governor().tool_result_provider_chars_for(
            self._context_budget_class(budget_class)
        )

    def _tool_execution_timeout(self, tool_call: ToolCall) -> float:
        timeout = float(self.config.tool_timeout)
        tool_def = self._tool_definition_by_name.get(tool_call.tool_name)
        if tool_def is None:
            return timeout
        static_timeout = getattr(tool_def, "execution_timeout_seconds", None)
        if static_timeout is not None:
            try:
                timeout = max(timeout, float(static_timeout))
            except (TypeError, ValueError):
                pass
        argument_name = getattr(tool_def, "execution_timeout_argument", None)
        if not argument_name:
            return timeout
        raw_value = tool_call.arguments.get(str(argument_name))
        if raw_value is None:
            return timeout
        try:
            argument_timeout = float(raw_value)
        except (TypeError, ValueError):
            return timeout
        if argument_timeout < 0:
            return timeout
        padding = getattr(tool_def, "execution_timeout_padding", 0.0) or 0.0
        try:
            timeout = max(timeout, argument_timeout + float(padding))
        except (TypeError, ValueError):
            timeout = max(timeout, argument_timeout)
        return timeout

    def _tool_activity_heartbeat_interval(self) -> float:
        raw_interval = self.config.metadata.get("tool_activity_heartbeat_interval", 15.0)
        try:
            return float(raw_interval)
        except (TypeError, ValueError):
            return 15.0

    def _approval_wait_timeout(self) -> float:
        raw_timeout = self.config.metadata.get("approval_wait_timeout_seconds", 180.0)
        try:
            return max(0.0, float(raw_timeout))
        except (TypeError, ValueError):
            return 180.0

    def _max_safe_tool_concurrency(self) -> int:
        try:
            value = int(self.config.max_safe_tool_concurrency)
        except (TypeError, ValueError):
            return 6
        return max(1, value)

    def _write_turn_call_log(self, kind: str, **payload: Any) -> None:
        if self._turn_call_logger is not None:
            self._turn_call_logger.write(kind, payload)

    def _notify_provider_call_observer(
        self,
        *,
        ttft_ms: int | None,
        duration_ms: int,
        ok: bool,
        failure_kind: str = "",
    ) -> None:
        """Report one finished provider call to the optional observer.

        The observer is gateway-injected diagnostics plumbing; its failures
        are logged at debug level and must never affect the turn.
        """
        observer = getattr(self.config, "provider_call_observer", None)
        if observer is None:
            return
        provider_id = self.config.provider_id or str(
            getattr(self.provider, "provider_name", "") or ""
        )
        try:
            observer(
                provider_id=provider_id,
                model=self.config.model_id or "",
                ttft_ms=ttft_ms,
                duration_ms=duration_ms,
                ok=ok,
                failure_kind=failure_kind,
            )
        except Exception as exc:  # noqa: BLE001 - observer must never affect the turn
            logger.debug("provider_call_observer_failed", error=str(exc))

    def _write_context_stage(
        self,
        stage: str,
        messages: list[Message],
        **payload: Any,
    ) -> None:
        if self._turn_call_logger is None:
            return
        self._write_turn_call_log(
            "context_stage",
            stage=stage,
            message_count=len(messages),
            payload_chars=session_payload_chars(messages),
            messages=messages,
            **payload,
        )

    def _switch_to_invalid_response_fallback(self, reason: str) -> bool:
        fallback = getattr(self.provider, "fallback_after_invalid_response", None)
        if not callable(fallback):
            return False
        try:
            return bool(fallback(reason))
        except Exception as exc:  # noqa: BLE001 - fallback support is optional
            logger.warning(
                "provider.invalid_response_fallback_failed",
                session_key=self._session_key,
                reason=reason,
                error=str(exc),
            )
            return False

    @staticmethod
    def _tool_call_string_arg(
        tool_call: ToolCall | None,
        *names: str,
    ) -> str | None:
        if tool_call is None:
            return None
        for name in names:
            value = tool_call.arguments.get(name)
            if isinstance(value, str) and value.strip():
                return value
        return None

    def _tokenjuice_max_inline_chars(self, fallback: int | None = None) -> int:
        if fallback is not None and fallback > 0:
            return max(1, int(fallback))
        return max(1, int(self.config.tool_result_projection_max_inline_chars))

    def _fresh_diagnostic_policy_enabled(self) -> bool:
        return bool(
            getattr(
                self.config,
                "tool_result_fresh_diagnostic_policy_enabled",
                False,
            )
        )

    def _diagnostic_retrieval_gate_enabled(self) -> bool:
        return bool(
            getattr(
                self.config,
                "tool_result_diagnostic_retrieval_gate_enabled",
                False,
            )
        )

    def _fresh_diagnostic_inline_max_chars(self) -> int:
        if not self._fresh_diagnostic_policy_enabled():
            return 0
        return max(
            0,
            int(
                getattr(
                    self.config,
                    "tool_result_fresh_diagnostic_inline_max_chars",
                    64_000,
                )
                or 0
            ),
        )

    @staticmethod
    def _tool_result_diagnostic_reason(result: ToolResult, content: str) -> str | None:
        if result.is_error:
            return "is_error"
        status: Mapping[str, Any] = result.execution_status or {}
        if isinstance(status, Mapping):
            preservation_class = str(status.get("preservation_class") or "")
            if preservation_class == "diagnostic":
                return "diagnostic_preservation_class"
            if str(status.get("status") or "") in {"error", "timeout", "cancelled"}:
                return "diagnostic_execution_status"
        scan = content[:_TOOL_RESULT_HINT_SCAN_MAX_CHARS]
        if (
            _TOOL_RESULT_HINT_PATTERN.search(scan)
            and not _CLEAN_TEST_SUMMARY_RE.search(scan)
            and not _CLEAN_PASSED_FAILED_SUMMARY_RE.search(scan)
            and not _CLEAN_ERROR_COUNT_RE.search(scan)
        ):
            return "failure_anchor"
        return None

    def _record_fresh_diagnostic_result(
        self,
        *,
        reason: str,
        tool_name: str,
        tool_use_id: str,
        original_chars: int,
    ) -> None:
        self.config.metadata["tool_projection_fresh_diagnostic_results"] = (
            self.config.metadata.get("tool_projection_fresh_diagnostic_results", 0) + 1
        )
        self._write_turn_call_log(
            "tool_projection_fresh_diagnostic",
            tool_use_id=tool_use_id,
            name=tool_name,
            reason=reason,
            original_chars=original_chars,
        )

    def _record_projected_diagnostic_evidence(
        self,
        *,
        handle: str | None,
        tool_name: str,
        tool_use_id: str,
        reason: str,
        original_chars: int,
        projected_chars: int,
    ) -> None:
        self.config.metadata["tool_projection_fresh_diagnostic_projections"] = (
            self.config.metadata.get("tool_projection_fresh_diagnostic_projections", 0) + 1
        )
        append_runtime_event(
            self.config.runtime_events_path,
            {
                "feature": "tool_result_projection",
                "name": "tool_projection_fresh_diagnostic",
                "action": "projected",
                "reason": reason,
                "session_key": self._session_key,
                "agent_id": self.config.tool_result_store_agent_id
                or self.config.metadata.get("agent_id"),
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "tool_result_handle": handle,
                "tool_result_handle_present": bool(handle),
                "original_chars": original_chars,
                "projected_chars": projected_chars,
            },
        )
        if not self._diagnostic_retrieval_gate_enabled():
            return
        if not handle:
            return
        self._projected_diagnostic_evidence[handle] = {
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "reason": reason,
            "original_chars": original_chars,
            "projected_chars": projected_chars,
        }

    @staticmethod
    def _retrieval_tool_call_handle(tc: ToolCall) -> str | None:
        if tc.tool_name != "retrieve_tool_result":
            return None
        raw_handle = tc.arguments.get("handle")
        if not isinstance(raw_handle, str):
            return None
        handle = raw_handle.strip()
        return handle or None

    @staticmethod
    def _retrieval_tool_call_is_focused(tc: ToolCall) -> bool:
        if tc.tool_name != "retrieve_tool_result":
            return False
        raw_mode = tc.arguments.get("mode")
        mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else ""
        if mode in {"query", "grep", "slice", "head_tail", "raw_slice"}:
            return True
        return any(
            isinstance(tc.arguments.get(key), str) and str(tc.arguments.get(key)).strip()
            for key in ("query", "pattern")
        ) or any(tc.arguments.get(key) is not None for key in ("start_line", "end_line", "offset"))

    def _record_focused_diagnostic_retrieval(
        self,
        tc: ToolCall,
        result: ToolResult,
    ) -> None:
        if result.is_error or not self._retrieval_tool_call_is_focused(tc):
            return
        handle = self._retrieval_tool_call_handle(tc)
        if handle is None or handle not in self._projected_diagnostic_evidence:
            return
        self._focused_retrieved_tool_result_handles.add(handle)
        self.config.metadata["tool_projection_diagnostic_retrievals"] = (
            self.config.metadata.get("tool_projection_diagnostic_retrievals", 0) + 1
        )
        append_runtime_event(
            self.config.runtime_events_path,
            {
                "feature": "tool_result_retrieval",
                "name": "tool_projection_diagnostic_retrieval",
                "session_key": self._session_key,
                "agent_id": self.config.tool_result_store_agent_id
                or self.config.metadata.get("agent_id"),
                "tool_use_id": tc.tool_use_id,
                "tool_name": tc.tool_name,
                "tool_result_handle": handle,
                "mode": tc.arguments.get("mode"),
                "query": tc.arguments.get("query"),
            },
        )
        self._write_turn_call_log(
            "tool_projection_diagnostic_retrieval",
            tool_use_id=tc.tool_use_id,
            name=tc.tool_name,
            tool_result_handle=handle,
            mode=tc.arguments.get("mode"),
            query=tc.arguments.get("query"),
        )

    def _projected_diagnostic_retrieval_gate_tool_result(self, tc: ToolCall) -> ToolResult | None:
        if not self._diagnostic_retrieval_gate_enabled():
            return None
        if tc.tool_name not in _DIAGNOSTIC_RETRIEVAL_GATED_TOOL_NAMES:
            return None
        pending = [
            (handle, details)
            for handle, details in self._projected_diagnostic_evidence.items()
            if handle not in self._focused_retrieved_tool_result_handles
        ]
        if not pending:
            return None
        handle, details = pending[-1]
        self.config.metadata["tool_projection_diagnostic_retrieval_gate_blocks"] = (
            self.config.metadata.get("tool_projection_diagnostic_retrieval_gate_blocks", 0) + 1
        )
        tool_name = str(details.get("tool_name") or "tool")
        reason = str(details.get("reason") or "diagnostic")
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content=(
                "Runtime guard: this action depends on incomplete diagnostic evidence. "
                f"The recent {tool_name} result was projected with preview_complete=false "
                f"for reason {reason!r}. Before calling {tc.tool_name}, use "
                "retrieve_tool_result with the projected tool_result_handle and a focused "
                "query, grep, line slice, or raw_slice for the failing test, traceback, "
                f"line reference, or error phrase. tool_result_handle: {handle}"
            ),
            is_error=True,
            execution_status=runtime_execution_status(
                "error",
                reason="projected_diagnostic_requires_retrieval",
            ),
        )

    def _tokenjuice_tool_reduction(
        self,
        *,
        tool_name: str,
        content: str,
        is_error: bool,
        tool_use_id: str,
        arguments: dict[str, Any] | None = None,
        command: str | None = None,
        cwd: str | None = None,
        max_inline_chars: int | None = None,
    ) -> str | None:
        reduction = reduce_tool_result_with_tokenjuice(
            tool_name=tool_name,
            content=content,
            is_error=is_error,
            tool_use_id=tool_use_id,
            arguments=arguments,
            command=command,
            cwd=cwd,
            max_inline_chars=self._tokenjuice_max_inline_chars(max_inline_chars),
        )
        if reduction is None:
            return None
        self.config.metadata["tool_projection_backend"] = "tokenjuice"
        if reduction.reducer:
            self.config.metadata["tool_projection_tokenjuice_reducer"] = reduction.reducer
        return reduction.inline_text

    def _semantic_tool_result_projection_skip_reason(
        self,
        result: ToolResult,
        *,
        tool_call: ToolCall | None = None,
    ) -> str | None:
        if result.is_error:
            return None
        if result.tool_name in _SEMANTIC_TOOL_RESULT_PROJECTION_SKIP_TOOLS:
            return f"semantic_{result.tool_name}_preserved"
        if result.tool_name == "exec_command" and exec_command_invokes_git_diff(
            self._tool_call_string_arg(tool_call, "command")
        ):
            return "semantic_git_diff_preserved"
        if result.tool_name == "exec_command" and exec_command_invokes_source_context_read(
            self._tool_call_string_arg(tool_call, "command"),
            content=result.content,
        ):
            return "semantic_source_context_preserved"
        return None

    @staticmethod
    def _tool_definition_schema_payload(tool: ToolDefinition) -> dict[str, Any]:
        try:
            return tool.model_dump(mode="json", exclude_none=True)
        except Exception:
            return {
                "name": getattr(tool, "name", ""),
                "description": getattr(tool, "description", ""),
                "input_schema": getattr(tool, "input_schema", None),
            }

    @staticmethod
    def _sha256_short(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def _record_provider_tool_schema_event(
        self,
        *,
        tools: list[ToolDefinition] | None,
        iteration: int,
        attempt: int,
        call_id: str,
        tools_supported: bool,
    ) -> None:
        if not self.config.runtime_events_path:
            return
        tool_names = [tool.name for tool in tools or []]
        target_names = ["retrieve_tool_result"]
        target_schemas: dict[str, dict[str, Any]] = {}
        schema_hashes: dict[str, str] = {}
        for tool in tools or []:
            payload = self._tool_definition_schema_payload(tool)
            payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            schema_hashes[tool.name] = self._sha256_short(payload_json)
            if tool.name in target_names:
                input_schema = payload.get("input_schema") or {}
                properties = (
                    input_schema.get("properties")
                    if isinstance(input_schema, dict)
                    else {}
                )
                target_schemas[tool.name] = {
                    "schema_hash": schema_hashes[tool.name],
                    "description_sha256": self._sha256_short(str(payload.get("description") or "")),
                    "description_chars": len(str(payload.get("description") or "")),
                    "parameter_names": sorted((properties or {}).keys()),
                    "required": list(input_schema.get("required") or [])
                    if isinstance(input_schema, dict)
                    else [],
                }
        append_runtime_event(
            self.config.runtime_events_path,
            {
                "feature": "provider_tool_schema",
                "mechanism": "tool_visibility_observer",
                "mode": "log",
                "reason": "provider_request_tools",
                "session_key": self._session_key,
                "agent_id": self.config.tool_result_store_agent_id
                or self.config.metadata.get("agent_id"),
                "iteration": iteration,
                "attempt": attempt,
                "call_id": call_id,
                "tools_supported": tools_supported,
                "sent_to_provider": bool(tools),
                "tool_count": len(tool_names),
                "tool_names": tool_names,
                "target_tool_visible": {
                    name: name in set(tool_names) for name in target_names
                },
                "target_schemas": target_schemas,
                "schema_hashes": schema_hashes,
            },
        )

    def _record_tool_projection_runtime_event(
        self,
        *,
        outcome: str,
        tool_name: str,
        tool_use_id: str,
        original_chars: int,
        projected_chars: int | None = None,
        reducer: str | None = None,
        tool_result_handle: str | None = None,
        arguments: dict[str, Any] | None = None,
        is_error: bool = False,
        json_guard_applied: bool = False,
        reason: str | None = None,
    ) -> None:
        if not self.config.runtime_events_path:
            return
        event: dict[str, Any] = {
            "feature": "tool_result_projection",
            "mechanism": "tokenjuice",
            "mode": "log",
            "reason": reason or outcome,
            "session_key": self._session_key,
            "agent_id": self.config.tool_result_store_agent_id
            or self.config.metadata.get("agent_id"),
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "outcome": outcome,
            "is_error": is_error,
            "original_chars": original_chars,
            "projected_chars": projected_chars,
            "reducer": reducer,
            "tool_result_handle": tool_result_handle,
            "tool_result_handle_present": bool(tool_result_handle),
            "json_guard_applied": json_guard_applied,
        }
        event_arguments = _projection_event_arguments(arguments)
        if event_arguments is not None:
            event["tool_arguments"] = event_arguments
            command = event_arguments.get("command") or event_arguments.get("cmd")
            if isinstance(command, str):
                event["command"] = command
        if projected_chars is not None:
            event["saved_chars"] = max(0, original_chars - projected_chars)
        append_runtime_event(self.config.runtime_events_path, event)

    def _projection_signal_hints_active(self) -> bool:
        return _projection_signal_hints_enabled(
            bool(getattr(self.config, "projection_signal_hints", False))
        )

    def _record_projection_signal_hint_event(
        self,
        *,
        builder: str,
        tool_name: str,
        tool_use_id: str,
        tool_result_handle: str | None,
        original_chars: int,
        signal_match_lines: int,
        signal_first_line: int | None,
    ) -> None:
        self.config.metadata["tool_projection_signal_hints"] = (
            self.config.metadata.get("tool_projection_signal_hints", 0) + 1
        )
        append_runtime_event(
            self.config.runtime_events_path,
            {
                "feature": "tool_result_projection",
                "name": "projection_signal_hints",
                "action": "hint_appended",
                "mechanism": "signal_scan",
                "mode": "log",
                "session_key": self._session_key,
                "agent_id": self.config.tool_result_store_agent_id
                or self.config.metadata.get("agent_id"),
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "tool_result_handle": tool_result_handle,
                "original_chars": original_chars,
                "signal_match_lines": signal_match_lines,
                "signal_first_line": signal_first_line,
                "builder": builder,
            },
        )

    @staticmethod
    def _count_image_blocks(messages: list[Message]) -> int:
        count = 0
        for message in messages:
            if not isinstance(message.content, list):
                continue
            count += sum(1 for block in message.content if isinstance(block, ContentBlockImage))
        return count

    def _dedup_repeated_tool_results_for_provider(
        self,
        messages: list[Message],
    ) -> list[Message]:
        """Elide older byte-identical tool results in the provider view only.

        Long single-turn episodes re-run the same read/grep/diff commands many
        times, and full-history replay resends every identical payload on every
        iteration. When ``provider_history_dedup_enabled`` is on, the newest
        occurrence of each repeated result stays full and older duplicates are
        replaced by a short stub naming the surviving ``tool_use_id``. The pass
        never mutates persisted history; error results, artifact results, the
        two most recent results, frozen-full results, and existing provider
        projections are left untouched.
        """
        self._provider_history_dedup_survivor_ids = set()
        if not getattr(self.config, "provider_history_dedup_enabled", False):
            return messages
        min_repeats = max(
            2, int(getattr(self.config, "provider_history_dedup_min_repeats", 2) or 2)
        )

        tool_result_refs: list[tuple[int, int, ContentBlockToolResult]] = []
        for message_index, message in enumerate(messages):
            if not isinstance(message.content, list):
                continue
            for block_index, block in enumerate(message.content):
                if isinstance(block, ContentBlockToolResult):
                    tool_result_refs.append((message_index, block_index, block))
        if len(tool_result_refs) < min_repeats:
            return messages

        recent_ids = {id(block) for _m, _b, block in tool_result_refs[-2:]}
        by_digest: dict[str, list[tuple[int, int, ContentBlockToolResult, str]]] = {}
        for message_index, block_index, block in tool_result_refs:
            if not isinstance(block.content, str):
                continue
            content = block.content
            if (
                len(content) < _PROVIDER_HISTORY_DEDUP_MIN_CHARS
                or block.is_error
                or _tool_result_content_has_artifact(content)
                or _tool_result_content_is_provider_projection(content)
            ):
                continue
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            by_digest.setdefault(digest, []).append(
                (message_index, block_index, block, content)
            )

        replacements: dict[tuple[int, int], ContentBlockToolResult] = {}
        survivor_ids: set[str] = set()
        chars_saved = 0
        for digest, occurrences in by_digest.items():
            if len(occurrences) < min_repeats:
                continue
            survivor = occurrences[-1][2]
            for message_index, block_index, block, content in occurrences[:-1]:
                if id(block) in recent_ids:
                    continue
                if block.tool_use_id in self._provider_tool_result_frozen_full_ids:
                    # Already shown to the model as final full content on a
                    # prior request — never retroactively downgrade it, but
                    # still counted above so newer duplicates get elided.
                    continue
                stub = (
                    "[duplicate_tool_result_elided]\n"
                    f"tool_use_id: {block.tool_use_id}\n"
                    f"original_chars: {len(content)}\n"
                    f"sha256: {digest}\n"
                    f"identical_to_tool_use_id: {survivor.tool_use_id}\n"
                    "reason: byte-identical content appears again at the newer "
                    "tool result above; read it there instead of re-running the "
                    "same command.\n"
                )
                replacements[(message_index, block_index)] = ContentBlockToolResult(
                    tool_use_id=block.tool_use_id,
                    content=stub,
                    is_error=block.is_error,
                )
                chars_saved += max(0, len(content) - len(stub))
                survivor_ids.add(survivor.tool_use_id)

        if not replacements:
            return messages

        self._provider_history_dedup_survivor_ids = survivor_ids

        projected: list[Message] = []
        for message_index, message in enumerate(messages):
            if not isinstance(message.content, list):
                projected.append(message)
                continue
            next_content: list[Any] = []
            message_changed = False
            for block_index, block in enumerate(message.content):
                replacement = replacements.get((message_index, block_index))
                if replacement is None:
                    next_content.append(block)
                    continue
                next_content.append(replacement)
                message_changed = True
            if not message_changed:
                projected.append(message)
                continue
            projected.append(
                Message(
                    role=message.role,
                    content=next_content,
                    reasoning_content=getattr(message, "reasoning_content", None),
                )
            )

        self.config.metadata["provider_history_dedup_applied"] = True
        self.config.metadata["provider_history_dedup_elided"] = (
            self.config.metadata.get("provider_history_dedup_elided", 0)
            + len(replacements)
        )
        self.config.metadata["provider_history_dedup_chars_saved"] = (
            self.config.metadata.get("provider_history_dedup_chars_saved", 0)
            + chars_saved
        )
        self._write_turn_call_log(
            "provider_history_dedup",
            elided_tool_results=len(replacements),
            chars_saved=chars_saved,
        )
        return projected

    def _compact_aggregate_tool_results_for_provider(
        self,
        messages: list[Message],
    ) -> list[Message]:
        """Compact old bulky tool results in the provider request view only.

        This pass handles both single oversized tool results and the aggregate
        case where many under-threshold results accumulate across iterations.
        It never mutates persisted history and it preserves recent, error, and
        artifact-producing results unless a successful single result alone
        exceeds the provider request cap.
        """

        tool_name_by_use_id: dict[str, str] = {}
        tool_input_by_use_id: dict[str, dict[str, Any]] = {}
        tool_result_refs: list[tuple[int, int, ContentBlockToolResult]] = []
        for message_index, message in enumerate(messages):
            if not isinstance(message.content, list):
                continue
            for block_index, block in enumerate(message.content):
                if isinstance(block, ContentBlockToolUse):
                    tool_name_by_use_id[block.id] = block.name
                    if isinstance(block.input, dict):
                        tool_input_by_use_id[block.id] = dict(block.input)
                elif isinstance(block, ContentBlockToolResult):
                    tool_result_refs.append((message_index, block_index, block))

        messages = self._compact_absolute_tool_results_for_provider(
            messages,
            tool_result_refs,
            tool_name_by_use_id,
            tool_input_by_use_id,
        )
        tool_result_refs = []
        for message_index, message in enumerate(messages):
            if not isinstance(message.content, list):
                continue
            for block_index, block in enumerate(message.content):
                if isinstance(block, ContentBlockToolResult):
                    tool_result_refs.append((message_index, block_index, block))

        if len(tool_result_refs) <= 2:
            return messages

        recent_ids = {id(block) for _message_index, _block_index, block in tool_result_refs[-2:]}
        budget_tokens = int(self.config.context_window_tokens * _AGGREGATE_TOOL_RESULT_MAX_SHARE)
        eligible_refs: list[tuple[int, int, ContentBlockToolResult, str, int]] = []
        semantic_preserve_refs: list[tuple[str, str, int, str]] = []
        total_tool_result_tokens = 0
        for message_index, block_index, block in tool_result_refs:
            content = block.content if isinstance(block.content, str) else str(block.content)
            tokens = _tool_result_budget_tokens(content)
            total_tool_result_tokens += tokens
            tool_name = tool_name_by_use_id.get(block.tool_use_id, "tool")
            semantic_skip_reason = self._semantic_provider_tool_result_projection_skip_reason(
                tool_use_id=block.tool_use_id,
                tool_name=tool_name,
                content=content,
                is_error=block.is_error,
                arguments=tool_input_by_use_id.get(block.tool_use_id),
            )
            if (
                id(block) in recent_ids
                or block.is_error
                or _tool_result_content_has_artifact(content)
                or _tool_result_content_is_provider_projection(content)
                or semantic_skip_reason is not None
                or block.tool_use_id in self._provider_tool_result_frozen_full_ids
                or block.tool_use_id in self._provider_history_dedup_survivor_ids
            ):
                if semantic_skip_reason is not None:
                    semantic_preserve_refs.append(
                        (block.tool_use_id, tool_name, len(content), semantic_skip_reason)
                    )
                continue
            eligible_refs.append((message_index, block_index, block, content, tokens))

        if total_tool_result_tokens <= budget_tokens:
            return messages
        for tool_use_id, tool_name, original_chars, reason in semantic_preserve_refs:
            self._record_provider_tool_result_semantic_preserve(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                original_chars=original_chars,
                reason=reason,
                mechanism="aggregate",
            )
        if not eligible_refs:
            return messages

        replacements: dict[tuple[int, int], ContentBlockToolResult] = {}
        stored_handles: list[str] = []

        for message_index, block_index, block, content, original_tokens in eligible_refs:
            if total_tool_result_tokens <= budget_tokens:
                break
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            stored = self._store_tool_result_snapshot(
                content,
                tool_use_id=block.tool_use_id,
                tool_name=tool_name_by_use_id.get(block.tool_use_id, "tool"),
            )
            if stored is None and self.config.tool_result_store_dir:
                continue
            if stored is not None:
                stored_handles.append(stored.handle)
            head = content[:240]
            tail = content[-240:] if len(content) > 240 else ""
            omitted = max(0, len(content) - len(head) - len(tail))
            handle_line = f"tool_result_handle: {stored.handle}\n" if stored is not None else ""
            retrieve_hint = _TOOL_RESULT_RETRIEVE_HINT if stored is not None else ""
            search_hints = _tool_result_search_hints(content) if stored is not None else ""
            signal_lines = ""
            if stored is not None and self._projection_signal_hints_active():
                signal_lines, signal_matches, signal_first_line = _tool_result_signal_scan(
                    content,
                    handle=stored.handle,
                    head_chars=len(head),
                    tail_chars=len(tail),
                )
                if signal_lines:
                    self._record_projection_signal_hint_event(
                        builder="aggregate",
                        tool_name=tool_name_by_use_id.get(block.tool_use_id, "tool"),
                        tool_use_id=block.tool_use_id,
                        tool_result_handle=stored.handle,
                        original_chars=len(content),
                        signal_match_lines=signal_matches,
                        signal_first_line=signal_first_line,
                    )
            compacted = (
                "[aggregate_tool_result_compacted]\n"
                f"tool_use_id: {block.tool_use_id}\n"
                f"original_chars: {len(content)}\n"
                f"original_tokens_estimate: {_tool_result_budget_tokens(content)}\n"
                f"sha256: {digest}\n"
                f"{handle_line}"
                f"{retrieve_hint}"
                f"{search_hints}"
                f"{signal_lines}"
                f"omitted_chars: {omitted}\n"
                f"preview_complete: {str(omitted == 0).lower()}\n"
                "reason: older non-error tool result compacted for provider context budget.\n"
                f"head:\n{head}"
            )
            if tail and tail != head:
                compacted += f"\n...\ntail:\n{tail}"
            replacement: ContentBlockToolResult | None
            replacement = ContentBlockToolResult(
                tool_use_id=block.tool_use_id,
                content=compacted,
                is_error=block.is_error,
            )
            replacements[(message_index, block_index)] = replacement
            self._freeze_provider_tool_result_projection(replacement)
            replacement_tokens = _tool_result_budget_tokens(compacted)
            total_tool_result_tokens -= max(0, original_tokens - replacement_tokens)

        if not replacements:
            return messages

        compacted_messages: list[Message] = []
        for message_index, message in enumerate(messages):
            if not isinstance(message.content, list):
                compacted_messages.append(message)
                continue
            next_content: list[Any] = []
            message_changed = False
            for block_index, block in enumerate(message.content):
                replacement = replacements.get((message_index, block_index))
                if replacement is None:
                    next_content.append(block)
                    continue
                next_content.append(replacement)
                message_changed = True
            if not message_changed:
                compacted_messages.append(message)
                continue
            compacted_messages.append(
                Message(
                    role=message.role,
                    content=next_content,
                    reasoning_content=getattr(message, "reasoning_content", None),
                )
            )

        before_tokens = sum(
            _tool_result_budget_tokens(
                block.content if isinstance(block.content, str) else str(block.content)
            )
            for _message_index, _block_index, block in tool_result_refs
        )
        after_tokens = 0
        for message in compacted_messages:
            if not isinstance(message.content, list):
                continue
            for block in message.content:
                if isinstance(block, ContentBlockToolResult):
                    content = (
                        block.content if isinstance(block.content, str) else str(block.content)
                    )
                    after_tokens += _tool_result_budget_tokens(content)
        saved_tokens = max(0, before_tokens - after_tokens)
        if saved_tokens == 0 and replacements:
            saved_tokens = 1

        self.config.metadata["tool_aggregate_projection_applied"] = True
        self.config.metadata["tool_aggregate_projection_calls"] = (
            self.config.metadata.get("tool_aggregate_projection_calls", 0) + 1
        )
        self.config.metadata["tool_aggregate_projection_tokens_before"] = before_tokens
        self.config.metadata["tool_aggregate_projection_tokens_after"] = after_tokens
        self.config.metadata["tool_aggregate_projection_tokens_saved"] = saved_tokens
        self.config.metadata["tool_projection_applied"] = True
        self.config.metadata["tool_projection_calls"] = self.config.metadata.get(
            "tool_projection_calls", 0
        ) + len(replacements)
        self.config.metadata["tool_projection_tokens_before"] = (
            self.config.metadata.get("tool_projection_tokens_before", 0) + before_tokens
        )
        self.config.metadata["tool_projection_tokens_after"] = (
            self.config.metadata.get("tool_projection_tokens_after", 0) + after_tokens
        )
        self.config.metadata["tool_projection_tokens_saved"] = (
            self.config.metadata.get("tool_projection_tokens_saved", 0) + saved_tokens
        )
        self._write_turn_call_log(
            "tool_aggregate_projection",
            original_tool_results=len(tool_result_refs),
            compacted_tool_results=len(replacements),
            tool_result_handles=stored_handles,
            tokens_before=before_tokens,
            tokens_after=after_tokens,
        )
        return compacted_messages

    def _compact_absolute_tool_results_for_provider(
        self,
        messages: list[Message],
        tool_result_refs: list[tuple[int, int, ContentBlockToolResult]],
        tool_name_by_use_id: dict[str, str],
        tool_input_by_use_id: dict[str, dict[str, Any]],
    ) -> list[Message]:
        cap = self._tool_result_provider_request_max_chars(ToolResultBudgetClass.LOCAL)
        if cap <= 0 or not tool_result_refs:
            return messages

        def _content(block: ContentBlockToolResult) -> str:
            return block.content if isinstance(block.content, str) else str(block.content)

        total_chars = sum(len(_content(block)) for _m, _b, block in tool_result_refs)
        external_cap = self._tool_result_provider_request_max_chars(ToolResultBudgetClass.EXTERNAL)
        external_chars = sum(
            len(_content(block))
            for _m, _b, block in tool_result_refs
            if resolve_budget_class(tool_name_by_use_id.get(block.tool_use_id, ""))
            is ToolResultBudgetClass.EXTERNAL
        )
        if total_chars <= cap and external_chars <= external_cap:
            return messages

        def _over_budget() -> bool:
            return total_chars > cap or external_chars > external_cap

        keep_recent = max(0, int(getattr(self.config, "tool_result_external_keep_recent", 2)))
        recent_refs = tool_result_refs[-keep_recent:] if keep_recent else []
        recent_ids = {id(block) for _m, _b, block in recent_refs}
        external_refs = [
            (message_index, block_index, block)
            for message_index, block_index, block in tool_result_refs
            if resolve_budget_class(tool_name_by_use_id.get(block.tool_use_id, ""))
            is ToolResultBudgetClass.EXTERNAL
        ]
        recent_external_refs = external_refs[-keep_recent:] if keep_recent else []
        recent_external_ids = {id(block) for _m, _b, block in recent_external_refs}
        replacements: dict[tuple[int, int], ContentBlockToolResult] = {}

        for message_index, block_index, block in tool_result_refs:
            if not _over_budget():
                break
            content = _content(block)
            tool_name = tool_name_by_use_id.get(block.tool_use_id, "")
            budget_class = resolve_budget_class(tool_name)
            if _tool_result_content_is_provider_projection(content):
                continue
            if block.tool_use_id in self._provider_tool_result_frozen_full_ids:
                continue
            semantic_skip_reason = self._semantic_provider_tool_result_projection_skip_reason(
                tool_use_id=block.tool_use_id,
                tool_name=tool_name or "tool",
                content=content,
                is_error=block.is_error,
                arguments=tool_input_by_use_id.get(block.tool_use_id),
            )
            if semantic_skip_reason is not None:
                self._record_provider_tool_result_semantic_preserve(
                    tool_use_id=block.tool_use_id,
                    tool_name=tool_name or "tool",
                    original_chars=len(content),
                    reason=semantic_skip_reason,
                    mechanism="absolute",
                )
                continue
            result_cap = self._tool_result_provider_request_max_chars(budget_class)
            single_over_budget = result_cap > 0 and len(content) > result_cap
            replacement_content: str | None = None
            if budget_class is ToolResultBudgetClass.CONTROL:
                replacement_content = compact_tool_result_content(
                    tool_name=tool_name,
                    content=content,
                    max_preview_chars=160,
                    budget_class=budget_class,
                    is_error=block.is_error,
                )
            elif (
                budget_class is ToolResultBudgetClass.EXTERNAL
                and not block.is_error
                and not _tool_result_content_has_artifact(content)
                and (single_over_budget or id(block) not in recent_external_ids)
            ):
                replacement_content = self._tool_result_projection_for_provider(
                    content,
                    tool_use_id=block.tool_use_id,
                    tool_name=tool_name or "tool",
                    reason="external tool result compacted for provider request context",
                    max_preview_chars=min(result_cap, 4_000),
                )
            elif (
                not block.is_error
                and not _tool_result_content_has_artifact(content)
                and (
                    single_over_budget
                    or (self.config.context_window_tokens >= 64_000 and id(block) not in recent_ids)
                )
            ):
                replacement_content = self._tool_result_projection_for_provider(
                    content=content,
                    tool_use_id=block.tool_use_id,
                    tool_name=tool_name or "tool",
                    reason="tool result compacted for provider request context",
                    max_preview_chars=min(result_cap, 4_000),
                )

            if replacement_content is None or len(replacement_content) >= len(content):
                continue
            replacement: ContentBlockToolResult | None
            replacement = ContentBlockToolResult(
                tool_use_id=block.tool_use_id,
                content=replacement_content,
                is_error=block.is_error,
            )
            replacements[(message_index, block_index)] = replacement
            self._freeze_provider_tool_result_projection(replacement)
            saved_chars = len(content) - len(replacement_content)
            total_chars -= saved_chars
            if budget_class is ToolResultBudgetClass.EXTERNAL:
                external_chars -= saved_chars

        if not replacements:
            return messages

        compacted_messages: list[Message] = []
        for message_index, message in enumerate(messages):
            if not isinstance(message.content, list):
                compacted_messages.append(message)
                continue
            next_content: list[Any] = []
            message_changed = False
            for block_index, content_block in enumerate(message.content):
                replacement = replacements.get((message_index, block_index))
                if replacement is None:
                    next_content.append(content_block)
                    continue
                next_content.append(replacement)
                message_changed = True
            if not message_changed:
                compacted_messages.append(message)
                continue
            compacted_messages.append(
                Message(
                    role=message.role,
                    content=next_content,
                    reasoning_content=getattr(message, "reasoning_content", None),
                )
            )

        self.config.metadata["tool_provider_guard_projection_applied"] = True
        self.config.metadata["tool_provider_guard_projection_calls"] = (
            self.config.metadata.get("tool_provider_guard_projection_calls", 0) + 1
        )
        self.config.metadata["tool_projection_applied"] = True
        self.config.metadata["tool_projection_calls"] = self.config.metadata.get(
            "tool_projection_calls", 0
        ) + len(replacements)
        return compacted_messages

    def _semantic_provider_tool_result_projection_skip_reason(
        self,
        *,
        tool_use_id: str,
        tool_name: str,
        content: str,
        is_error: bool,
        arguments: dict[str, Any] | None,
    ) -> str | None:
        tool_call = (
            ToolCall(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                arguments=arguments,
            )
            if arguments is not None
            else None
        )
        return self._semantic_tool_result_projection_skip_reason(
            ToolResult(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                content=content,
                is_error=is_error,
            ),
            tool_call=tool_call,
        )

    def _record_provider_tool_result_semantic_preserve(
        self,
        *,
        tool_use_id: str,
        tool_name: str,
        original_chars: int,
        reason: str,
        mechanism: str,
    ) -> None:
        self.config.metadata["tool_provider_projection_semantic_preserves"] = (
            self.config.metadata.get("tool_provider_projection_semantic_preserves", 0) + 1
        )
        self._write_turn_call_log(
            "tool_provider_projection_noop",
            tool_use_id=tool_use_id,
            name=tool_name,
            original_chars=original_chars,
            reason=reason,
            mechanism=mechanism,
        )

    def _tool_result_projection_for_provider(
        self,
        content: str,
        *,
        tool_use_id: str,
        tool_name: str,
        reason: str,
        max_preview_chars: int,
    ) -> str | None:
        max_preview_chars = max(0, int(max_preview_chars))
        if max_preview_chars > 0:
            max_preview_chars = max(1, min(max_preview_chars, 4_000))
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        stored = self._store_tool_result_snapshot(
            content,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
        )
        if stored is None and self.config.tool_result_store_dir:
            return None
        handle_line = f"tool_result_handle: {stored.handle}\n" if stored is not None else ""
        retrieve_hint = _TOOL_RESULT_RETRIEVE_HINT if stored is not None else ""
        search_hints = _tool_result_search_hints(content) if stored is not None else ""
        if max_preview_chars <= 0:
            head = ""
            tail = ""
        elif len(content) <= max_preview_chars:
            head = content
            tail = ""
        else:
            head_chars = max(1, int(max_preview_chars * 0.65))
            tail_chars = max(0, max_preview_chars - head_chars)
            head = content[:head_chars]
            tail = content[-tail_chars:] if tail_chars else ""
        omitted = max(0, len(content) - len(head) - len(tail))
        signal_lines = ""
        if stored is not None and self._projection_signal_hints_active():
            signal_lines, signal_matches, signal_first_line = _tool_result_signal_scan(
                content,
                handle=stored.handle,
                head_chars=len(head),
                tail_chars=len(tail),
            )
            if signal_lines:
                self._record_projection_signal_hint_event(
                    builder="provider_single",
                    tool_name=tool_name,
                    tool_use_id=tool_use_id,
                    tool_result_handle=stored.handle,
                    original_chars=len(content),
                    signal_match_lines=signal_matches,
                    signal_first_line=signal_first_line,
                )
        projection = (
            "[tool_result_projection]\n"
            f"tool: {tool_name}\n"
            f"tool_use_id: {tool_use_id}\n"
            f"original_chars: {len(content)}\n"
            f"sha256: {digest}\n"
            f"{handle_line}"
            f"{retrieve_hint}"
            f"{search_hints}"
            f"{signal_lines}"
            f"omitted_chars: {omitted}\n"
            f"preview_complete: {str(omitted == 0).lower()}\n"
            f"reason: {reason}.\n"
            f"head:\n{head}"
        )
        if tail:
            projection += f"\n...\ntail:\n{tail}"
        return projection

    def _sanitize_projected_tool_use_arguments_for_provider(
        self,
        messages: list[Message],
        *,
        record: bool = True,
    ) -> list[Message]:
        cap = self._tool_use_argument_provider_request_max_chars("")
        replacements: dict[tuple[int, int], ContentBlockToolUse] = {}

        for message_index, message in enumerate(messages):
            if not isinstance(message.content, list):
                continue
            for block_index, block in enumerate(message.content):
                if not isinstance(block, ContentBlockToolUse):
                    continue
                if self._has_provider_context_argument_marker(block.input):
                    replacements[(message_index, block_index)] = ContentBlockToolUse(
                        id=block.id,
                        name=block.name,
                        input=self._provider_compacted_arguments_placeholder(
                            block.name,
                            block.input,
                        ),
                    )
                    continue

                legacy_projected_input = dict(block.input)
                legacy_projection_scrubbed = False
                for key, value in block.input.items():
                    if not isinstance(value, str) or not value.startswith(
                        (
                            _TOOL_ARGUMENT_PROJECTION_PREFIX,
                            _HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX,
                            _INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX,
                        )
                    ):
                        continue
                    legacy_projected_input[key] = self._provider_projection_placeholder(
                        block.name,
                        key,
                    )
                    legacy_projection_scrubbed = True
                if legacy_projection_scrubbed:
                    replacements[(message_index, block_index)] = ContentBlockToolUse(
                        id=block.id,
                        name=block.name,
                        input=legacy_projected_input,
                    )

        if not replacements:
            return messages

        sanitized_messages: list[Message] = []
        for message_index, message in enumerate(messages):
            if not isinstance(message.content, list):
                sanitized_messages.append(message)
                continue
            next_content: list[Any] = []
            changed = False
            for block_index, block in enumerate(message.content):
                replacement = replacements.get((message_index, block_index))
                if replacement is None:
                    next_content.append(block)
                    continue
                next_content.append(replacement)
                changed = True
            if not changed:
                sanitized_messages.append(message)
                continue
            if not next_content:
                continue
            sanitized_messages.append(
                Message(
                    role=message.role,
                    content=next_content,
                    reasoning_content=getattr(message, "reasoning_content", None),
                )
            )

        if record:
            self.config.metadata["tool_argument_provider_view_summaries_applied"] = True
            metadata_key = "tool_argument_provider_view_summaries"
            self.config.metadata[metadata_key] = self.config.metadata.get(
                metadata_key, 0
            ) + len(replacements)
            self._write_turn_call_log(
                "tool_argument_provider_view_summary",
                sanitized_tool_uses=len(replacements),
                max_chars=cap,
            )
        return sanitized_messages

    def _store_tool_result_snapshot(
        self,
        content: str,
        *,
        tool_use_id: str,
        tool_name: str,
    ) -> ToolResultRecord | None:
        if not self.config.tool_result_store_dir:
            return None
        session_id = self.config.tool_result_store_session_id or self._session_key
        session_key = self.config.tool_result_store_session_key or self._session_key
        agent_id = self.config.tool_result_store_agent_id
        if not agent_id and session_key:
            from opensquilla.session.keys import parse_agent_id

            agent_id = parse_agent_id(session_key)
        if not session_id or not session_key or not agent_id:
            self.config.metadata["tool_result_store_skips"] = (
                self.config.metadata.get("tool_result_store_skips", 0) + 1
            )
            return None
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        cache_key = (session_id, session_key, agent_id, tool_use_id, tool_name, sha)
        store = ToolResultStore(self.config.tool_result_store_dir)
        cached = self._tool_result_snapshot_cache.get(cache_key)
        if cached is not None:
            try:
                meta_path = (
                    store._record_dir(cached.handle, session_id=session_id)
                    / TOOL_RESULT_META_NAME
                )
            except ValueError:
                meta_path = None
            if meta_path is not None and meta_path.exists():
                self.config.metadata["tool_result_store_cache_hits"] = (
                    self.config.metadata.get("tool_result_store_cache_hits", 0) + 1
                )
                return cached
            self._tool_result_snapshot_cache.pop(cache_key, None)
        try:
            record = store.write(
                content,
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                session_id=session_id,
                session_key=session_key,
                agent_id=agent_id,
                max_bytes=self.config.tool_result_store_max_bytes,
                disk_budget_bytes=self.config.tool_result_store_disk_budget_bytes,
                retention_seconds=self.config.tool_result_store_retention_seconds,
            )
        except ToolResultStoreBudgetError as exc:
            self.config.metadata["tool_result_store_skips"] = (
                self.config.metadata.get("tool_result_store_skips", 0) + 1
            )
            logger.info(
                "tool_result_store.skipped",
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                reason=str(exc),
            )
            return None
        except Exception as exc:  # pragma: no cover - storage must not break turns
            logger.warning(
                "tool_result_store.write_failed",
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                error=str(exc),
            )
            return None
        self.config.metadata["tool_result_store_writes"] = (
            self.config.metadata.get("tool_result_store_writes", 0) + 1
        )
        self._tool_result_snapshot_cache[cache_key] = record
        return record

    def _tool_result_projection_payload(
        self,
        stored: ToolResultRecord,
        *,
        raw_content: str,
        projected_content: str,
        signal_lines: str = "",
    ) -> str:
        return (
            "[tool_result_projection]\n"
            f"tool_result_handle: {stored.handle}\n"
            f"sha256: {stored.sha256}\n"
            f"original_chars: {stored.chars}\n"
            "preview_complete: false\n"
            f"{_TOOL_RESULT_RETRIEVE_HINT}"
            f"{_tool_result_search_hints(raw_content)}"
            f"{signal_lines}"
            f"{projected_content}"
        )

    def _tool_result_projection_store_unavailable_noop(
        self,
        result: ToolResult,
        *,
        reason: str,
        arguments: dict[str, Any] | None = None,
        projected_chars: int | None = None,
        reducer: str | None = None,
        json_guard_applied: bool = False,
    ) -> ToolResult:
        self.config.metadata["tool_projection_noops"] = (
            self.config.metadata.get("tool_projection_noops", 0) + 1
        )
        self._write_turn_call_log(
            "tool_projection_noop",
            tool_use_id=result.tool_use_id,
            name=result.tool_name,
            original_chars=len(result.content),
            projected_chars=projected_chars,
            reason=reason,
        )
        self._record_tool_projection_runtime_event(
            outcome="noop",
            reason=reason,
            tool_name=result.tool_name,
            tool_use_id=result.tool_use_id,
            original_chars=len(result.content),
            projected_chars=projected_chars,
            reducer=reducer,
            tool_result_handle=None,
            arguments=arguments,
            is_error=result.is_error,
            json_guard_applied=json_guard_applied,
        )
        return result

    def _json_guard_projection_result(
        self,
        *,
        original_result: ToolResult,
        guarded_result: ToolResult,
        stored: ToolResultRecord,
        raw_content: str,
        arguments: dict[str, Any] | None = None,
    ) -> ToolResult:
        signal_lines = ""
        signal_matches = 0
        signal_first_line: int | None = None
        if self._projection_signal_hints_active():
            signal_lines, signal_matches, signal_first_line = _tool_result_signal_scan(
                raw_content,
                handle=stored.handle,
                preview_lines=frozenset(guarded_result.content.splitlines()),
            )
        projected_content = self._tool_result_projection_payload(
            stored,
            raw_content=raw_content,
            projected_content=guarded_result.content,
            signal_lines=signal_lines,
        )
        if len(projected_content) >= len(raw_content):
            return self._tool_result_projection_store_unavailable_noop(
                original_result,
                reason="json_guard_non_shrinking_after_envelope",
                arguments=arguments,
                projected_chars=len(projected_content),
                json_guard_applied=True,
            )
        if signal_lines:
            self._record_projection_signal_hint_event(
                builder="json_guard",
                tool_name=guarded_result.tool_name,
                tool_use_id=guarded_result.tool_use_id,
                tool_result_handle=stored.handle,
                original_chars=len(raw_content),
                signal_match_lines=signal_matches,
                signal_first_line=signal_first_line,
            )

        tokens_before = get_approx_tokens(raw_content)
        tokens_after = get_approx_tokens(projected_content)
        self.config.metadata["tool_projection_applied"] = True
        self.config.metadata["tool_projection_calls"] = (
            self.config.metadata.get("tool_projection_calls", 0) + 1
        )
        self.config.metadata["tool_projection_tokens_before"] = (
            self.config.metadata.get("tool_projection_tokens_before", 0) + tokens_before
        )
        self.config.metadata["tool_projection_tokens_after"] = (
            self.config.metadata.get("tool_projection_tokens_after", 0) + tokens_after
        )
        self.config.metadata["tool_projection_tokens_saved"] = self.config.metadata.get(
            "tool_projection_tokens_saved", 0
        ) + max(0, tokens_before - tokens_after)
        self._write_turn_call_log(
            "tool_projection_applied",
            tool_use_id=guarded_result.tool_use_id,
            name=guarded_result.tool_name,
            tool_result_handle=stored.handle,
            original_chars=len(raw_content),
            projected_chars=len(projected_content),
            reason="json_guard",
        )
        self._record_tool_projection_runtime_event(
            outcome="applied",
            reason="json_guard",
            tool_name=guarded_result.tool_name,
            tool_use_id=guarded_result.tool_use_id,
            original_chars=len(raw_content),
            projected_chars=len(projected_content),
            reducer="json_guard",
            tool_result_handle=stored.handle,
            arguments=arguments,
            is_error=guarded_result.is_error,
            json_guard_applied=True,
        )
        return ToolResult(
            tool_use_id=guarded_result.tool_use_id,
            tool_name=guarded_result.tool_name,
            content=projected_content,
            is_error=guarded_result.is_error,
            artifacts=list(guarded_result.artifacts),
            execution_status=guarded_result.execution_status,
            terminates_turn=guarded_result.terminates_turn,
        )

    async def _project_tool_result_for_llm(
        self,
        result: ToolResult,
        *,
        tool_call: ToolCall | None = None,
    ) -> ToolResult:
        original_result = result
        projection_arguments = (
            dict(tool_call.arguments)
            if tool_call is not None and isinstance(tool_call.arguments, dict)
            else None
        )
        raw_snapshot_content = result.content
        raw_snapshot_record: ToolResultRecord | None = None
        raw_snapshot_store_attempted = False
        if self.config.tool_result_store_full_trace and self.config.tool_result_store_dir:
            raw_snapshot_store_attempted = True
            # The snapshot write does blocking filesystem work — including a store-wide
            # cleanup scan — so run it in a worker thread to keep the gateway event loop
            # responsive while the store grows (issue #305).
            raw_snapshot_record = await asyncio.to_thread(
                self._store_tool_result_snapshot,
                raw_snapshot_content,
                tool_use_id=result.tool_use_id,
                tool_name=result.tool_name,
            )
        json_guard_record: ToolResultRecord | None = None
        guarded_content, guarded = _omit_large_json_tool_fields(result.content)
        if guarded:
            if self.config.tool_result_store_dir:
                json_guard_record = raw_snapshot_record
                if json_guard_record is None and not raw_snapshot_store_attempted:
                    json_guard_record = await asyncio.to_thread(
                        self._store_tool_result_snapshot,
                        result.content,
                        tool_use_id=result.tool_use_id,
                        tool_name=result.tool_name,
                    )
                if json_guard_record is None:
                    return self._tool_result_projection_store_unavailable_noop(
                        original_result,
                        reason="json_guard_store_unavailable",
                        arguments=projection_arguments,
                        projected_chars=len(guarded_content),
                        json_guard_applied=True,
                    )
            result = ToolResult(
                tool_use_id=result.tool_use_id,
                tool_name=result.tool_name,
                content=guarded_content,
                is_error=result.is_error,
                artifacts=list(result.artifacts),
                execution_status=(
                    mark_execution_status_truncated(result.execution_status)
                    if result.execution_status is not None
                    else None
                ),
                terminates_turn=result.terminates_turn,
            )
            self.config.metadata["tool_json_guard_applied"] = True
            self.config.metadata["tool_json_guard_calls"] = (
                self.config.metadata.get("tool_json_guard_calls", 0) + 1
            )
        json_guard_applied = guarded

        self.config.metadata["tool_projection_attempts"] = (
            self.config.metadata.get("tool_projection_attempts", 0) + 1
        )
        diagnostic_reason = self._tool_result_diagnostic_reason(result, raw_snapshot_content)
        if diagnostic_reason is not None:
            self._record_fresh_diagnostic_result(
                reason=diagnostic_reason,
                tool_name=result.tool_name,
                tool_use_id=result.tool_use_id,
                original_chars=len(raw_snapshot_content),
            )
        semantic_skip_reason = self._semantic_tool_result_projection_skip_reason(
            result,
            tool_call=tool_call,
        )
        if semantic_skip_reason is not None:
            if json_guard_record is not None:
                return self._json_guard_projection_result(
                    original_result=original_result,
                    guarded_result=result,
                    stored=json_guard_record,
                    raw_content=raw_snapshot_content,
                    arguments=projection_arguments,
                )
            self.config.metadata["tool_projection_noops"] = (
                self.config.metadata.get("tool_projection_noops", 0) + 1
            )
            self.config.metadata["tool_projection_semantic_preserves"] = (
                self.config.metadata.get("tool_projection_semantic_preserves", 0) + 1
            )
            self._write_turn_call_log(
                "tool_projection_noop",
                tool_use_id=result.tool_use_id,
                name=result.tool_name,
                original_chars=len(result.content),
                reason=semantic_skip_reason,
            )
            self._record_tool_projection_runtime_event(
                outcome="noop",
                reason=semantic_skip_reason,
                tool_name=result.tool_name,
                tool_use_id=result.tool_use_id,
                original_chars=len(result.content),
                arguments=projection_arguments,
                is_error=result.is_error,
                json_guard_applied=json_guard_applied,
            )
            return result
        fresh_diagnostic_cap = self._fresh_diagnostic_inline_max_chars()
        if (
            diagnostic_reason is not None
            and fresh_diagnostic_cap > 0
            and len(raw_snapshot_content) <= fresh_diagnostic_cap
            and not json_guard_applied
        ):
            self.config.metadata["tool_projection_noops"] = (
                self.config.metadata.get("tool_projection_noops", 0) + 1
            )
            self.config.metadata["tool_projection_fresh_diagnostic_one_hop_preserves"] = (
                self.config.metadata.get(
                    "tool_projection_fresh_diagnostic_one_hop_preserves",
                    0,
                )
                + 1
            )
            append_runtime_event(
                self.config.runtime_events_path,
                {
                    "feature": "tool_result_projection",
                    "name": "tool_projection_fresh_diagnostic",
                    "action": "one_hop_preserved",
                    "reason": diagnostic_reason,
                    "session_key": self._session_key,
                    "agent_id": self.config.tool_result_store_agent_id
                    or self.config.metadata.get("agent_id"),
                    "tool_name": result.tool_name,
                    "tool_use_id": result.tool_use_id,
                    "original_chars": len(raw_snapshot_content),
                },
            )
            self._write_turn_call_log(
                "tool_projection_noop",
                tool_use_id=result.tool_use_id,
                name=result.tool_name,
                original_chars=len(raw_snapshot_content),
                reason="fresh_diagnostic_one_hop_preserved",
                diagnostic_reason=diagnostic_reason,
            )
            self._record_tool_projection_runtime_event(
                outcome="noop",
                reason="fresh_diagnostic_one_hop_preserved",
                tool_name=result.tool_name,
                tool_use_id=result.tool_use_id,
                original_chars=len(raw_snapshot_content),
                arguments=projection_arguments,
                is_error=result.is_error,
                json_guard_applied=json_guard_applied,
            )
            return original_result
        reduction = reduce_tool_result_with_tokenjuice(
            tool_name=result.tool_name,
            content=result.content,
            is_error=result.is_error,
            tool_use_id=result.tool_use_id,
            arguments=tool_call.arguments if tool_call is not None else None,
            command=self._tool_call_string_arg(tool_call, "command"),
            cwd=self._tool_call_string_arg(tool_call, "workdir", "cwd"),
            max_inline_chars=self._tokenjuice_max_inline_chars(),
        )
        if reduction is None:
            if json_guard_record is not None:
                return self._json_guard_projection_result(
                    original_result=original_result,
                    guarded_result=result,
                    stored=json_guard_record,
                    raw_content=raw_snapshot_content,
                    arguments=projection_arguments,
                )
            self.config.metadata["tool_projection_noops"] = (
                self.config.metadata.get("tool_projection_noops", 0) + 1
            )
            self._write_turn_call_log(
                "tool_projection_noop",
                tool_use_id=result.tool_use_id,
                name=result.tool_name,
                original_chars=len(result.content),
            )
            self._record_tool_projection_runtime_event(
                outcome="noop",
                reason="no_reduction",
                tool_name=result.tool_name,
                tool_use_id=result.tool_use_id,
                original_chars=len(result.content),
                arguments=projection_arguments,
                is_error=result.is_error,
                json_guard_applied=json_guard_applied,
            )
            return result
        self.config.metadata["tool_projection_backend"] = "tokenjuice"
        if reduction.reducer:
            self.config.metadata["tool_projection_tokenjuice_reducer"] = reduction.reducer
        projected_content = reduction.inline_text

        stored: ToolResultRecord | None = None
        stored_handle: str | None = None
        signal_matches = 0
        signal_first_line: int | None = None
        if self.config.tool_result_store_dir:
            placeholder_handle = "tr-" + ("0" * 32)
            # Scan once here and re-render with the real handle after the
            # store write: placeholder and stored handles have identical
            # length, so the probe below measures the true envelope size.
            probe_signal_lines = ""
            if self._projection_signal_hints_active():
                (
                    probe_signal_lines,
                    signal_matches,
                    signal_first_line,
                ) = _tool_result_signal_scan(
                    raw_snapshot_content,
                    handle=placeholder_handle,
                    preview_lines=frozenset(projected_content.splitlines()),
                )
            candidate_with_envelope = (
                "[tool_result_projection]\n"
                f"tool_result_handle: {placeholder_handle}\n"
                f"sha256: {hashlib.sha256(raw_snapshot_content.encode('utf-8')).hexdigest()}\n"
                f"original_chars: {len(raw_snapshot_content)}\n"
                f"{_TOOL_RESULT_RETRIEVE_HINT}"
                f"{_tool_result_search_hints(raw_snapshot_content)}"
                f"{probe_signal_lines}"
                f"{projected_content}"
            )
            if len(candidate_with_envelope) >= len(raw_snapshot_content):
                self.config.metadata["tool_projection_noops"] = (
                    self.config.metadata.get("tool_projection_noops", 0) + 1
                )
                self._write_turn_call_log(
                    "tool_projection_noop",
                    tool_use_id=result.tool_use_id,
                    name=result.tool_name,
                    original_chars=len(raw_snapshot_content),
                    projected_chars=len(candidate_with_envelope),
                    reason="non_shrinking_after_envelope",
                )
                self._record_tool_projection_runtime_event(
                    outcome="noop",
                    reason="non_shrinking_after_envelope",
                    tool_name=result.tool_name,
                    tool_use_id=result.tool_use_id,
                    original_chars=len(raw_snapshot_content),
                    projected_chars=len(candidate_with_envelope),
                    reducer=reduction.reducer,
                    tool_result_handle=None,
                    arguments=projection_arguments,
                    is_error=result.is_error,
                    json_guard_applied=json_guard_applied,
                )
                return original_result
            stored = json_guard_record
            if stored is None:
                stored = raw_snapshot_record
            if stored is None and not raw_snapshot_store_attempted:
                stored = await asyncio.to_thread(
                    self._store_tool_result_snapshot,
                    raw_snapshot_content,
                    tool_use_id=result.tool_use_id,
                    tool_name=result.tool_name,
                )
            stored_handle = stored.handle if stored is not None else None
            if stored is None:
                return self._tool_result_projection_store_unavailable_noop(
                    original_result,
                    reason="tool_result_store_unavailable",
                    arguments=projection_arguments,
                    projected_chars=len(projected_content),
                    reducer=reduction.reducer,
                    json_guard_applied=json_guard_applied,
                )
        signal_lines = ""
        if stored is not None:
            signal_lines = _render_projection_signal_lines(
                handle=stored.handle,
                match_count=signal_matches,
                first_line_number=signal_first_line,
            )
            projected_content = self._tool_result_projection_payload(
                stored,
                raw_content=raw_snapshot_content,
                projected_content=projected_content,
                signal_lines=signal_lines,
            )

        if len(projected_content) >= len(raw_snapshot_content):
            self.config.metadata["tool_projection_noops"] = (
                self.config.metadata.get("tool_projection_noops", 0) + 1
            )
            self._write_turn_call_log(
                "tool_projection_noop",
                tool_use_id=result.tool_use_id,
                name=result.tool_name,
                original_chars=len(raw_snapshot_content),
                projected_chars=len(projected_content),
                reason="non_shrinking_after_envelope",
            )
            self._record_tool_projection_runtime_event(
                outcome="noop",
                reason="non_shrinking_after_envelope",
                tool_name=result.tool_name,
                tool_use_id=result.tool_use_id,
                original_chars=len(raw_snapshot_content),
                projected_chars=len(projected_content),
                reducer=reduction.reducer,
                tool_result_handle=stored_handle,
                arguments=projection_arguments,
                is_error=result.is_error,
                json_guard_applied=json_guard_applied,
            )
            return original_result

        if signal_lines and stored is not None:
            self._record_projection_signal_hint_event(
                builder="fresh",
                tool_name=result.tool_name,
                tool_use_id=result.tool_use_id,
                tool_result_handle=stored.handle,
                original_chars=len(raw_snapshot_content),
                signal_match_lines=signal_matches,
                signal_first_line=signal_first_line,
            )

        tokens_before = get_approx_tokens(raw_snapshot_content)
        tokens_after = get_approx_tokens(projected_content)
        self.config.metadata["tool_projection_applied"] = True
        self.config.metadata["tool_projection_calls"] = (
            self.config.metadata.get("tool_projection_calls", 0) + 1
        )
        self.config.metadata["tool_projection_tokens_before"] = (
            self.config.metadata.get("tool_projection_tokens_before", 0) + tokens_before
        )
        self.config.metadata["tool_projection_tokens_after"] = (
            self.config.metadata.get("tool_projection_tokens_after", 0) + tokens_after
        )
        self.config.metadata["tool_projection_tokens_saved"] = self.config.metadata.get(
            "tool_projection_tokens_saved", 0
        ) + max(0, tokens_before - tokens_after)

        self._write_turn_call_log(
            "tool_projection_applied",
            tool_use_id=result.tool_use_id,
            name=result.tool_name,
            tool_result_handle=stored_handle,
            original_chars=len(raw_snapshot_content),
            projected_chars=len(projected_content),
        )
        self._record_tool_projection_runtime_event(
            outcome="applied",
            tool_name=result.tool_name,
            tool_use_id=result.tool_use_id,
            original_chars=len(raw_snapshot_content),
            projected_chars=len(projected_content),
            reducer=reduction.reducer,
            tool_result_handle=stored_handle,
            arguments=projection_arguments,
            is_error=result.is_error,
            json_guard_applied=json_guard_applied,
        )
        if diagnostic_reason is not None:
            self._record_projected_diagnostic_evidence(
                handle=stored_handle,
                tool_name=result.tool_name,
                tool_use_id=result.tool_use_id,
                reason=diagnostic_reason,
                original_chars=len(raw_snapshot_content),
                projected_chars=len(projected_content),
            )
        return ToolResult(
            tool_use_id=result.tool_use_id,
            tool_name=result.tool_name,
            content=projected_content,
            is_error=result.is_error,
            artifacts=list(result.artifacts),
            execution_status=result.execution_status,
            terminates_turn=result.terminates_turn,
        )

    async def _canonicalize_tool_result(
        self,
        result: ToolResult,
        *,
        tool_call: ToolCall | None = None,
    ) -> ToolResult:
        return await self._project_tool_result_for_llm(result, tool_call=tool_call)

    def _record_provider_tool_result_projection(
        self,
        result: ToolResult,
        projected_result: ToolResult,
    ) -> None:
        if projected_result.content != result.content:
            self._freeze_provider_tool_result_projection(
                ContentBlockToolResult(
                    tool_use_id=projected_result.tool_use_id,
                    content=projected_result.content,
                    is_error=projected_result.is_error,
                    execution_status=projected_result.execution_status,
                )
            )
            return
        self._provider_tool_result_overrides.pop(result.tool_use_id, None)
        if not _tool_result_content_is_provider_projection(result.content):
            self._provider_tool_result_frozen_full_ids.add(result.tool_use_id)

    def _freeze_provider_tool_result_projection(self, replacement: ContentBlockToolResult) -> None:
        self._provider_tool_result_frozen_full_ids.discard(replacement.tool_use_id)
        self._provider_tool_result_overrides[replacement.tool_use_id] = replacement
        self._provider_tool_result_frozen_overrides.setdefault(
            replacement.tool_use_id,
            ContentBlockToolResult(
                tool_use_id=replacement.tool_use_id,
                content=replacement.content,
                is_error=replacement.is_error,
                execution_status=replacement.execution_status,
            ),
        )

    def _remember_provider_visible_tool_results(self, messages: list[Message]) -> None:
        for message in messages:
            if not isinstance(message.content, list):
                continue
            for block in message.content:
                if not isinstance(block, ContentBlockToolResult):
                    continue
                if block.tool_use_id in self._provider_tool_result_frozen_overrides:
                    continue
                content = block.content if isinstance(block.content, str) else str(block.content)
                if content.startswith("[duplicate_tool_result_elided]\n"):
                    # Dedup elision depends on another block's current state
                    # (its survivor), not solely on this block's own content —
                    # never freeze it; let dedup recompute it every request.
                    continue
                if _tool_result_content_is_provider_projection(content):
                    self._freeze_provider_tool_result_projection(
                        ContentBlockToolResult(
                            tool_use_id=block.tool_use_id,
                            content=content,
                            is_error=block.is_error,
                            execution_status=block.execution_status,
                        )
                    )
                    continue
                self._provider_tool_result_frozen_full_ids.add(block.tool_use_id)

    async def _project_tool_result_for_delivery(
        self,
        result: ToolResult,
        *,
        tool_call: ToolCall | None = None,
    ) -> ToolResult:
        if _pending_approval_payload(result.content) is not None:
            self._provider_tool_result_overrides.pop(result.tool_use_id, None)
            return result
        projected_result = await self._project_tool_result_for_llm(
            result,
            tool_call=tool_call,
        )
        self._record_provider_tool_result_projection(result, projected_result)
        return projected_result

    def _tool_result_compression_mode(self) -> str:
        mode = self.config.tool_result_compression_mode
        if mode in {"off", "truncate", "summarize"}:
            return mode
        return "truncate" if self.config.tool_result_compression_enabled else "off"

    def _tool_result_over_budget(self, text: str) -> bool:
        budget_tokens = int(
            self.config.context_window_tokens * self.config.tool_result_compression_max_share
        )
        return get_approx_tokens(text) > budget_tokens

    async def _compress_tool_result(self, result: ToolResult) -> ToolResult:
        """Compatibility wrapper for legacy compression callers.

        The current runtime projects tool results with Tokenjuice. This helper
        remains for embedded tests and callers that exercise the older
        compression API directly.
        """
        guarded_content, guarded = _omit_large_json_tool_fields(result.content)
        if guarded:
            result = ToolResult(
                tool_use_id=result.tool_use_id,
                tool_name=result.tool_name,
                content=guarded_content,
                is_error=result.is_error,
                artifacts=list(result.artifacts),
                execution_status=(
                    mark_execution_status_truncated(result.execution_status)
                    if result.execution_status is not None
                    else None
                ),
                terminates_turn=result.terminates_turn,
            )
        mode = self._tool_result_compression_mode()
        if mode == "off" or not self._tool_result_over_budget(result.content):
            return result

        budget_tokens = int(
            self.config.context_window_tokens * self.config.tool_result_compression_max_share
        )
        max_preview_chars = max(0, budget_tokens * 4)
        compressed_content = compact_tool_result_content(
            tool_name=result.tool_name,
            content=result.content,
            max_preview_chars=max_preview_chars,
            budget_class=resolve_budget_class(result.tool_name),
            is_error=result.is_error,
        )
        return ToolResult(
            tool_use_id=result.tool_use_id,
            tool_name=result.tool_name,
            content=compressed_content,
            is_error=result.is_error,
            artifacts=list(result.artifacts),
            execution_status=(
                mark_execution_status_truncated(result.execution_status)
                if result.execution_status is not None
                else None
            ),
            terminates_turn=result.terminates_turn,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> AgentState:
        return self._state

    def refresh_system_prompt(self, new_prompt: str) -> None:
        """Update system prompt mid-turn (called after compaction to reflect fresh memory)."""
        # Invariant: this mutates `_context.system_prompt`, but
        # `chat_cfg.system` passed to the provider is snapshotted at
        # turn-start (see run_turn below). Refreshes therefore only take
        # effect on subsequent turns — never mid-turn — so memory_save
        # cannot swap the system prompt under an in-flight provider call.
        if self.config.system_prompt is not None:
            self.config.system_prompt = new_prompt
            if self._context is not None:
                self._context.system_prompt = new_prompt
            # cache_breakpoints carry the previous base's
            # text and would mismatch the refreshed prompt on the next
            # provider call (chat_cfg.system would be new_prompt while
            # chat_cfg.cache_breakpoints[0]['text'] still pointed at the
            # pre-compaction base). Re-anchor breakpoints on the new prompt.
            # Callers (TurnRunner compaction-refresh) MUST pass only the
            # cacheable base here — if ``_assemble_prompt`` returns a
            # tuple, the dynamic suffix is dropped before this call so
            # ``new_prompt`` is byte-identical to the next turn's base.
            if self.config.cache_breakpoints:
                self.config.cache_breakpoints = [{"text": new_prompt, "cache": "true"}]

    def clear_history(self) -> None:
        self._history = []

    def set_history(self, messages: list[Message]) -> None:
        self._history = list(messages)

    def history_snapshot(self) -> list[Message]:
        """Return a detached history list for read-only session forks."""

        return list(self._history)

    def _usage_context_for_turn(self) -> UsageExecutionContext:
        """Return the injected execution identity or a safe direct-Agent fallback."""

        if self._usage_execution_context is not None:
            return self._usage_execution_context
        execution_id = uuid.uuid4().hex
        return UsageExecutionContext(
            execution_id=execution_id,
            agent_run_id=execution_id,
            agent_id=str(
                self.config.tool_result_store_agent_id
                or (self.config.metadata or {}).get("agent_id")
                or ""
            ),
            run_kind="agent",
        )

    async def _usage_call_start(
        self,
        scope: UsageAccountingScope,
    ) -> UsageCallStart | None:
        return await start_usage_call(
            scope,
            provider=str(
                self.config.provider_id
                or getattr(self.provider, "provider_name", "")
                or ""
            ),
            model=str(self.config.model_id or ""),
        )

    async def _usage_call_finalize(
        self,
        call: UsageCallStart,
        provider_done: object,
    ) -> None:
        scope = current_usage_accounting_scope()
        sink = scope.sink if scope is not None else self._usage_event_sink
        if sink is None:
            return
        result = normalize_provider_usage(
            provider_done,
            default_provider=call.provider,
            default_model=call.model,
            completed_at_ms=time.time_ns() // 1_000_000,
        )
        finalize_task = asyncio.create_task(sink.finalize(call, result))
        try:
            await asyncio.shield(finalize_task)
        except asyncio.CancelledError:
            # A provider usage receipt already exists.  Finish its durable
            # write before allowing turn cancellation to unwind.
            with contextlib.suppress(Exception):
                await finalize_task
            raise
        except Exception as exc:  # noqa: BLE001 - sink owns persistence/retry policy
            # The durable started row remains available to gateway recovery.
            # Never discard an already-generated provider response because a
            # terminal ledger update is temporarily unavailable.
            logger.warning(
                "usage_accounting.finalize_failed",
                event_id=call.event_id,
                error=str(exc),
            )

    async def _usage_call_unknown(self, call: UsageCallStart, reason: str) -> None:
        scope = current_usage_accounting_scope()
        sink = scope.sink if scope is not None else self._usage_event_sink
        if sink is None:
            return
        stable_reason = normalize_usage_unknown_reason(reason)
        unknown_task = asyncio.create_task(sink.mark_unknown(call, stable_reason))
        try:
            await asyncio.shield(unknown_task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await unknown_task
            raise
        except Exception as exc:  # noqa: BLE001 - preserve the original turn outcome
            logger.warning(
                "usage_accounting.mark_unknown_failed",
                event_id=call.event_id,
                reason=stable_reason,
                error=str(exc),
            )

    async def run_turn(
        self,
        message: str,
        extra_messages: list[Message] | None = None,
        semantic_message: str | None = None,
        *,
        pending_input_provider: PendingInputProvider | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one agent turn, yielding AgentEvents.

        Explicit state machine — no recursion. Tool loop iterates until
        the model finishes, unless config.max_iterations is a positive cap.
        """
        if self._session_key:
            from opensquilla.sandbox.escalation import (
                clear_sandbox_approval_denials,
                prune_once_mount_grants,
            )

            clear_sandbox_approval_denials(self._session_key)
            # "Allow once" path grants authorize at most the granting turn; expire
            # them at the start of the next turn so a later access re-prompts
            # instead of being silently allowed for the whole session (issue #418).
            prune_once_mount_grants(self._session_key)
        scope = current_usage_accounting_scope()
        if self._usage_event_sink is not None:
            context = self._usage_context_for_turn()
            if not (
                scope is not None
                and scope.sink is self._usage_event_sink
                and scope.context.execution_id == context.execution_id
            ):
                scope = UsageAccountingScope(
                    sink=self._usage_event_sink,
                    context=context,
                )
        with bind_usage_accounting_scope(scope):
            async for event in self._turn_generator(
                message,
                extra_messages,
                semantic_message,
                pending_input_provider=pending_input_provider,
            ):
                yield event

    async def _turn_generator(
        self,
        message: str,
        extra_messages: list[Message] | None = None,
        semantic_message: str | None = None,
        *,
        pending_input_provider: PendingInputProvider | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Async generator that drives the state machine."""
        self._provider_tool_result_overrides = {}
        self._projected_diagnostic_evidence = {}
        self._focused_retrieved_tool_result_handles = set()
        self._current_turn_message = message
        _meta_invoke_turn_count.set(0)
        usage_scope = current_usage_accounting_scope()

        # ------ IDLE → THINKING ------
        yield self._transition(AgentState.THINKING)

        # PR7/9 E2E fix — consume meta_resolution's awaiting-branch
        # outcomes. meta_resolution stages six distinct outcomes on
        # ctx.metadata (resume / errors / cancelled / expired /
        # race_lost / [trigger match for fresh turn]) and returns; the
        # runtime owns the user-visible feedback for the first five so
        # the turn terminates cleanly instead of falling through to the
        # LLM (which would re-trigger meta_invoke and hit the
        # awaiting-guard with an opaque message).
        metadata = self.config.metadata or {}
        meta_resume = metadata.get("meta_resume")
        if meta_resume is not None:
            async for ev in self._run_meta_resume(meta_resume):
                yield ev
            return
        meta_launch = metadata.get("meta_launch")
        if meta_launch is not None:
            launch_name = (
                meta_launch.get("name") if isinstance(meta_launch, dict) else None
            )
            if launch_name:
                async for ev in self._run_meta_launch(launch_name):
                    yield ev
                return
        clarify_outcome = self._read_clarify_outcome(metadata)
        if clarify_outcome is not None:
            text, terminates = clarify_outcome
            async for ev in self._emit_terminal_text(text, iterations=0):
                yield ev
            _ = terminates  # always terminates today; reserved for future
            return

        # Use the system prompt from config (wired by gateway via identity.prompt)
        if self._context is None:
            self._context = ContextAssembly(
                system_prompt=self.config.system_prompt or "",
                workspace_dir=self.config.workspace_dir,
            )

        thinking_prompt = semantic_message if semantic_message is not None else message
        thinking_enabled, thinking_budget = self.config.resolve_thinking(prompt=thinking_prompt)

        # Preprocess history for the provider request view. This does not
        # mutate persisted transcript rows or tool result content.
        # Some reasoning tool-call providers require the prior assistant
        # tool-call message to carry its reasoning_content while reasoning is
        # enabled, so keep that narrow field only for tool-call history.
        caps_reasoning_format = (
            getattr(self.config.model_capabilities, "reasoning_format", "")
            if self.config.model_capabilities is not None
            else ""
        )
        preserve_reasoning_content = bool(
            _is_direct_deepseek_v4_model_id(self.config.model_id)
            or (
                thinking_enabled
                and caps_reasoning_format == "deepseek"
                and _is_deepseek_model_id(self.config.model_id)
            )
            or (thinking_enabled and caps_reasoning_format == "dashscope")
        )
        loaded_history = list(self._history)
        self._write_context_stage("session:loaded", loaded_history)
        sanitized_history, sanitize_result = sanitize_session_messages(loaded_history)
        sanitized_history, historical_projection_result = project_historical_tool_payloads(
            sanitized_history,
            preserve_reasoning_content=preserve_reasoning_content,
        )
        sanitized_history = repair_tool_pairing(sanitized_history)
        sanitized_history = drop_reasoning(
            sanitized_history,
            preserve_tool_call_reasoning=thinking_enabled,
            preserve_reasoning_content=preserve_reasoning_content,
        )
        preserve_historical_images = bool(
            self.config.preserve_historical_images
            and getattr(self.config.model_capabilities, "supports_vision", False)
            if self.config.model_capabilities is not None
            else False
        )
        sanitized_history = _strip_historical_image_blocks(
            sanitized_history,
            preserve_images=preserve_historical_images,
        )
        self._write_context_stage(
            "session:sanitized",
            sanitized_history,
            sanitize=sanitize_result,
            historical_projection=historical_projection_result.__dict__,
        )
        history = limit_turns(sanitized_history, self.config.max_history_turns)
        history = repair_tool_pairing(history)
        self._write_context_stage(
            "session:limited",
            history,
            removed_messages=max(len(sanitized_history) - len(history), 0),
        )

        # Build initial message list
        turn_messages: list[Message] = list(history)
        # Count-aware recovery may summarize only content before this boundary.
        # Skills context, multimodal inputs, and the active user request all
        # belong to the protected current turn.
        current_turn_start_index = len(turn_messages)
        message_count_request_view: _MessageCountRequestView | None = None
        # Insert this turn's skills context BEFORE the user content so it
        # joins turn_messages permanently (persists into self._history at
        # turn end). Re-inserting a fresh skills_ctx into request_messages
        # every turn — the previous design — broke the KV-cache prefix:
        # past skills_ctx vanished while a new one slid in at a moving
        # position, so providers couldn't cache the conversation prefix.
        # Now each turn's skills list lands in history once and stays there;
        # only the runtime context (timestamp) remains transient.
        skills_context_message = self._skills_context_message()
        if skills_context_message is not None:
            turn_messages.append(skills_context_message)
        # Keep persisted history and persisted skills as the provider-visible
        # prefix. Request-scoped context can change every turn, so keep it near
        # the current turn instead of letting it invalidate implicit prefix
        # caches from messages[0].
        request_context_insert_index = len(turn_messages)
        runtime_context_insert_index = len(turn_messages)
        if extra_messages:
            turn_messages.extend(extra_messages)
        # Only append text message if non-empty (multimodal may use extra_messages instead)
        if message:
            if not extra_messages:
                runtime_context_insert_index = len(turn_messages)
            turn_messages.append(Message(role="user", content=message))
        self._write_context_stage("prompt:before", turn_messages)
        self._write_context_stage(
            "prompt:images",
            turn_messages,
            image_blocks=self._count_image_blocks(turn_messages),
        )
        runtime_context = self._runtime_context_block()
        runtime_context_message = self._runtime_context_message(runtime_context)
        request_context_message = self._request_context_message(self.config.request_context_prompt)
        turn_objective_message = self._turn_objective_message(
            semantic_message if semantic_message is not None else message,
            enabled=self._turn_objective_reminder_enabled,
            max_chars=self._turn_objective_reminder_max_chars,
        )
        runtime_context_hash = hashlib.sha256(runtime_context.encode("utf-8")).hexdigest()[:16]

        chat_cfg = ChatConfig(
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            top_p=self.config.top_p,
            system=self._context.system_prompt,
            thinking=thinking_enabled,
            thinking_budget_tokens=thinking_budget,
            thinking_budget_explicit=(
                self.config.thinking_budget_tokens != _THINKING_BUDGET_DEFAULT
            ),
            timeout=self.config.request_timeout,
            stop_sequences=self.config.stop_sequences,
            cache_breakpoints=self._cache_breakpoints_without_runtime_context(
                self.config.cache_breakpoints
            ),
            cache_mode=self.config.cache_mode,
            output_json_schema=self.config.output_json_schema,
            output_json_schema_strict=self.config.output_json_schema_strict,
            model_capabilities=self.config.model_capabilities,
            thinking_level=(
                self.config.thinking if isinstance(self.config.thinking, ThinkingLevel) else None
            ),
            provider_request_max_chars=self._provider_request_proof_max_chars(),
            tool_choice=None,
        )
        _thinking_fallback_done = False
        _disable_thinking_for_next_provider_call = False
        _reasoning_stream_char_cap = max(
            0, int(getattr(self.config, "reasoning_stream_char_cap", 0) or 0)
        )

        _log = structlog.get_logger("opensquilla.engine.agent")

        def _positive_float(value: Any) -> float | None:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed > 0 else None

        iterations = 0
        overflow_retries = 0
        # Keep lifetime usage separate from the live context-window gauge.
        # Compaction shrinks what the model sees next; it must not erase the
        # turn's already-spent provider tokens from the final DoneEvent.
        total_input_tokens = 0
        total_output_tokens = 0
        total_reasoning_tokens = 0
        total_cached_tokens = 0
        total_cache_write_tokens = 0
        total_billed_cost = 0.0
        total_provider_billed_entries = 0
        total_unbilled_entries = 0
        # Estimate-backed accumulator for max_turn_cost_usd: billed cost when a
        # call reported one, otherwise the layered-resolver estimate — unlike
        # total_billed_cost, this never sits at 0.0 for a cost-blind provider.
        # Computed once so the (potentially network-blocking) price resolver is
        # only ever touched when the gate is actually enabled.
        turn_cost_budget_enabled = (
            _positive_float(getattr(self.config, "max_turn_cost_usd", 0.0)) is not None
        )
        total_cost_usd_accum = 0.0
        total_cost_usd_accum_has_billed = False
        # Tracks whether any component of total_cost_usd_accum came from the
        # estimator (as opposed to a provider-reported billed cost), so the
        # gate's error message can report "billed" / "estimated" / "mixed".
        total_cost_usd_accum_has_estimate = False

        def _accumulate_turn_cost(
            event: object,
            *,
            default_provider: str,
            default_model: str,
        ) -> None:
            nonlocal total_cost_usd_accum
            nonlocal total_cost_usd_accum_has_billed
            nonlocal total_cost_usd_accum_has_estimate

            if not turn_cost_budget_enabled:
                return
            budget_usage = normalize_provider_usage(
                event,
                default_provider=default_provider,
                default_model=default_model,
                completed_at_ms=0,
            )
            total_cost_usd_accum += (
                budget_usage.billed_cost_nanos + budget_usage.estimated_cost_nanos
            ) / 1_000_000_000
            total_cost_usd_accum_has_billed |= budget_usage.cost_source in {
                "provider_billed",
                "mixed",
            }
            total_cost_usd_accum_has_estimate |= budget_usage.cost_source in {
                "opensquilla_estimate",
                "mixed",
            }

        usage_turn_baseline = (
            self._usage_tracker.session_checkpoint(self._session_key)
            if self._usage_tracker and self._session_key
            else None
        )
        turn_llm_calls = 0
        turn_tool_errors = 0
        async def _review_inflight_sandbox_request(
            payload: dict[str, object],
        ) -> RuleAssessment | None:
            return await _review_pending_elevation_if_configured(
                dict(payload),
                transcript=turn_messages,
                runtime_events_path=self.config.runtime_events_path,
            )

        if self._tool_context is not None:
            self._tool_context.on_sandbox_auto_review = _review_inflight_sandbox_request
        last_actual_model = ""
        last_actual_provider = ""
        turn_model_usage_breakdown: list[dict[str, Any]] = []
        last_ensemble_trace: dict[str, Any] | None = None
        turn_ensemble_request_count = 0
        terminal_error: ErrorEvent | None = None
        final_text_parts: list[str] = []
        final_reasoning_parts: list[str] = []
        artifact_delivery_final_response_pending = False
        artifact_delivery_degraded_final_response = False
        artifact_delivery_final_response_artifacts: list[dict[str, Any]] = []
        max_iterations_finalization_attempted = False
        max_iterations_finalization_pending = False
        max_iterations_finalization_message: Message | None = None
        max_iterations_deadline_extension_logged = False
        post_write_convergence_finalization_pending = False
        post_write_convergence_finalization_message: Message | None = None
        placeholder_offense_iterations = 0
        deadline_wrapup_armed = False
        deadline_wrapup_message: Message | None = None
        deadline_thinking_off_armed = False
        endgame_git_freeze_armed = False
        endgame_fix_directive_fired = False
        plan_only_iteration_streak = 0
        reasoning_only_act_now_message: Message | None = None
        mid_budget_nudge_fired_fractions: set[float] = set()
        workspace_diff_recovery_attempted = False
        failed_tool_finalization_recovery_keys: set[str] = set()
        post_tool_empty_recovery_attempted = False
        text_only_tool_recovery_injections = 0
        text_only_tool_recovery_pending = False
        reasoning_prefill_recovery_attempted = False
        final_diff_contract_recovery_attempted = False
        source_loop_recovery_attempted_keys: set[str] = set()
        workspace_edit_gate_details: dict[str, Any] | None = None
        workspace_edit_gate_recovery_read_paths: set[str] = set()
        workspace_edit_gate_recovery_reads_remaining = 0
        runtime_recovery_scaffolding_pending = False
        repeated_tool_call_key: tuple[str, str] | None = None
        repeated_tool_call_count = 0
        repeated_tool_call_workspace_write_count = len(self._effective_workspace_write_records())
        repeated_tool_call_last_result_is_error = False
        last_executed_results: list[ToolResult] = []
        last_post_write_progress_count = self._post_write_progress_count()
        post_write_focused_verification_observed = False
        post_write_focused_verification_success_observed = False
        last_post_write_failed_verification: dict[str, Any] | None = None
        finalize_evidence_strict = bool(
            getattr(self.config, "finalize_evidence_strict", False)
        )
        finalize_evidence_tracker = (
            FinalizeEvidenceTracker(strict=finalize_evidence_strict)
            if (
                bool(getattr(self.config, "finalize_evidence_gate_enabled", False))
                or finalize_evidence_strict
            )
            else None
        )
        finalize_evidence_gate_keys: set[str] = set()
        submit_review_enabled = bool(
            getattr(self.config, "submit_review_enabled", False)
        )
        submit_review_state = SubmitReviewState()
        submit_review_diff_max_chars = int(
            getattr(self.config, "submit_review_diff_max_chars", 20000)
        )
        patch_hygiene_block_mode = str(
            getattr(self.config, "patch_hygiene_block_mode", "off") or "off"
        )
        patch_hygiene_block_keys: set[str] = set()
        scratch_verify_mirror_enabled = bool(
            getattr(self.config, "scratch_verify_mirror", False)
        )
        if self._tool_context is not None:
            # Rides the ToolContext in place (endgame_git_freeze precedent):
            # deny messages append the verify-mirror guidance only while on,
            # and the flag is reset each turn because the context outlives it.
            self._tool_context.scratch_verify_mirror_active = (
                scratch_verify_mirror_enabled
            )
        finalize_variant_challenge_enabled = bool(
            getattr(self.config, "finalize_variant_challenge", False)
        )
        finalize_variant_challenge_fired = False
        recent_failure_anchor_summaries: list[str] = []
        progress_watchdog_mode = getattr(self.config, "progress_watchdog_mode", "log")
        progress_watchdog = ProgressWatchdog(
            repeated_tool_error_threshold=max(
                1,
                int(
                    getattr(
                        self.config,
                        "progress_watchdog_repeated_tool_error_threshold",
                        3,
                    )
                    or 3
                ),
            ),
            repeated_provider_failure_threshold=max(
                1,
                int(
                    getattr(
                        self.config,
                        "progress_watchdog_repeated_provider_failure_threshold",
                        2,
                    )
                    or 2
                ),
            ),
            repeated_failure_anchor_threshold=max(
                1,
                int(
                    getattr(
                        self.config,
                        "progress_watchdog_repeated_failure_anchor_threshold",
                        3,
                    )
                    or 3
                ),
            ),
            observe_only=progress_watchdog_mode != "block",
        )
        post_write_convergence_tracker = (
            PostWriteConvergenceTracker(
                warn_threshold=max(
                    1,
                    int(
                        getattr(
                            self.config,
                            "post_write_convergence_warn_threshold",
                            3,
                        )
                        or 3
                    ),
                ),
                finalize_after_warning=max(
                    1,
                    int(
                        getattr(
                            self.config,
                            "post_write_convergence_finalize_after_warning",
                            3,
                        )
                        or 3
                    ),
                ),
            )
            if bool(getattr(self.config, "post_write_convergence_enabled", False))
            else None
        )
        runtime_recovery_mode: RuntimeRecoveryMode = getattr(
            self.config, "runtime_recovery_mode", "log"
        )
        runtime_recovery_source_loop_max_nudges = max(
            1,
            int(getattr(self.config, "runtime_recovery_source_loop_max_nudges", 1) or 1),
        )
        runtime_diagnostics = (
            RuntimeDiagnosticsObserver(
                session_key=self._session_key,
                agent_id=(
                    self.config.tool_result_store_agent_id
                    or self.config.metadata.get("agent_id")
                ),
            )
            if self.config.runtime_events_path or runtime_recovery_mode == "warn_model"
            else None
        )
        _fallback = FallbackPolicy(
            max_retries=self.config.max_provider_retries,
            base_backoff_ms=self.config.retry_base_backoff_ms,
            max_backoff_ms=self.config.retry_max_backoff_ms,
        )

        # Timeout budgets: optional total turn budget, idle LLM stream budget,
        # and per-tool execution budget.
        _loop = asyncio.get_running_loop()
        _total_deadline = _loop.time() + self.config.timeout if self.config.timeout > 0 else None

        # Endgame git freeze: once remaining wall clock drops below the margin,
        # the shell tools block workspace-reverting git commands outright so
        # the current diff survives runner-side collection. The armed flag
        # rides the ToolContext in place (router_control precedent); it is
        # reset here because the context outlives the turn.
        endgame_git_freeze_margin_seconds = max(
            0,
            int(getattr(self.config, "endgame_git_freeze_margin_seconds", 0) or 0),
        )
        if endgame_git_freeze_margin_seconds > 0 and self._tool_context is not None:
            self._tool_context.endgame_git_freeze_active = False
            self._tool_context.endgame_git_freeze_instrumentation_exempt = bool(
                getattr(self.config, "endgame_git_freeze_instrumentation_exempt", False)
            )

        def _arm_endgame_git_freeze_if_due() -> None:
            nonlocal endgame_git_freeze_armed
            if (
                endgame_git_freeze_armed
                or endgame_git_freeze_margin_seconds <= 0
                or _total_deadline is None
                or _loop.time() <= _total_deadline - endgame_git_freeze_margin_seconds
            ):
                return
            endgame_git_freeze_armed = True
            if self._tool_context is not None:
                self._tool_context.endgame_git_freeze_active = True
            self._write_turn_call_log(
                "turn_policy_decision",
                action="endgame_git_freeze",
                reason="deadline_margin",
                code="endgame_git_freeze",
                iteration=iterations,
                remaining_seconds=int(max(0.0, _total_deadline - _loop.time())),
                margin_seconds=endgame_git_freeze_margin_seconds,
            )

        def _defer_max_iterations_cap() -> bool:
            """Whether the iteration cap yields to remaining wall-clock time.

            True keeps the loop running normal iterations past the cap while
            more than the extension margin remains before the total deadline;
            the cap re-applies once the margin is reached. A finalization
            attempt that already happened is never reopened.
            """
            nonlocal max_iterations_deadline_extension_logged
            extend_seconds = max(
                0,
                int(
                    getattr(self.config, "max_iterations_deadline_extend_seconds", 0)
                    or 0
                ),
            )
            if (
                extend_seconds <= 0
                or _total_deadline is None
                or max_iterations_finalization_attempted
                or _loop.time() >= _total_deadline - extend_seconds
            ):
                return False
            if not max_iterations_deadline_extension_logged:
                max_iterations_deadline_extension_logged = True
                remaining_seconds = int(max(0.0, _total_deadline - _loop.time()))
                self._write_turn_call_log(
                    "turn_policy_decision",
                    action="max_iterations_deadline_extension",
                    reason="deadline_headroom",
                    code="max_iterations_deadline_extension",
                    iteration=iterations,
                    max_iterations=self.config.max_iterations,
                    remaining_seconds=remaining_seconds,
                    extend_margin_seconds=extend_seconds,
                )
                append_runtime_event(
                    self.config.runtime_events_path,
                    {
                        "feature": "max_iterations_deadline_extension",
                        "name": "max_iterations_deadline_extension.active",
                        "action": "defer_finalization",
                        "reason": "deadline_headroom",
                        "iteration": iterations,
                        "max_iterations": self.config.max_iterations,
                        "remaining_seconds": remaining_seconds,
                        "extend_margin_seconds": extend_seconds,
                        "session_key": self._session_key,
                        "agent_id": (
                            self.config.tool_result_store_agent_id
                            or self.config.metadata.get("agent_id")
                        ),
                    },
                )
            return True

        tools_supported = True
        if self.config.model_capabilities is not None:
            tools_supported = bool(getattr(self.config.model_capabilities, "supports_tools", True))
        provider_tool_definitions = self.tool_definitions or None
        if not tools_supported:
            provider_tool_definitions = None

        def _turn_budget_error() -> ErrorEvent | None:
            max_llm_calls = self._positive_int(getattr(self.config, "max_turn_llm_calls", 0))
            if max_llm_calls is not None and turn_llm_calls > max_llm_calls:
                return ErrorEvent(
                    message=(
                        f"Turn stopped after {turn_llm_calls} LLM calls "
                        f"(max_turn_llm_calls={max_llm_calls})."
                    ),
                    code="turn_llm_call_budget_exceeded",
                )
            max_input = self._positive_int(getattr(self.config, "max_turn_input_tokens", 0))
            if max_input is not None and total_input_tokens > max_input:
                return ErrorEvent(
                    message=(
                        f"Turn stopped after {total_input_tokens} input tokens "
                        f"(max_turn_input_tokens={max_input})."
                    ),
                    code="turn_input_token_budget_exceeded",
                )
            max_output = self._positive_int(getattr(self.config, "max_turn_output_tokens", 0))
            if max_output is not None and total_output_tokens > max_output:
                return ErrorEvent(
                    message=(
                        f"Turn stopped after {total_output_tokens} output tokens "
                        f"(max_turn_output_tokens={max_output})."
                    ),
                    code="turn_output_token_budget_exceeded",
                )
            max_cost = _positive_float(getattr(self.config, "max_turn_billed_cost_usd", 0.0))
            if max_cost is not None and total_billed_cost > max_cost:
                return ErrorEvent(
                    message=(
                        f"Turn stopped after ${total_billed_cost:.6f} billed cost "
                        f"(max_turn_billed_cost_usd=${max_cost:.6f})."
                    ),
                    code="turn_billed_cost_budget_exceeded",
                )
            max_total = _positive_float(getattr(self.config, "max_turn_cost_usd", 0.0))
            if max_total is not None and total_cost_usd_accum > max_total:
                if total_cost_usd_accum_has_billed and total_cost_usd_accum_has_estimate:
                    cost_basis = "mixed"
                elif total_cost_usd_accum_has_estimate:
                    cost_basis = "estimated"
                else:
                    cost_basis = "billed"
                return ErrorEvent(
                    message=(
                        f"Turn stopped after ${total_cost_usd_accum:.6f} "
                        f"({cost_basis} cost basis; "
                        f"max_turn_cost_usd=${max_total:.6f})."
                    ),
                    code="turn_cost_budget_exceeded",
                )
            max_tool_errors = self._positive_int(getattr(self.config, "max_turn_tool_errors", 0))
            if max_tool_errors is not None and turn_tool_errors >= max_tool_errors:
                return ErrorEvent(
                    message=(
                        f"Turn stopped after {turn_tool_errors} tool errors "
                        f"(max_turn_tool_errors={max_tool_errors})."
                    ),
                    code="turn_tool_error_budget_exceeded",
                )
            return None

        def _turn_llm_call_budget_error(next_call_number: int) -> ErrorEvent | None:
            max_llm_calls = self._positive_int(getattr(self.config, "max_turn_llm_calls", 0))
            if max_llm_calls is None or next_call_number <= max_llm_calls:
                return None
            return ErrorEvent(
                message=(
                    f"Turn stopped before LLM call {next_call_number} "
                    f"(max_turn_llm_calls={max_llm_calls})."
                ),
                code="turn_llm_call_budget_exceeded",
            )

        def _finish_artifact_delivery_degraded(
            *,
            reason: str,
            code: str,
        ) -> WarningEvent:
            nonlocal artifact_delivery_degraded_final_response
            nonlocal artifact_delivery_final_response_pending
            if not "".join(final_text_parts).strip():
                final_text_parts.append(
                    self._artifact_delivery_final_response_text(
                        artifact_delivery_final_response_artifacts
                    )
                )
            artifact_delivery_degraded_final_response = True
            artifact_delivery_final_response_pending = False
            self._write_turn_call_log(
                "artifact_final_response_degraded",
                reason=reason,
                code=code,
                artifact_count=len(artifact_delivery_final_response_artifacts),
            )
            return WarningEvent(
                code="artifact_delivery_final_response_degraded",
                message=(
                    "Artifact delivery completed, but the model could not generate "
                    "the final explanatory response. Returning a deterministic "
                    "completion message instead."
                ),
            )

        def _finish_artifact_delivery_without_provider() -> None:
            final_response_text = self._artifact_delivery_final_response_text(
                artifact_delivery_final_response_artifacts
            )
            current_text = "".join(final_text_parts)
            if final_response_text not in current_text:
                prefix = "\n\n" if current_text.strip() else ""
                final_text_parts.append(prefix + final_response_text)
            self._write_turn_call_log(
                "artifact_final_response_synthesized",
                reason="publish_artifact_completed",
                artifact_count=len(artifact_delivery_final_response_artifacts),
            )

        try:
            while True:
                if (
                    self.config.max_iterations > 0
                    and iterations >= self.config.max_iterations
                    and not _defer_max_iterations_cap()
                ):
                    max_iterations_source = str(
                        self.config.metadata.get("agent_max_iterations_source", "agent_config")
                    )
                    if max_iterations_source == "session config":
                        max_iterations_guidance = (
                            "Set session agent_max_iterations=0 for unlimited tasks."
                        )
                    elif max_iterations_source == "gateway config":
                        max_iterations_guidance = (
                            "Set gateway agent_max_iterations=0 for unlimited tasks."
                        )
                    elif max_iterations_source.startswith("env "):
                        max_iterations_guidance = (
                            "Set OPENSQUILLA_AGENT_MAX_ITERATIONS=0 for unlimited tasks."
                        )
                    elif max_iterations_source == "explicit argument":
                        max_iterations_guidance = (
                            "Pass --max-iterations 0 or max_iterations=0 for unlimited tasks."
                        )
                    else:
                        max_iterations_guidance = (
                            "Set AgentConfig.max_iterations=0 for unlimited tasks."
                        )
                    if not max_iterations_finalization_attempted:
                        max_iterations_finalization_attempted = True
                        max_iterations_finalization_pending = True
                        max_iterations_finalization_message = Message(
                            role="user",
                            content=(
                                "The configured iteration limit has been reached. "
                                "Do not call tools. Provide the best concise final "
                                "answer from the work completed so far."
                            ),
                        )
                        self._write_turn_call_log(
                            "turn_policy_decision",
                            action="finalize_partial",
                            reason="max_iterations",
                            code="max_iterations",
                            iteration=iterations,
                            max_iterations=self.config.max_iterations,
                            max_iterations_source=max_iterations_source,
                        )
                    else:
                        self._write_turn_call_log(
                            "turn_policy_decision",
                            action="partial",
                            reason="max_iterations",
                            code="max_iterations",
                            iteration=iterations,
                            max_iterations=self.config.max_iterations,
                            max_iterations_source=max_iterations_source,
                        )
                        terminal_error = ErrorEvent(
                            message=(
                                f"Reached max_iterations={self.config.max_iterations} "
                                f"from {max_iterations_source} after a finalization attempt. "
                                f"{max_iterations_guidance}"
                            ),
                            code="max_iterations",
                        )
                        yield terminal_error
                        break

                # Check total turn deadline (if configured)
                if _total_deadline is not None and _loop.time() > _total_deadline:
                    raise TimeoutError(f"Agent total timeout after {self.config.timeout}s")

                # Pre-deadline wrap-up: arm once when remaining wall clock drops
                # below the configured margin. The directive is spliced into
                # every subsequent provider request and rebuilt each iteration
                # so the remaining-time figure stays current; tools stay
                # available so the model can still apply and verify a final fix.
                wrapup_margin_seconds = max(
                    0,
                    int(getattr(self.config, "deadline_wrapup_margin_seconds", 0) or 0),
                )
                if (
                    wrapup_margin_seconds > 0
                    and _total_deadline is not None
                    and (
                        deadline_wrapup_armed
                        or _loop.time() > _total_deadline - wrapup_margin_seconds
                    )
                ):
                    remaining_seconds = max(0.0, _total_deadline - _loop.time())
                    deadline_wrapup_message = Message(
                        role="user",
                        content=_DEADLINE_WRAPUP_DIRECTIVE_TEMPLATE.format(
                            minutes=max(1, int(remaining_seconds // 60)),
                        ),
                    )
                    if not deadline_wrapup_armed:
                        deadline_wrapup_armed = True
                        self._write_turn_call_log(
                            "turn_policy_decision",
                            action="deadline_wrapup",
                            reason="deadline_margin",
                            code="deadline_wrapup",
                            iteration=iterations,
                            remaining_seconds=int(remaining_seconds),
                            margin_seconds=wrapup_margin_seconds,
                        )

                # Pre-deadline thinking cutoff: once remaining wall clock drops
                # below the configured margin, thinking stays off for every
                # remaining provider call so the final stretch is spent on tool
                # calls rather than a single long reasoning stream.
                thinking_off_margin_seconds = max(
                    0,
                    int(
                        getattr(self.config, "deadline_thinking_off_margin_seconds", 0)
                        or 0
                    ),
                )
                if (
                    thinking_off_margin_seconds > 0
                    and _total_deadline is not None
                    and not deadline_thinking_off_armed
                    and _loop.time() > _total_deadline - thinking_off_margin_seconds
                ):
                    deadline_thinking_off_armed = True
                    self._write_turn_call_log(
                        "turn_policy_decision",
                        action="deadline_thinking_off",
                        reason="deadline_margin",
                        code="deadline_thinking_off",
                        iteration=iterations,
                        remaining_seconds=int(
                            max(0.0, _total_deadline - _loop.time())
                        ),
                        margin_seconds=thinking_off_margin_seconds,
                    )
                    # The turn-call log is a raw debug stream that run
                    # harnesses do not collect; the runtime event is what
                    # lets delivery gates tell this designed endgame
                    # thinking cutoff (every later call runs
                    # thinking-disabled) apart from a treatment delivery
                    # failure.
                    append_runtime_event(
                        self.config.runtime_events_path,
                        {
                            "feature": "deadline_thinking_off",
                            "name": "deadline_thinking_off.armed",
                            "action": "disable_thinking_until_deadline",
                            "reason": "deadline_margin",
                            "iteration": iterations,
                            "remaining_seconds": int(
                                max(0.0, _total_deadline - _loop.time())
                            ),
                            "margin_seconds": thinking_off_margin_seconds,
                            "session_key": self._session_key,
                            "agent_id": (
                                self.config.tool_result_store_agent_id
                                or self.config.metadata.get("agent_id")
                            ),
                        },
                    )

                # Endgame git freeze arming; re-checked before tool execution
                # because a long provider stream can cross the margin
                # mid-iteration.
                _arm_endgame_git_freeze_if_due()

                iterations += 1
                # The act-now message answers one reasoning-only failure; a
                # fresh iteration starts from a clean request.
                reasoning_only_act_now_message = None

                # ------ THINKING → STREAMING ------
                yield self._transition(AgentState.STREAMING)

                # Collect this LLM response
                assistant_text_parts: list[str] = []
                tool_calls: list[ToolCall] = []
                pending_tools: dict[str, _StreamAccumulator] = {}
                tool_argument_heartbeat_chars: dict[str, int] = {}
                iter_input_tokens = 0
                iter_output_tokens = 0
                iter_reasoning_tokens = 0
                iter_reasoning_content: str | None = None
                iter_thinking_signature: str | None = None
                provider_error: ProviderErrorEvent | None = None

                _retry_attempt = 0
                _call_attempt = 0
                _reasoning_cap_preempt_done = False
                attempt_reasoning_stream_chars = 0
                _retry_policy = _ProviderRetryPolicy.from_provider_budget(
                    _fallback.max_retries,
                    length_capped_continuations=self.config.length_capped_continuations,
                    # The act-now lever injects a directive on the first
                    # reasoning-only retry; the second budgeted retry gives the
                    # directive one delivery attempt of its own.
                    reasoning_only_retries=(
                        2
                        if bool(getattr(self.config, "reasoning_only_act_now", False))
                        else 1
                    ),
                )
                _attempt_retries_used = _retry_policy.used_attempts()
                _invalid_response_fallback_done = False
                _message_limit_recovery_done = False
                while _retry_attempt <= _fallback.max_retries:
                    provider_error = None
                    assistant_text_parts = []
                    tool_calls = []
                    pending_tools = {}
                    seen_tool_use_ids: set[str] = set()
                    # Plain assistant text streams live as the answer the moment it
                    # arrives. text_presentation_decided flips to True once a tool
                    # appears this call, after which later text is tagged as
                    # intermediate narration between tools rather than the answer.
                    text_presentation_decided = False
                    tool_argument_heartbeat_chars = {}
                    iter_input_tokens = 0
                    iter_output_tokens = 0
                    iter_reasoning_tokens = 0
                    iter_reasoning_content = None
                    iter_thinking_signature = None
                    _got_error = False
                    _stream_policy_preempt = False
                    attempt_reasoning_stream_chars = 0
                    provider_done_for_log: ProviderDoneEvent | None = None
                    provider_error_for_log: ProviderErrorEvent | None = None
                    cost_receipt_counted = False
                    call_id = f"{iterations}.{_call_attempt}"
                    call_started_at = time.monotonic()
                    provider_tools_for_call = (
                        None
                        if (
                            artifact_delivery_final_response_pending
                            or max_iterations_finalization_pending
                            or post_write_convergence_finalization_pending
                        )
                        else provider_tool_definitions
                    )
                    provider_tools_for_call = self._workspace_edit_gate_tool_definitions(
                        provider_tools_for_call,
                        workspace_edit_gate_details,
                        recovery_read_paths=workspace_edit_gate_recovery_read_paths,
                        recovery_reads_remaining=(
                            workspace_edit_gate_recovery_reads_remaining
                        ),
                    )
                    tools_supported_for_call = (
                        tools_supported
                        and not artifact_delivery_final_response_pending
                        and not max_iterations_finalization_pending
                        and not post_write_convergence_finalization_pending
                    )
                    ignored_post_delivery_tool_use = False
                    if message_count_request_view is not None:
                        base_request_turn_messages = message_count_request_view.materialize(
                            turn_messages
                        )
                        active_request_context_insert_index = (
                            message_count_request_view.request_context_insert_index
                        )
                        active_runtime_context_insert_index = (
                            message_count_request_view.runtime_context_insert_index
                        )
                        active_protected_turn_start_index = (
                            message_count_request_view.protected_turn_start_index
                        )
                    else:
                        base_request_turn_messages = turn_messages
                        active_request_context_insert_index = request_context_insert_index
                        active_runtime_context_insert_index = runtime_context_insert_index
                        active_protected_turn_start_index = current_turn_start_index

                    request_suffix_messages: list[Message] = []
                    if (
                        post_write_convergence_finalization_pending
                        and post_write_convergence_finalization_message is not None
                    ):
                        request_suffix_messages = [post_write_convergence_finalization_message]
                    elif (
                        max_iterations_finalization_pending
                        and max_iterations_finalization_message is not None
                    ):
                        request_suffix_messages = [max_iterations_finalization_message]
                    elif reasoning_only_act_now_message is not None and (
                        not turn_messages or turn_messages[-1].role != "assistant"
                    ):
                        # Act-now beats the wrap-up directive for this retry:
                        # it answers the reasoning-only failure that just
                        # happened, and the wrap-up splice resumes on the next
                        # request. Withheld on an assistant tail for the same
                        # reasoning-prefill reason as below.
                        request_suffix_messages = [reasoning_only_act_now_message]
                    elif deadline_wrapup_message is not None and (
                        not turn_messages or turn_messages[-1].role != "assistant"
                    ):
                        # Wrap-up defers to the finalization messages above,
                        # which already demand a final answer, and is withheld
                        # while the turn ends on an assistant message: the
                        # reasoning-prefill continuation requires the assistant
                        # tail to stay the last request message.
                        request_suffix_messages = [deadline_wrapup_message]
                    request_turn_messages = [
                        *base_request_turn_messages,
                        *request_suffix_messages,
                    ]
                    (
                        request_messages,
                        request_sanitize_result,
                    ) = await self._provider_request_messages_with_sanitize_async(
                        request_turn_messages,
                        request_context_message=request_context_message,
                        request_context_insert_index=active_request_context_insert_index,
                        runtime_context_message=runtime_context_message,
                        runtime_context_insert_index=active_runtime_context_insert_index,
                        turn_objective_message=turn_objective_message,
                    )
                    validation_error = validate_provider_chat_request(
                        self.provider,
                        request_messages,
                    )
                    if validation_error is not None:
                        terminal_error = ErrorEvent(
                            message=validation_error.message,
                            code=validation_error.code,
                        )
                        self._write_turn_call_log(
                            "turn_policy_decision",
                            action="stop",
                            reason=terminal_error.message,
                            code=terminal_error.code,
                            iteration=iterations,
                            attempt=_call_attempt,
                        )
                        yield self._transition(AgentState.ERROR)
                        yield terminal_error
                        break
                    identical_request_action = self._identical_request_loop_break_action(
                        request_messages,
                        first_attempt=_call_attempt == 0,
                    )
                    if identical_request_action == "abort":
                        terminal_error = ErrorEvent(
                            message=(
                                "Turn stopped after "
                                f"{self._identical_request_streak} consecutive "
                                "byte-identical provider requests "
                                "(identical_request_loop_break_threshold="
                                f"{self.config.identical_request_loop_break_threshold})."
                            ),
                            code="identical_request_loop_abort",
                        )
                        self._write_turn_call_log(
                            "turn_policy_decision",
                            action=(
                                "artifact_degraded_finish"
                                if artifact_delivery_final_response_pending
                                else "stop"
                            ),
                            reason=terminal_error.message,
                            code=terminal_error.code,
                            identical_request_streak=self._identical_request_streak,
                            iteration=iterations,
                            attempt=_call_attempt,
                        )
                        if artifact_delivery_final_response_pending:
                            yield _finish_artifact_delivery_degraded(
                                reason=terminal_error.message,
                                code=terminal_error.code,
                            )
                            terminal_error = None
                        else:
                            yield self._transition(AgentState.ERROR)
                            yield terminal_error
                        break
                    if identical_request_action == "perturb":
                        request_messages = self._append_identical_request_loop_nudge(
                            request_messages
                        )
                        if _call_attempt == 0:
                            self.config.metadata["identical_request_loop_perturbations"] = (
                                self.config.metadata.get(
                                    "identical_request_loop_perturbations", 0
                                )
                                + 1
                            )
                            self._write_turn_call_log(
                                "identical_request_loop_perturbed",
                                identical_request_streak=self._identical_request_streak,
                                iteration=iterations,
                            )
                    self._write_context_stage(
                        "stream:context",
                        request_messages,
                        call_id=call_id,
                        iteration=iterations,
                        attempt=_call_attempt,
                        sanitize=request_sanitize_result,
                    )

                    terminal_error = _turn_llm_call_budget_error(turn_llm_calls + 1)
                    if terminal_error is not None:
                        self._write_turn_call_log(
                            "turn_policy_decision",
                            action=(
                                "artifact_degraded_finish"
                                if artifact_delivery_final_response_pending
                                else "stop"
                            ),
                            reason=terminal_error.message,
                            code=terminal_error.code,
                            sent_llm_calls=turn_llm_calls,
                            attempted_llm_call=turn_llm_calls + 1,
                            iteration=iterations,
                            attempt=_call_attempt,
                        )
                        if artifact_delivery_final_response_pending:
                            yield _finish_artifact_delivery_degraded(
                                reason=terminal_error.message,
                                code=terminal_error.code,
                            )
                            terminal_error = None
                        else:
                            yield self._transition(AgentState.ERROR)
                            yield terminal_error
                        break

                    call_chat_cfg = self._workspace_edit_gate_chat_config(
                        chat_cfg,
                        workspace_edit_gate_details,
                        provider_tools_for_call,
                        recovery_read_paths=workspace_edit_gate_recovery_read_paths,
                        recovery_reads_remaining=(
                            workspace_edit_gate_recovery_reads_remaining
                        ),
                    )
                    forced_tool_choice = self.config.metadata.get("meta_match_tool_choice")
                    if (
                        forced_tool_choice is not None
                        and workspace_edit_gate_details is None
                        and provider_tools_for_call
                        and request_messages
                        and not _tail_has_tool_result(request_messages)
                    ):
                        call_chat_cfg = call_chat_cfg.model_copy(
                            update={"tool_choice": forced_tool_choice}
                        )
                    _attempt_thinking_disabled = False
                    if _disable_thinking_for_next_provider_call:
                        call_chat_cfg = _chat_config_with_thinking_disabled(call_chat_cfg)
                        _disable_thinking_for_next_provider_call = False
                        _attempt_thinking_disabled = True
                    if deadline_thinking_off_armed:
                        call_chat_cfg = _chat_config_with_thinking_disabled(call_chat_cfg)
                        _attempt_thinking_disabled = True
                    if self._provider_request_correlation is not None:
                        call_chat_cfg = call_chat_cfg.model_copy(
                            update={
                                "provider_request_correlation": (
                                    self._provider_request_correlation
                                )
                            }
                        )

                    self._write_turn_call_log(
                        "llm_request",
                        call_id=call_id,
                        iteration=iterations,
                        attempt=_call_attempt,
                        messages=request_messages,
                        tools=provider_tools_for_call,
                        config=call_chat_cfg,
                    )
                    self._record_provider_tool_schema_event(
                        tools=provider_tools_for_call,
                        iteration=iterations,
                        attempt=_call_attempt,
                        call_id=call_id,
                        tools_supported=tools_supported_for_call,
                    )
                    turn_llm_calls += 1
                    cache_prompt_snapshot = None
                    if self._session_key:
                        cache_prompt_snapshot = record_prompt_state(
                            messages=request_messages,
                            tools=provider_tools_for_call,
                            config=call_chat_cfg,
                            model=self.config.model_id or "",
                        )

                    _got_done_event = False
                    attempt_user_visible_emitted = False
                    # Time-to-first-event for this provider call, stamped once
                    # at the first streamed event (diagnostics only).
                    first_event_at: float | None = None

                    def _notify_call_outcome(*, ok: bool, failure_kind: str = "") -> None:
                        self._notify_provider_call_observer(
                            ttft_ms=(
                                int((first_event_at - call_started_at) * 1000)
                                if first_event_at is not None
                                else None
                            ),
                            duration_ms=int((time.monotonic() - call_started_at) * 1000),
                            ok=ok,
                            failure_kind=failure_kind,
                        )

                    usage_call: UsageCallStart | None = None
                    usage_call_terminal = False
                    usage_unknown_reason = "provider_stream_ended_without_usage"
                    if usage_scope is not None and not provider_accounts_physical_usage(
                        self.provider
                    ):
                        usage_call = await self._usage_call_start(usage_scope)

                    try:
                        if self._failure_injector is None:
                            raw_stream = self.provider.chat(
                                request_messages,
                                tools=provider_tools_for_call,
                                config=call_chat_cfg,
                            )
                        else:
                            # Test-only seam: the injector either delegates this
                            # exact call to self.provider or replaces it with one
                            # scripted synthetic failure (see provider/types.py).
                            raw_stream = self._failure_injector.chat(
                                self.provider,
                                request_messages,
                                tools=provider_tools_for_call,
                                config=call_chat_cfg,
                            )
                        async for raw_ev in self._stream_provider_events_with_deadline(
                            raw_stream,
                            loop=_loop,
                            total_deadline=_total_deadline,
                        ):
                            if first_event_at is None:
                                first_event_at = time.monotonic()
                            if isinstance(raw_ev, ProviderTextDelta):
                                assistant_text_parts.append(raw_ev.text)
                                if raw_ev.text:
                                    attempt_user_visible_emitted = True
                                if text_presentation_decided:
                                    # A tool already appeared this call, so all
                                    # text here is intermediate narration.
                                    yield TextDeltaEvent(
                                        text=raw_ev.text, presentation="intermediate"
                                    )
                                else:
                                    # No tool has appeared yet. Stream the text live,
                                    # token by token, as the answer rather than
                                    # holding it until the call ends: buffering froze
                                    # the Web UI for the whole generation on plain
                                    # (no-tool) Q&A, which is the common case on any
                                    # tools-capable model (issue #358). If a tool
                                    # later appears this call, subsequent text flips
                                    # to "intermediate" above; the few pre-tool tokens
                                    # already shown as answer are a deliberate,
                                    # harmless trade for live output.
                                    yield TextDeltaEvent(
                                        text=raw_ev.text, presentation="answer"
                                    )

                            elif isinstance(raw_ev, ProviderReasoningDelta):
                                # Reasoning is the model's thinking, not the
                                # answer: re-emit as ThinkingEvent and keep it
                                # out of assistant_text_parts. The joined text
                                # still arrives via DoneEvent.reasoning_content.
                                yield ThinkingEvent(text=raw_ev.text)
                                if (
                                    wrapup_margin_seconds > 0
                                    and _total_deadline is not None
                                    and not deadline_wrapup_armed
                                    and not attempt_user_visible_emitted
                                    and not pending_tools
                                    and not tool_calls
                                    # Mirror the request-splice gates: the
                                    # finalization messages take precedence
                                    # over the directive, and the splice is
                                    # withheld on an assistant tail. Preempting
                                    # a stream the retry cannot splice into
                                    # discards reasoning for a directive-free,
                                    # otherwise identical request.
                                    and not artifact_delivery_final_response_pending
                                    and not max_iterations_finalization_pending
                                    and not post_write_convergence_finalization_pending
                                    and (
                                        not turn_messages
                                        or turn_messages[-1].role != "assistant"
                                    )
                                    and _loop.time()
                                    > _total_deadline - wrapup_margin_seconds
                                ):
                                    # The wrap-up directive arms only at
                                    # iteration boundaries, so a reasoning-only
                                    # stream that consumes the whole margin ends
                                    # at the hard deadline without the directive
                                    # ever being delivered. Preempt while margin
                                    # remains and retry the call with the
                                    # directive spliced in; the discarded
                                    # reasoning prefix was running into the hard
                                    # kill anyway. One-shot: arming makes this
                                    # branch unreachable afterwards.
                                    remaining_seconds = max(
                                        0.0, _total_deadline - _loop.time()
                                    )
                                    deadline_wrapup_message = Message(
                                        role="user",
                                        content=_DEADLINE_WRAPUP_DIRECTIVE_TEMPLATE.format(
                                            minutes=max(
                                                1, int(remaining_seconds // 60)
                                            ),
                                        ),
                                    )
                                    deadline_wrapup_armed = True
                                    # The retry runs thinking-disabled: the
                                    # margin exists to spend the last stretch
                                    # answering, and a thinking-on retry can
                                    # burn the entire remainder on another
                                    # reasoning mega-stream that the hard
                                    # deadline then kills with nothing
                                    # delivered.
                                    _disable_thinking_for_next_provider_call = True
                                    if bool(
                                        getattr(
                                            self.config,
                                            "deadline_wrapup_sticky_thinking_off",
                                            False,
                                        )
                                    ):
                                        # Sticky variant: the one-shot above
                                        # covers only the retry; the next
                                        # iteration re-enables thinking and can
                                        # spend the rest of the margin on
                                        # another mega-stream. Arming the
                                        # deadline cutoff keeps every remaining
                                        # call thinking-disabled.
                                        deadline_thinking_off_armed = True
                                        append_runtime_event(
                                            self.config.runtime_events_path,
                                            {
                                                "feature": "deadline_wrapup",
                                                "name": (
                                                    "deadline_wrapup"
                                                    ".sticky_thinking_off"
                                                ),
                                                "action": (
                                                    "disable_thinking"
                                                    "_until_deadline"
                                                ),
                                                "reason": (
                                                    "reasoning_stream_preempt"
                                                ),
                                                "iteration": iterations,
                                                "attempt": _call_attempt,
                                                "session_key": self._session_key,
                                                "agent_id": (
                                                    self.config.tool_result_store_agent_id
                                                    or self.config.metadata.get(
                                                        "agent_id"
                                                    )
                                                ),
                                            },
                                        )
                                    self._write_turn_call_log(
                                        "turn_policy_decision",
                                        action="deadline_wrapup",
                                        reason="reasoning_stream_preempt",
                                        code="deadline_wrapup_preempt",
                                        iteration=iterations,
                                        attempt=_call_attempt,
                                        remaining_seconds=int(remaining_seconds),
                                        margin_seconds=wrapup_margin_seconds,
                                    )
                                    _got_error = True
                                    _stream_policy_preempt = True
                                    break  # break stream, retry with directive
                                if (
                                    _reasoning_stream_char_cap > 0
                                    and not _reasoning_cap_preempt_done
                                ):
                                    attempt_reasoning_stream_chars += len(
                                        raw_ev.text or ""
                                    )
                                    if (
                                        attempt_reasoning_stream_chars
                                        > _reasoning_stream_char_cap
                                        and not attempt_user_visible_emitted
                                        and not pending_tools
                                        and not tool_calls
                                        # Thinking already off for this call:
                                        # a retry sans thinking changes
                                        # nothing, so let the stream run.
                                        and not _attempt_thinking_disabled
                                    ):
                                        # Runaway reasoning-only stream: discard
                                        # the partial reasoning and retry the
                                        # call with thinking disabled for that
                                        # retry only, so the budget goes to
                                        # tool calls instead of one unbounded
                                        # reasoning stream. One preempt per
                                        # iteration: if the provider keeps
                                        # streaming reasoning on the retry, it
                                        # runs to completion.
                                        _reasoning_cap_preempt_done = True
                                        _disable_thinking_for_next_provider_call = True
                                        self._write_turn_call_log(
                                            "turn_policy_decision",
                                            action="reasoning_cap",
                                            reason="reasoning_stream_char_cap",
                                            code="reasoning_cap_preempt",
                                            iteration=iterations,
                                            attempt=_call_attempt,
                                            reasoning_chars=(
                                                attempt_reasoning_stream_chars
                                            ),
                                            cap_chars=_reasoning_stream_char_cap,
                                        )
                                        # The turn-call log is a raw debug
                                        # stream that run harnesses do not
                                        # collect; the runtime event is what
                                        # lets delivery gates tell a designed
                                        # cap preempt (whose retry runs
                                        # thinking-disabled) apart from a
                                        # treatment delivery failure.
                                        append_runtime_event(
                                            self.config.runtime_events_path,
                                            {
                                                "feature": "reasoning_cap",
                                                "name": "reasoning_cap.preempt",
                                                "action": "retry_without_thinking",
                                                "reason": (
                                                    "reasoning_stream_char_cap"
                                                ),
                                                "iteration": iterations,
                                                "attempt": _call_attempt,
                                                "reasoning_chars": (
                                                    attempt_reasoning_stream_chars
                                                ),
                                                "cap_chars": (
                                                    _reasoning_stream_char_cap
                                                ),
                                                "session_key": self._session_key,
                                                "agent_id": (
                                                    self.config.tool_result_store_agent_id
                                                    or self.config.metadata.get(
                                                        "agent_id"
                                                    )
                                                ),
                                            },
                                        )
                                        _got_error = True
                                        _stream_policy_preempt = True
                                        break  # break stream, retry sans thinking

                            elif isinstance(raw_ev, ProviderToolUseStart):
                                if not tools_supported_for_call:
                                    if (
                                        artifact_delivery_final_response_pending
                                        or max_iterations_finalization_pending
                                        or post_write_convergence_finalization_pending
                                    ):
                                        ignored_post_delivery_tool_use = True
                                    continue
                                if (
                                    not isinstance(raw_ev.tool_use_id, str)
                                    or not raw_ev.tool_use_id.strip()
                                    or not isinstance(raw_ev.tool_name, str)
                                    or not raw_ev.tool_name.strip()
                                    or raw_ev.tool_use_id in seen_tool_use_ids
                                ):
                                    provider_error = ProviderErrorEvent(
                                        message=(
                                            "Provider emitted a duplicate or invalid "
                                            "tool identity in one response"
                                        ),
                                        code="provider_protocol_error",
                                    )
                                    provider_error_for_log = provider_error
                                    _got_error = True
                                    pending_tools.clear()
                                    tool_calls.clear()
                                    tool_argument_heartbeat_chars.clear()
                                    break
                                seen_tool_use_ids.add(raw_ev.tool_use_id)
                                # A tool follows, so any further text this call is
                                # intermediate narration between tools, not the answer.
                                text_presentation_decided = True
                                pending_tools[raw_ev.tool_use_id] = _StreamAccumulator(
                                    tool_use_id=raw_ev.tool_use_id,
                                    tool_name=raw_ev.tool_name,
                                    synthetic_from_text=raw_ev.synthetic_from_text,
                                )
                                tool_argument_heartbeat_chars[raw_ev.tool_use_id] = 0
                                attempt_user_visible_emitted = True
                                yield ToolUseStartEvent(
                                    tool_use_id=raw_ev.tool_use_id,
                                    tool_name=raw_ev.tool_name,
                                    synthetic_from_text=raw_ev.synthetic_from_text,
                                    started_at=int(time.time() * 1000),
                                )

                            elif isinstance(raw_ev, ProviderToolUseDelta):
                                if not tools_supported_for_call:
                                    continue
                                delta_tool_use_id = raw_ev.tool_use_id
                                acc = (
                                    pending_tools.get(delta_tool_use_id)
                                    if isinstance(delta_tool_use_id, str)
                                    and delta_tool_use_id.strip()
                                    else None
                                )
                                if acc is None or not isinstance(raw_ev.json_fragment, str):
                                    provider_error = ProviderErrorEvent(
                                        message=(
                                            "Provider emitted a tool argument delta without "
                                            "a matching active tool call"
                                        ),
                                        code="provider_protocol_error",
                                    )
                                    provider_error_for_log = provider_error
                                    _got_error = True
                                    pending_tools.clear()
                                    tool_calls.clear()
                                    tool_argument_heartbeat_chars.clear()
                                    break
                                json_fragment = raw_ev.json_fragment
                                acc.json_buf.append(json_fragment)
                                acc.json_chars += len(json_fragment)
                                if json_fragment:
                                    yield ToolUseDeltaEvent(
                                        tool_use_id=raw_ev.tool_use_id,
                                        json_fragment=json_fragment,
                                    )
                                last_heartbeat_chars = tool_argument_heartbeat_chars.get(
                                    raw_ev.tool_use_id, 0
                                )
                                if (
                                    acc.json_chars - last_heartbeat_chars
                                    >= _TOOL_ARGUMENT_HEARTBEAT_CHARS
                                ):
                                    tool_argument_heartbeat_chars[raw_ev.tool_use_id] = (
                                        acc.json_chars
                                    )
                                    yield RunHeartbeatEvent(
                                        phase="llm_tool_arguments",
                                        elapsed_ms=int(
                                            (time.monotonic() - call_started_at) * 1000
                                        ),
                                        idle_ms=0,
                                        message=(f"Receiving {acc.tool_name} arguments"),
                                    )

                            elif isinstance(raw_ev, ToolUseEndEvent):
                                if not tools_supported_for_call:
                                    if (
                                        artifact_delivery_final_response_pending
                                        or max_iterations_finalization_pending
                                        or post_write_convergence_finalization_pending
                                    ):
                                        ignored_post_delivery_tool_use = True
                                    continue
                                end_tool_use_id = raw_ev.tool_use_id
                                if (
                                    isinstance(end_tool_use_id, str)
                                    and end_tool_use_id.strip()
                                ):
                                    acc = pending_tools.pop(end_tool_use_id, None)
                                    tool_argument_heartbeat_chars.pop(end_tool_use_id, None)
                                else:
                                    acc = None
                                if acc is None:
                                    provider_error = ProviderErrorEvent(
                                        message=(
                                            "Provider ended a tool call without one "
                                            "matching start event"
                                        ),
                                        code="provider_protocol_error",
                                    )
                                    provider_error_for_log = provider_error
                                    _got_error = True
                                    pending_tools.clear()
                                    tool_calls.clear()
                                    tool_argument_heartbeat_chars.clear()
                                    break
                                invalid_arguments = not isinstance(raw_ev.arguments, dict)
                                if not invalid_arguments:
                                    try:
                                        json.dumps(raw_ev.arguments, allow_nan=False)
                                    except (OverflowError, RecursionError, TypeError, ValueError):
                                        invalid_arguments = True
                                if (
                                    not isinstance(raw_ev.tool_name, str)
                                    or not raw_ev.tool_name.strip()
                                    or raw_ev.tool_name != acc.tool_name
                                    or invalid_arguments
                                ):
                                    provider_error = ProviderErrorEvent(
                                        message=(
                                            "Provider ended a tool call with inconsistent "
                                            "identity or invalid arguments"
                                        ),
                                        code="provider_protocol_error",
                                    )
                                    provider_error_for_log = provider_error
                                    _got_error = True
                                    pending_tools.clear()
                                    tool_calls.clear()
                                    tool_argument_heartbeat_chars.clear()
                                    break
                                # ToolUseEndEvent is the provider boundary's
                                # canonical, validated argument object.  Do not
                                # reparse provisional deltas here: that would
                                # undo provider-specific repair and can replace
                                # authoritative terminal arguments with a
                                # malformed ``_raw`` fallback.
                                arguments = raw_ev.arguments
                                synthetic_from_text = (
                                    acc.synthetic_from_text
                                    if acc is not None
                                    else raw_ev.synthetic_from_text
                                )
                                tool_calls.append(
                                    ToolCall(
                                        tool_use_id=raw_ev.tool_use_id,
                                        tool_name=raw_ev.tool_name,
                                        arguments=arguments,
                                        synthetic_from_text=synthetic_from_text,
                                    )
                                )

                            elif isinstance(raw_ev, ProviderDoneEvent):
                                # Call ended. All text was already streamed live as
                                # it arrived, so there is nothing held to flush here.
                                if _got_done_event:
                                    # One physical stream owns one receipt. A
                                    # malformed adapter repeating Done must not
                                    # duplicate either legacy or ledger totals.
                                    continue
                                provider_done_for_log = raw_ev
                                _got_done_event = True
                                physical_usage_provider = str(
                                    getattr(
                                        raw_ev,
                                        "_opensquilla_usage_provider",
                                        "",
                                    )
                                    or ""
                                )
                                physical_usage_model = str(
                                    getattr(raw_ev, "_opensquilla_usage_model", "")
                                    or raw_ev.model
                                    or self.config.model_id
                                    or ""
                                )
                                executed_provider_id = str(
                                    physical_usage_provider
                                    or getattr(raw_ev, "provider", "")
                                    or getattr(self.provider, "active_provider_id", "")
                                    or self.config.provider_id
                                    or getattr(self.provider, "provider_id", "")
                                    or getattr(self.provider, "provider_name", "")
                                    or ""
                                )
                                if usage_call is not None and not usage_call_terminal:
                                    # A malformed provider that emits duplicate Done
                                    # events still finalizes this call envelope once.
                                    usage_call_terminal = True
                                    await self._usage_call_finalize(usage_call, raw_ev)
                                iter_input_tokens = raw_ev.input_tokens
                                iter_output_tokens = raw_ev.output_tokens
                                iter_reasoning_tokens = raw_ev.reasoning_tokens
                                iter_reasoning_content = raw_ev.reasoning_content
                                iter_thinking_signature = raw_ev.thinking_signature
                                total_billed_cost += raw_ev.billed_cost
                                total_input_tokens += raw_ev.input_tokens
                                total_output_tokens += raw_ev.output_tokens
                                total_reasoning_tokens += raw_ev.reasoning_tokens
                                total_cached_tokens += raw_ev.cached_tokens
                                total_cache_write_tokens += raw_ev.cache_write_tokens
                                usage_breakdown = getattr(
                                    raw_ev,
                                    "model_usage_breakdown",
                                    None,
                                )
                                valid_usage_breakdown = (
                                    [
                                        dict(usage_row)
                                        for usage_row in usage_breakdown
                                        if isinstance(usage_row, dict)
                                    ]
                                    if isinstance(usage_breakdown, list)
                                    else []
                                )
                                usage_source_rows = (
                                    valid_usage_breakdown
                                    if valid_usage_breakdown
                                    else [
                                        {
                                            "cost_source": getattr(
                                                raw_ev,
                                                "cost_source",
                                                "none",
                                            ),
                                            "billed_cost": raw_ev.billed_cost,
                                            "billing_receipt": getattr(
                                                raw_ev,
                                                "billing_receipt",
                                                None,
                                            ),
                                        }
                                    ]
                                )
                                for usage_source_row in usage_source_rows:
                                    usage_source = str(
                                        usage_source_row.get("cost_source")
                                        or usage_source_row.get("costSource")
                                        or "none"
                                    ).strip().lower()
                                    usage_receipt = usage_source_row.get(
                                        "billing_receipt",
                                        usage_source_row.get("billingReceipt"),
                                    )
                                    if isinstance(usage_receipt, dict):
                                        receipt_status = str(
                                            usage_receipt.get("status") or ""
                                        ).strip().lower()
                                    else:
                                        receipt_status = str(
                                            getattr(usage_receipt, "status", "") or ""
                                        ).strip().lower()
                                    legacy_billed_cost = _usage_float(
                                        usage_source_row.get(
                                            "billed_cost",
                                            usage_source_row.get("billedCost", 0.0),
                                        )
                                    )
                                    if usage_source == "mixed":
                                        total_provider_billed_entries += 1
                                        total_unbilled_entries += 1
                                    elif (
                                        usage_source in {
                                            "provider_billed",
                                            "openrouter_usage",
                                        }
                                        or receipt_status == "confirmed"
                                        # Compatibility bridge for adapters and
                                        # test doubles predating native receipts.
                                        # Zero remains ambiguous and therefore
                                        # requires an explicit source/receipt.
                                        or legacy_billed_cost > 0.0
                                    ):
                                        total_provider_billed_entries += 1
                                    else:
                                        total_unbilled_entries += 1
                                _accumulate_turn_cost(
                                    raw_ev,
                                    default_provider=executed_provider_id,
                                    default_model=physical_usage_model,
                                )
                                cost_receipt_counted = True
                                if physical_usage_model:
                                    last_actual_model = physical_usage_model
                                last_actual_provider = executed_provider_id
                                # Usage/cost accounting is billed-attempt based: discarded
                                # invalid responses still consumed provider tokens, but
                                # they must not be appended to conversation history or the
                                # live context-window gauge below.
                                if valid_usage_breakdown:
                                    turn_model_usage_breakdown.extend(valid_usage_breakdown)
                                if self._usage_tracker and self._session_key:
                                    # Forward the provider's real per-call billed_cost so
                                    # the per-model breakdown can show actual numbers
                                    # instead of the cache-blind pricing-table estimate.
                                    # See engine/usage.py:ModelUsage.billed_cost and
                                    # gateway/rpc_usage.py:_reconcile_breakdown_to_row
                                    # (the pro-rate fallback now skips when items
                                    # already carry real billed totals).
                                    if valid_usage_breakdown:
                                        for usage_row in valid_usage_breakdown:
                                            cache_read = (
                                                usage_row.get("cache_read_tokens")
                                                if "cache_read_tokens" in usage_row
                                                else usage_row.get("cached_tokens")
                                            )
                                            self._usage_tracker.add(
                                                self._session_key,
                                                input_tokens=_usage_int(
                                                    usage_row.get("input_tokens") or 0
                                                ),
                                                output_tokens=_usage_int(
                                                    usage_row.get("output_tokens") or 0
                                                ),
                                                model_id=str(
                                                    usage_row.get("model")
                                                    or physical_usage_model
                                                    or ""
                                                ),
                                                cache_read_tokens=_usage_int(cache_read or 0),
                                                cache_write_tokens=_usage_int(
                                                    usage_row.get("cache_write_tokens") or 0
                                                ),
                                                billed_cost=_usage_float(
                                                    usage_row.get("billed_cost") or 0.0
                                                ),
                                                provider=str(
                                                    usage_row.get("provider")
                                                    or physical_usage_provider
                                                    or executed_provider_id
                                                ),
                                                cost_source=str(
                                                    usage_row.get("cost_source")
                                                    or usage_row.get("costSource")
                                                    or "none"
                                                ),
                                            )
                                    else:
                                        self._usage_tracker.add(
                                            self._session_key,
                                            input_tokens=raw_ev.input_tokens,
                                            output_tokens=raw_ev.output_tokens,
                                            model_id=physical_usage_model,
                                            cache_read_tokens=raw_ev.cached_tokens,
                                            cache_write_tokens=raw_ev.cache_write_tokens,
                                            billed_cost=raw_ev.billed_cost,
                                            provider=executed_provider_id,
                                            cost_source=getattr(raw_ev, "cost_source", "none"),
                                        )
                                ensemble_trace = getattr(raw_ev, "ensemble_trace", None)
                                if isinstance(ensemble_trace, dict):
                                    last_ensemble_trace = dict(ensemble_trace)
                                    turn_ensemble_request_count += _usage_int(
                                        ensemble_trace.get("llm_request_count") or 0
                                    )

                            elif isinstance(raw_ev, ProviderErrorEvent):
                                provider_error_for_log = raw_ev
                                usage_unknown_reason = provider_error_usage_reason(
                                    raw_ev.code
                                )
                                known_usage_receipt = has_known_provider_usage_receipt(raw_ev)
                                if (
                                    usage_call is not None
                                    and not usage_call_terminal
                                    and known_usage_receipt
                                ):
                                    usage_call_terminal = True
                                    await self._usage_call_finalize(usage_call, raw_ev)
                                if known_usage_receipt and not cost_receipt_counted:
                                    _accumulate_turn_cost(
                                        raw_ev,
                                        default_provider=(
                                            usage_call.provider
                                            if usage_call is not None
                                            else str(
                                                self.config.provider_id
                                                or getattr(
                                                    self.provider,
                                                    "provider_name",
                                                    "",
                                                )
                                                or ""
                                            )
                                        ),
                                        default_model=(
                                            usage_call.model
                                            if usage_call is not None
                                            else str(self.config.model_id or "")
                                        ),
                                    )
                                    cost_receipt_counted = True
                                # One-shot thinking/reasoning fallback
                                _err_lower = raw_ev.message.lower()
                                if (
                                    thinking_enabled
                                    and not _thinking_fallback_done
                                    and ("thinking" in _err_lower or "reasoning" in _err_lower)
                                ):
                                    _thinking_fallback_done = True
                                    _disable_thinking_for_next_provider_call = True
                                    _got_error = True
                                    break  # break stream, retry

                                provider_error = raw_ev
                                _got_error = True
                                break  # break stream loop

                            elif isinstance(raw_ev, ProviderHeartbeatEvent):
                                yield RunHeartbeatEvent(
                                    phase=raw_ev.phase,
                                    message=raw_ev.message,
                                )
                            elif isinstance(raw_ev, ProviderEnsembleProgressEvent):
                                yield EnsembleProgressEvent(
                                    event_type=raw_ev.event_type,
                                    proposer_index=raw_ev.proposer_index,
                                    proposer_label=raw_ev.proposer_label,
                                    proposer_model=raw_ev.proposer_model,
                                    proposer_provider=raw_ev.proposer_provider,
                                    sample_index=raw_ev.sample_index,
                                    elapsed_ms=raw_ev.elapsed_ms,
                                    input_tokens=raw_ev.input_tokens,
                                    output_tokens=raw_ev.output_tokens,
                                    cost_usd=raw_ev.cost_usd,
                                    error=raw_ev.error,
                                )
                    except _IterationStreamTimeoutError:
                        usage_unknown_reason = "iteration_timeout"
                        _notify_call_outcome(ok=False, failure_kind="iteration_timeout")
                        if artifact_delivery_final_response_pending:
                            yield _finish_artifact_delivery_degraded(
                                reason=(
                                    f"Iteration {iterations} exceeded "
                                    f"iteration_timeout ({self.config.iteration_timeout}s) "
                                    "during final artifact response generation"
                                ),
                                code="iteration_timeout",
                            )
                            break
                        yield self._transition(AgentState.ERROR)
                        terminal_error = ErrorEvent(
                            message=(
                                f"Iteration {iterations} exceeded iteration_timeout"
                                f" ({self.config.iteration_timeout}s) during LLM streaming"
                            ),
                            code="iteration_timeout",
                        )
                        yield terminal_error
                        break
                    except asyncio.CancelledError:
                        usage_unknown_reason = "cancelled"
                        raise
                    except TimeoutError:
                        # Total-deadline timeout raised by the stream wrapper:
                        # record the failed call, then propagate unchanged.
                        usage_unknown_reason = "total_timeout"
                        _notify_call_outcome(ok=False, failure_kind="total_timeout")
                        raise
                    except Exception:
                        # A provider stream that raises (instead of yielding a
                        # ProviderErrorEvent) must still enter the stats before
                        # the exception propagates unchanged.
                        usage_unknown_reason = "provider_exception"
                        _notify_call_outcome(ok=False, failure_kind="raised")
                        raise
                    finally:
                        if usage_call is not None and not usage_call_terminal:
                            await self._usage_call_unknown(
                                usage_call,
                                usage_unknown_reason,
                            )

                    call_duration_ms = int((time.monotonic() - call_started_at) * 1000)
                    _notify_call_outcome(
                        ok=provider_error_for_log is None,
                        failure_kind=(
                            str(provider_error_for_log.code or "provider_error")
                            if provider_error_for_log is not None
                            else ""
                        ),
                    )
                    response_payload = {
                        "call_id": call_id,
                        "iteration": iterations,
                        "attempt": _call_attempt,
                        "duration_ms": call_duration_ms,
                        "text": "".join(assistant_text_parts),
                        "tool_calls": [
                            {
                                "tool_use_id": tc.tool_use_id,
                                "name": tc.tool_name,
                                "arguments": tc.arguments,
                            }
                            for tc in tool_calls
                        ],
                        "got_done_event": _got_done_event,
                    }
                    if provider_done_for_log is not None:
                        usage_payload: dict[str, Any] = {
                            "stop_reason": provider_done_for_log.stop_reason,
                            "input_tokens": provider_done_for_log.input_tokens,
                            "output_tokens": provider_done_for_log.output_tokens,
                            "reasoning_tokens": provider_done_for_log.reasoning_tokens,
                            "cached_tokens": provider_done_for_log.cached_tokens,
                            "cache_write_tokens": provider_done_for_log.cache_write_tokens,
                            "billed_cost": provider_done_for_log.billed_cost,
                            "cost_source": getattr(provider_done_for_log, "cost_source", "none"),
                            "model": provider_done_for_log.model,
                            "provider": str(
                                getattr(provider_done_for_log, "provider", "")
                                or getattr(self.provider, "active_provider_id", "")
                                or self.config.provider_id
                                or getattr(self.provider, "provider_id", "")
                                or getattr(self.provider, "provider_name", "")
                                or ""
                            ),
                        }
                        response_payload["usage"] = usage_payload
                        model_usage_breakdown = getattr(
                            provider_done_for_log,
                            "model_usage_breakdown",
                            None,
                        )
                        if model_usage_breakdown:
                            usage_payload["model_usage_breakdown"] = model_usage_breakdown
                        ensemble_trace = getattr(provider_done_for_log, "ensemble_trace", None)
                        if ensemble_trace:
                            response_payload["ensemble_trace"] = ensemble_trace
                    if provider_error_for_log is not None:
                        response_payload["error"] = {
                            "message": provider_error_for_log.message,
                            "code": provider_error_for_log.code,
                        }
                        self._write_turn_call_log("llm_error", **response_payload)
                    else:
                        self._write_turn_call_log("llm_response", **response_payload)

                    # -- after async for (retry loop level) --
                    terminal_error = _turn_budget_error()
                    if terminal_error is not None:
                        if artifact_delivery_final_response_pending:
                            yield _finish_artifact_delivery_degraded(
                                reason=terminal_error.message,
                                code=terminal_error.code,
                            )
                            terminal_error = None
                        else:
                            yield self._transition(AgentState.ERROR)
                            yield terminal_error
                        break
                    response_text = "".join(assistant_text_parts)
                    if (
                        ignored_post_delivery_tool_use
                        and not response_text.strip()
                        # A policy preempt retries this call; emitting the
                        # canned finalization text first would surface it
                        # before the retried attempt's real answer.
                        and not _stream_policy_preempt
                    ):
                        if artifact_delivery_final_response_pending:
                            response_text = self._artifact_delivery_final_response_text(
                                artifact_delivery_final_response_artifacts
                            )
                        elif max_iterations_finalization_pending:
                            response_text = (
                                "I reached the configured iteration limit after completing "
                                "the available tool step. Here is the best partial result so far."
                            )
                        elif post_write_convergence_finalization_pending:
                            response_text = (
                                "The workspace diff stayed stable after clean validation. "
                                "Here is the current validated patch state."
                            )
                        if response_text:
                            assistant_text_parts.append(response_text)
                            attempt_user_visible_emitted = True
                            yield TextDeltaEvent(text=response_text)
                    post_tool_turn = _tail_has_tool_result(request_messages)
                    if (
                        not post_tool_turn
                        and request_turn_messages
                        and (
                            (
                                deadline_wrapup_message is not None
                                and request_turn_messages[-1] is deadline_wrapup_message
                            )
                            or (
                                reasoning_only_act_now_message is not None
                                and request_turn_messages[-1]
                                is reasoning_only_act_now_message
                            )
                        )
                    ):
                        # The spliced wrap-up or act-now directive is not
                        # conversation history; empty-response recovery must
                        # still see the post-tool shape of the underlying
                        # turn. A nudge stacked after the tool results is
                        # likewise runtime-injected and must not hide that
                        # shape.
                        tail_index = len(turn_messages) - 1
                        while tail_index >= 0 and _is_runtime_nudge_message(
                            turn_messages[tail_index]
                        ):
                            tail_index -= 1
                        post_tool_turn = tail_index >= 0 and _message_has_tool_result(
                            turn_messages[tail_index]
                        )
                    if not post_tool_turn and (
                        bool(getattr(self.config, "mid_budget_no_diff_nudge", False))
                        or int(
                            getattr(self.config, "plan_only_act_now_threshold", 0) or 0
                        )
                        > 0
                        or int(
                            getattr(
                                self.config,
                                "endgame_fix_directive_margin_seconds",
                                0,
                            )
                            or 0
                        )
                        > 0
                    ):
                        # A nudge stacked after watchdog or recovery guidance
                        # pushes the tool results out of the lookback window,
                        # which would disable empty-response retry/recovery on
                        # exactly the stalled turns the lever targets. The
                        # nudge is runtime-injected, not conversation history:
                        # recompute the turn shape as if it were absent.
                        post_tool_turn = _tail_has_tool_result_ignoring_nudges(turn_messages)
                    stop_reason = (
                        getattr(provider_done_for_log, "stop_reason", None)
                        if provider_done_for_log is not None
                        else None
                    )
                    attempt_classification = _classify_provider_attempt(
                        text=response_text,
                        tool_calls=tool_calls,
                        pending_tools=pending_tools,
                        got_done_event=_got_done_event,
                        stop_reason=stop_reason,
                        reasoning_content=iter_reasoning_content,
                        reasoning_tokens=iter_reasoning_tokens,
                        user_visible_emitted=attempt_user_visible_emitted,
                    )
                    if (
                        attempt_classification.kind != _ProviderAttemptKind.OK
                        # An engine-chosen preempt truncated the stream; the
                        # incomplete attempt is self-inflicted, not a provider
                        # failure signal for the tool-loop observer.
                        and not _stream_policy_preempt
                    ):
                        self._record_tool_loop_runtime_event(
                            reason=attempt_classification.kind.value,
                            iteration=iterations,
                            provider_call_count=turn_llm_calls,
                            call_attempt=_call_attempt,
                            provider_retry_attempt=_retry_attempt,
                            post_tool_turn=post_tool_turn,
                            got_done_event=_got_done_event,
                            stop_reason=stop_reason,
                            tool_call_count=len(tool_calls),
                            pending_tool_count=len(pending_tools),
                            visible_text_chars=len(response_text.strip()),
                            reasoning_chars=len(iter_reasoning_content or ""),
                            reasoning_tokens=iter_reasoning_tokens,
                            input_tokens=iter_input_tokens,
                            output_tokens=iter_output_tokens,
                        )
                    if not _got_error and attempt_classification.kind != _ProviderAttemptKind.OK:
                        logger.warning(
                            "provider.invalid_response",
                            session_key=self._session_key,
                            model=last_actual_model or self.config.model_id or "",
                            provider=type(self.provider).__name__,
                            classification=attempt_classification.kind.value,
                            iteration=iterations,
                            call_attempt=_call_attempt,
                            provider_retry_attempt=_retry_attempt,
                            post_tool_turn=post_tool_turn,
                            got_done_event=_got_done_event,
                            stop_reason=stop_reason,
                            iter_input_tokens=iter_input_tokens,
                            iter_output_tokens=iter_output_tokens,
                            iter_reasoning_tokens=iter_reasoning_tokens,
                            reasoning_chars=len(iter_reasoning_content or ""),
                        )

                        large_context_invalid = _is_large_context_invalid_response(
                            attempt_classification.kind,
                            input_tokens=iter_input_tokens,
                        )
                        supports_reasoning_replay = supports_reasoning_prefill_replay(
                            model_capabilities=self.config.model_capabilities,
                            reasoning_content=iter_reasoning_content,
                            thinking_signature=iter_thinking_signature,
                        )
                        reasoning_prefill = reasoning_prefill_decision(
                            global_mode=getattr(
                                self.config,
                                "runtime_recovery_mode",
                                "log",
                            ),
                            mode=getattr(
                                self.config,
                                "reasoning_prefill_recovery_mode",
                                "log",
                            ),
                            attempt_kind=attempt_classification.kind.value,
                            attempted=reasoning_prefill_recovery_attempted,
                            supports_replay=supports_reasoning_replay,
                            reasoning_chars=len(iter_reasoning_content or ""),
                            reasoning_tokens=iter_reasoning_tokens,
                        )
                        if reasoning_prefill is not None:
                            self._record_runtime_recovery_event(
                                reasoning_prefill,
                                iteration=iterations,
                                provider_call_count=turn_llm_calls,
                                call_attempt=_call_attempt,
                                stop_reason=stop_reason,
                                input_tokens=iter_input_tokens,
                                output_tokens=iter_output_tokens,
                            )
                            if reasoning_prefill.action == "prefill" and iter_reasoning_content:
                                turn_messages.append(
                                    _build_reasoning_prefill_message(
                                        reasoning_content=iter_reasoning_content,
                                        thinking_signature=iter_thinking_signature,
                                    )
                                )
                                runtime_recovery_scaffolding_pending = True
                                reasoning_prefill_recovery_attempted = True
                                self.config.metadata["reasoning_prefill_recoveries"] = (
                                    self.config.metadata.get(
                                        "reasoning_prefill_recoveries",
                                        0,
                                    )
                                    + 1
                                )
                                self._write_turn_call_log(
                                    "runtime_recovery",
                                    action="prefill",
                                    mode=reasoning_prefill.mode,
                                    reason=reasoning_prefill.reason,
                                    details=reasoning_prefill.details,
                                )
                                yield WarningEvent(
                                    code="provider_reasoning_prefill_continue",
                                    message=(
                                        "The provider returned reasoning without visible "
                                        "content; continuing once with the reasoning "
                                        "prefilled."
                                    ),
                                )
                                _call_attempt += 1
                                continue

                        reasoning_continuation = reasoning_continuation_decision(
                            global_mode=getattr(
                                self.config,
                                "runtime_recovery_mode",
                                "log",
                            ),
                            mode=getattr(
                                self.config,
                                "reasoning_prefill_recovery_mode",
                                "log",
                            ),
                            attempt_kind=attempt_classification.kind.value,
                            attempted=reasoning_prefill_recovery_attempted,
                            supports_replay=supports_reasoning_replay,
                            provider_reasoning_format=(
                                self.config.model_capabilities.reasoning_format
                                if self.config.model_capabilities
                                else None
                            ),
                            reasoning_chars=len(iter_reasoning_content or ""),
                            reasoning_tokens=iter_reasoning_tokens,
                        )
                        if reasoning_continuation is not None:
                            self._record_runtime_recovery_event(
                                reasoning_continuation,
                                iteration=iterations,
                                provider_call_count=turn_llm_calls,
                                call_attempt=_call_attempt,
                                stop_reason=stop_reason,
                                input_tokens=iter_input_tokens,
                                output_tokens=iter_output_tokens,
                            )
                            if (
                                reasoning_continuation.action == "nudge"
                                and reasoning_continuation.message
                            ):
                                turn_messages.append(
                                    Message(
                                        role="assistant",
                                        content=[ContentBlockText(text="")],
                                    )
                                )
                                turn_messages.append(
                                    Message(
                                        role="user",
                                        content=reasoning_continuation.message,
                                    )
                                )
                                runtime_recovery_scaffolding_pending = True
                                reasoning_prefill_recovery_attempted = True
                                self.config.metadata["reasoning_continuation_recoveries"] = (
                                    self.config.metadata.get(
                                        "reasoning_continuation_recoveries",
                                        0,
                                    )
                                    + 1
                                )
                                self._write_turn_call_log(
                                    "runtime_recovery",
                                    action="nudge",
                                    mode=reasoning_continuation.mode,
                                    reason=reasoning_continuation.reason,
                                    details=reasoning_continuation.details,
                                )
                                yield WarningEvent(
                                    code="provider_reasoning_continuation",
                                    message=(
                                        "The provider returned reasoning without visible "
                                        "content; asking it to continue once without "
                                        "replaying hidden reasoning."
                                    ),
                                )
                                _call_attempt += 1
                                continue

                        post_tool_empty = post_tool_empty_decision(
                            global_mode=getattr(
                                self.config,
                                "runtime_recovery_mode",
                                "log",
                            ),
                            mode=getattr(
                                self.config,
                                "post_tool_empty_recovery_mode",
                                "log",
                            ),
                            attempt_kind=attempt_classification.kind.value,
                            post_tool_turn=post_tool_turn,
                            attempted=post_tool_empty_recovery_attempted,
                            reasoning_present=bool(
                                (iter_reasoning_content and iter_reasoning_content.strip())
                                or iter_reasoning_tokens > 0
                            ),
                        )
                        if post_tool_empty is not None:
                            self._record_runtime_recovery_event(
                                post_tool_empty,
                                iteration=iterations,
                                provider_call_count=turn_llm_calls,
                                call_attempt=_call_attempt,
                                stop_reason=stop_reason,
                                input_tokens=iter_input_tokens,
                                output_tokens=iter_output_tokens,
                            )
                            if post_tool_empty.action == "nudge" and post_tool_empty.message:
                                turn_messages.append(
                                    Message(
                                        role="assistant",
                                        content=[ContentBlockText(text="")],
                                    )
                                )
                                turn_messages.append(
                                    Message(role="user", content=post_tool_empty.message)
                                )
                                runtime_recovery_scaffolding_pending = True
                                post_tool_empty_recovery_attempted = True
                                self.config.metadata["post_tool_empty_recoveries"] = (
                                    self.config.metadata.get("post_tool_empty_recoveries", 0) + 1
                                )
                                self._write_turn_call_log(
                                    "runtime_recovery",
                                    action="nudge",
                                    mode=post_tool_empty.mode,
                                    reason=post_tool_empty.reason,
                                    details=post_tool_empty.details,
                                )
                                yield WarningEvent(
                                    code="post_tool_empty_recovery",
                                    message=(
                                        "The provider returned an empty response after "
                                        "tool results; asking it to continue once."
                                    ),
                                )
                                _call_attempt += 1
                                continue

                        if large_context_invalid:
                            if (
                                not _invalid_response_fallback_done
                                and self._switch_to_invalid_response_fallback(
                                    attempt_classification.kind.value
                                )
                            ):
                                _invalid_response_fallback_done = True
                                yield WarningEvent(
                                    code="provider_large_context_fallback",
                                    message=(
                                        "The provider returned no visible response for a "
                                        "large input; trying a fallback provider once."
                                    ),
                                )
                                _call_attempt += 1
                                continue

                            if (
                                attempt_classification.kind == _ProviderAttemptKind.REASONING_ONLY
                                and thinking_enabled
                                and _retry_policy.can_retry_attempt(
                                    _ProviderAttemptKind.REASONING_ONLY,
                                    _attempt_retries_used,
                                )
                            ):
                                _attempt_retries_used[_ProviderAttemptKind.REASONING_ONLY] += 1
                                _thinking_fallback_done = True
                                _disable_thinking_for_next_provider_call = True
                                logger.warning(
                                    "provider.large_context_visible_retry",
                                    session_key=self._session_key,
                                    model=last_actual_model or self.config.model_id or "",
                                    provider=type(self.provider).__name__,
                                    classification=attempt_classification.kind.value,
                                    iteration=iterations,
                                    call_attempt=_call_attempt,
                                    attempt=_attempt_retries_used.get(
                                        _ProviderAttemptKind.REASONING_ONLY, 0
                                    ),
                                    budget=_retry_policy.attempt_budgets.get(
                                        _ProviderAttemptKind.REASONING_ONLY, 0
                                    ),
                                    iter_input_tokens=iter_input_tokens,
                                    iter_output_tokens=iter_output_tokens,
                                    iter_reasoning_tokens=iter_reasoning_tokens,
                                    reasoning_chars=len(iter_reasoning_content or ""),
                                )
                                yield WarningEvent(
                                    code="provider_large_context_visible_retry",
                                    message=(
                                        "The provider returned reasoning without visible "
                                        "content for a large input; retrying once with "
                                        "thinking disabled."
                                    ),
                                )
                                _call_attempt += 1
                                continue

                            yield self._transition(AgentState.ERROR)
                            terminal_error = ErrorEvent(
                                message=(
                                    "Provider returned no visible response for a large input. "
                                    "Send the material as an attachment, summarize or shorten "
                                    "the prompt, or use a stronger model."
                                ),
                                code="empty_response",
                            )
                            yield terminal_error
                            break

                        if (
                            attempt_classification.kind == _ProviderAttemptKind.REASONING_ONLY
                            and thinking_enabled
                            and _retry_policy.can_retry_attempt(
                                _ProviderAttemptKind.REASONING_ONLY,
                                _attempt_retries_used,
                            )
                        ):
                            _attempt_retries_used[_ProviderAttemptKind.REASONING_ONLY] += 1
                            if (
                                bool(
                                    getattr(
                                        self.config, "reasoning_only_act_now", False
                                    )
                                )
                                and reasoning_only_act_now_message is None
                            ):
                                # Today's bare retry re-sends the identical
                                # request; the model that just answered it with
                                # reasoning only usually does so again. Splice
                                # in an explicit act-now instruction so the
                                # retry differs where it matters.
                                reasoning_only_act_now_message = Message(
                                    role="user",
                                    content=_REASONING_ONLY_ACT_NOW_DIRECTIVE,
                                )
                                append_runtime_event(
                                    self.config.runtime_events_path,
                                    {
                                        "feature": "reasoning_only_act_now",
                                        "name": "reasoning_only_act_now.injected",
                                        "action": "retry_with_act_now_directive",
                                        "reason": "provider_reasoning_only",
                                        "iteration": iterations,
                                        "attempt": _call_attempt,
                                        "reasoning_chars": len(
                                            iter_reasoning_content or ""
                                        ),
                                        "session_key": self._session_key,
                                        "agent_id": (
                                            self.config.tool_result_store_agent_id
                                            or self.config.metadata.get("agent_id")
                                        ),
                                    },
                                )
                            if getattr(
                                self.config, "reasoning_only_thinking_fallback", False
                            ):
                                _thinking_fallback_done = True
                                _disable_thinking_for_next_provider_call = True
                                yield WarningEvent(
                                    code="provider_reasoning_only_retry",
                                    message=(
                                        "The provider returned reasoning without visible "
                                        "content; retrying once with thinking disabled."
                                    ),
                                )
                            else:
                                yield WarningEvent(
                                    code="provider_reasoning_only_retry",
                                    message=(
                                        "The provider returned reasoning without visible content; "
                                        "retrying once to request visible content."
                                    ),
                                )
                            _call_attempt += 1
                            continue

                        if (
                            attempt_classification.kind == _ProviderAttemptKind.MALFORMED_EMPTY
                            and _retry_policy.can_retry_attempt(
                                _ProviderAttemptKind.MALFORMED_EMPTY,
                                _attempt_retries_used,
                            )
                        ):
                            _attempt_retries_used[_ProviderAttemptKind.MALFORMED_EMPTY] += 1
                            delay = backoff_sleep(
                                0,
                                _fallback.base_backoff_ms,
                                _fallback.max_backoff_ms,
                                _fake=True,
                            )
                            yield WarningEvent(
                                code="provider_empty_retry",
                                message="The provider returned an empty response; retrying once.",
                            )
                            await asyncio.sleep(delay)
                            _call_attempt += 1
                            continue

                        if (
                            attempt_classification.kind == _ProviderAttemptKind.STREAM_INCOMPLETE
                            and not attempt_classification.user_visible_emitted
                            and _retry_policy.can_retry_attempt(
                                _ProviderAttemptKind.STREAM_INCOMPLETE,
                                _attempt_retries_used,
                            )
                        ):
                            _attempt_retries_used[_ProviderAttemptKind.STREAM_INCOMPLETE] += 1
                            delay = backoff_sleep(
                                0,
                                _fallback.base_backoff_ms,
                                _fallback.max_backoff_ms,
                                _fake=True,
                            )
                            yield WarningEvent(
                                code="provider_empty_retry",
                                message=(
                                    "The provider stream ended before completion; retrying once."
                                ),
                            )
                            await asyncio.sleep(delay)
                            _call_attempt += 1
                            continue

                        if (
                            attempt_classification.kind == _ProviderAttemptKind.LENGTH_CAPPED
                            and _retry_policy.can_retry_attempt(
                                _ProviderAttemptKind.LENGTH_CAPPED,
                                _attempt_retries_used,
                            )
                        ):
                            _attempt_retries_used[_ProviderAttemptKind.LENGTH_CAPPED] += 1
                            visible_text = _append_length_capped_continuation(
                                turn_messages,
                                response_text=response_text,
                                tool_calls=tool_calls,
                            )
                            if visible_text:
                                final_text_parts.append(visible_text)
                            logger.warning(
                                "provider.output_truncated_continue",
                                session_key=self._session_key,
                                model=last_actual_model or self.config.model_id or "",
                                provider=type(self.provider).__name__,
                                iteration=iterations,
                                call_attempt=_call_attempt,
                                attempt=_attempt_retries_used.get(
                                    _ProviderAttemptKind.LENGTH_CAPPED, 0
                                ),
                                budget=_retry_policy.attempt_budgets.get(
                                    _ProviderAttemptKind.LENGTH_CAPPED, 0
                                ),
                                tool_calls=len(tool_calls),
                                visible_chars=len(visible_text),
                                iter_input_tokens=iter_input_tokens,
                                iter_output_tokens=iter_output_tokens,
                                iter_reasoning_tokens=iter_reasoning_tokens,
                            )
                            yield WarningEvent(
                                code="provider_output_continue",
                                message=(
                                    "The provider reached its output limit; continuing "
                                    "the response automatically."
                                ),
                            )
                            _call_attempt += 1
                            continue

                        if (
                            attempt_classification.kind
                            in {
                                _ProviderAttemptKind.REASONING_ONLY,
                                _ProviderAttemptKind.MALFORMED_EMPTY,
                            }
                            and not _invalid_response_fallback_done
                            and self._switch_to_invalid_response_fallback(
                                attempt_classification.kind.value
                            )
                        ):
                            _invalid_response_fallback_done = True
                            yield WarningEvent(
                                code="provider_empty_retry",
                                message=(
                                    "The provider returned no visible response; "
                                    "retrying on a fallback provider."
                                ),
                            )
                            _call_attempt += 1
                            continue

                        yield self._transition(AgentState.ERROR)
                        if attempt_classification.kind == _ProviderAttemptKind.INCOMPLETE_TOOLS:
                            terminal_error = ErrorEvent(
                                message="Provider stream ended with an incomplete tool call",
                                code="incomplete_tool_stream",
                            )
                            yield terminal_error
                            break
                        if attempt_classification.kind == _ProviderAttemptKind.STREAM_INCOMPLETE:
                            terminal_error = ErrorEvent(
                                message="Provider stream ended before a done event",
                                code="provider_stream_incomplete",
                            )
                            yield terminal_error
                            break
                        if attempt_classification.kind == _ProviderAttemptKind.LENGTH_CAPPED:
                            visible_text = response_text
                            logger.warning(
                                "provider.output_truncated_exhausted",
                                session_key=self._session_key,
                                model=last_actual_model or self.config.model_id or "",
                                provider=type(self.provider).__name__,
                                iteration=iterations,
                                call_attempt=_call_attempt,
                                attempt=_attempt_retries_used.get(
                                    _ProviderAttemptKind.LENGTH_CAPPED, 0
                                ),
                                budget=_retry_policy.attempt_budgets.get(
                                    _ProviderAttemptKind.LENGTH_CAPPED, 0
                                ),
                                tool_calls=len(tool_calls),
                                visible_chars=len(visible_text),
                                partial_preserved=bool(visible_text or final_text_parts),
                            )
                            yield WarningEvent(
                                code="provider_output_truncated",
                                message=(
                                    "The provider stopped because the output limit was reached."
                                ),
                            )
                            terminal_error = ErrorEvent(
                                message=_PROVIDER_OUTPUT_TRUNCATED_REPLY,
                                code="provider_output_truncated",
                            )
                            yield terminal_error
                            break
                        logger.warning(
                            "provider.empty_response",
                            session_key=self._session_key,
                            model=last_actual_model or self.config.model_id or "",
                            provider=type(self.provider).__name__,
                            iteration=iterations,
                            retry_attempt=_call_attempt,
                            post_tool_turn=post_tool_turn,
                            got_done_event=_got_done_event,
                            stop_reason=stop_reason,
                            iter_input_tokens=iter_input_tokens,
                            iter_output_tokens=iter_output_tokens,
                            iter_reasoning_tokens=iter_reasoning_tokens,
                            reasoning_chars=len(iter_reasoning_content or ""),
                        )
                        self._record_tool_loop_runtime_event(
                            reason="provider_empty_response_terminal",
                            iteration=iterations,
                            provider_call_count=turn_llm_calls,
                            call_attempt=_call_attempt,
                            provider_retry_attempt=_retry_attempt,
                            post_tool_turn=post_tool_turn,
                            got_done_event=_got_done_event,
                            stop_reason=stop_reason,
                            input_tokens=iter_input_tokens,
                            output_tokens=iter_output_tokens,
                            reasoning_tokens=iter_reasoning_tokens,
                            reasoning_chars=len(iter_reasoning_content or ""),
                        )
                        terminal_error = ErrorEvent(
                            message="Provider returned an empty response",
                            code="empty_response",
                        )
                        yield terminal_error
                        break

                    if (
                        not _got_error
                        and attempt_classification.kind == _ProviderAttemptKind.OK
                        and (stop_reason or "").lower() == "length"
                    ):
                        yield WarningEvent(
                            code="provider_output_truncated",
                            message="The provider stopped because the output limit was reached.",
                        )

                    if (
                        not _got_error
                        and self._session_key
                        and cache_prompt_snapshot is not None
                        and provider_done_for_log is not None
                    ):
                        cache_report = check_response_for_cache_break(
                            self._session_key,
                            cache_prompt_snapshot,
                            provider_done_for_log.cached_tokens,
                        )
                        if cache_report.break_detected:
                            logger.warning(
                                "prompt_cache.break_detected",
                                session_key=self._session_key,
                                **cache_report.to_log_dict(),
                            )

                    if not _got_error:
                        break  # stream OK, exit retry loop

                    if provider_error is None:
                        _call_attempt += 1
                        continue

                    if provider_error is not None:
                        provider_error_status_code = (
                            int(provider_error.code) if str(provider_error.code).isdigit() else None
                        )
                        failure_kind = classify_provider_error(
                            provider_name=getattr(self.provider, "provider_name", ""),
                            status_code=provider_error_status_code,
                            raw_code=provider_error.code,
                            message=provider_error.message,
                        )
                        kind = _fallback.classify_error(
                            provider_error.message,
                            provider_name=getattr(self.provider, "provider_name", ""),
                            status_code=provider_error_status_code,
                            raw_code=provider_error.code,
                        )
                        message_limit_proof = provider_error.message_limit_proof
                        if message_limit_proof is not None:
                            proof_log = {
                                "actual_wire_messages": (
                                    message_limit_proof.actual_wire_messages
                                ),
                                "limit": message_limit_proof.limit,
                                "logical_messages": message_limit_proof.logical_messages,
                                "system_messages": message_limit_proof.system_messages,
                                "tool_result_messages": (
                                    message_limit_proof.tool_result_messages
                                ),
                                "provider_kind": message_limit_proof.provider_kind,
                                "model": message_limit_proof.model,
                                "base_host": message_limit_proof.base_host,
                                "iteration": iterations,
                                "attempt": _call_attempt,
                            }
                            _log.warning(
                                "provider_request_message_limit_detected",
                                **proof_log,
                            )
                            self._write_turn_call_log(
                                "provider_request_message_limit_detected",
                                **proof_log,
                            )
                            if _message_limit_recovery_done:
                                _log.warning(
                                    "provider_request_message_limit_recovery_repeated",
                                    **proof_log,
                                )
                                self._write_turn_call_log(
                                    "provider_request_message_limit_recovery_repeated",
                                    **proof_log,
                                )
                                yield self._transition(AgentState.ERROR)
                                terminal_error = ErrorEvent(
                                    message=(
                                        "The provider rejected the request message count "
                                        "again after one safe recovery attempt."
                                    ),
                                    code="provider_request_message_limit_exhausted",
                                )
                                yield terminal_error
                                break

                            _message_limit_recovery_done = True
                            recovery_outcome, recovery_reason = (
                                await self._recover_provider_message_count_limit(
                                    base_request_turn_messages,
                                    request_suffix_messages=request_suffix_messages,
                                    proof=message_limit_proof,
                                    config=call_chat_cfg,
                                    identical_request_perturbed=(
                                        identical_request_action == "perturb"
                                    ),
                                    request_context_message=request_context_message,
                                    request_context_insert_index=(
                                        active_request_context_insert_index
                                    ),
                                    runtime_context_message=runtime_context_message,
                                    runtime_context_insert_index=(
                                        active_runtime_context_insert_index
                                    ),
                                    turn_objective_message=turn_objective_message,
                                    protected_turn_start_index=(
                                        active_protected_turn_start_index
                                    ),
                                )
                            )
                            if recovery_outcome is None:
                                _log.warning(
                                    "provider_request_message_limit_recovery_refused",
                                    reason=recovery_reason,
                                    **proof_log,
                                )
                                self._write_turn_call_log(
                                    "provider_request_message_limit_recovery_refused",
                                    reason=recovery_reason,
                                    **proof_log,
                                )
                                yield self._transition(AgentState.ERROR)
                                terminal_error = ErrorEvent(
                                    message=(
                                        "The provider request exceeds its message-count "
                                        "limit, and older history could not be summarized "
                                        "without changing protected turn or tool state."
                                    ),
                                    code="provider_request_message_limit_exhausted",
                                )
                                yield terminal_error
                                break

                            message_count_request_view = _MessageCountRequestView(
                                messages=recovery_outcome.messages,
                                canonical_tail_start=len(turn_messages),
                                request_context_insert_index=(
                                    recovery_outcome.request_context_insert_index
                                ),
                                runtime_context_insert_index=(
                                    recovery_outcome.runtime_context_insert_index
                                ),
                                protected_turn_start_index=(
                                    recovery_outcome.protected_turn_start_index
                                ),
                            )
                            recovery_log = {
                                **proof_log,
                                "target_wire_messages": (
                                    message_limit_proof.limit
                                    - self._message_count_headroom(
                                        message_limit_proof.limit
                                    )
                                ),
                                "projected_wire_messages": (
                                    recovery_outcome.projected_wire_messages
                                ),
                                "removed_logical_messages": (
                                    recovery_outcome.removed_count
                                ),
                            }
                            _log.info(
                                "provider_request_message_limit_recovery_success",
                                **recovery_log,
                            )
                            self._write_turn_call_log(
                                "provider_request_message_limit_recovery_success",
                                **recovery_log,
                            )
                            yield WarningEvent(
                                code="provider_request_message_limit_recovery_success",
                                message=(
                                    "Older history was summarized for this provider "
                                    "request; retrying once."
                                ),
                            )
                            _call_attempt += 1
                            continue
                        if artifact_delivery_final_response_pending:
                            yield _finish_artifact_delivery_degraded(
                                reason=provider_error.message,
                                code=provider_error.code,
                            )
                            break
                        if max_iterations_finalization_pending:
                            response_text = (
                                "I reached the configured iteration limit, and the "
                                "provider could not generate an additional wrap-up. "
                                "Returning the best partial result from completed work."
                            )
                            assistant_text_parts.append(response_text)
                            provider_done_for_log = ProviderDoneEvent(stop_reason="stop")
                            _got_done_event = True
                            _got_error = False
                            max_iterations_finalization_pending = False
                            self._write_turn_call_log(
                                "turn_policy_decision",
                                action="partial_after_finalization_provider_error",
                                reason="max_iterations",
                                code="max_iterations",
                                provider_error_code=provider_error.code,
                            )
                            yield TextDeltaEvent(text=response_text)
                            break
                        if post_write_convergence_finalization_pending:
                            response_text = (
                                "The workspace diff was stable after clean validation, "
                                "and the provider could not generate an additional wrap-up. "
                                "Returning the current validated patch state."
                            )
                            assistant_text_parts.append(response_text)
                            provider_done_for_log = ProviderDoneEvent(stop_reason="stop")
                            _got_done_event = True
                            _got_error = False
                            post_write_convergence_finalization_pending = False
                            self._write_turn_call_log(
                                "turn_policy_decision",
                                action="partial_after_finalization_provider_error",
                                reason="post_write_convergence",
                                code="post_write_convergence",
                                provider_error_code=provider_error.code,
                            )
                            yield TextDeltaEvent(text=response_text)
                            break
                        if (
                            failure_kind == ProviderFailureKind.EMPTY_RESPONSE
                            and _retry_policy.can_retry_provider_failure(
                                failure_kind,
                                post_tool_turn=post_tool_turn,
                                provider_retry_attempt=_retry_attempt,
                            )
                        ):
                            self._record_tool_loop_runtime_event(
                                reason="provider_empty_response_after_tool",
                                iteration=iterations,
                                provider_call_count=turn_llm_calls,
                                call_attempt=_call_attempt,
                                provider_retry_attempt=_retry_attempt,
                                post_tool_turn=post_tool_turn,
                                provider_error_code=provider_error.code,
                                retrying=True,
                            )
                            delay = backoff_sleep(
                                _retry_attempt,
                                _fallback.base_backoff_ms,
                                _fallback.max_backoff_ms,
                                _fake=True,
                            )
                            _log.warning(
                                "provider.empty_response_retry",
                                attempt=_retry_attempt + 1,
                                delay_s=round(delay, 2),
                                post_tool_turn=True,
                            )
                            yield WarningEvent(
                                code="provider_empty_retry",
                                message=(
                                    "The provider returned an empty response after tool "
                                    "execution; retrying once."
                                ),
                            )
                            await asyncio.sleep(delay)
                            _retry_attempt += 1
                            _call_attempt += 1
                            continue
                        if failure_kind == ProviderFailureKind.CONTEXT_OVERFLOW:
                            self._record_provider_context_overflow_reason(provider_error)
                            provider_compaction_window_tokens = (
                                self._provider_budget_compaction_window_tokens(provider_error)
                            )
                            provider_estimated_tokens = self._provider_budget_estimated_tokens(
                                provider_error
                            )
                            provider_compaction_refusal_reason = (
                                self._last_compaction_refusal_reason
                            )
                            overflow_total_tokens = provider_estimated_tokens
                            if overflow_total_tokens is None:
                                overflow_total_tokens = (
                                    provider_compaction_window_tokens
                                    or self.config.context_window_tokens
                                ) + 1
                            if overflow_retries >= self.config.max_overflow_retries:
                                yield self._transition(AgentState.ERROR)
                                terminal_error = self._context_overflow_error()
                                yield terminal_error
                                break
                            overflow_retries += 1
                            yield WarningEvent(
                                code="context_auto_compaction_start",
                                message=(
                                    "Provider context limit reached; compacting older "
                                    "context before retrying."
                                ),
                            )
                            overflow_outcome = await self._check_context_overflow(
                                turn_messages,
                                overflow_total_tokens,
                                request_context_insert_index=request_context_insert_index,
                                runtime_context_insert_index=runtime_context_insert_index,
                                protected_turn_start_index=current_turn_start_index,
                                compaction_window_tokens=provider_compaction_window_tokens,
                            )
                            if overflow_outcome is None:
                                yield self._transition(AgentState.ERROR)
                                terminal_error = self._context_overflow_error()
                                yield terminal_error
                                break
                            if (
                                provider_compaction_refusal_reason
                                and self._last_compaction_refusal_reason is None
                            ):
                                self._last_compaction_refusal_reason = (
                                    provider_compaction_refusal_reason
                                )
                            next_request_context_insert_index = (
                                overflow_outcome.request_context_insert_index
                                if overflow_outcome.request_context_insert_index is not None
                                else request_context_insert_index
                            )
                            next_runtime_context_insert_index = (
                                overflow_outcome.runtime_context_insert_index
                                if overflow_outcome.runtime_context_insert_index is not None
                                else runtime_context_insert_index
                            )
                            next_request_messages = await self._provider_request_messages_async(
                                overflow_outcome.messages,
                                request_context_message=request_context_message,
                                request_context_insert_index=next_request_context_insert_index,
                                runtime_context_message=runtime_context_message,
                                runtime_context_insert_index=next_runtime_context_insert_index,
                                turn_objective_message=turn_objective_message,
                            )
                            if not self._provider_request_is_smaller(
                                request_messages,
                                next_request_messages,
                            ):
                                yield self._transition(AgentState.ERROR)
                                if (
                                    self._last_compaction_refusal_reason
                                    != "provider_recent_tail_too_large"
                                ):
                                    self._last_compaction_refusal_reason = "compaction_not_smaller"
                                terminal_error = self._context_overflow_error()
                                yield terminal_error
                                break
                            turn_messages = overflow_outcome.messages
                            request_context_insert_index = next_request_context_insert_index
                            runtime_context_insert_index = next_runtime_context_insert_index
                            if overflow_outcome.protected_turn_start_index is not None:
                                current_turn_start_index = (
                                    overflow_outcome.protected_turn_start_index
                                )
                            message_count_request_view = None
                            yield WarningEvent(
                                code="context_auto_compaction_retry",
                                message="Context compacted; retrying the provider request.",
                            )
                            yield CompactionEvent(
                                compaction_id=overflow_outcome.compaction_id,
                                summary=overflow_outcome.summary,
                                kept_entries=overflow_outcome.kept_entries,
                                kept_count=len(overflow_outcome.messages),
                                removed_count=overflow_outcome.removed_count,
                            )
                            _call_attempt += 1
                            continue
                        should_retry = _fallback.should_retry(kind, _retry_attempt)
                        retry_failed_call_safe = (
                            getattr(
                                self.provider,
                                "retry_failed_call_safe",
                                True,
                            )
                            is not False
                        )
                        if should_retry and not retry_failed_call_safe:
                            _log.warning(
                                "provider.retry_suppressed",
                                reason="composite_provider",
                                kind=kind.value,
                                provider=getattr(self.provider, "provider_name", ""),
                            )
                            should_retry = False
                        if not should_retry:
                            yield self._transition(AgentState.ERROR)
                            terminal_error = ErrorEvent(
                                message=provider_error.message,
                                code=provider_error.code,
                            )
                            yield terminal_error
                            break
                        delay = backoff_sleep(
                            _retry_attempt,
                            _fallback.base_backoff_ms,
                            _fallback.max_backoff_ms,
                            _fake=True,
                        )
                        _log.warning(
                            "provider.retry",
                            attempt=_retry_attempt + 1,
                            kind=kind.value,
                            delay_s=round(delay, 2),
                        )
                        await asyncio.sleep(delay)
                        _retry_attempt += 1
                        _call_attempt += 1

                if terminal_error is not None:
                    break
                if artifact_delivery_degraded_final_response:
                    break

                response_text = "".join(assistant_text_parts)
                final_stop_reason = (
                    getattr(provider_done_for_log, "stop_reason", None)
                    if provider_done_for_log is not None
                    else None
                )
                final_classification = _classify_provider_attempt(
                    text=response_text,
                    tool_calls=tool_calls,
                    pending_tools=pending_tools,
                    got_done_event=_got_done_event,
                    stop_reason=final_stop_reason,
                    reasoning_content=iter_reasoning_content,
                    reasoning_tokens=iter_reasoning_tokens,
                    user_visible_emitted=attempt_user_visible_emitted,
                )
                if final_classification.kind != _ProviderAttemptKind.OK:
                    if text_only_tool_recovery_pending:
                        text_only_mode = getattr(
                            self.config,
                            "text_only_tool_recovery_mode",
                            "off",
                        )
                        self.config.metadata[
                            "text_only_tool_recovery_next_action_errors"
                        ] = (
                            self.config.metadata.get(
                                "text_only_tool_recovery_next_action_errors",
                                0,
                            )
                            + 1
                        )
                        decision = RuntimeRecoveryDecision(
                            action="observe",
                            mechanism="text_only_tool_recovery",
                            reason="next_action_after_recovery",
                            mode=str(text_only_mode),
                            injected_to_model=False,
                            details={
                                "next_action": "error",
                                "provider_attempt_kind": final_classification.kind.value,
                            },
                        )
                        self._record_runtime_recovery_event(
                            decision,
                            iteration=iterations,
                            provider_call_count=turn_llm_calls,
                        )
                        self._write_turn_call_log(
                            "runtime_recovery",
                            action="observe",
                            mode=text_only_mode,
                            reason="text_only_next_action",
                            details=decision.details,
                        )
                        text_only_tool_recovery_pending = False
                    logger.warning(
                        "provider.invalid_response_unhandled",
                        session_key=self._session_key,
                        model=last_actual_model or self.config.model_id or "",
                        provider=type(self.provider).__name__,
                        classification=final_classification.kind.value,
                        iteration=iterations,
                        call_attempt=_call_attempt,
                        got_done_event=_got_done_event,
                        stop_reason=final_stop_reason,
                        iter_input_tokens=iter_input_tokens,
                        iter_output_tokens=iter_output_tokens,
                        iter_reasoning_tokens=iter_reasoning_tokens,
                        reasoning_chars=len(iter_reasoning_content or ""),
                    )
                    yield self._transition(AgentState.ERROR)
                    if final_classification.kind == _ProviderAttemptKind.INCOMPLETE_TOOLS:
                        terminal_error = ErrorEvent(
                            message="Provider stream ended with an incomplete tool call",
                            code="incomplete_tool_stream",
                        )
                        yield terminal_error
                        break
                    if final_classification.kind == _ProviderAttemptKind.STREAM_INCOMPLETE:
                        terminal_error = ErrorEvent(
                            message="Provider stream ended before a done event",
                            code="provider_stream_incomplete",
                        )
                        yield terminal_error
                        break
                    if final_classification.kind == _ProviderAttemptKind.LENGTH_CAPPED:
                        terminal_error = ErrorEvent(
                            message=_PROVIDER_OUTPUT_TRUNCATED_REPLY,
                            code="provider_output_truncated",
                        )
                        yield terminal_error
                        break
                    terminal_error = ErrorEvent(
                        message="Provider returned an empty response",
                        code="empty_response",
                    )
                    yield terminal_error
                    break

                if iter_reasoning_content:
                    final_reasoning_parts.append(iter_reasoning_content)

                # Check overflow against the live provider request, not
                # cumulative billable usage for the whole turn.
                estimated_context_tokens = self._estimate_live_request_tokens(
                    request_messages,
                    tools=provider_tools_for_call,
                    config=call_chat_cfg,
                )
                overflow_outcome = await self._check_context_overflow(
                    turn_messages,
                    estimated_context_tokens,
                    request_context_insert_index=request_context_insert_index,
                    runtime_context_insert_index=runtime_context_insert_index,
                    protected_turn_start_index=current_turn_start_index,
                )
                if overflow_outcome is None:
                    if overflow_retries >= self.config.max_overflow_retries:
                        yield self._transition(AgentState.ERROR)
                        terminal_error = self._context_overflow_error()
                        yield terminal_error
                        break
                    overflow_retries += 1
                    _log.warning(
                        "compaction.retry",
                        attempt=overflow_retries,
                        max=self.config.max_overflow_retries,
                    )
                    continue  # retry the tool loop iteration
                if overflow_outcome.compacted:
                    # Compaction happened — replace message list. Lifetime
                    # counters keep feeding DoneEvent usage/cost accounting for
                    # this turn.
                    turn_messages = overflow_outcome.messages
                    if overflow_outcome.request_context_insert_index is not None:
                        request_context_insert_index = overflow_outcome.request_context_insert_index
                    if overflow_outcome.runtime_context_insert_index is not None:
                        runtime_context_insert_index = overflow_outcome.runtime_context_insert_index
                    if overflow_outcome.protected_turn_start_index is not None:
                        current_turn_start_index = overflow_outcome.protected_turn_start_index
                    message_count_request_view = None
                    yield CompactionEvent(
                        compaction_id=overflow_outcome.compaction_id,
                        summary=overflow_outcome.summary,
                        kept_entries=overflow_outcome.kept_entries,
                        kept_count=len(overflow_outcome.messages),
                        removed_count=overflow_outcome.removed_count,
                    )
                    overflow_retries = 0  # reset on success
                    # Rebuild chat_cfg so next LLM call uses refreshed system
                    # prompt. Read cache_breakpoints from the
                    # refreshed self.config (re-anchored by
                    # refresh_system_prompt) — chat_cfg.cache_breakpoints
                    # would still hold pre-compaction base text and miss the
                    # cache on the next provider call.
                    chat_cfg = ChatConfig(
                        max_tokens=chat_cfg.max_tokens,
                        temperature=chat_cfg.temperature,
                        top_p=chat_cfg.top_p,
                        system=self._context.system_prompt,
                        thinking=thinking_enabled,
                        thinking_budget_tokens=thinking_budget,
                        thinking_budget_explicit=chat_cfg.thinking_budget_explicit,
                        timeout=chat_cfg.timeout,
                        stop_sequences=chat_cfg.stop_sequences,
                        cache_breakpoints=self._cache_breakpoints_without_runtime_context(
                            self.config.cache_breakpoints
                        ),
                        cache_mode=chat_cfg.cache_mode,
                        output_json_schema=chat_cfg.output_json_schema,
                        output_json_schema_strict=chat_cfg.output_json_schema_strict,
                        model_capabilities=self.config.model_capabilities,
                        thinking_level=(
                            self.config.thinking
                            if isinstance(self.config.thinking, ThinkingLevel)
                            else None
                        ),
                        provider_request_max_chars=(self._provider_request_proof_max_chars()),
                        tool_choice=chat_cfg.tool_choice,
                    )

                assembled_text = "".join(assistant_text_parts)
                visible_text = assembled_text
                if text_only_tool_recovery_pending:
                    text_only_mode = getattr(
                        self.config,
                        "text_only_tool_recovery_mode",
                        "off",
                    )
                    next_action = (
                        "tool_call"
                        if tool_calls
                        else "text"
                        if visible_text.strip()
                        else "empty"
                    )
                    metadata_key: str | None
                    metadata_key = f"text_only_tool_recovery_next_action_{next_action}s"
                    self.config.metadata[metadata_key] = (
                        self.config.metadata.get(metadata_key, 0) + 1
                    )
                    decision = RuntimeRecoveryDecision(
                        action="observe",
                        mechanism="text_only_tool_recovery",
                        reason="next_action_after_recovery",
                        mode=str(text_only_mode),
                        injected_to_model=False,
                        details={
                            "next_action": next_action,
                            "tool_call_count": len(tool_calls),
                            "visible_text_chars": len(visible_text),
                        },
                    )
                    self._record_runtime_recovery_event(
                        decision,
                        iteration=iterations,
                        provider_call_count=turn_llm_calls,
                    )
                    self._write_turn_call_log(
                        "runtime_recovery",
                        action="observe",
                        mode=text_only_mode,
                        reason="text_only_next_action",
                        details=decision.details,
                    )
                    text_only_tool_recovery_pending = False
                if visible_text:
                    final_text_parts.append(visible_text)

                preflight_tool_results: dict[str, ToolResult] = {}
                terminal_projection_preflight_error = False
                resolved_tool_calls: list[ToolCall] = []
                for tc in tool_calls:
                    resolved = self._rehydrate_projected_tool_arguments(tc)
                    if isinstance(resolved, ToolResult):
                        preflight_tool_results[tc.tool_use_id] = resolved
                        if self._is_provider_context_projection_reuse_result(resolved):
                            terminal_projection_preflight_error = True
                        resolved_tool_calls.append(self._sanitize_projected_tool_call_arguments(tc))
                        continue
                    resolved_tool_calls.append(resolved)
                tool_calls = resolved_tool_calls

                if runtime_recovery_scaffolding_pending:
                    cleaned_turn_messages = _drop_runtime_recovery_scaffolding(turn_messages)
                    if message_count_request_view is not None:
                        message_count_request_view = (
                            message_count_request_view.rebase_after_canonical_suffix_cleanup(
                                turn_messages,
                                cleaned_turn_messages,
                            )
                        )
                    turn_messages = cleaned_turn_messages
                    runtime_recovery_scaffolding_pending = False

                repeated_tool_call_recovery_message: str | None = None
                repeated_tool_call_recovery_details: dict[str, Any] | None = None
                repeat_threshold = max(
                    0,
                    int(
                        getattr(
                            self.config,
                            "repeated_tool_call_recovery_threshold",
                            3,
                        )
                        or 0
                    ),
                )
                if (
                    len(tool_calls) == 1
                    and repeat_threshold > 0
                    and tool_calls[0].tool_name
                    in self._repeated_tool_call_recovery_tool_names()
                ):
                    current_repeat_key = self._tool_call_repeat_key(tool_calls[0])
                    current_workspace_write_count = len(self._effective_workspace_write_records())
                    if (
                        current_repeat_key == repeated_tool_call_key
                        and current_workspace_write_count
                        == repeated_tool_call_workspace_write_count
                    ):
                        repeated_tool_call_count += 1
                    else:
                        repeated_tool_call_key = current_repeat_key
                        repeated_tool_call_count = 1
                        repeated_tool_call_workspace_write_count = current_workspace_write_count
                        repeated_tool_call_last_result_is_error = False
                    if repeated_tool_call_count >= repeat_threshold:
                        # Repeated failed tools already have a separate recovery path
                        # that returns a ToolResult to the model. This guard is for
                        # successful no-new-information loops that can trigger provider
                        # rejection before the model gets another turn.
                        if not repeated_tool_call_last_result_is_error:
                            repeated_tool_call_recovery_message = (
                                self._repeated_tool_call_recovery_message(
                                    tool_calls[0],
                                    repeat_count=repeated_tool_call_count,
                                )
                            )
                            repeated_tool_call_recovery_details = {
                                "tool_name": tool_calls[0].tool_name,
                                "tool_use_id": tool_calls[0].tool_use_id,
                                "arguments_hash": current_repeat_key[1],
                                "arguments_preview": self._tool_call_arguments_preview(
                                    tool_calls[0]
                                ),
                                "repeat_count": repeated_tool_call_count,
                                "repeat_threshold": repeat_threshold,
                                "workspace_write_count": current_workspace_write_count,
                            }
                elif tool_calls:
                    repeated_tool_call_key = None
                    repeated_tool_call_count = 0
                    repeated_tool_call_workspace_write_count = len(
                        self._effective_workspace_write_records()
                    )
                    repeated_tool_call_last_result_is_error = False

                if repeated_tool_call_recovery_message is not None:
                    assistant_content: list[Any] = []
                    if iter_reasoning_content and iter_thinking_signature:
                        assistant_content.append(
                            ContentBlockThinking(
                                thinking=iter_reasoning_content,
                                signature=iter_thinking_signature,
                            )
                        )
                    if visible_text:
                        assistant_content.append(ContentBlockText(text=visible_text))
                    if assistant_content:
                        turn_messages.append(
                            Message(
                                role="assistant",
                                content=assistant_content,
                                reasoning_content=iter_reasoning_content,
                            )
                        )
                    turn_messages.append(
                        Message(role="user", content=repeated_tool_call_recovery_message)
                    )
                    runtime_recovery_scaffolding_pending = True
                    self.config.metadata["repeated_tool_call_recoveries"] = (
                        self.config.metadata.get("repeated_tool_call_recoveries", 0) + 1
                    )
                    recovery_decision = RuntimeRecoveryDecision(
                        action="nudge",
                        mechanism="repeated_tool_call_recovery",
                        reason="repeated_identical_tool_call",
                        mode="warn_model",
                        injected_to_model=True,
                        message=repeated_tool_call_recovery_message,
                        details=repeated_tool_call_recovery_details or {},
                    )
                    self._record_runtime_recovery_event(
                        recovery_decision,
                        iteration=iterations,
                        provider_call_count=turn_llm_calls,
                    )
                    self._write_turn_call_log(
                        "runtime_recovery",
                        action="nudge",
                        mode="warn_model",
                        reason="repeated_identical_tool_call",
                        details=repeated_tool_call_recovery_details or {},
                    )
                    yield WarningEvent(
                        code="repeated_tool_call_recovery",
                        message=(
                            "Runtime skipped a repeated identical tool call and "
                            "asked the model to change approach."
                        ),
                    )
                    continue

                # Build assistant message for history
                assistant_content = []
                if iter_thinking_signature:
                    assistant_content.append(
                        ContentBlockThinking(
                            thinking=iter_reasoning_content or "",
                            signature=iter_thinking_signature,
                        )
                    )
                if visible_text:
                    assistant_content.append(ContentBlockText(text=visible_text))
                for tc in tool_calls:
                    assistant_content.append(
                        ContentBlockToolUse(
                            id=tc.tool_use_id,
                            name=tc.tool_name,
                            input=tc.arguments,
                        )
                    )
                if assistant_content:
                    turn_messages.append(
                        Message(
                            role="assistant",
                            content=assistant_content,
                            reasoning_content=iter_reasoning_content,
                        )
                    )

                # Detect incomplete tool calls (stream interrupted mid-generation)
                if pending_tools and not tool_calls:
                    _log.warning(
                        "agent.stream_interrupted",
                        session_key=self._session_key,
                        pending_tool_ids=list(pending_tools.keys()),
                        pending_tool_names=[acc.tool_name for acc in pending_tools.values()],
                        got_done_event=_got_done_event,
                        text_len=len(assembled_text),
                        iteration=iterations,
                    )
                if not _got_done_event and (assembled_text or pending_tools):
                    _log.warning(
                        "agent.provider_stream_incomplete",
                        session_key=self._session_key,
                        got_text=bool(assembled_text),
                        pending_tools=len(pending_tools),
                        tool_calls=len(tool_calls),
                    )

                # No tool calls → we're done
                if not tool_calls:
                    text_only_mode = getattr(
                        self.config,
                        "text_only_tool_recovery_mode",
                        "off",
                    )
                    tool_choice_none = (
                        isinstance(call_chat_cfg.tool_choice, str)
                        and call_chat_cfg.tool_choice.strip().lower() == "none"
                    )
                    text_only_candidate = (
                        text_only_mode != "off"
                        and bool(visible_text.strip())
                        and bool(provider_tools_for_call)
                        and not tool_choice_none
                        and not last_executed_results
                        and not max_iterations_finalization_pending
                        and not artifact_delivery_final_response_pending
                        and not post_write_convergence_finalization_pending
                    )
                    if text_only_candidate:
                        self.config.metadata["text_only_tool_recovery_detections"] = (
                            self.config.metadata.get(
                                "text_only_tool_recovery_detections",
                                0,
                            )
                            + 1
                        )
                        should_inject_text_only = (
                            text_only_mode == "warn_model"
                            and text_only_tool_recovery_injections
                            < _TEXT_ONLY_TOOL_RECOVERY_LIMIT
                        )
                        decision = RuntimeRecoveryDecision(
                            action="nudge" if should_inject_text_only else "observe",
                            mechanism="text_only_tool_recovery",
                            reason="text_only_no_tool_call",
                            mode=str(text_only_mode),
                            injected_to_model=should_inject_text_only,
                            message=(
                                _TEXT_ONLY_TOOL_RECOVERY_MESSAGE
                                if should_inject_text_only
                                else None
                            ),
                            details={
                                "visible_text_chars": len(visible_text),
                                "available_tool_count": len(provider_tools_for_call or []),
                                "recovery_injections": text_only_tool_recovery_injections,
                                "limit": _TEXT_ONLY_TOOL_RECOVERY_LIMIT,
                            },
                        )
                        self._record_runtime_recovery_event(
                            decision,
                            iteration=iterations,
                            provider_call_count=turn_llm_calls,
                        )
                        self._write_turn_call_log(
                            "runtime_recovery",
                            action=decision.action,
                            mode=text_only_mode,
                            reason=decision.reason,
                            details=decision.details,
                        )
                        if should_inject_text_only:
                            if visible_text and final_text_parts:
                                final_text_parts.pop()
                            turn_messages.append(
                                Message(role="user", content=_TEXT_ONLY_TOOL_RECOVERY_MESSAGE)
                            )
                            runtime_recovery_scaffolding_pending = True
                            text_only_tool_recovery_pending = True
                            text_only_tool_recovery_injections += 1
                            self.config.metadata["text_only_tool_recovery_injections"] = (
                                self.config.metadata.get(
                                    "text_only_tool_recovery_injections",
                                    0,
                                )
                                + 1
                            )
                            yield WarningEvent(
                                code="text_only_tool_recovery",
                                message=(
                                    "The model returned text without a tool call; "
                                    "asking it to call tools if the task is not complete."
                                ),
                            )
                            continue
                    if (
                        progress_watchdog_mode == "warn_model"
                        and not max_iterations_finalization_pending
                        and not artifact_delivery_final_response_pending
                    ):
                        failed_tool_finalization = (
                            await self._failed_tool_finalization_recovery_details(
                                last_executed_results,
                                post_write_verification_failure=(
                                    last_post_write_failed_verification
                                ),
                                post_write_verification_success_observed=(
                                    post_write_focused_verification_success_observed
                                ),
                                final_text=visible_text,
                            )
                        )
                        if failed_tool_finalization is not None:
                            recovery_key = self._failed_tool_finalization_recovery_key(
                                failed_tool_finalization
                            )
                            if (
                                recovery_key in failed_tool_finalization_recovery_keys
                                or len(failed_tool_finalization_recovery_keys)
                                >= _FAILED_FINALIZATION_RECOVERY_LIMIT
                            ):
                                failed_tool_finalization = None
                            else:
                                failed_tool_finalization_recovery_keys.add(recovery_key)
                                failed_tool_finalization["recovery_key"] = recovery_key
                        if failed_tool_finalization is not None:
                            recovery_message: str | None
                            recovery_message = (
                                self._failed_tool_finalization_recovery_message(
                                    failed_tool_finalization
                                )
                            )
                            self._record_tool_loop_runtime_event(
                                reason=str(failed_tool_finalization["reason"]),
                                iteration=iterations,
                                provider_call_count=turn_llm_calls,
                                workspace_write_count=len(
                                    self._effective_workspace_write_records()
                                ),
                                injected_to_model=True,
                                hint_text_sha256=hashlib.sha256(
                                    recovery_message.encode("utf-8")
                                ).hexdigest(),
                                details=failed_tool_finalization,
                            )
                            if visible_text and final_text_parts:
                                final_text_parts.pop()
                            turn_messages.append(Message(role="user", content=recovery_message))
                            self.config.metadata["failed_tool_finalization_recoveries"] = (
                                self.config.metadata.get(
                                    "failed_tool_finalization_recoveries",
                                    0,
                                )
                                + 1
                            )
                            self._write_turn_call_log(
                                "progress_watchdog",
                                action="warn",
                                mode=progress_watchdog_mode,
                                reason=str(failed_tool_finalization["reason"]),
                                details=failed_tool_finalization,
                            )
                            yield WarningEvent(
                                code="failed_tool_finalization_recovery",
                                message=(
                                    "The model attempted to finish after a failed "
                                    "tool result with a workspace diff; asking it "
                                    "to fix or re-validate once."
                                ),
                            )
                            continue
                    submit_review_red_detected = False
                    if (
                        finalize_evidence_tracker is not None
                        and not max_iterations_finalization_pending
                        and not artifact_delivery_final_response_pending
                        and not post_write_convergence_finalization_pending
                    ):
                        gate_status = await self._workspace_git_status_porcelain()
                        gate_observation = finalize_evidence_tracker.build_observation(
                            has_workspace_diff=bool(gate_status and gate_status.strip()),
                        )
                        if gate_observation.should_challenge:
                            submit_review_red_detected = True
                            gate_key = finalize_evidence_gate_key(gate_observation)
                            # Never spend the run's last LLM call or deadline
                            # slack on a challenge: with no headroom for a
                            # follow-up call the injection would discard the
                            # model's final answer and end the turn in a hard
                            # budget/timeout error instead of a submission.
                            gate_headroom = _turn_llm_call_budget_error(
                                turn_llm_calls + 1
                            ) is None and (
                                _total_deadline is None or _loop.time() < _total_deadline
                            )
                            gate_suppressed = (
                                gate_key in finalize_evidence_gate_keys
                                or len(finalize_evidence_gate_keys)
                                >= FINALIZE_EVIDENCE_GATE_CHALLENGE_LIMIT
                                or not gate_headroom
                            )
                            self.config.metadata["finalize_evidence_gate_detections"] = (
                                self.config.metadata.get(
                                    "finalize_evidence_gate_detections",
                                    0,
                                )
                                + 1
                            )
                            gate_message = (
                                None
                                if gate_suppressed
                                else finalize_evidence_challenge_message(gate_observation)
                            )
                            self._record_runtime_event(
                                "finalize_evidence_gate.challenge",
                                feature="finalize_evidence_gate",
                                reason=gate_observation.primary_reason,
                                iteration=iterations,
                                provider_call_count=turn_llm_calls,
                                injected_to_model=bool(gate_message),
                                recovery_key=gate_key,
                                details=gate_observation.to_event_details(),
                            )
                            if gate_message is not None:
                                finalize_evidence_gate_keys.add(gate_key)
                                if visible_text and final_text_parts:
                                    final_text_parts.pop()
                                turn_messages.append(
                                    Message(role="user", content=gate_message)
                                )
                                self.config.metadata[
                                    "finalize_evidence_gate_recoveries"
                                ] = (
                                    self.config.metadata.get(
                                        "finalize_evidence_gate_recoveries",
                                        0,
                                    )
                                    + 1
                                )
                                self._write_turn_call_log(
                                    "finalize_evidence_gate",
                                    action="warn",
                                    mode="on",
                                    reason=gate_observation.primary_reason,
                                    details=gate_observation.to_event_details(),
                                )
                                yield WarningEvent(
                                    code="finalize_evidence_gate_recovery",
                                    message=(
                                        "The model attempted to finish with "
                                        "unresolved red execution evidence; asking "
                                        "it to re-verify once."
                                    ),
                                )
                                continue
                    if (
                        patch_hygiene_block_mode in ("test_paths", "protected_paths")
                        and not max_iterations_finalization_pending
                        and not artifact_delivery_final_response_pending
                        and not post_write_convergence_finalization_pending
                    ):
                        hygiene_status = await self._workspace_git_status_porcelain()
                        if patch_hygiene_block_mode == "protected_paths":
                            hygiene_offending_paths = (
                                self._porcelain_status_protected_paths(hygiene_status)
                            )
                            hygiene_reason = "protected_paths_in_final_diff"
                        else:
                            hygiene_offending_paths = self._porcelain_status_test_paths(
                                hygiene_status
                            )
                            hygiene_reason = "test_paths_in_final_diff"
                        if hygiene_offending_paths:
                            hygiene_key = _patch_hygiene_block_key(
                                hygiene_offending_paths
                            )
                            # Same headroom rule as the evidence gate: never
                            # spend the run's last LLM call or deadline slack
                            # on a challenge.
                            hygiene_headroom = _turn_llm_call_budget_error(
                                turn_llm_calls + 1
                            ) is None and (
                                _total_deadline is None or _loop.time() < _total_deadline
                            )
                            hygiene_suppressed = (
                                hygiene_key in patch_hygiene_block_keys
                                or len(patch_hygiene_block_keys)
                                >= _PATCH_HYGIENE_BLOCK_CHALLENGE_LIMIT
                                or not hygiene_headroom
                            )
                            self.config.metadata["patch_hygiene_block_detections"] = (
                                self.config.metadata.get(
                                    "patch_hygiene_block_detections",
                                    0,
                                )
                                + 1
                            )
                            if hygiene_suppressed:
                                hygiene_message = None
                            elif patch_hygiene_block_mode == "protected_paths":
                                hygiene_message = (
                                    _patch_hygiene_block_protected_message(
                                        hygiene_offending_paths
                                    )
                                )
                            else:
                                hygiene_message = _patch_hygiene_block_message(
                                    hygiene_offending_paths
                                )
                            self._record_runtime_event(
                                "patch_hygiene_block.challenge",
                                feature="patch_hygiene_block",
                                reason=hygiene_reason,
                                iteration=iterations,
                                provider_call_count=turn_llm_calls,
                                injected_to_model=bool(hygiene_message),
                                recovery_key=hygiene_key,
                                details={
                                    "offending_paths": hygiene_offending_paths[:20],
                                    "offending_path_count": len(
                                        hygiene_offending_paths
                                    ),
                                },
                            )
                            if hygiene_message is not None:
                                patch_hygiene_block_keys.add(hygiene_key)
                                if visible_text and final_text_parts:
                                    final_text_parts.pop()
                                turn_messages.append(
                                    Message(role="user", content=hygiene_message)
                                )
                                self.config.metadata[
                                    "patch_hygiene_block_recoveries"
                                ] = (
                                    self.config.metadata.get(
                                        "patch_hygiene_block_recoveries",
                                        0,
                                    )
                                    + 1
                                )
                                self._write_turn_call_log(
                                    "patch_hygiene_block",
                                    action="warn",
                                    mode=patch_hygiene_block_mode,
                                    reason=hygiene_reason,
                                    details={
                                        "offending_paths": hygiene_offending_paths[
                                            :20
                                        ],
                                        "offending_path_count": len(
                                            hygiene_offending_paths
                                        ),
                                    },
                                )
                                if patch_hygiene_block_mode == "protected_paths":
                                    hygiene_warning = (
                                        "The model attempted to finish with "
                                        "write-policy-protected files still "
                                        "changed in the workspace diff; asking "
                                        "it to revert them once."
                                    )
                                else:
                                    hygiene_warning = (
                                        "The model attempted to finish with test "
                                        "files still changed in the workspace "
                                        "diff; asking it to revert them once."
                                    )
                                yield WarningEvent(
                                    code="patch_hygiene_block_recovery",
                                    message=hygiene_warning,
                                )
                                continue
                    if (
                        finalize_variant_challenge_enabled
                        and not finalize_variant_challenge_fired
                        and not max_iterations_finalization_pending
                        and not artifact_delivery_final_response_pending
                        and not post_write_convergence_finalization_pending
                    ):
                        variant_status = await self._workspace_git_status_porcelain()
                        if variant_status and variant_status.strip():
                            # Same headroom rule as the evidence gate: never
                            # spend the run's last LLM call or deadline slack
                            # on a challenge.
                            variant_headroom = _turn_llm_call_budget_error(
                                turn_llm_calls + 1
                            ) is None and (
                                _total_deadline is None or _loop.time() < _total_deadline
                            )
                            variant_message = (
                                _finalize_variant_challenge_message()
                                if variant_headroom
                                else None
                            )
                            self.config.metadata[
                                "finalize_variant_challenge_detections"
                            ] = (
                                self.config.metadata.get(
                                    "finalize_variant_challenge_detections",
                                    0,
                                )
                                + 1
                            )
                            self._record_runtime_event(
                                "finalize_variant_challenge.challenge",
                                feature="finalize_variant_challenge",
                                reason="finalize_with_workspace_diff",
                                iteration=iterations,
                                provider_call_count=turn_llm_calls,
                                injected_to_model=bool(variant_message),
                            )
                            if variant_message is not None:
                                # One challenge per turn, fired or not again:
                                # the sweep is uniform and non-escalating, so
                                # a second injection would only burn budget.
                                finalize_variant_challenge_fired = True
                                if visible_text and final_text_parts:
                                    final_text_parts.pop()
                                turn_messages.append(
                                    Message(role="user", content=variant_message)
                                )
                                self.config.metadata[
                                    "finalize_variant_challenge_recoveries"
                                ] = (
                                    self.config.metadata.get(
                                        "finalize_variant_challenge_recoveries",
                                        0,
                                    )
                                    + 1
                                )
                                self._write_turn_call_log(
                                    "finalize_variant_challenge",
                                    action="warn",
                                    mode="on",
                                    reason="finalize_with_workspace_diff",
                                    details={
                                        "iteration": iterations,
                                        "provider_call_count": turn_llm_calls,
                                    },
                                )
                                yield WarningEvent(
                                    code="finalize_variant_challenge_recovery",
                                    message=(
                                        "The model attempted to finish; asking it "
                                        "once to sweep the input classes reachable "
                                        "by its change."
                                    ),
                                )
                                continue
                    if (
                        progress_watchdog_mode == "warn_model"
                        and not workspace_diff_recovery_attempted
                        and not max_iterations_finalization_pending
                        and not artifact_delivery_final_response_pending
                    ):
                        empty_diff_reason = await self._empty_diff_finalization_reason(visible_text)
                        if empty_diff_reason is not None:
                            recovery_message = self._empty_diff_recovery_message(empty_diff_reason)
                            self._record_tool_loop_runtime_event(
                                reason=empty_diff_reason,
                                iteration=iterations,
                                provider_call_count=turn_llm_calls,
                                workspace_write_count=len(
                                    self._effective_workspace_write_records()
                                ),
                                injected_to_model=True,
                                hint_text_sha256=hashlib.sha256(
                                    recovery_message.encode("utf-8")
                                ).hexdigest(),
                            )
                            workspace_diff_recovery_attempted = True
                            if visible_text and final_text_parts:
                                final_text_parts.pop()
                            turn_messages.append(
                                Message(
                                    role="user",
                                    content=recovery_message,
                                )
                            )
                            self.config.metadata["workspace_diff_recoveries"] = (
                                self.config.metadata.get("workspace_diff_recoveries", 0) + 1
                            )
                            self._write_turn_call_log(
                                "progress_watchdog",
                                action="warn",
                                mode=progress_watchdog_mode,
                                reason=empty_diff_reason,
                                details={
                                    "iteration": iterations,
                                    "provider_call_count": turn_llm_calls,
                                    "workspace_write_count": len(
                                        self._effective_workspace_write_records()
                                    ),
                                },
                            )
                            yield WarningEvent(
                                code="workspace_diff_recovery",
                                message=(
                                    "The model attempted to finish without a clear "
                                    "workspace diff; asking it to reassess once."
                                ),
                            )
                            continue
                    if (
                        submit_review_enabled
                        and submit_review_state.stage == 0
                        and not submit_review_red_detected
                        and not max_iterations_finalization_pending
                        and not artifact_delivery_final_response_pending
                        and not post_write_convergence_finalization_pending
                    ):
                        submit_implicit_headroom_ok = _turn_llm_call_budget_error(
                            turn_llm_calls + 1
                        ) is None and (
                            _total_deadline is None or _loop.time() < _total_deadline
                        )
                        (
                            implicit_file_index,
                            implicit_diff_text,
                        ) = await self._workspace_submit_review_capture()
                        implicit_diff_empty = not (
                            implicit_file_index.strip() or implicit_diff_text.strip()
                        )
                        if submit_review_should_fire_implicit(
                            submit_review_state,
                            enabled=submit_review_enabled,
                            diff_empty=implicit_diff_empty,
                            headroom_ok=submit_implicit_headroom_ok,
                            other_gate_injected=False,
                            red_detected=submit_review_red_detected,
                            pending_flags_clear=True,
                        ):
                            submit_review_message = build_submit_review_message(
                                implicit_file_index,
                                implicit_diff_text,
                                implicit=True,
                                max_chars=submit_review_diff_max_chars,
                            )
                            submit_review_state.mark_reviewed("implicit")
                            if visible_text and final_text_parts:
                                final_text_parts.pop()
                            turn_messages.append(
                                Message(role="user", content=submit_review_message)
                            )
                            self.config.metadata["submit_review_implicit_recoveries"] = (
                                self.config.metadata.get(
                                    "submit_review_implicit_recoveries",
                                    0,
                                )
                                + 1
                            )
                            self._record_runtime_event(
                                "submit_review.implicit",
                                feature="submit_review",
                                reason="finalize_on_green_diff",
                                iteration=iterations,
                                provider_call_count=turn_llm_calls,
                                injected_to_model=True,
                                details={
                                    "diff_truncated": submit_review_diff_is_truncated(
                                        implicit_diff_text,
                                        submit_review_diff_max_chars,
                                    ),
                                },
                            )
                            self._write_turn_call_log(
                                "submit_review",
                                action="warn",
                                mode="implicit",
                                reason="finalize_on_green_diff",
                            )
                            yield WarningEvent(
                                code="submit_review_implicit",
                                message=(
                                    "The model finished with unreviewed workspace "
                                    "changes; showing it a review of its own diff "
                                    "once before finalizing."
                                ),
                            )
                            continue
                    final_diff_contract_mode = getattr(
                        self.config,
                        "final_diff_contract_mode",
                        "log",
                    )
                    if (
                        final_diff_contract_mode != "off"
                        and not max_iterations_finalization_pending
                        and not artifact_delivery_final_response_pending
                    ):
                        final_diff_observation = self._final_diff_contract_observation()
                        if final_diff_observation is not None and (
                            final_diff_observation.diff_paths or final_diff_observation.suspicious
                        ):
                            should_warn_model = (
                                final_diff_contract_mode == "warn_model"
                                and final_diff_observation.suspicious
                                and not final_diff_contract_recovery_attempted
                            )
                            recovery_message = (
                                final_diff_contract_recovery_message(final_diff_observation)
                                if should_warn_model
                                else None
                            )
                            self._record_final_diff_contract_event(
                                final_diff_observation,
                                iteration=iterations,
                                provider_call_count=turn_llm_calls,
                                mode=str(final_diff_contract_mode),
                                injected_to_model=bool(recovery_message),
                                hint_text=recovery_message,
                            )
                            if recovery_message:
                                final_diff_contract_recovery_attempted = True
                                if visible_text and final_text_parts:
                                    final_text_parts.pop()
                                turn_messages.append(Message(role="user", content=recovery_message))
                                self.config.metadata["final_diff_contract_recoveries"] = (
                                    self.config.metadata.get(
                                        "final_diff_contract_recoveries",
                                        0,
                                    )
                                    + 1
                                )
                                self._write_turn_call_log(
                                    "final_diff_contract",
                                    action="warn",
                                    mode=final_diff_contract_mode,
                                    reason=final_diff_observation.primary_reason,
                                    details=final_diff_observation.to_event_details(),
                                )
                                yield WarningEvent(
                                    code="final_diff_contract_recovery",
                                    message=(
                                        "Runtime detected a suspicious final diff; "
                                        "asking the model to reconcile it once."
                                    ),
                                )
                                continue
                    max_iterations_finalization_pending = False
                    post_write_convergence_finalization_pending = False
                    break
                tool_calls = [self._coerce_meta_tool_call(tc) for tc in tool_calls]
                tool_calls = self._force_matched_meta_invoke_tool_calls(tool_calls)

                tool_deadline = _loop.time() + self.config.iteration_timeout
                _arm_endgame_git_freeze_if_due()

                # ------ STREAMING → TOOL_CALLING ------
                yield self._transition(AgentState.TOOL_CALLING)

                # Execute tools and collect results. Concurrent/keyed tools run
                # in bounded batches; mutex tools run serially. Results are
                # emitted in the original tool_calls arrival order regardless
                # of completion order.
                from opensquilla.engine.runtime import (  # noqa: PLC0415
                    _get_tool_concurrency_policy,
                )

                tool_result_blocks: list[ContentBlockToolResult] = []
                executed_results: list[ToolResult] = []
                turn_yielded = False

                # Map tool_use_id -> ToolResult built up below.
                results_by_id: dict[str, ToolResult] = {}
                executed_tool_calls_by_id: dict[str, ToolCall] = {}
                path_patch_snapshots_by_id: dict[str, ToolCall] = {}

                def _cap_timeout_by_deadlines(timeout: float) -> float:
                    remaining = min(timeout, max(0.0, tool_deadline - _loop.time()))
                    if _total_deadline is not None:
                        remaining = min(remaining, max(0.0, _total_deadline - _loop.time()))
                    return max(0.001, remaining)

                async def _run_one(tc: ToolCall) -> ToolResult:
                    nonlocal workspace_edit_gate_details
                    nonlocal workspace_edit_gate_recovery_read_paths
                    nonlocal workspace_edit_gate_recovery_reads_remaining
                    started = time.monotonic()
                    self._write_turn_call_log(
                        "tool_request",
                        iteration=iterations,
                        tool_use_id=tc.tool_use_id,
                        name=tc.tool_name,
                        arguments=tc.arguments,
                    )
                    execution_tc = path_patch_snapshots_by_id.get(tc.tool_use_id)
                    if execution_tc is None:
                        execution_tc = self._snapshot_apply_patch_path_call(tc)
                        if execution_tc is not tc:
                            path_patch_snapshots_by_id[tc.tool_use_id] = execution_tc
                    approval_id = self._tool_call_string_arg(tc, "approval_id")
                    if approval_id is not None and execution_tc is not tc:
                        execution_arguments = dict(execution_tc.arguments)
                        execution_arguments["approval_id"] = approval_id
                        execution_tc = replace(
                            execution_tc,
                            arguments=execution_arguments,
                        )
                    executed_tool_calls_by_id[tc.tool_use_id] = execution_tc
                    tool_timeout = _cap_timeout_by_deadlines(
                        self._tool_execution_timeout(execution_tc)
                    )
                    snapshot_failure: ToolResult | None = None
                    if (
                        tc.tool_name == "apply_patch"
                        and self._tool_call_string_arg(tc, "path") is not None
                        and not (
                            self._tool_call_string_arg(tc, "patch") or ""
                        ).strip()
                        and execution_tc is tc
                    ):
                        snapshot_failure = ToolResult(
                            tool_use_id=tc.tool_use_id,
                            tool_name=tc.tool_name,
                            content=(
                                "apply_patch could not securely snapshot the patch file "
                                "before execution. Retry with inline patch text or a "
                                "readable UTF-8 patch file under the workspace or "
                                "configured scratch directory."
                            ),
                            is_error=True,
                            execution_status=runtime_execution_status(
                                "error",
                                reason="patch_snapshot_failed",
                            ),
                        )
                    preflight_result = (
                        preflight_tool_results.get(tc.tool_use_id) or snapshot_failure
                    )
                    gate_recovery_read = self._workspace_edit_gate_allows_recovery_read(
                        execution_tc,
                        workspace_edit_gate_recovery_read_paths,
                    )
                    gate_result = self._workspace_edit_gate_tool_result(
                        execution_tc,
                        workspace_edit_gate_details,
                        recovery_read_paths=workspace_edit_gate_recovery_read_paths,
                        recovery_reads_remaining=(
                            workspace_edit_gate_recovery_reads_remaining
                        ),
                    )
                    diagnostic_retrieval_gate_result = (
                        self._projected_diagnostic_retrieval_gate_tool_result(execution_tc)
                    )
                    if gate_result is not None:
                        self._record_tool_loop_runtime_event(
                            reason="workspace_edit_gate_blocked_tool_call",
                            iteration=iterations,
                            provider_call_count=turn_llm_calls,
                            tool_name=tc.tool_name,
                            gate_details=dict(workspace_edit_gate_details or {}),
                            workspace_write_count=len(
                                self._effective_workspace_write_records()
                            ),
                            injected_to_model=True,
                        )
                        res = gate_result
                    elif diagnostic_retrieval_gate_result is not None:
                        self._record_tool_loop_runtime_event(
                            reason="projected_diagnostic_requires_retrieval",
                            iteration=iterations,
                            provider_call_count=turn_llm_calls,
                            tool_name=tc.tool_name,
                            workspace_write_count=len(
                                self._effective_workspace_write_records()
                            ),
                            injected_to_model=True,
                        )
                        res = diagnostic_retrieval_gate_result
                    elif preflight_result is not None:
                        res = preflight_result
                    else:
                        try:
                            res = await asyncio.wait_for(
                                self._execute_tool(execution_tc), timeout=tool_timeout
                            )
                        except TimeoutError:
                            res = ToolResult(
                                tool_use_id=tc.tool_use_id,
                                tool_name=tc.tool_name,
                                content=(f"Tool '{tc.tool_name}' timed out after {tool_timeout}s"),
                                is_error=True,
                                execution_status=runtime_execution_status(
                                    "timeout",
                                    reason="runtime_timeout",
                                    timed_out=True,
                                ),
                            )
                    duration_ms = int((time.monotonic() - started) * 1000)
                    self._record_focused_diagnostic_retrieval(execution_tc, res)
                    if len(self._effective_workspace_write_records()) > 0:
                        workspace_edit_gate_details = None
                        workspace_edit_gate_recovery_read_paths.clear()
                        workspace_edit_gate_recovery_reads_remaining = 0
                    elif (
                        workspace_edit_gate_details is not None
                        and tc.tool_name in {"apply_patch", "edit_file"}
                        and res.is_error
                        and self._workspace_edit_gate_edit_error_allows_read(res)
                    ):
                        target_paths = self._workspace_edit_gate_target_paths(execution_tc)
                        if target_paths:
                            workspace_edit_gate_recovery_read_paths = {
                                str(path) for path in target_paths
                            }
                            workspace_edit_gate_recovery_reads_remaining = min(
                                2,
                                len(workspace_edit_gate_recovery_read_paths),
                            )
                            self.config.metadata["workspace_edit_gate_patch_recoveries"] = (
                                self.config.metadata.get(
                                    "workspace_edit_gate_patch_recoveries",
                                    0,
                                )
                                + 1
                            )
                            self._record_tool_loop_runtime_event(
                                reason="workspace_edit_gate_patch_recovery_enabled",
                                iteration=iterations,
                                provider_call_count=turn_llm_calls,
                                tool_name=tc.tool_name,
                                target_paths=sorted(
                                    workspace_edit_gate_recovery_read_paths
                                ),
                                injected_to_model=False,
                            )
                    elif gate_recovery_read:
                        workspace_edit_gate_recovery_reads_remaining = max(
                            0,
                            workspace_edit_gate_recovery_reads_remaining - 1,
                        )
                        if workspace_edit_gate_recovery_reads_remaining <= 0:
                            workspace_edit_gate_recovery_read_paths.clear()
                    self._record_patch_evidence_tool_result(
                        iteration=iterations,
                        tool_call=execution_tc,
                        result=res,
                        duration_ms=duration_ms,
                    )
                    self._write_turn_call_log(
                        "tool_response",
                        iteration=iterations,
                        tool_use_id=res.tool_use_id,
                        name=res.tool_name,
                        result=res.content,
                        result_chars=len(res.content),
                        is_error=res.is_error,
                        duration_ms=duration_ms,
                    )
                    return res

                async def _collect_tool_tasks(
                    task_to_tool_call: dict[asyncio.Task[ToolResult], ToolCall],
                ) -> AsyncIterator[RunHeartbeatEvent]:
                    pending = set(task_to_tool_call)
                    if not pending:
                        return

                    interval = self._tool_activity_heartbeat_interval()
                    started = time.monotonic()
                    last_event_at = started
                    try:
                        while pending:
                            remaining = max(0.0, tool_deadline - _loop.time())
                            if _total_deadline is not None:
                                remaining = min(
                                    remaining,
                                    max(0.0, _total_deadline - _loop.time()),
                                )
                            if remaining <= 0:
                                for task, tc in list(task_to_tool_call.items()):
                                    if task in pending:
                                        task.cancel()
                                        results_by_id[tc.tool_use_id] = ToolResult(
                                            tool_use_id=tc.tool_use_id,
                                            tool_name=tc.tool_name,
                                            content=(
                                                f"Tool '{tc.tool_name}' timed out after "
                                                f"{self.config.iteration_timeout}s"
                                            ),
                                            is_error=True,
                                            execution_status=runtime_execution_status(
                                                "timeout",
                                                reason="runtime_timeout",
                                                timed_out=True,
                                            ),
                                        )
                                return
                            wait_timeout = remaining if interval <= 0 else min(interval, remaining)
                            done, pending = await asyncio.wait(
                                pending,
                                timeout=max(0.001, wait_timeout),
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if not done:
                                if _loop.time() >= tool_deadline or (
                                    _total_deadline is not None and _loop.time() >= _total_deadline
                                ):
                                    for task, tc in list(task_to_tool_call.items()):
                                        if task in pending:
                                            task.cancel()
                                            results_by_id[tc.tool_use_id] = ToolResult(
                                                tool_use_id=tc.tool_use_id,
                                                tool_name=tc.tool_name,
                                                content=(
                                                    f"Tool '{tc.tool_name}' timed out after "
                                                    f"{self.config.iteration_timeout}s"
                                                ),
                                                is_error=True,
                                                execution_status=runtime_execution_status(
                                                    "timeout",
                                                    reason="runtime_timeout",
                                                    timed_out=True,
                                                ),
                                            )
                                    return
                                now = time.monotonic()
                                yield RunHeartbeatEvent(
                                    phase="tool",
                                    elapsed_ms=int((now - started) * 1000),
                                    idle_ms=int((now - last_event_at) * 1000),
                                    message="Tool still running",
                                )
                                continue

                            last_event_at = time.monotonic()
                            for task in done:
                                tc = task_to_tool_call[task]
                                try:
                                    outcome = task.result()
                                except asyncio.CancelledError:
                                    outcome = ToolResult(
                                        tool_use_id=tc.tool_use_id,
                                        tool_name=tc.tool_name,
                                        content=f"Tool '{tc.tool_name}' was cancelled",
                                        is_error=True,
                                        execution_status=runtime_execution_status(
                                            "cancelled",
                                            reason="cancelled",
                                        ),
                                    )
                                except Exception as exc:  # noqa: BLE001
                                    outcome = ToolResult(
                                        tool_use_id=tc.tool_use_id,
                                        tool_name=tc.tool_name,
                                        content=f"Tool '{tc.tool_name}' raised: {exc}",
                                        is_error=True,
                                        execution_status=runtime_execution_status(
                                            "error",
                                            reason="runtime_error",
                                        ),
                                    )
                                results_by_id[tc.tool_use_id] = outcome
                    finally:
                        for task in pending:
                            if not task.done():
                                task.cancel()
                        for task in pending:
                            with contextlib.suppress(asyncio.CancelledError):
                                await task

                # Dispatch preserving original order: accumulate consecutive
                # concurrent/keyed tools into a batch and flush before each
                # mutex tool, then run the mutex tool serially. This ensures
                # that a parallel tool appearing after a mutex tool cannot start
                # until that mutex tool has completed.
                parallel_batch: list[ToolCall] = []

                async def _flush_parallel_batch(
                    batch: list[ToolCall],
                ) -> AsyncIterator[RunHeartbeatEvent]:
                    if not batch:
                        return
                    semaphore = asyncio.Semaphore(self._max_safe_tool_concurrency())
                    keyed_locks: dict[Any, asyncio.Lock] = {}
                    limiters: dict[Any, asyncio.Semaphore] = {}

                    async def _run_limited(tc: ToolCall) -> ToolResult:
                        policy = _get_tool_concurrency_policy(
                            tc.tool_name,
                            tc.arguments,
                            parent_session_key=self._session_key,
                        )
                        key_lock = (
                            keyed_locks.setdefault(policy.key, asyncio.Lock())
                            if policy.key is not None
                            else None
                        )
                        limiter = None
                        if policy.max_inflight is not None:
                            limit_key = policy.limit_key or tc.tool_name
                            limiter = limiters.setdefault(
                                limit_key,
                                asyncio.Semaphore(max(1, int(policy.max_inflight))),
                            )

                        async def _run_after_policy_locks() -> ToolResult:
                            async with semaphore:
                                return await _run_one(tc)

                        async def _run_after_key_lock() -> ToolResult:
                            if limiter is None:
                                return await _run_after_policy_locks()
                            async with limiter:
                                return await _run_after_policy_locks()

                        if key_lock is None:
                            return await _run_after_key_lock()
                        async with key_lock:
                            return await _run_after_key_lock()

                    task_to_tool_call = {asyncio.create_task(_run_limited(tc)): tc for tc in batch}
                    async for event in _collect_tool_tasks(task_to_tool_call):
                        yield event

                for tc in tool_calls:
                    if submit_review_enabled and tc.tool_name == "submit":
                        # Control-only tool: never dispatched to the registry
                        # (its body raises). Flush prior work so the captured
                        # diff reflects every edit in this batch, then answer
                        # the submit with the review/confirm text directly.
                        async for event in _flush_parallel_batch(parallel_batch):
                            yield event
                        parallel_batch = []
                        (
                            submit_file_index,
                            submit_diff_text,
                        ) = await self._workspace_submit_review_capture()
                        submit_diff_empty = not (
                            submit_file_index.strip() or submit_diff_text.strip()
                        )
                        submit_headroom_ok = _turn_llm_call_budget_error(
                            turn_llm_calls + 1
                        ) is None and (
                            _total_deadline is None or _loop.time() < _total_deadline
                        )
                        submit_action = evaluate_explicit_submit(
                            submit_review_state,
                            diff_empty=submit_diff_empty,
                            headroom_ok=submit_headroom_ok,
                        )
                        if submit_action is SubmitAction.SHOW_CHECKLIST:
                            submit_content = build_submit_review_message(
                                submit_file_index,
                                submit_diff_text,
                                implicit=False,
                                max_chars=submit_review_diff_max_chars,
                            )
                        elif submit_action is SubmitAction.NUDGE:
                            submit_content = submit_review_nudge_message()
                        elif submit_action is SubmitAction.EMPTY_DIFF_NOTE:
                            submit_content = submit_review_empty_diff_note()
                        else:
                            submit_content = submit_review_confirmation_message()
                        # A confirming submit finalizes the turn: the model has
                        # cleared the review handshake and asked to submit, so its
                        # current workspace changes become the final answer and the
                        # loop ends here — the same outcome as the model going quiet
                        # with a non-empty diff. Ending on confirm (rather than
                        # replying and looping) is what makes a repeated submit
                        # unable to re-enter this branch, so the confirmed state can
                        # never re-fire. The checklist/nudge/empty-diff replies keep
                        # the turn open so the model can act on them.
                        submit_terminates_turn = submit_action is SubmitAction.CONFIRM
                        self._record_runtime_event(
                            "submit_review.explicit",
                            feature="submit_review",
                            reason=submit_action.value,
                            iteration=iterations,
                            provider_call_count=turn_llm_calls,
                            injected_to_model=True,
                            details={
                                "stage": submit_review_state.stage,
                                "nudges": submit_review_state.nudges,
                                "diff_empty": submit_diff_empty,
                                "diff_truncated": submit_review_diff_is_truncated(
                                    submit_diff_text,
                                    submit_review_diff_max_chars,
                                ),
                                "terminates_turn": submit_terminates_turn,
                            },
                        )
                        results_by_id[tc.tool_use_id] = ToolResult(
                            tool_use_id=tc.tool_use_id,
                            tool_name="submit",
                            content=submit_content,
                            is_error=False,
                            terminates_turn=submit_terminates_turn,
                        )
                        continue
                    if tc.tool_name == "meta_invoke":
                        async for event in _flush_parallel_batch(parallel_batch):
                            yield event
                        parallel_batch = []
                        active_ctx = (
                            current_tool_context.get() or self._tool_context or ToolContext()
                        )
                        async for ev in self._run_one_streaming(tc, active_ctx):
                            if isinstance(ev, ToolResult):
                                results_by_id[tc.tool_use_id] = ev
                            else:
                                yield ev
                        continue
                    if submit_review_enabled:
                        # Any real (non-submit) tool counts as work after a
                        # review was shown; distinguishes continued work from an
                        # immediate rubber-stamp re-submit.
                        submit_review_observe_tool_activity(submit_review_state)
                    policy = _get_tool_concurrency_policy(
                        tc.tool_name,
                        tc.arguments,
                        parent_session_key=self._session_key,
                    )
                    if policy.mode != "mutex":
                        parallel_batch.append(tc)
                    else:
                        async for event in _flush_parallel_batch(parallel_batch):
                            yield event
                        parallel_batch = []
                        async for event in _collect_tool_tasks(
                            {asyncio.create_task(_run_one(tc)): tc}
                        ):
                            yield event

                async for event in _flush_parallel_batch(parallel_batch):
                    yield event

                # Emit results in original tool_calls order.
                for tc in tool_calls:
                    result = results_by_id[tc.tool_use_id]
                    result_tool_call = tc
                    for artifact in result.artifacts:
                        yield ArtifactEvent(**_artifact_event_kwargs(artifact))
                    projected_result = await self._project_tool_result_for_delivery(
                        result,
                        tool_call=result_tool_call,
                    )
                    pending_approval = _pending_approval_payload(result.content)
                    if pending_approval is not None and not tc.arguments.get("approval_id"):
                        suspended = _suspend_tool_request(tc, pending_approval)
                        assessment = await _review_pending_elevation_if_configured(
                            pending_approval,
                            transcript=turn_messages,
                            runtime_events_path=self.config.runtime_events_path,
                            suspended_action=suspended.action,
                        )
                        # Human-owned approvals need a lifecycle event so the UI can
                        # render its card. Automatic rule reviews remain internal.
                        if assessment is None:
                            yield ToolResultEvent(
                                tool_use_id=projected_result.tool_use_id,
                                tool_name=projected_result.tool_name,
                                result=projected_result.content,
                                is_error=projected_result.is_error,
                                arguments=tc.arguments,
                                execution_status=projected_result.execution_status,
                            )
                        await _wait_for_pending_approval_resolution(
                            pending_approval,
                            timeout=_cap_timeout_by_deadlines(self._approval_wait_timeout()),
                        )
                        approval_entry = None
                        from opensquilla.gateway.approval_queue import get_approval_queue

                        try:
                            approval_entry = get_approval_queue().get(
                                str(pending_approval["approval_id"])
                            )
                        except KeyError:
                            approval_entry = None
                        if approval_entry is None or not approval_entry.resolved:
                            turn_yielded = True
                        elif not approval_entry.approved:
                            suspended.deny(str(pending_approval["approval_id"]))
                            rationale = str(
                                approval_entry.params.get("reviewRationale") or ""
                            ).strip()
                            result = ToolResult(
                                tool_use_id=tc.tool_use_id,
                                tool_name=tc.tool_name,
                                content=json.dumps(
                                    {
                                        "status": "approval_denied",
                                        "approval_id": pending_approval["approval_id"],
                                        "message": rationale
                                        or "The exact elevated action was not approved.",
                                    },
                                    ensure_ascii=False,
                                ),
                                is_error=False,
                            )
                            projected_result = await self._project_tool_result_for_delivery(
                                result,
                                tool_call=tc,
                            )
                            yield ToolResultEvent(
                                tool_use_id=projected_result.tool_use_id,
                                tool_name=projected_result.tool_name,
                                result=projected_result.content,
                                is_error=projected_result.is_error,
                                arguments=tc.arguments,
                                execution_status=projected_result.execution_status,
                            )
                            if approval_entry.resolution == "expired":
                                turn_yielded = True
                        else:
                            resumed_call = suspended.approve(
                                str(pending_approval["approval_id"])
                            )
                            result = await _run_one(suspended.begin_execution())
                            suspended.complete()
                            result_tool_call = resumed_call
                            for artifact in result.artifacts:
                                yield ArtifactEvent(**_artifact_event_kwargs(artifact))
                            projected_result = await self._project_tool_result_for_delivery(
                                result,
                                tool_call=result_tool_call,
                            )
                            yield ToolResultEvent(
                                tool_use_id=projected_result.tool_use_id,
                                tool_name=projected_result.tool_name,
                                result=projected_result.content,
                                is_error=projected_result.is_error,
                                arguments=tc.arguments,
                                execution_status=projected_result.execution_status,
                            )
                            replay_event = router_control_replay_event_from_payload(
                                result.content
                            )
                            if replay_event is not None:
                                yield replay_event
                            if _pending_approval_payload(result.content) is not None:
                                turn_yielded = True
                    else:
                        yield ToolResultEvent(
                            tool_use_id=projected_result.tool_use_id,
                            tool_name=projected_result.tool_name,
                            result=projected_result.content,
                            is_error=projected_result.is_error,
                            arguments=tc.arguments,
                            execution_status=projected_result.execution_status,
                        )
                        replay_event = router_control_replay_event_from_payload(
                            result.content
                        )
                        if replay_event is not None:
                            yield replay_event
                    executed_results.append(result)
                    while self._pending_warnings:
                        yield self._pending_warnings.pop(0)
                    if self._is_turn_yield_result(result) or result.terminates_turn:
                        turn_yielded = True
                    tool_result_blocks.append(
                        ContentBlockToolResult(
                            tool_use_id=projected_result.tool_use_id,
                            content=projected_result.content,
                            is_error=projected_result.is_error,
                            execution_status=projected_result.execution_status,
                        )
                    )

                terminal_artifacts = self._terminal_artifact_delivery_artifacts(executed_results)
                if terminal_artifacts:
                    artifact_delivery_final_response_artifacts = terminal_artifacts

                turn_tool_errors += sum(1 for result in executed_results if result.is_error)
                first_tool_error = next(
                    (result for result in executed_results if result.is_error),
                    None,
                )
                workspace_write_count = len(self._effective_workspace_write_records())
                mutation_receipt_counts = self._workspace_mutation_receipt_counts()
                post_write_progress_count = self._post_write_progress_count(
                    workspace_write_count=workspace_write_count,
                    mutation_receipt_counts=mutation_receipt_counts,
                )
                if len(tool_calls) == 1:
                    current_repeat_key = self._tool_call_repeat_key(tool_calls[0])
                    if current_repeat_key == repeated_tool_call_key:
                        repeated_tool_call_last_result_is_error = any(
                            result.is_error for result in executed_results
                        )
                        repeated_tool_call_workspace_write_count = workspace_write_count
                if post_write_progress_count > last_post_write_progress_count:
                    last_post_write_progress_count = post_write_progress_count
                    post_write_focused_verification_observed = False
                    post_write_focused_verification_success_observed = False
                    last_post_write_failed_verification = None
                if finalize_evidence_tracker is not None:
                    for tc, result in zip(tool_calls, executed_results, strict=False):
                        executed_tc = executed_tool_calls_by_id.get(tc.tool_use_id, tc)
                        if tc.tool_name in _GATE_WRITE_TOOL_NAMES:
                            for write_path, is_scratch in (
                                self._finalize_evidence_write_targets(executed_tc)
                            ):
                                finalize_evidence_tracker.observe_write(
                                    write_path,
                                    is_error=bool(result.is_error),
                                    iteration=iterations,
                                    scratch=is_scratch,
                                )
                            continue
                        if tc.tool_name not in _GATE_EXECUTION_TOOL_NAMES:
                            continue
                        gate_command = self._execution_command_for_progress(executed_tc)
                        if not gate_command:
                            continue
                        gate_result_text = self._tool_result_text_for_anchor(result.content)
                        gate_red, gate_exit_code, gate_timed_out, gate_status_reason = (
                            execution_signals_from_result(
                                tool_name=result.tool_name,
                                content_text=gate_result_text,
                                execution_status=result.execution_status,
                                is_error=bool(result.is_error),
                            )
                        )
                        gate_evidence_credit = True
                        if scratch_verify_mirror_enabled:
                            gate_evidence_credit = (
                                self._scratch_verify_mirror_evidence_credit(
                                    gate_command
                                )
                            )
                            if not gate_evidence_credit:
                                self._record_runtime_event(
                                    "scratch_verify_mirror.credit_withheld",
                                    feature="scratch_verify_mirror",
                                    reason="mirror_diverged_from_workspace",
                                    iteration=iterations,
                                    command=gate_command[:500],
                                )
                        finalize_evidence_tracker.observe_execution(
                            gate_command,
                            red=gate_red,
                            exit_code=gate_exit_code,
                            timed_out=gate_timed_out,
                            status_reason=gate_status_reason,
                            failure_anchors=(
                                self._failure_anchor_lines(gate_result_text)
                                if gate_red
                                else []
                            ),
                            iteration=iterations,
                            evidence_credit=gate_evidence_credit,
                        )
                focused_verification_success_before_results = (
                    post_write_focused_verification_success_observed
                )
                source_context_signature = self._source_context_signature(
                    tool_calls,
                    executed_results,
                )
                successful_source_context_tool_result = source_context_signature is not None
                successful_execution_tool_result = any(
                    not result.is_error and result.tool_name in _EXECUTION_TOOL_NAMES
                    for result in executed_results
                )
                current_focused_verification_observed = False
                if post_write_progress_count > 0:
                    for tc, result in zip(tool_calls, executed_results, strict=False):
                        if result.tool_name not in _EXECUTION_TOOL_NAMES:
                            continue
                        command = self._execution_command_for_progress(tc)
                        if command and self._command_looks_like_focused_verification(command):
                            current_focused_verification_observed = True
                            post_write_focused_verification_observed = True
                            result_text = self._tool_result_text_for_anchor(result.content)
                            verification_state = (
                                self._classify_focused_verification_result(result)
                            )
                            self._record_runtime_event(
                                "focused_verification.classified",
                                feature="verification",
                                tool_name=result.tool_name,
                                command=command[:500],
                                state=verification_state,
                                is_error=bool(result.is_error),
                            )
                            clean_validation_success = (
                                self._tool_result_has_validation_success_signal(result_text)
                                and not self._tool_result_has_failure_signal(result_text)
                            )
                            if clean_validation_success:
                                post_write_focused_verification_success_observed = True
                                last_post_write_failed_verification = None
                            elif result.is_error or self._tool_result_has_failure_signal(
                                result_text
                            ):
                                execution_status: Mapping[str, Any] = (
                                    result.execution_status or {}
                                )
                                status_reason = ""
                                if isinstance(execution_status, Mapping):
                                    status_reason = str(execution_status.get("reason") or "")
                                post_write_focused_verification_success_observed = False
                                last_post_write_failed_verification = {
                                    "reason": (
                                        "final_response_after_failed_focused_"
                                        "verification_with_diff"
                                    ),
                                    "tool_name": result.tool_name,
                                    "command": command[:500],
                                    "execution_status_reason": status_reason or None,
                                    "failure_anchors": self._failure_anchor_lines(result_text)[:3],
                                    "workspace_write_count": workspace_write_count,
                                    "changed_receipt_count": mutation_receipt_counts[
                                        "changed_receipt_count"
                                    ],
                                }
                            else:
                                post_write_focused_verification_success_observed = True
                                last_post_write_failed_verification = None
                failure_anchor_summary = self._failure_anchor_summary_from_tool_results(
                    tool_calls,
                    executed_results,
                )
                if (
                    failure_anchor_summary
                    and failure_anchor_summary not in recent_failure_anchor_summaries
                ):
                    recent_failure_anchor_summaries.append(failure_anchor_summary)
                    recent_failure_anchor_summaries[:] = recent_failure_anchor_summaries[-3:]
                runtime_diff_paths = self._workspace_diff_paths_for_runtime_event()
                runtime_diff_fingerprint = (
                    self._workspace_diff_fingerprint_for_runtime_event()
                )
                runtime_diagnostic_events: list[dict[str, Any]] = []
                if runtime_diagnostics is not None:
                    for runtime_event in runtime_diagnostics.observe_tool_results(
                        iteration=iterations,
                        provider_call_count=turn_llm_calls,
                        tool_calls=tool_calls,
                        results=executed_results,
                        read_records=self._workspace_read_records(),
                        write_records=self._workspace_write_records(),
                        scratch_records=self._scratch_write_records(),
                        diff_paths=runtime_diff_paths,
                        diff_fingerprint=runtime_diff_fingerprint,
                        failure_anchor_summary=failure_anchor_summary,
                    ):
                        runtime_diagnostic_events.append(runtime_event)
                        append_runtime_event(self.config.runtime_events_path, runtime_event)
                post_write_convergence_guidance: str | None = None
                if post_write_convergence_tracker is not None:
                    continued_activity_after_verification = bool(
                        (
                            focused_verification_success_before_results
                            or (
                                post_write_focused_verification_success_observed
                                and not current_focused_verification_observed
                            )
                        )
                        and (
                            successful_execution_tool_result
                            or successful_source_context_tool_result
                        )
                    )
                    post_write_convergence_decision = (
                        post_write_convergence_tracker.observe(
                            PostWriteConvergenceObservation(
                                iteration=iterations,
                                provider_call_count=turn_llm_calls,
                                workspace_write_count=workspace_write_count,
                                changed_receipt_count=mutation_receipt_counts[
                                    "changed_receipt_count"
                                ],
                                diff_fingerprint=runtime_diff_fingerprint,
                                diff_paths=runtime_diff_paths,
                                focused_verification_success_observed=(
                                    post_write_focused_verification_success_observed
                                ),
                                continued_activity_after_verification=(
                                    continued_activity_after_verification
                                ),
                            )
                        )
                    )
                    if (
                        post_write_convergence_decision.action == "finalize"
                        and progress_watchdog_mode == "warn_model"
                    ):
                        post_write_convergence_finalization_pending = True
                        post_write_convergence_finalization_message = Message(
                            role="user",
                            content=_post_write_convergence_message(
                                post_write_convergence_decision
                            ),
                        )
                        post_write_convergence_guidance = (
                            post_write_convergence_finalization_message.content
                            if isinstance(
                                post_write_convergence_finalization_message.content,
                                str,
                            )
                            else None
                        )
                    elif (
                        post_write_convergence_decision.action == "warn"
                        and progress_watchdog_mode == "warn_model"
                    ):
                        post_write_convergence_guidance = _post_write_convergence_message(
                            post_write_convergence_decision
                        )
                    if post_write_convergence_decision.action != "observe":
                        self._record_post_write_convergence_event(
                            post_write_convergence_decision,
                            mode=progress_watchdog_mode,
                            injected_to_model=bool(post_write_convergence_guidance),
                            hint_text=post_write_convergence_guidance,
                        )
                        metadata_key = {
                            "warn": "post_write_convergence_warnings",
                            "finalize": "post_write_convergence_finalizations",
                            "reset": "post_write_convergence_resets",
                        }.get(post_write_convergence_decision.action)
                        if metadata_key:
                            self.config.metadata[metadata_key] = (
                                self.config.metadata.get(metadata_key, 0) + 1
                            )
                        self._write_turn_call_log(
                            "post_write_convergence",
                            action=post_write_convergence_decision.action,
                            mode=progress_watchdog_mode,
                            reason=post_write_convergence_decision.reason,
                            details=post_write_convergence_decision.details,
                        )
                        if post_write_convergence_guidance:
                            yield WarningEvent(
                                code=(
                                    "post_write_convergence_finalization"
                                    if post_write_convergence_decision.action == "finalize"
                                    else "post_write_convergence_warning"
                                ),
                                message=(
                                    "Runtime detected stable post-verification diff "
                                    "activity and asked the model to converge."
                                ),
                            )
                progress_watchdog_guidance: str | None = None
                watchdog_decision = None
                if progress_watchdog_mode != "off" and post_write_convergence_guidance is None:
                    watchdog_decision = progress_watchdog.observe(
                        ProgressObservation(
                            iteration=iterations,
                            provider_call_count=turn_llm_calls,
                            successful_tool_result=any(
                                not result.is_error for result in executed_results
                            ),
                            successful_source_context_tool_result=(
                                successful_source_context_tool_result
                            ),
                            successful_execution_tool_result=successful_execution_tool_result,
                            source_context_signature=source_context_signature,
                            user_visible_output=bool("".join(final_text_parts).strip()),
                            artifact_completed=bool(terminal_artifacts),
                            workspace_write_count=workspace_write_count,
                            changed_receipt_count=mutation_receipt_counts[
                                "changed_receipt_count"
                            ],
                            noop_receipt_count=mutation_receipt_counts[
                                "noop_receipt_count"
                            ],
                            partial_receipt_count=mutation_receipt_counts[
                                "partial_receipt_count"
                            ],
                            workspace_change_likely_required=(
                                self._turn_likely_requires_workspace_change("")
                            ),
                            scratch_write_count=len(self._scratch_write_records()),
                            post_write_focused_verification_observed=(
                                post_write_focused_verification_observed
                            ),
                            tool_error_signature=(
                                None
                                if first_tool_error is None
                                else self._tool_error_signature(first_tool_error)
                            ),
                            failure_anchor_signature=(
                                self._failure_anchor_signature(failure_anchor_summary)
                            ),
                            failure_anchor_summary=failure_anchor_summary,
                        )
                    )
                if watchdog_decision is not None and watchdog_decision.action != "observe":
                    watchdog_hint_text: str | None = None
                    if (
                        watchdog_decision.action == "warn"
                        and progress_watchdog_mode == "warn_model"
                    ):
                        watchdog_hint_text = _progress_watchdog_guidance_message(
                            watchdog_decision.reason,
                            watchdog_decision.details,
                        )
                    self._record_tool_loop_runtime_event(
                        reason=watchdog_decision.reason,
                        iteration=iterations,
                        provider_call_count=turn_llm_calls,
                        watchdog_action=watchdog_decision.action,
                        watchdog_mode=progress_watchdog_mode,
                        details=watchdog_decision.details,
                        workspace_write_count=workspace_write_count,
                        source_context_signature=source_context_signature,
                        injected_to_model=bool(watchdog_hint_text),
                        hint_text_sha256=(
                            hashlib.sha256(watchdog_hint_text.encode("utf-8")).hexdigest()
                            if watchdog_hint_text
                            else None
                        ),
                    )
                    self._write_turn_call_log(
                        "progress_watchdog",
                        action=watchdog_decision.action,
                        mode=progress_watchdog_mode,
                        reason=watchdog_decision.reason,
                        details=watchdog_decision.details,
                    )
                    if watchdog_hint_text:
                        progress_watchdog_guidance = watchdog_hint_text
                        gate_details = self._workspace_edit_gate_details(
                            watchdog_decision.reason,
                            watchdog_decision.details,
                        )
                        if gate_details is not None:
                            workspace_edit_gate_details = gate_details
                            workspace_edit_gate_recovery_read_paths.clear()
                            workspace_edit_gate_recovery_reads_remaining = 0
                            self.config.metadata["workspace_edit_gate_activations"] = (
                                self.config.metadata.get(
                                    "workspace_edit_gate_activations",
                                    0,
                                )
                                + 1
                            )
                    elif watchdog_decision.action == "block":
                        terminal_error = ErrorEvent(
                            message=(
                                "Runtime progress watchdog stopped the turn after "
                                "repeated activity without clear progress."
                            ),
                            code="progress_watchdog_blocked",
                        )
                source_loop_recovery_guidance: str | None = None
                if progress_watchdog_guidance is None:
                    source_loop_recovery = source_loop_recovery_decision(
                        global_mode=runtime_recovery_mode,
                        diagnostic_events=runtime_diagnostic_events,
                        attempted=bool(source_loop_recovery_attempted_keys),
                        attempted_event_keys=source_loop_recovery_attempted_keys,
                        max_nudges=runtime_recovery_source_loop_max_nudges,
                    )
                    if source_loop_recovery is not None:
                        self._record_runtime_recovery_event(
                            source_loop_recovery,
                            iteration=iterations,
                            provider_call_count=turn_llm_calls,
                            workspace_write_count=workspace_write_count,
                            source_context_signature=source_context_signature,
                        )
                        recovery_event_key = source_loop_recovery.details.get(
                            "recovery_event_key"
                        )
                        if isinstance(recovery_event_key, str) and recovery_event_key:
                            source_loop_recovery_attempted_keys.add(recovery_event_key)
                        else:
                            source_loop_recovery_attempted_keys.add(
                                f"legacy:{len(source_loop_recovery_attempted_keys) + 1}"
                            )
                        self._write_turn_call_log(
                            "runtime_recovery",
                            action=source_loop_recovery.action,
                            mode=source_loop_recovery.mode,
                            reason=source_loop_recovery.reason,
                            details=source_loop_recovery.details,
                        )
                        if (
                            source_loop_recovery.action == "nudge"
                            and source_loop_recovery.message
                        ):
                            source_loop_recovery_guidance = source_loop_recovery.message
                            runtime_recovery_scaffolding_pending = True
                            self.config.metadata["source_loop_recoveries"] = (
                                self.config.metadata.get("source_loop_recoveries", 0) + 1
                            )
                            yield WarningEvent(
                                code="source_loop_recovery",
                                message=(
                                    "Runtime detected repeated source-loop evidence; "
                                    "asking the model to reassess the current patch once."
                                ),
                            )
                budget_error = _turn_budget_error()
                if terminal_error is None:
                    terminal_error = budget_error
                if terminal_error is not None:
                    if artifact_delivery_final_response_pending:
                        yield _finish_artifact_delivery_degraded(
                            reason=terminal_error.message,
                            code=terminal_error.code,
                        )
                        terminal_error = None
                    else:
                        yield self._transition(AgentState.ERROR)
                        yield terminal_error
                    break

                if any(_is_threshold_denial(result) for result in executed_results):
                    yield self._transition(AgentState.ERROR)
                    terminal_error = ErrorEvent(
                        message=(
                            "Autonomous execution paused after repeated sandbox denials. "
                            "Human intervention is required before continuing."
                        ),
                        code="sandbox_threshold_exceeded",
                    )
                    yield terminal_error
                    break

                # Per-iteration deadline check after tool execution
                if _loop.time() > tool_deadline:
                    yield self._transition(AgentState.ERROR)
                    terminal_error = ErrorEvent(
                        message=(
                            f"Iteration {iterations} exceeded iteration_timeout"
                            f" ({self.config.iteration_timeout}s) during tool execution"
                        ),
                        code="iteration_timeout",
                    )
                    yield terminal_error
                    break

                # Feed tool results back as user message
                turn_messages.append(
                    Message(role="user", content=tool_result_blocks)  # type: ignore[arg-type]
                )
                if pending_input_provider is not None:
                    pending_inputs = pending_input_provider.drain_pending()
                    if pending_inputs:
                        turn_messages.append(
                            Message(
                                role="user",
                                content=[
                                    ContentBlockText(text=pending_input)
                                    for pending_input in pending_inputs
                                ],
                            )
                        )
                if progress_watchdog_guidance is not None:
                    turn_messages.append(Message(role="user", content=progress_watchdog_guidance))
                if (
                    post_write_convergence_guidance is not None
                    and not post_write_convergence_finalization_pending
                ):
                    turn_messages.append(
                        Message(role="user", content=post_write_convergence_guidance)
                    )
                if (
                    bool(getattr(self.config, "mid_budget_no_diff_nudge", False))
                    and _total_deadline is not None
                    and self.config.timeout > 0
                ):
                    elapsed_fraction = 1.0 - (
                        max(0.0, _total_deadline - _loop.time()) / self.config.timeout
                    )
                    due_fractions = [
                        fraction
                        for fraction in _MID_BUDGET_NO_DIFF_NUDGE_FRACTIONS
                        if fraction not in mid_budget_nudge_fired_fractions
                        and elapsed_fraction >= fraction
                    ]
                    if due_fractions:
                        # Checkpoints are consumed when crossed whether or not
                        # a nudge fires: one crossed while a diff existed must
                        # not fire late if that diff is reverted, and crossing
                        # several at once yields a single nudge.
                        mid_budget_nudge_fired_fractions.update(due_fractions)
                        nudge_fraction = max(due_fractions)
                        # The evidence probe shells out to git; keep it off
                        # the event loop.
                        has_change_evidence = await asyncio.to_thread(
                            self._workspace_has_source_change_evidence
                        )
                        if not has_change_evidence:
                            turn_messages.append(
                                Message(
                                    role="user",
                                    # Report real elapsed time, not the
                                    # checkpoint constant: one long stream can
                                    # carry the turn far past the checkpoint
                                    # before it is noticed.
                                    content=_MID_BUDGET_NO_DIFF_NUDGE_TEMPLATE.format(
                                        percent=int(elapsed_fraction * 100),
                                    ),
                                )
                            )
                            self._write_turn_call_log(
                                "turn_policy_decision",
                                action="mid_budget_no_diff_nudge",
                                reason="budget_fraction",
                                code="mid_budget_no_diff_nudge",
                                iteration=iterations,
                                budget_fraction=nudge_fraction,
                                elapsed_fraction=round(elapsed_fraction, 3),
                            )
                # One-shot endgame fix directive: inside the margin with no
                # source fix beyond diagnostic instrumentation, direct the
                # model to commit to its best-supported fix now. The margin
                # crossing is consumed whether or not the directive fires —
                # a fix present at crossing time that is reverted later must
                # not trigger a late directive.
                endgame_fix_margin_seconds = max(
                    0,
                    int(
                        getattr(self.config, "endgame_fix_directive_margin_seconds", 0)
                        or 0
                    ),
                )
                if (
                    endgame_fix_margin_seconds > 0
                    and _total_deadline is not None
                    and not endgame_fix_directive_fired
                    and _loop.time() > _total_deadline - endgame_fix_margin_seconds
                ):
                    endgame_fix_directive_fired = True
                    # The probe shells out to git; keep it off the event loop.
                    has_source_fix = await asyncio.to_thread(
                        self._workspace_source_fix_beyond_instrumentation
                    )
                    if not has_source_fix:
                        remaining_seconds = max(0.0, _total_deadline - _loop.time())
                        turn_messages.append(
                            Message(
                                role="user",
                                content=_ENDGAME_FIX_DIRECTIVE_TEMPLATE.format(
                                    minutes=max(1, int(remaining_seconds // 60)),
                                ),
                            )
                        )
                        self._write_turn_call_log(
                            "turn_policy_decision",
                            action="endgame_fix_directive",
                            reason="deadline_margin_no_fix",
                            code="endgame_fix_directive",
                            iteration=iterations,
                            remaining_seconds=int(remaining_seconds),
                            margin_seconds=endgame_fix_margin_seconds,
                        )
                        append_runtime_event(
                            self.config.runtime_events_path,
                            {
                                "feature": "endgame_fix_directive",
                                "name": "endgame_fix_directive.injected",
                                "action": "append_fix_directive",
                                "reason": "deadline_margin_no_fix",
                                "iteration": iterations,
                                "remaining_seconds": int(remaining_seconds),
                                "margin_seconds": endgame_fix_margin_seconds,
                                "session_key": self._session_key,
                                "agent_id": (
                                    self.config.tool_result_store_agent_id
                                    or self.config.metadata.get("agent_id")
                                ),
                            },
                        )
                # Plan-only act-now directive: consecutive iterations whose
                # executed tool calls are all update_plan mean the model is
                # narrating progress instead of making it.
                plan_only_threshold = max(
                    0,
                    int(getattr(self.config, "plan_only_act_now_threshold", 0) or 0),
                )
                if plan_only_threshold > 0:
                    if executed_results and all(
                        getattr(result, "tool_name", None) == "update_plan"
                        for result in executed_results
                    ):
                        plan_only_iteration_streak += 1
                    else:
                        plan_only_iteration_streak = 0
                    if plan_only_iteration_streak >= plan_only_threshold:
                        turn_messages.append(
                            Message(
                                role="user",
                                content=_PLAN_ONLY_ACT_NOW_DIRECTIVE,
                            )
                        )
                        self._write_turn_call_log(
                            "turn_policy_decision",
                            action="plan_only_act_now",
                            reason="plan_only_streak",
                            code="plan_only_act_now",
                            iteration=iterations,
                            streak=plan_only_iteration_streak,
                            threshold=plan_only_threshold,
                        )
                        append_runtime_event(
                            self.config.runtime_events_path,
                            {
                                "feature": "plan_only_act_now",
                                "name": "plan_only_act_now.injected",
                                "action": "append_act_now_directive",
                                "reason": "plan_only_streak",
                                "iteration": iterations,
                                "streak": plan_only_iteration_streak,
                                "threshold": plan_only_threshold,
                                "session_key": self._session_key,
                                "agent_id": (
                                    self.config.tool_result_store_agent_id
                                    or self.config.metadata.get("agent_id")
                                ),
                            },
                        )
                        # Reset so the directive re-fires only after another
                        # full streak, not on every subsequent iteration.
                        plan_only_iteration_streak = 0
                # Count iterations that blocked a compacted-placeholder reuse
                # (preflight or dispatch path) and escalate the recovery
                # directive once the configured threshold is reached. This
                # runs before the source-loop recovery guidance append below:
                # that guidance must stay the final runtime-injected message
                # of the turn so _drop_runtime_recovery_scaffolding can pop it
                # from the end.
                if terminal_projection_preflight_error or any(
                    self._is_provider_context_projection_reuse_result(result)
                    for result in executed_results
                ):
                    placeholder_offense_iterations += 1
                    placeholder_escalation_threshold = max(
                        0,
                        int(
                            getattr(self.config, "placeholder_escalation_threshold", 0)
                            or 0
                        ),
                    )
                    if (
                        placeholder_escalation_threshold > 0
                        and placeholder_offense_iterations
                        >= placeholder_escalation_threshold
                    ):
                        turn_messages.append(
                            Message(
                                role="user",
                                content=_PLACEHOLDER_ESCALATION_DIRECTIVE,
                            )
                        )
                        self._write_turn_call_log(
                            "placeholder_offense_escalation",
                            iteration=iterations,
                            offense_iterations=placeholder_offense_iterations,
                            threshold=placeholder_escalation_threshold,
                        )
                        # The turn-call log is a raw debug stream that run
                        # harnesses do not collect; the runtime event is what
                        # lets delivery gates tell this designed escalation
                        # apart from a treatment delivery failure.
                        append_runtime_event(
                            self.config.runtime_events_path,
                            {
                                "feature": "placeholder_escalation",
                                "name": "placeholder_escalation.injected",
                                "action": "append_escalation_directive",
                                "reason": "placeholder_offense_threshold",
                                "iteration": iterations,
                                "offense_iterations": placeholder_offense_iterations,
                                "threshold": placeholder_escalation_threshold,
                                "session_key": self._session_key,
                                "agent_id": (
                                    self.config.tool_result_store_agent_id
                                    or self.config.metadata.get("agent_id")
                                ),
                            },
                        )
                if source_loop_recovery_guidance is not None:
                    # Appended last: _drop_runtime_recovery_scaffolding pops
                    # the one-shot directive from the end of the turn, so no
                    # other runtime-injected message may follow it.
                    turn_messages.append(
                        Message(role="user", content=source_loop_recovery_guidance)
                    )
                if terminal_projection_preflight_error:
                    self._write_turn_call_log(
                        "tool_argument_projection_rehydrate_recovery",
                        iteration=iterations,
                        tool_use_ids=sorted(preflight_tool_results),
                    )
                if terminal_artifacts:
                    _finish_artifact_delivery_without_provider()
                    break
                last_executed_results = list(executed_results)
                if turn_yielded:
                    break

                # ------ TOOL_CALLING → THINKING ------
                yield self._transition(AgentState.THINKING)
                # Loop continues

        except TimeoutError:
            if artifact_delivery_final_response_pending:
                yield _finish_artifact_delivery_degraded(
                    reason=f"Agent turn timed out after {self.config.timeout}s",
                    code="agent_runtime_timeout",
                )
            else:
                # Total turn deadline exceeded (raised by manual check above)
                yield self._transition(AgentState.ERROR)
                terminal_error = ErrorEvent(
                    message=f"Agent turn timed out after {self.config.timeout}s",
                    code="agent_runtime_timeout",
                )
                yield terminal_error

        if terminal_error is None:
            # Persist successful turns into in-memory history. Error turns are
            # persisted by TurnRunner as system errors, while their usage still
            # flows through the final DoneEvent below when provider usage exists.
            self._history = list(turn_messages)
            self._write_context_stage("session:after", self._history)

        # ------ → DONE ------
        # Compute per-turn cost from pricing table
        done_model = last_actual_model
        if not done_model and self._usage_tracker and self._session_key:
            su = self._usage_tracker.get(self._session_key)
            if su and su.model_id:
                done_model = su.model_id
        if not done_model:
            done_model = self.config.model_id or ""
        done_provider = (
            last_actual_provider
            or self.config.provider_id
            or getattr(self.provider, "provider_id", "")
            or getattr(self.provider, "provider_name", "")
            or ""
        )
        from opensquilla.engine.pricing import estimate_cost, resolve_model_price

        turn_estimate = estimate_cost(
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cache_read_tokens=total_cached_tokens,
            cache_write_tokens=total_cache_write_tokens,
            price=resolve_model_price(done_model, done_provider).entry,
        )
        estimated_cost = turn_estimate.cost_usd
        estimate_basis: str | None
        if total_provider_billed_entries and not total_unbilled_entries:
            done_cost = total_billed_cost
            cost_source = "provider_billed"
            estimate_basis = None
        elif total_provider_billed_entries:
            # The tracker/model breakdown below supplies the exact estimated
            # component when available. Preserve the provider receipt source
            # even when its confirmed amount is zero.
            done_cost = total_billed_cost
            cost_source = "mixed"
            estimate_basis = turn_estimate.basis
        elif estimated_cost > 0.0:
            done_cost = estimated_cost
            cost_source = "opensquilla_static_estimate"
            estimate_basis = turn_estimate.basis
        else:
            done_cost = 0.0
            cost_source = "unavailable"
            has_turn_tokens = bool(
                total_input_tokens
                or total_output_tokens
                or total_cached_tokens
                or total_cache_write_tokens
            )
            estimate_basis = "free" if turn_estimate.basis == "free" and has_turn_tokens else None

        session_totals = (
            self._usage_tracker.session_snapshot(self._session_key)
            if self._usage_tracker and self._session_key
            else None
        )
        turn_usage_delta = (
            self._usage_tracker.session_delta_snapshot(self._session_key, usage_turn_baseline)
            if self._usage_tracker and self._session_key
            else None
        )
        done_input_tokens = total_input_tokens
        done_output_tokens = total_output_tokens
        done_cached_tokens = total_cached_tokens
        done_cache_write_tokens = total_cache_write_tokens
        done_billed_cost = total_billed_cost
        if turn_usage_delta and (
            turn_usage_delta.input_tokens
            or turn_usage_delta.output_tokens
            or turn_usage_delta.cache_read_tokens
            or turn_usage_delta.cache_write_tokens
            or turn_usage_delta.cost_usd
            or turn_usage_delta.billed_cost
        ):
            done_input_tokens = turn_usage_delta.input_tokens
            done_output_tokens = turn_usage_delta.output_tokens
            done_cached_tokens = turn_usage_delta.cache_read_tokens
            done_cache_write_tokens = turn_usage_delta.cache_write_tokens
            done_cost = turn_usage_delta.cost_usd
            done_billed_cost = turn_usage_delta.billed_cost
            cost_source = _cost_source_for_usage(
                done_cost,
                done_billed_cost,
                turn_usage_delta.cost_source,
            )
            if cost_source == "provider_billed":
                estimate_basis = None
            elif cost_source in {"mixed", "opensquilla_estimate"}:
                # The delta includes an estimated component; disclose the
                # turn-level estimator basis for it.
                estimate_basis = turn_estimate.basis
            elif estimate_basis != "free":
                # "unavailable": no estimated dollars in the reported cost.
                estimate_basis = None

        has_usage = bool(
            done_input_tokens
            or done_output_tokens
            or total_reasoning_tokens
            or done_cached_tokens
            or done_cache_write_tokens
            or done_billed_cost
            or total_provider_billed_entries
        )
        summarized_model_usage_breakdown = _summarize_model_usage_breakdown(
            turn_model_usage_breakdown
        )
        final_ensemble_trace = (
            dict(last_ensemble_trace) if isinstance(last_ensemble_trace, dict) else None
        )
        if final_ensemble_trace is not None and turn_ensemble_request_count > 0:
            final_ensemble_trace["llm_request_count"] = turn_ensemble_request_count
        await self._write_patch_evidence_ledger(
            final_status=(
                "ok"
                if terminal_error is None
                else (terminal_error.code or "agent_error")
            ),
            iterations=iterations,
            provider_call_count=turn_llm_calls,
        )
        if runtime_diagnostics is not None and terminal_error is not None:
            runtime_diff_paths = self._workspace_diff_paths_for_runtime_event()
            for runtime_event in runtime_diagnostics.observe_finish_error(
                iteration=iterations,
                provider_call_count=turn_llm_calls,
                error_code=terminal_error.code,
                changed_files=self._relative_paths_from_records(self._workspace_write_records()),
                diff_paths=runtime_diff_paths,
                diff_fingerprint=self._workspace_diff_fingerprint_for_runtime_event(),
            ):
                append_runtime_event(self.config.runtime_events_path, runtime_event)
        if bool(getattr(self.config, "final_diff_salvage", False)):
            # Last engine-controlled moment before the runner collects the
            # patch from the worktree: if prior source writes ended in an
            # empty workspace diff, re-apply the newest captured candidate per
            # path. Runs for normal finalization and terminal errors alike;
            # the contract observation below then reflects the salvaged state.
            self._attempt_final_diff_salvage(
                trigger="terminal_error" if terminal_error is not None else "finalize",
                iteration=iterations,
            )
        if terminal_error is not None:
            final_diff_contract_mode = getattr(
                self.config,
                "final_diff_contract_mode",
                "log",
            )
            if final_diff_contract_mode != "off":
                final_diff_observation = self._final_diff_contract_observation()
                if final_diff_observation is not None and (
                    final_diff_observation.diff_paths or final_diff_observation.suspicious
                ):
                    self._record_final_diff_contract_event(
                        final_diff_observation,
                        iteration=iterations,
                        provider_call_count=turn_llm_calls,
                        mode=str(final_diff_contract_mode),
                        injected_to_model=False,
                        hint_text=None,
                    )
        if terminal_error is None or has_usage:
            if terminal_error is None:
                yield self._transition(AgentState.DONE)
            yield DoneEvent(
                text="".join(final_text_parts),
                input_tokens=done_input_tokens,
                output_tokens=done_output_tokens,
                reasoning_tokens=total_reasoning_tokens,
                cached_tokens=done_cached_tokens,
                cache_write_tokens=done_cache_write_tokens,
                iterations=iterations,
                cost_usd=done_cost,
                billed_cost=done_billed_cost,
                cost_source=cost_source,
                model=done_model,
                provider=done_provider,
                runtime_context_hash=runtime_context_hash,
                runtime_context_chars=len(runtime_context),
                reasoning_content=(
                    "\n".join(final_reasoning_parts) if final_reasoning_parts else None
                ),
                session_totals=session_totals,
                model_usage_breakdown=summarized_model_usage_breakdown,
                ensemble_trace=final_ensemble_trace,
                estimate_basis=estimate_basis,
                text_snapshot="".join(final_text_parts),
            )
        # Reset for next turn
        self._state = AgentState.IDLE

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _workspace_write_records(self) -> list[dict[str, Any]]:
        ctx = self._tool_context or current_tool_context.get()
        if ctx is None:
            return []
        records = getattr(ctx, "workspace_file_writes", []) or []
        return [record for record in records if isinstance(record, dict)]

    def _workspace_has_source_change_evidence(self) -> bool:
        """Best-effort check that this agent's run produced a source change.

        Used by the mid-budget nudge: write receipts and captured diff
        candidates cover tool-mediated edits, and the live tracked diff
        covers shell-made edits that leave no receipts. Only this agent's
        own ToolContext counts — the contextvar fallback inside a child
        agent resolves to the parent's context — and untracked files do
        not: scratch artifacts from merely running the code (caches,
        coverage files, logs) are not source progress.
        """

        ctx = self._tool_context
        if ctx is not None:
            records = getattr(ctx, "workspace_file_writes", []) or []
            if any(
                isinstance(record, dict)
                and not self._workspace_write_record_looks_synthetic(record)
                and not self._workspace_write_record_targets_configured_scratch(record)
                for record in records
            ):
                return True
            if getattr(ctx, "source_diff_candidates", []) or []:
                return True
        return bool(self._workspace_tracked_diff_paths_for_nudge())

    def _workspace_source_fix_beyond_instrumentation(self) -> bool:
        """Whether the tracked diff contains more than diagnostic output.

        Used by the endgame fix directive: an instrumentation-only diff
        (added print/log lines, nothing removed) means the model has been
        investigating, not fixing. Probe failures count as a fix existing —
        the conservative direction, since the directive tells the model to
        stop investigating and a misfire on a real fix wastes the message.
        """

        paths = self._workspace_tracked_diff_paths_for_nudge()
        if not paths:
            return False
        ctx = self._tool_context
        raw_workspace = getattr(ctx, "workspace_dir", None) if ctx is not None else None
        if not raw_workspace:
            raw_workspace = self.config.workspace_dir
        if not raw_workspace:
            return True
        workspace_dir = Path(raw_workspace).expanduser().resolve(strict=False)
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace_dir), "diff", "HEAD", "--", *paths],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        if result.returncode != 0:
            return True
        patch = result.stdout or ""
        if not patch.strip():
            return False
        return not is_instrumentation_only_patch(patch)

    def _workspace_tracked_diff_paths_for_nudge(self) -> list[str]:
        ctx = self._tool_context
        raw_workspace = getattr(ctx, "workspace_dir", None) if ctx is not None else None
        if not raw_workspace:
            raw_workspace = self.config.workspace_dir
        if not raw_workspace:
            return []
        workspace_dir = Path(raw_workspace).expanduser().resolve(strict=False)
        if not workspace_dir.exists():
            return []
        ignored_paths = self._workspace_gitlink_paths(workspace_dir) | (
            self._workspace_internal_diagnostic_paths(workspace_dir)
        )
        paths: set[str] = set()
        for args in (("diff", "--name-only"), ("diff", "--cached", "--name-only")):
            try:
                result = subprocess.run(
                    ["git", "-C", str(workspace_dir), *args],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=2.0,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            for line in (result.stdout or "").splitlines():
                text = line.strip()
                if text:
                    normalized = _normalize_workspace_relative_path(text)
                    if normalized in ignored_paths:
                        continue
                    if self._workspace_relative_path_targets_scratch(normalized):
                        continue
                    paths.add(normalized)
        return sorted(paths)

    def _effective_workspace_write_records(self) -> list[dict[str, Any]]:
        return [
            record
            for record in self._workspace_write_records()
            if not self._workspace_write_record_looks_synthetic(record)
            and not self._workspace_write_record_targets_configured_scratch(record)
        ]

    def _workspace_write_record_targets_configured_scratch(
        self,
        record: Mapping[str, Any],
    ) -> bool:
        raw_path = str(record.get("path") or record.get("relative_path") or "")
        return self._workspace_relative_path_targets_scratch(raw_path)

    def _workspace_relative_path_targets_scratch(self, raw_path: str) -> bool:
        resolved, _ = self._configured_scratch_path_candidate(
            raw_path,
            relative_to="workspace",
        )
        return resolved is not None

    @staticmethod
    def _workspace_write_record_looks_synthetic(record: Mapping[str, Any]) -> bool:
        if not bool(record.get("created")):
            return False
        raw_path = str(record.get("relative_path") or record.get("path") or "")
        normalized = _normalize_workspace_relative_path(raw_path)
        if not normalized:
            return False
        name = Path(normalized).name.lower()
        return any(
            name == prefix
            or name.startswith(f"{prefix}.")
            or name.startswith(f"{prefix}_")
            for prefix in _SUSPICIOUS_NEW_WORKSPACE_WRITE_PREFIXES
        )

    def _workspace_read_records(self) -> list[dict[str, Any]]:
        ctx = self._tool_context or current_tool_context.get()
        if ctx is None:
            return []
        records = getattr(ctx, "workspace_file_reads", []) or []
        return [record for record in records if isinstance(record, dict)]

    def _scratch_write_records(self) -> list[dict[str, Any]]:
        ctx = self._tool_context or current_tool_context.get()
        if ctx is None:
            return []
        records = getattr(ctx, "scratch_file_writes", []) or []
        return [record for record in records if isinstance(record, dict)]

    def _workspace_mutation_records(self) -> list[dict[str, Any]]:
        ctx = self._tool_context or current_tool_context.get()
        if ctx is None:
            return []
        records = getattr(ctx, "workspace_mutation_records", []) or []
        return [record for record in records if isinstance(record, dict)]

    def _workspace_mutation_receipts(self) -> list[dict[str, Any]]:
        ctx = self._tool_context or current_tool_context.get()
        if ctx is None:
            return []
        records = getattr(ctx, "workspace_mutation_receipts", []) or []
        return [record for record in records if isinstance(record, dict)]

    def _changed_workspace_mutation_receipts(self) -> list[dict[str, Any]]:
        return [
            receipt
            for receipt in self._workspace_mutation_receipts()
            if receipt.get("changed") is True
            and receipt.get("classification") != "scratch"
        ]

    def _workspace_mutation_receipt_counts(self) -> dict[str, int]:
        receipts = [
            receipt
            for receipt in self._workspace_mutation_receipts()
            if receipt.get("classification") != "scratch"
        ]
        return {
            "changed_receipt_count": len(self._changed_workspace_mutation_receipts()),
            "noop_receipt_count": sum(
                1 for receipt in receipts if receipt.get("changed") is False
            ),
            "partial_receipt_count": sum(
                1 for receipt in receipts if receipt.get("partial") is True
            ),
        }

    def _post_write_progress_count(
        self,
        *,
        workspace_write_count: int | None = None,
        mutation_receipt_counts: Mapping[str, int] | None = None,
    ) -> int:
        if workspace_write_count is None:
            workspace_write_count = len(self._effective_workspace_write_records())
        if mutation_receipt_counts is None:
            mutation_receipt_counts = self._workspace_mutation_receipt_counts()
        changed_receipts = max(
            0,
            int(mutation_receipt_counts.get("changed_receipt_count", 0) or 0),
        )
        receipt_count = (
            changed_receipts
            + max(0, int(mutation_receipt_counts.get("noop_receipt_count", 0) or 0))
            + max(0, int(mutation_receipt_counts.get("partial_receipt_count", 0) or 0))
        )
        if receipt_count > 0:
            return changed_receipts
        return max(0, int(workspace_write_count or 0))

    def _workspace_mutation_receipt_summary(self) -> dict[str, int]:
        receipts = self._workspace_mutation_receipts()
        counts = self._workspace_mutation_receipt_counts()
        return {
            "workspace_mutation_receipt_count": len(receipts),
            **counts,
        }

    def _final_diff_contract_observation(self) -> FinalDiffContractObservation | None:
        diff_paths = self._workspace_diff_paths_for_final_diff_contract()
        known_scratch_paths = [
            path for path in diff_paths if self._workspace_relative_path_targets_scratch(path)
        ]
        write_records = self._effective_workspace_write_records()
        mutation_receipts = self._workspace_mutation_receipts()
        source_diff_candidates = []
        if self.config.source_diff_candidate_mode != "off" and self._tool_context:
            source_diff_candidates = list(
                getattr(self._tool_context, "source_diff_candidates", []) or []
            )
        if not diff_paths and not write_records and not mutation_receipts:
            return None
        return build_final_diff_contract_observation(
            diff_paths=diff_paths,
            read_records=self._workspace_read_records(),
            write_records=write_records,
            mutation_records=self._workspace_mutation_records(),
            mutation_receipts=mutation_receipts,
            source_diff_candidates=source_diff_candidates,
            known_scratch_paths=known_scratch_paths,
        )

    def _record_final_diff_contract_event(
        self,
        observation: FinalDiffContractObservation,
        *,
        iteration: int,
        provider_call_count: int,
        mode: str,
        injected_to_model: bool,
        hint_text: str | None = None,
    ) -> None:
        details = observation.to_event_details()
        details.update(self._workspace_mutation_receipt_summary())
        event = {
            "feature": "final_diff_contract",
            "name": "final_diff_contract.observed",
            "mode": mode,
            "reason": observation.primary_reason,
            "action": "nudge" if injected_to_model else "observe",
            "iteration": iteration,
            "provider_call_count": provider_call_count,
            "session_key": self._session_key,
            "agent_id": self.config.tool_result_store_agent_id
            or self.config.metadata.get("agent_id"),
            "injected_to_model": injected_to_model,
            "evidence": details,
            "details": details,
            "diff_paths": observation.diff_paths,
            "read_files": self._relative_paths_from_records(self._workspace_read_records()),
            "changed_files": self._relative_paths_from_records(self._workspace_write_records()),
            "mutation_records": self._workspace_mutation_records(),
            "hint_text_sha256": (
                hashlib.sha256(hint_text.encode("utf-8")).hexdigest()
                if hint_text
                else None
            ),
            "trigger_confidence": "final_diff_contract_gate",
        }
        append_runtime_event(self.config.runtime_events_path, event)

    # Cap on blocking `git apply` churn per salvage pass: the calls run on the
    # event loop thread, so a pathological candidate list must not be able to
    # stall the turn for the whole wrap-up window.
    _FINAL_DIFF_SALVAGE_TIME_BUDGET_SECONDS = 20.0

    def _attempt_final_diff_salvage(
        self,
        *,
        trigger: str,
        iteration: int,
    ) -> list[dict[str, Any]]:
        """Re-apply captured source-diff candidates whose paths lost their diff.

        Opt-in via final_diff_salvage (OPENSQUILLA_FINAL_DIFF_SALVAGE). Fires
        only when no tracked path carries a live diff: a healthy non-empty
        tracked diff means the agent finished with work it chose to keep, and
        re-applying a candidate the agent deliberately reverted would append
        abandoned edits to a scoring patch. With the tracked diff empty the
        collection is losing that path's earlier work anyway, so applying a
        stale candidate can only help. Untracked files (scratch repros and
        the like) never veto. Applies the newest candidate per path whose
        path shows no live diff, oldest-fallback on conflict,
        each guarded by `git apply --check`; applied candidates are marked
        restored, and a stale marker from an earlier turn is cleared once the
        path's diff is gone again so a later revert stays salvageable. The
        pass stops once its time budget is spent.
        """

        if not bool(getattr(self.config, "final_diff_salvage", False)):
            return []
        ctx = self._tool_context
        candidates = (
            list(getattr(ctx, "source_diff_candidates", []) or []) if ctx is not None else []
        )
        if not candidates:
            return []
        workspace = self._workspace_dir_for_status()
        if workspace is None:
            return []
        if self._workspace_diff_paths_for_final_diff_contract(include_untracked=False):
            # A tracked path still carries a live diff: the run ends with a
            # non-empty scored patch the agent chose to keep, and candidates
            # for clean paths are exactly the edits it deliberately reverted.
            # Resurrecting those here would corrupt a healthy final diff.
            return []
        live_diff_paths = set(self._workspace_diff_paths_for_final_diff_contract())
        deadline = time.monotonic() + self._FINAL_DIFF_SALVAGE_TIME_BUDGET_SECONDS
        applied: list[dict[str, Any]] = []
        handled_paths: set[str] = set()
        for candidate in reversed(candidates):
            paths = [
                path for path in candidate.get("paths", []) if isinstance(path, str) and path
            ]
            if not paths or paths[0] in handled_paths:
                continue
            path = paths[0]
            if path in live_diff_paths:
                # The path already carries a live diff; there is nothing to
                # salvage and stacking a stale candidate on top would clobber
                # newer in-worktree work.
                handled_paths.add(path)
                continue
            if candidate.get("restored") is True:
                # An earlier pass applied this candidate but its diff is gone
                # again, so the restore was undone; clear the stale marker
                # instead of skipping the path forever.
                candidate["restored"] = False
            patch = candidate.get("patch")
            if not isinstance(patch, str) or not patch.strip():
                continue
            if bool(getattr(self.config, "final_diff_salvage_veto", False)):
                # Vetoed candidates stay out of handled_paths on purpose: an
                # older, non-vetoed candidate for the same path may still be
                # worth salvaging.
                if candidate.get("lost") is True:
                    # The agent explicitly reverted this patch; resurrecting
                    # it would score edits the agent chose to abandon.
                    self._record_final_diff_salvage_event(
                        candidate,
                        trigger=trigger,
                        iteration=iteration,
                        action="vetoed_lost",
                    )
                    continue
                if is_instrumentation_only_patch(patch):
                    self._record_final_diff_salvage_event(
                        candidate,
                        trigger=trigger,
                        iteration=iteration,
                        action="vetoed_instrumentation",
                    )
                    continue
            if time.monotonic() >= deadline:
                self._record_final_diff_salvage_event(
                    candidate,
                    trigger=trigger,
                    iteration=iteration,
                    action="time_budget_exhausted",
                )
                break
            if not self._apply_final_diff_salvage_patch(workspace, patch, check_only=True):
                self._record_final_diff_salvage_event(
                    candidate, trigger=trigger, iteration=iteration, action="check_failed"
                )
                continue
            if not self._apply_final_diff_salvage_patch(workspace, patch, check_only=False):
                self._record_final_diff_salvage_event(
                    candidate, trigger=trigger, iteration=iteration, action="apply_failed"
                )
                continue
            candidate["restored"] = True
            handled_paths.add(path)
            applied.append(candidate)
            self._record_final_diff_salvage_event(
                candidate, trigger=trigger, iteration=iteration, action="applied"
            )
        if applied:
            self._write_turn_call_log(
                "turn_policy_decision",
                action="final_diff_salvage",
                reason=trigger,
                code="final_diff_salvage",
                iteration=iteration,
                candidate_ids=[candidate.get("candidate_id") for candidate in applied],
                paths=sorted(handled_paths),
            )
        return applied

    def _apply_final_diff_salvage_patch(
        self,
        workspace: Path,
        patch: str,
        *,
        check_only: bool,
    ) -> bool:
        args = ["git", "-C", str(workspace), "apply"]
        if check_only:
            args.append("--check")
        args.append("-")
        try:
            result = subprocess.run(
                args,
                input=patch,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _record_final_diff_salvage_event(
        self,
        candidate: dict[str, Any],
        *,
        trigger: str,
        iteration: int,
        action: str,
    ) -> None:
        event = {
            "feature": "final_diff_salvage",
            "name": f"final_diff_salvage.{action}",
            "action": action,
            "trigger": trigger,
            "iteration": iteration,
            "candidate_id": candidate.get("candidate_id"),
            "paths": list(candidate.get("paths", []) or []),
            "patch_sha256": candidate.get("patch_sha256"),
            "patch_chars": len(candidate.get("patch") or ""),
            "session_key": self._session_key,
            "agent_id": self.config.tool_result_store_agent_id
            or self.config.metadata.get("agent_id"),
        }
        append_runtime_event(self.config.runtime_events_path, event)

    def _record_patch_evidence_tool_result(
        self,
        *,
        iteration: int,
        tool_call: ToolCall,
        result: ToolResult,
        duration_ms: int,
    ) -> None:
        if self._patch_evidence_ledger is None:
            return
        result_text = self._tool_result_text_for_anchor(result.content)
        command = self._execution_command_for_progress(tool_call) or ""
        self._patch_evidence_ledger.record_tool_result(
            iteration=iteration,
            tool_name=tool_call.tool_name,
            arguments=tool_call.arguments,
            result_text=result_text,
            is_error=result.is_error,
            duration_ms=duration_ms,
            failure_anchors=self._failure_anchor_lines(result_text)
            if result.is_error or self._tool_result_has_failure_signal(result_text)
            else [],
            focused_verification=bool(
                command and self._command_looks_like_focused_verification(command)
            ),
        )

    async def _write_patch_evidence_ledger(
        self,
        *,
        final_status: str,
        iterations: int,
        provider_call_count: int,
    ) -> None:
        if self._patch_evidence_ledger is None:
            return
        try:
            await asyncio.to_thread(
                self._patch_evidence_ledger.write_final,
                read_records=self._workspace_read_records(),
                write_records=self._workspace_write_records(),
                scratch_records=self._scratch_write_records(),
                final_status=final_status,
                iterations=iterations,
                provider_call_count=provider_call_count,
            )
        except Exception as exc:  # noqa: BLE001
            self.config.metadata["patch_evidence_ledger_write_error"] = str(exc)[:300]

    def _workspace_dir_for_status(self) -> Path | None:
        ctx = self._tool_context or current_tool_context.get()
        workspace_dir = getattr(ctx, "workspace_dir", None) if ctx is not None else None
        if not workspace_dir:
            return None
        workspace = Path(workspace_dir).expanduser().resolve(strict=False)
        if not workspace.exists():
            return None
        return workspace

    def _scratch_verify_mirror_root(self) -> Path | None:
        ctx = self._tool_context or current_tool_context.get()
        scratch_dir = getattr(ctx, "scratch_dir", None) if ctx is not None else None
        if not scratch_dir:
            return None
        return (
            Path(scratch_dir).expanduser().resolve(strict=False)
            / _VERIFY_MIRROR_DIR_NAME
        )

    @staticmethod
    def _command_references_verify_mirror(command: str, mirror_root: Path) -> bool:
        if not command:
            return False
        if f"{_VERIFY_MIRROR_DIR_NAME}/" in command:
            return True
        return mirror_root.as_posix() in command

    @staticmethod
    def _git_head_blob(workspace: Path, relative_path: str) -> bytes | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace), "show", f"HEAD:{relative_path}"],
                capture_output=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def _scratch_verify_mirror_evidence_credit(self, command: str) -> bool:
        """Anti-weakening hash guard for scratch verify-mirror runs.

        A command that references the verify-mirror tree earns verification
        credit ONLY while every mirror file that shadows a workspace path is
        byte-identical to that workspace file (or to its HEAD blob when the
        workspace copy is gone). Mirror files with no counterpart in either
        place are the model's own new checks and stay allowed — they shadow
        nothing. Any unreadable or unverifiable state withholds credit: the
        guard must fail closed, not open.
        """

        mirror_root = self._scratch_verify_mirror_root()
        if mirror_root is None or not self._command_references_verify_mirror(
            command, mirror_root
        ):
            return True
        if not mirror_root.is_dir():
            return True
        workspace = self._workspace_dir_for_status()
        if workspace is None:
            return False
        checked = 0
        for mirror_file in sorted(mirror_root.rglob("*")):
            if not mirror_file.is_file():
                continue
            checked += 1
            if checked > _VERIFY_MIRROR_MAX_FILES:
                return False
            try:
                relative = mirror_file.relative_to(mirror_root)
            except ValueError:
                continue
            try:
                mirror_digest = hashlib.sha256(mirror_file.read_bytes()).digest()
            except OSError:
                return False
            original = workspace / relative
            if original.is_file():
                try:
                    original_digest = hashlib.sha256(original.read_bytes()).digest()
                except OSError:
                    return False
                if mirror_digest != original_digest:
                    return False
                continue
            head_blob = self._git_head_blob(workspace, relative.as_posix())
            if head_blob is None:
                # Tracked nowhere: a new check file, not a shadowed original.
                continue
            if mirror_digest != hashlib.sha256(head_blob).digest():
                return False
        return True

    async def _workspace_git_status_porcelain(self) -> str | None:
        workspace = self._workspace_dir_for_status()
        if workspace is None:
            return None

        def _run_status() -> str | None:
            try:
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(workspace),
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=2.0,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if result.returncode != 0:
                return None
            gitlink_paths = self._workspace_gitlink_paths(workspace)
            return self._filter_ignored_porcelain_status(result.stdout, gitlink_paths)

        return await asyncio.to_thread(_run_status)

    async def _workspace_submit_review_capture(self) -> tuple[str, str]:
        """Capture ``(per-file summary, unified diff)`` for the submit review.

        The per-file summary comes from ``git status`` (so untracked scratch
        files appear even though they are absent from ``git diff``); the diff
        body is ``git diff HEAD`` for tracked changes. Best-effort: any failure
        yields an empty diff and the review degrades to the summary alone.
        """
        workspace = self._workspace_dir_for_status()
        if workspace is None:
            return "", ""

        def _run_diff() -> str:
            try:
                result = subprocess.run(
                    ["git", "-C", str(workspace), "diff", "HEAD"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=4.0,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return ""
            if result.returncode != 0:
                return ""
            return result.stdout

        file_index = await self._workspace_git_status_porcelain() or ""
        diff_text = await asyncio.to_thread(_run_diff)
        return file_index, diff_text

    @staticmethod
    def _porcelain_status_code(line: str) -> str:
        if len(line) >= 2:
            return line[:2]
        return line

    @staticmethod
    def _porcelain_status_path(line: str) -> str | None:
        raw_status_line = line.rstrip()
        if not raw_status_line.strip():
            return None
        text = (
            raw_status_line[3:].strip()
            if len(raw_status_line) > 3
            else raw_status_line.strip()
        )
        if " -> " in text:
            text = text.split(" -> ", 1)[1].strip()
        return _normalize_workspace_relative_path(text) or None

    @staticmethod
    def _porcelain_status_is_new_file(line: str) -> bool:
        code = Agent._porcelain_status_code(line)
        return code == "??" or "A" in code

    @staticmethod
    def _porcelain_status_test_paths(status: str | None) -> list[str]:
        """Test-classified paths with a live diff, per porcelain-v1 status.

        Renames count both sides: moving a test file away still mutates the
        test tree. Scratch-classified paths never count even when their name
        looks test-like (classify_workspace_path puts the scratch check first
        only for the scratch directory; root scratch artifacts are already
        filtered out of the status upstream).
        """

        if not status:
            return []
        test_paths: list[str] = []
        for line in status.splitlines():
            if not line.strip():
                continue
            raw = line.rstrip()
            text = raw[3:].strip() if len(raw) > 3 else raw.strip()
            sides = (
                [side.strip() for side in text.split(" -> ", 1)]
                if " -> " in text
                else [text]
            )
            for side in sides:
                path = _normalize_workspace_relative_path(side)
                if not path:
                    continue
                if classify_workspace_path(path) != "test-like":
                    continue
                if path not in test_paths:
                    test_paths.append(path)
        return test_paths

    def _porcelain_status_protected_paths(self, status: str | None) -> list[str]:
        """Deny-glob-protected paths with a live diff, per porcelain-v1 status.

        The ``protected_paths`` hygiene mode reuses the deployment's
        workspace write-deny globs verbatim — the engine carries no path
        taxonomy of its own here, so whatever the configuration protects
        from writes is also what the final diff must leave untouched.
        Renames count both sides: moving a protected file away still
        mutates the protected tree.
        """

        if not status:
            return []
        workspace = self._workspace_dir_for_status()
        if workspace is None:
            return []
        ctx = self._tool_context or current_tool_context.get()
        if ctx is None or not getattr(ctx, "workspace_write_deny_globs", None):
            return []
        protected: list[str] = []
        for line in status.splitlines():
            if not line.strip():
                continue
            raw = line.rstrip()
            text = raw[3:].strip() if len(raw) > 3 else raw.strip()
            sides = (
                [side.strip() for side in text.split(" -> ", 1)]
                if " -> " in text
                else [text]
            )
            for side in sides:
                path = _normalize_workspace_relative_path(side)
                if not path:
                    continue
                match = match_workspace_write_deny(
                    workspace / path,
                    original_path=path,
                    workspace=workspace,
                    ctx=ctx,
                )
                if match is None:
                    continue
                if path not in protected:
                    protected.append(path)
        return protected

    @staticmethod
    def _is_root_scratch_artifact_path(path: str | None) -> bool:
        if not path:
            return False
        normalized = _normalize_workspace_relative_path(path)
        if not normalized or "/" in normalized:
            return False
        name = Path(normalized).name
        if name in _ROOT_SCRATCH_ARTIFACT_NAMES:
            return True
        suffix = Path(name).suffix.lower()
        if suffix not in _ROOT_SCRATCH_ARTIFACT_SUFFIXES:
            return False
        return any(name.startswith(prefix) for prefix in _ROOT_SCRATCH_ARTIFACT_PREFIXES)

    @staticmethod
    def _filter_ignored_porcelain_status(status: str, gitlink_paths: set[str]) -> str:
        if not status:
            return status
        kept: list[str] = []
        for line in status.splitlines():
            path = Agent._porcelain_status_path(line)
            if path and path in gitlink_paths:
                continue
            if (
                path
                and Agent._porcelain_status_is_new_file(line)
                and Agent._is_root_scratch_artifact_path(path)
            ):
                continue
            kept.append(line)
        if not kept:
            return ""
        return "\n".join(kept) + "\n"

    @staticmethod
    def _filter_gitlink_porcelain_status(status: str, gitlink_paths: set[str]) -> str:
        return Agent._filter_ignored_porcelain_status(status, gitlink_paths)

    @staticmethod
    def _workspace_gitlink_paths(workspace_dir: Path) -> set[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace_dir), "ls-files", "-s"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return set()
        if result.returncode != 0:
            return set()
        paths: set[str] = set()
        for line in (result.stdout or "").splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4 and parts[0] == "160000":
                paths.add(_normalize_workspace_relative_path(parts[3]))
        return paths

    def _workspace_ignored_diff_paths(self, workspace_dir: Path) -> set[str]:
        ignored = self._workspace_gitlink_paths(workspace_dir)
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace_dir),
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ignored
        if result.returncode != 0:
            return ignored
        for line in (result.stdout or "").splitlines():
            path = self._porcelain_status_path(line)
            if (
                path
                and self._porcelain_status_is_new_file(line)
                and self._is_root_scratch_artifact_path(path)
            ):
                ignored.add(path)
        return ignored

    async def _failed_tool_finalization_recovery_details(
        self,
        results: list[ToolResult],
        *,
        post_write_verification_failure: Mapping[str, Any] | None = None,
        post_write_verification_success_observed: bool = False,
        final_text: str = "",
    ) -> dict[str, Any] | None:
        status = await self._workspace_git_status_porcelain()
        if status is None or not status.strip():
            return None
        workspace_write_count = len(self._effective_workspace_write_records())
        mutation_receipt_counts = self._workspace_mutation_receipt_counts()
        post_write_progress_count = self._post_write_progress_count(
            workspace_write_count=workspace_write_count,
            mutation_receipt_counts=mutation_receipt_counts,
        )
        base_details: dict[str, Any] = {
            "workspace_write_count": workspace_write_count,
            **mutation_receipt_counts,
            "git_status_porcelain": status[:1000],
            "diff_fingerprint": self._workspace_diff_fingerprint_for_runtime_event(),
        }
        if post_write_verification_failure:
            details = {
                **base_details,
                **dict(post_write_verification_failure),
            }
            details["reason"] = "final_response_after_failed_focused_verification_with_diff"
            return details
        failed_result = next((result for result in reversed(results) if result.is_error), None)
        if failed_result is not None:
            execution_status: Mapping[str, Any] = failed_result.execution_status or {}
            status_reason = ""
            if isinstance(execution_status, Mapping):
                status_reason = str(execution_status.get("reason") or "")
            reason = (
                "final_response_after_masked_pipeline_failure_with_diff"
                if status_reason == "masked_pipeline_failure"
                else "final_response_after_failed_tool_with_diff"
            )
            result_text = self._tool_result_text_for_anchor(failed_result.content)
            failure_anchors = self._failure_anchor_lines(result_text)
            return {
                **base_details,
                "reason": reason,
                "tool_name": failed_result.tool_name,
                "execution_status_reason": status_reason or None,
                "failure_anchors": failure_anchors[:3],
            }
        if (
            post_write_progress_count > 0
            and not post_write_verification_success_observed
            and self._turn_likely_requires_workspace_change(final_text)
        ):
            return {
                **base_details,
                "reason": "final_response_without_successful_focused_verification",
                "tool_name": None,
                "execution_status_reason": None,
                "failure_anchors": [],
            }
        return None

    @staticmethod
    def _failed_tool_finalization_recovery_key(details: Mapping[str, Any]) -> str:
        key_payload = {
            "reason": details.get("reason"),
            "diff_fingerprint": details.get("diff_fingerprint"),
            "git_status_porcelain": details.get("git_status_porcelain"),
            "tool_name": details.get("tool_name"),
            "command": details.get("command"),
            "execution_status_reason": details.get("execution_status_reason"),
            "failure_anchors": details.get("failure_anchors"),
        }
        encoded = json.dumps(key_payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def _failed_tool_finalization_recovery_message(self, details: Mapping[str, Any]) -> str:
        reason = str(details.get("reason") or "")
        if reason == "final_response_after_failed_focused_verification_with_diff":
            command = str(details.get("command") or "a focused validation command").strip()
            command_text = f" Command: {command}." if command else ""
            status_reason = str(details.get("execution_status_reason") or "").strip()
            reason_text = f" Reason: {status_reason}." if status_reason else ""
            anchors = details.get("failure_anchors")
            anchor_text = ""
            if isinstance(anchors, list) and anchors:
                rendered = " | ".join(str(anchor) for anchor in anchors[:3] if anchor)
                if rendered:
                    anchor_text = f" Recent failure signal: {rendered}."
            return (
                "[Runtime progress warning]\n"
                "The model is about to finish after repository edits, but the latest "
                f"focused validation still failed.{command_text}{reason_text}"
                f"{anchor_text} Do not "
                "finalize this patch yet. Use the validation failure to revise the "
                "source diff, then rerun focused validation. If validation is impossible, "
                "explain the blocker after checking the changed files."
            )
        if reason == "final_response_without_successful_focused_verification":
            return (
                "[Runtime progress warning]\n"
                "The model is about to finish with repository edits before any focused "
                "validation command succeeded. Do not finalize yet. Run a focused "
                "validation command for the changed behavior, or explicitly explain why "
                "validation cannot be run after checking the changed files."
            )
        tool_name = str(details.get("tool_name") or "a tool")
        status_reason = str(details.get("execution_status_reason") or "").strip()
        reason_text = (
            f" Reason: {status_reason}."
            if status_reason
            else ""
        )
        anchors = details.get("failure_anchors")
        anchor_text = ""
        if isinstance(anchors, list) and anchors:
            rendered = " | ".join(str(anchor) for anchor in anchors[:3] if anchor)
            if rendered:
                anchor_text = f" Recent failure signal: {rendered}."
        pipeline_text = (
            " If the command used a shell pipeline, rerun validation with "
            "`set -o pipefail` or without `| head`/`| tail` before relying on it."
            if status_reason == "masked_pipeline_failure"
            else ""
        )
        return (
            "[Runtime progress warning]\n"
            "The model is about to finish while the latest tool result failed "
            f"after repository edits. Latest failed tool: {tool_name}.{reason_text}"
            f"{anchor_text} Do not finalize this patch yet. Fix the source diff or "
            "rerun a focused validation command that succeeds cleanly."
            f"{pipeline_text}"
        )

    async def _empty_diff_finalization_reason(self, final_text: str) -> str | None:
        status = await self._workspace_git_status_porcelain()
        if status is None:
            return None
        if self._effective_workspace_write_records() and status == "":
            return "workspace_writes_without_git_status_changes"
        if status == "" and self._turn_likely_requires_workspace_change(final_text):
            return "final_response_without_workspace_diff"
        return None

    def _empty_diff_recovery_message(self, reason: str) -> str:
        if reason == "workspace_writes_without_git_status_changes":
            return (
                "[Runtime progress warning]\n"
                "The model is about to finish after recording workspace write "
                "operations, but `git status --porcelain --untracked-files=all` "
                "currently shows no repository diff. Inspect the current files and "
                "tool results. If a code change is required, apply it to the real "
                "workspace source file now. If no diff is required, explicitly explain "
                "why the repository should remain unchanged."
            )
        return (
            "[Runtime progress warning]\n"
            "The model is about to finish a code-fix style task while the repository "
            "has no visible workspace diff. Do not provide another plan only. Inspect "
            "the relevant project files, make the smallest justified source edit if "
            "one is available, or explicitly explain the blocker and why an empty diff "
            "is correct."
        )

    def _turn_likely_requires_workspace_change(self, final_text: str) -> bool:
        final_lower = " ".join((final_text or "").lower().split())
        if any(marker in final_lower for marker in _NO_CHANGE_FINAL_MARKERS):
            return False
        turn_lower = " ".join((getattr(self, "_current_turn_message", "") or "").lower().split())
        combined = f"{turn_lower}\n{final_lower}"
        return any(marker in combined for marker in _CODE_CHANGE_TASK_MARKERS)

    @staticmethod
    def _workspace_edit_gate_details(
        reason: str,
        details: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if reason not in _NO_WORKSPACE_WRITE_REASONS:
            return None
        if not details.get("workspace_change_likely_required"):
            return None
        try:
            count = int(details.get("count", 0) or 0)
            threshold = int(details.get("threshold", 0) or 0)
        except (TypeError, ValueError):
            return None
        if threshold <= 0 or count < threshold * 2:
            return None
        return {
            "reason": reason,
            "count": count,
            "threshold": threshold,
            "iteration": details.get("iteration"),
            "provider_call_count": details.get("provider_call_count"),
        }

    def _resolve_workspace_path_candidate(self, raw_path: str) -> Path | None:
        workspace = self._workspace_dir_for_status()
        try:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute() and workspace is not None:
                candidate = workspace / candidate
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None
        if workspace is None:
            return resolved
        if resolved == workspace or workspace in resolved.parents:
            return resolved
        return None

    def _configured_scratch_path_candidate(
        self,
        raw_path: str | None,
        *,
        relative_to: Literal["scratch", "workspace"] | None = None,
    ) -> tuple[Path | None, bool]:
        """Return a contained path and whether it targets configured scratch."""

        if not raw_path:
            return None, False

        ctx = self._tool_context or current_tool_context.get()
        raw_scratch = getattr(ctx, "scratch_dir", None) if ctx is not None else None
        if not raw_scratch:
            return None, False
        try:
            scratch = Path(raw_scratch).expanduser()
            if not scratch.is_absolute():
                scratch = Path.cwd() / scratch

            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                if relative_to == "scratch":
                    candidate = scratch / candidate
                elif relative_to == "workspace":
                    workspace = self._workspace_dir_for_status()
                    if workspace is None:
                        return None, False
                    candidate = workspace / candidate
                else:
                    return None, False
        except (OSError, RuntimeError, ValueError):
            return None, False

        lexical_scratch_target = False
        try:
            lexical_relative = candidate.relative_to(scratch)
            lexical_scratch_target = True
        except ValueError:
            lexical_relative = None
        if lexical_relative is not None and (
            not lexical_relative.parts or ".." in lexical_relative.parts
        ):
            return None, True

        try:
            resolved_scratch = scratch.resolve(strict=False)
            resolved = candidate.resolve(strict=False)
            resolved_relative = resolved.relative_to(resolved_scratch)
        except (OSError, RuntimeError, ValueError):
            return None, lexical_scratch_target
        if not resolved_relative.parts:
            return None, True

        workspace = self._workspace_dir_for_status()
        if workspace is not None:
            try:
                resolved.relative_to(workspace)
            except ValueError:
                pass
            else:
                try:
                    scratch_relative = resolved_scratch.relative_to(workspace)
                except ValueError:
                    return None, False
                if not scratch_relative.parts:
                    return None, False

        return resolved, True

    def _workspace_edit_gate_external_scratch_repro_target(
        self,
        tc: ToolCall,
    ) -> tuple[Path | None, bool]:
        """Return an allowed repro target and whether the path claimed scratch."""

        if tc.tool_name not in {"edit_file", "write_file", "write_scratch"}:
            return None, False
        resolved, claimed_scratch = self._configured_scratch_path_candidate(
            self._tool_call_string_arg(tc, "path"),
            relative_to=("scratch" if tc.tool_name == "write_scratch" else "workspace"),
        )
        if resolved is None or not is_repro_script_path(str(resolved)):
            return None, claimed_scratch

        workspace = self._workspace_dir_for_status()
        if workspace is not None and (resolved == workspace or workspace in resolved.parents):
            return None, True
        return resolved, True

    def _workspace_edit_gate_apply_patch_text(self, tc: ToolCall) -> str | None:
        patch = self._tool_call_string_arg(tc, "patch")
        if patch and patch.strip():
            return patch
        raw_path = self._tool_call_string_arg(tc, "path")
        workspace = self._workspace_dir_for_status()
        if not raw_path or workspace is None:
            return None
        try:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                candidate = workspace / candidate
            resolved = candidate.resolve(strict=False)
            allowed_roots = [workspace]
            ctx = self._tool_context or current_tool_context.get()
            raw_scratch = getattr(ctx, "scratch_dir", None) if ctx is not None else None
            if raw_scratch:
                allowed_roots.append(Path(raw_scratch).expanduser().resolve(strict=False))
            if not any(resolved.is_relative_to(root) for root in allowed_roots):
                return None
            open_flags = os.O_RDONLY
            open_flags |= getattr(os, "O_NONBLOCK", 0)
            open_flags |= getattr(os, "O_CLOEXEC", 0)
            open_flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(resolved, open_flags)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    return None
                with os.fdopen(fd, encoding="utf-8") as patch_file:
                    fd = -1
                    return patch_file.read()
            finally:
                if fd >= 0:
                    os.close(fd)
        except (OSError, RuntimeError, UnicodeError, ValueError):
            return None

    def _snapshot_apply_patch_path_call(self, tc: ToolCall) -> ToolCall:
        """Bind a path-mode patch to the exact text used by this execution."""

        if tc.tool_name != "apply_patch":
            return tc
        inline_patch = self._tool_call_string_arg(tc, "patch")
        if inline_patch and inline_patch.strip():
            return tc
        if self._tool_call_string_arg(tc, "path") is None:
            return tc
        patch = self._workspace_edit_gate_apply_patch_text(tc)
        if patch is None or not patch.strip():
            return tc
        arguments = dict(tc.arguments)
        arguments["patch"] = patch
        return replace(tc, arguments=arguments)

    def _workspace_edit_gate_apply_patch_raw_target_paths(self, tc: ToolCall) -> list[str]:
        patch = self._workspace_edit_gate_apply_patch_text(tc)
        if not patch:
            return []
        paths: list[str] = []
        in_patch = False
        prefixes = (
            "*** Add File: ",
            "*** Update File: ",
            "*** Delete File: ",
        )
        for raw_line in patch.splitlines():
            line = raw_line.rstrip("\r")
            marker = line.strip()
            if marker == "*** Begin Patch":
                in_patch = True
                continue
            if marker == "*** End Patch":
                break
            if not in_patch:
                continue
            for prefix in prefixes:
                if line.startswith(prefix):
                    raw_path = line.removeprefix(prefix).strip()
                    if raw_path:
                        paths.append(raw_path)
                    break
        return paths

    def _finalize_evidence_write_targets(
        self,
        tc: ToolCall,
    ) -> list[tuple[str | None, bool]]:
        if tc.tool_name == "apply_patch":
            patch_targets = self._workspace_edit_gate_apply_patch_raw_target_paths(tc)
            if patch_targets:
                return [
                    (
                        raw_path,
                        self._configured_scratch_path_candidate(
                            raw_path,
                            relative_to="workspace",
                        )[0]
                        is not None,
                    )
                    for raw_path in patch_targets
                ]
            # A successful apply_patch with unknown targets must invalidate prior
            # verification instead of treating its input patch file as a write.
            return [(None, False)]

        raw_path = self._tool_call_string_arg(tc, "path", "file_path")
        configured_scratch_path: Path | None = None
        if tc.tool_name in {"edit_file", "edit_source", "write_file", "write_scratch"}:
            configured_scratch_path, _ = self._configured_scratch_path_candidate(
                raw_path,
                relative_to=("scratch" if tc.tool_name == "write_scratch" else "workspace"),
            )
        return [
            (
                raw_path,
                tc.tool_name == "write_scratch" or configured_scratch_path is not None,
            )
        ]

    def _workspace_edit_gate_apply_patch_target_paths(self, tc: ToolCall) -> list[Path]:
        paths: list[Path] = []
        seen: set[Path] = set()
        for raw_path in self._workspace_edit_gate_apply_patch_raw_target_paths(tc):
            resolved = self._resolve_workspace_path_candidate(raw_path)
            if resolved is not None and resolved not in seen:
                seen.add(resolved)
                paths.append(resolved)
        return paths

    def _workspace_edit_gate_target_paths(self, tc: ToolCall) -> list[Path]:
        if tc.tool_name == "apply_patch":
            return self._workspace_edit_gate_apply_patch_target_paths(tc)
        if tc.tool_name not in {"edit_file", "write_file"}:
            return []
        raw_path = self._tool_call_string_arg(tc, "path")
        if raw_path is None:
            return []
        resolved = self._resolve_workspace_path_candidate(raw_path)
        return [] if resolved is None else [resolved]

    def _workspace_edit_gate_edit_block_detail(self, tc: ToolCall) -> str | None:
        if tc.tool_name == "apply_patch":
            if any(
                self._configured_scratch_path_candidate(
                    raw_path,
                    relative_to="workspace",
                )[1]
                for raw_path in self._workspace_edit_gate_apply_patch_raw_target_paths(tc)
            ):
                return (
                    "The apply_patch call targets configured scratch. Scratch files do "
                    "not count as the requested project source fix."
                )
            if self._workspace_edit_gate_apply_patch_target_paths(tc):
                return None
            return (
                "The apply_patch call targets '<missing or non-workspace patch target>'. "
                "apply_patch must use an exact wrapper line '*** Begin Patch' followed "
                "by a file operation line such as '*** Update File: path/to/source.ext', "
                "then '@@' hunks, then '*** End Patch'. Do not put the path on the "
                "Begin Patch or End Patch line."
            )
        if tc.tool_name not in {"edit_file", "write_file", "write_scratch"}:
            return None
        scratch_target, claimed_scratch = (
            self._workspace_edit_gate_external_scratch_repro_target(tc)
        )
        if tc.tool_name == "write_scratch":
            if scratch_target is not None:
                return None
            return (
                "The write_scratch call must target a contained executable reproduction "
                "script under an external scratch directory before the source fix."
            )
        if claimed_scratch:
            return (
                f"The {tc.tool_name} call targets configured scratch, but only a "
                "contained executable reproduction script under an external scratch "
                "directory may be written before the source fix."
            )
        raw_path = self._tool_call_string_arg(tc, "path") or "<missing path>"
        resolved = self._resolve_workspace_path_candidate(raw_path)
        if resolved is None:
            return (
                f"The {tc.tool_name} call targets {raw_path!r}, which is not a real "
                "file under the project workspace."
            )
        if tc.tool_name == "write_file" and self._workspace_edit_gate_write_looks_synthetic(
            tc, resolved
        ):
            return (
                f"The write_file call creates {raw_path!r}, which looks like a temporary "
                "marker or guard-unlock file rather than the requested source fix."
            )
        return None

    def _workspace_edit_gate_write_looks_synthetic(
        self,
        tc: ToolCall,
        resolved_path: Path,
    ) -> bool:
        if resolved_path.exists():
            return False
        name = resolved_path.name.lower()
        suspicious_name = any(
            name == prefix
            or name.startswith(f"{prefix}.")
            or name.startswith(f"{prefix}_")
            for prefix in _SUSPICIOUS_NEW_WORKSPACE_WRITE_PREFIXES
        )
        content = (self._tool_call_string_arg(tc, "content") or "").lower()
        suspicious_content = any(
            marker in content
            for marker in _SUSPICIOUS_NEW_WORKSPACE_WRITE_CONTENT_MARKERS
        )
        return suspicious_name or suspicious_content

    def _tool_call_targets_workspace_path(self, tc: ToolCall) -> bool:
        if tc.tool_name not in _WORKSPACE_EDIT_TOOL_NAMES:
            return False
        return self._workspace_edit_gate_edit_block_detail(tc) is None

    def _workspace_edit_gate_allows_recovery_read(
        self,
        tc: ToolCall,
        recovery_read_paths: set[str],
    ) -> bool:
        if tc.tool_name != "read_file" or not recovery_read_paths:
            return False
        raw_path = self._tool_call_string_arg(tc, "path")
        if raw_path is None:
            return False
        resolved = self._resolve_workspace_path_candidate(raw_path)
        return resolved is not None and str(resolved) in recovery_read_paths

    def _workspace_edit_gate_apply_patch_error_allows_read(self, result: ToolResult) -> bool:
        return self._workspace_edit_gate_edit_error_allows_read(result)

    def _workspace_edit_gate_edit_error_allows_read(self, result: ToolResult) -> bool:
        if not result.is_error:
            return False
        text = self._tool_result_text_for_anchor(result.content).lower()
        return (
            "context mismatch" in text
            or "could not find old_text" in text
            or "read the current file content" in text
        )

    def _workspace_edit_gate_tool_result(
        self,
        tc: ToolCall,
        gate_details: Mapping[str, Any] | None,
        *,
        recovery_read_paths: set[str],
        recovery_reads_remaining: int,
    ) -> ToolResult | None:
        if gate_details is None:
            return None
        if (
            recovery_reads_remaining > 0
            and self._workspace_edit_gate_allows_recovery_read(tc, recovery_read_paths)
        ):
            return None
        scratch_target, _ = self._workspace_edit_gate_external_scratch_repro_target(tc)
        if scratch_target is not None:
            return None
        gate_write_tool = (
            tc.tool_name in _WORKSPACE_EDIT_TOOL_NAMES or tc.tool_name == "write_scratch"
        )
        edit_block_detail = (
            self._workspace_edit_gate_edit_block_detail(tc)
            if gate_write_tool
            else None
        )
        if gate_write_tool and edit_block_detail is None:
            return None

        if gate_write_tool:
            detail = edit_block_detail or f"The {tc.tool_name} call is not allowed here."
        elif tc.tool_name == "read_file" and recovery_reads_remaining > 0:
            detail = (
                "Only the file targeted by the failed edit call may be read "
                "during this recovery step."
            )
        else:
            return None
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content=(
                "Runtime guard: this code-fix task appears to require a repository "
                "patch, but no tracked workspace source file has changed yet. "
                f"{detail} Use targeted source reads/searches only when needed to "
                "identify the exact edit. Do not write scratch notes as a substitute "
                "for a real source change; once localized, use an available "
                "source-edit tool on a real project source file allowed by the "
                "workspace write policy."
            ),
            is_error=True,
            execution_status=runtime_execution_status(
                "error",
                reason="workspace_edit_required",
            ),
        )

    @staticmethod
    def _workspace_edit_gate_tool_definitions(
        tools: list[ToolDefinition] | None,
        gate_details: Mapping[str, Any] | None,
        *,
        recovery_read_paths: set[str],
        recovery_reads_remaining: int,
    ) -> list[ToolDefinition] | None:
        if gate_details is None or not tools:
            return tools
        return tools

    def _workspace_edit_gate_system_prompt(
        self,
        system_prompt: str | None,
        gate_details: Mapping[str, Any] | None,
        *,
        recovery_read_paths: set[str],
        recovery_reads_remaining: int,
    ) -> str | None:
        if gate_details is None:
            return system_prompt
        workspace = self._workspace_dir_for_status()
        workspace_text = str(workspace) if workspace is not None else "the project workspace"
        if recovery_reads_remaining > 0 and recovery_read_paths:
            allowed_paths = ", ".join(sorted(recovery_read_paths))
            action_text = (
                "A previous source edit failed because its file context did not match. "
                f"Prioritize a targeted source read for the failed edit target path(s): "
                f"{allowed_paths}. After that targeted read, use an available "
                "source-edit tool on the real project source file."
            )
        else:
            action_text = (
                "Avoid more scratch-only work. If you can form a patch from the "
                "context already present in the conversation, use an available "
                "source-edit tool now; otherwise use targeted source reads/searches "
                "to localize the edit."
            )
        restriction = (
            "## Runtime Patch Progress Guidance\n\n"
            "This request still has no tracked source diff after repeated tool "
            f"activity. {action_text} Make the "
            f"smallest edit to a real project source file under {workspace_text} "
            "that is allowed by the workspace write policy. Do not edit tests unless "
            "the original user explicitly asked for test changes."
        )
        if not system_prompt:
            return restriction
        return f"{system_prompt.rstrip()}\n\n{restriction}"

    def _workspace_edit_gate_chat_config(
        self,
        chat_cfg: ChatConfig,
        gate_details: Mapping[str, Any] | None,
        tools: list[ToolDefinition] | None,
        *,
        recovery_read_paths: set[str],
        recovery_reads_remaining: int,
    ) -> ChatConfig:
        if gate_details is None:
            return chat_cfg
        update: dict[str, Any] = {
            "system": self._workspace_edit_gate_system_prompt(
                chat_cfg.system,
                gate_details,
                recovery_read_paths=recovery_read_paths,
                recovery_reads_remaining=recovery_reads_remaining,
            )
        }
        return chat_cfg.model_copy(update=update)

    def _execution_command_for_progress(self, tc: ToolCall) -> str | None:
        if tc.tool_name == "execute_code":
            return self._tool_call_string_arg(tc, "code")
        return self._tool_call_string_arg(tc, "command", "cmd")

    def _command_looks_like_focused_verification(self, command: str) -> bool:
        normalized = " " + " ".join((command or "").lower().split())
        return any(marker in normalized for marker in _FOCUSED_VERIFICATION_MARKERS)

    def _source_context_signature(
        self,
        tool_calls: list[ToolCall],
        results: list[ToolResult],
    ) -> str | None:
        signatures: list[str] = []
        for tc, result in zip(tool_calls, results, strict=False):
            if result.is_error:
                continue
            command = self._tool_call_string_arg(tc, "command", "cmd")
            is_source_context_tool = tc.tool_name in _SOURCE_CONTEXT_TOOL_NAMES
            is_exec_source_context = (
                tc.tool_name == "exec_command"
                and exec_command_invokes_source_context_read(
                    command,
                    content=result.content,
                )
            )
            if not is_source_context_tool and not is_exec_source_context:
                continue
            payload = json.dumps(
                tc.arguments,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            signatures.append(f"{tc.tool_name}:{command or ''}:{payload}")
        if not signatures:
            return None
        joined = "\n".join(signatures)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    @staticmethod
    def _tool_call_repeat_key(tc: ToolCall) -> tuple[str, str]:
        payload = json.dumps(
            tc.arguments,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return (tc.tool_name, hashlib.sha256(payload.encode("utf-8")).hexdigest())

    def _repeated_tool_call_recovery_tool_names(self) -> frozenset[str]:
        extra_tools = (
            getattr(self.config, "repeated_tool_call_recovery_extra_tools", None) or ()
        )
        if not extra_tools:
            return _REPEATED_TOOL_CALL_RECOVERY_TOOL_NAMES
        return _REPEATED_TOOL_CALL_RECOVERY_TOOL_NAMES | {
            str(name) for name in extra_tools
        }

    @staticmethod
    def _tool_call_arguments_preview(tc: ToolCall, *, max_chars: int = 400) -> str:
        payload = json.dumps(
            tc.arguments,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if len(payload) <= max_chars:
            return payload
        return payload[: max(0, max_chars - 3)] + "..."

    def _repeated_tool_call_recovery_message(
        self,
        tc: ToolCall,
        *,
        repeat_count: int,
    ) -> str:
        arguments_preview = self._tool_call_arguments_preview(tc)
        return (
            "[Runtime recovery]\n"
            f"The exact same {tc.tool_name} tool call has been requested "
            f"{repeat_count} times in a row with identical arguments. I skipped "
            "executing and replaying that duplicate call to avoid provider-side "
            "rejection for repetitive tool history. Do not call this exact tool "
            "with the same arguments again. Change the path, pattern, command, or "
            "arguments; inspect a different source window; use a different tool; "
            "or move to the patch/final answer if you already have enough evidence.\n"
            f"Repeated arguments: {arguments_preview}"
        )

    def _record_tool_context_runtime_event(self, event: dict[str, Any]) -> None:
        if not self.config.runtime_events_path:
            return
        payload = dict(event)
        if payload.get("session_key") is None:
            payload["session_key"] = self._session_key
        if payload.get("agent_id") is None:
            payload["agent_id"] = (
                self.config.tool_result_store_agent_id
                or self.config.metadata.get("agent_id")
            )
        append_runtime_event(self.config.runtime_events_path, payload)

    def _record_runtime_event(self, name: str, **details: Any) -> None:
        if not self.config.runtime_events_path:
            return
        event = {
            "name": name,
            "session_key": self._session_key,
            "agent_id": self.config.tool_result_store_agent_id
            or self.config.metadata.get("agent_id"),
            **details,
        }
        append_runtime_event(self.config.runtime_events_path, event)

    def _record_tool_loop_runtime_event(self, *, reason: str, **details: Any) -> None:
        if self.config.tool_loop_observer_mode != "log":
            return
        iteration = details.get("iteration")
        hint_text_sha256 = details.pop("hint_text_sha256", None)
        trigger_confidence = details.pop("trigger_confidence", "observed_runtime_signal")
        event = {
            "feature": "runtime_observer",
            "mechanism": "tool_loop_observer",
            "mode": self.config.tool_loop_observer_mode,
            "reason": reason,
            "iteration": int(iteration) if isinstance(iteration, int) else iteration,
            "session_key": self._session_key,
            "agent_id": self.config.tool_result_store_agent_id
            or self.config.metadata.get("agent_id"),
            "injected_to_model": bool(details.pop("injected_to_model", False)),
            "evidence": details,
            "read_files": self._relative_paths_from_records(self._workspace_read_records()),
            "changed_files": self._relative_paths_from_records(self._workspace_write_records()),
            "diff_paths": self._workspace_diff_paths_for_runtime_event(),
            "verification_commands": self._verification_commands_for_runtime_event(),
            "hint_text_sha256": hint_text_sha256,
            "trigger_confidence": trigger_confidence,
            "details": details,
        }
        append_runtime_event(self.config.runtime_events_path, event)

    def _record_runtime_recovery_event(
        self,
        decision: RuntimeRecoveryDecision,
        *,
        iteration: int,
        provider_call_count: int,
        call_attempt: int | None = None,
        **details: Any,
    ) -> None:
        hint_text_sha256 = (
            hashlib.sha256(decision.message.encode("utf-8")).hexdigest()
            if decision.message
            else None
        )
        evidence = {
            **decision.details,
            **details,
        }
        event = {
            "feature": "runtime_recovery",
            "mechanism": decision.mechanism,
            "mode": decision.mode,
            "reason": decision.reason,
            "action": decision.action,
            "iteration": iteration,
            "provider_call_count": provider_call_count,
            "call_attempt": call_attempt,
            "session_key": self._session_key,
            "agent_id": self.config.tool_result_store_agent_id
            or self.config.metadata.get("agent_id"),
            "injected_to_model": decision.injected_to_model,
            "evidence": evidence,
            "read_files": self._relative_paths_from_records(self._workspace_read_records()),
            "changed_files": self._relative_paths_from_records(self._workspace_write_records()),
            "diff_paths": self._workspace_diff_paths_for_runtime_event(),
            "verification_commands": self._verification_commands_for_runtime_event(),
            "hint_text_sha256": hint_text_sha256,
            "trigger_confidence": "runtime_recovery_gate",
            "details": evidence,
        }
        append_runtime_event(self.config.runtime_events_path, event)

    def _record_post_write_convergence_event(
        self,
        decision: PostWriteConvergenceDecision,
        *,
        mode: str,
        injected_to_model: bool,
        hint_text: str | None = None,
    ) -> None:
        event_name = {
            "warn": "post_write_convergence.warned",
            "finalize": "post_write_convergence.finalized",
            "reset": "post_write_convergence.reset_on_diff_change",
        }.get(decision.action)
        if event_name is None:
            return
        evidence = dict(decision.details)
        event = {
            "feature": "post_write_convergence",
            "mechanism": "stable_verified_workspace_diff",
            "name": event_name,
            "mode": mode,
            "reason": decision.reason,
            "action": decision.action,
            "iteration": evidence.get("iteration"),
            "provider_call_count": evidence.get("provider_call_count"),
            "session_key": self._session_key,
            "agent_id": self.config.tool_result_store_agent_id
            or self.config.metadata.get("agent_id"),
            "injected_to_model": injected_to_model,
            "evidence": evidence,
            "read_files": self._relative_paths_from_records(self._workspace_read_records()),
            "changed_files": self._relative_paths_from_records(self._workspace_write_records()),
            "diff_paths": self._workspace_diff_paths_for_runtime_event(),
            "verification_commands": self._verification_commands_for_runtime_event(),
            "hint_text_sha256": (
                hashlib.sha256(hint_text.encode("utf-8")).hexdigest()
                if hint_text
                else None
            ),
            "trigger_confidence": "post_write_convergence_gate",
            "details": evidence,
        }
        append_runtime_event(self.config.runtime_events_path, event)

    @staticmethod
    def _relative_paths_from_records(records: list[dict[str, Any]]) -> list[str]:
        paths: list[str] = []
        seen: set[str] = set()
        for record in records:
            raw = record.get("relative_path")
            if not isinstance(raw, str) or not raw:
                continue
            normalized = _normalize_workspace_relative_path(raw)
            if normalized and normalized not in seen:
                seen.add(normalized)
                paths.append(normalized)
        return paths

    def _workspace_diff_paths_for_runtime_event(self) -> list[str]:
        workspace_dir = self._workspace_dir_for_status()
        if workspace_dir is None:
            return []
        ignored_paths = self._workspace_ignored_diff_paths(workspace_dir)
        paths: set[str] = set()
        for args in (
            ("diff", "--name-only"),
            ("diff", "--cached", "--name-only"),
            ("status", "--porcelain=v1", "--untracked-files=all"),
        ):
            try:
                result = subprocess.run(
                    ["git", "-C", str(workspace_dir), *args],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=2.0,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            for line in (result.stdout or "").splitlines():
                if args[0] == "status":
                    text = self._porcelain_status_path(line) or ""
                else:
                    text = line.strip()
                if text:
                    normalized = _normalize_workspace_relative_path(text)
                    if normalized in ignored_paths:
                        continue
                    paths.add(normalized)
        return sorted(paths)

    def _workspace_diff_paths_for_final_diff_contract(
        self, *, include_untracked: bool = True
    ) -> list[str]:
        workspace_dir = self._workspace_dir_for_status()
        if workspace_dir is None:
            return []
        ignored_paths = self._workspace_gitlink_paths(workspace_dir) | (
            self._workspace_internal_diagnostic_paths(workspace_dir)
        )
        commands: tuple[tuple[str, ...], ...] = (
            ("diff", "--name-only"),
            ("diff", "--cached", "--name-only"),
        )
        if include_untracked:
            commands += (("status", "--porcelain=v1", "--untracked-files=all"),)
        paths: set[str] = set()
        for args in commands:
            try:
                result = subprocess.run(
                    ["git", "-C", str(workspace_dir), *args],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=2.0,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            for line in (result.stdout or "").splitlines():
                if args[0] == "status":
                    text = self._porcelain_status_path(line) or ""
                else:
                    text = line.strip()
                if text:
                    normalized = _normalize_workspace_relative_path(text)
                    if normalized in ignored_paths:
                        continue
                    paths.add(normalized)
        return sorted(paths)

    def _workspace_internal_diagnostic_paths(self, workspace_dir: Path) -> set[str]:
        ignored: set[str] = set()
        for raw_path in (
            self.config.runtime_events_path,
            self.config.patch_evidence_ledger_path,
        ):
            if not raw_path:
                continue
            try:
                relative = Path(raw_path).expanduser().resolve(strict=False).relative_to(
                    workspace_dir
                )
            except ValueError:
                continue
            ignored.add(relative.as_posix())
        return ignored

    def _workspace_diff_fingerprint_for_runtime_event(self) -> str | None:
        workspace_dir = self._workspace_dir_for_status()
        if workspace_dir is None:
            return None
        diff_paths = self._workspace_diff_paths_for_runtime_event()
        if not diff_paths:
            return None
        payload_parts: list[str] = []
        for args in (
            ("diff", "--no-ext-diff", "--binary", "--", *diff_paths),
            ("diff", "--cached", "--no-ext-diff", "--binary", "--", *diff_paths),
            ("status", "--porcelain=v1", "--untracked-files=all"),
        ):
            try:
                result = subprocess.run(
                    ["git", "-C", str(workspace_dir), *args],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=2.0,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            payload_parts.append(f"$ git {' '.join(args)}\n")
            stdout = result.stdout or ""
            if args[0] == "status":
                stdout = self._filter_gitlink_porcelain_status(
                    stdout,
                    self._workspace_ignored_diff_paths(workspace_dir),
                )
            payload_parts.append(stdout)
            if result.stderr:
                payload_parts.append("\n[stderr]\n")
                payload_parts.append(result.stderr)
        payload = "\n".join(payload_parts)
        if not payload.strip():
            return None
        return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]

    def _verification_commands_for_runtime_event(self) -> list[dict[str, Any]]:
        ledger = self._patch_evidence_ledger
        if ledger is None:
            return []
        commands = getattr(ledger, "verification_commands", []) or []
        return [dict(command) for command in commands if isinstance(command, dict)]

    @staticmethod
    def _failure_anchor_summary_from_tool_results(
        tool_calls: list[ToolCall],
        results: list[ToolResult],
    ) -> str:
        summaries: list[str] = []
        for tool_call, result in zip(tool_calls, results, strict=False):
            content = Agent._tool_result_text_for_anchor(result.content)
            if not content:
                continue
            if not result.is_error and not Agent._tool_result_has_failure_signal(content):
                continue
            anchors = Agent._failure_anchor_lines(content)
            if not anchors:
                continue
            summaries.append(f"{tool_call.tool_name}: " + " | ".join(anchors[:3]))
            if len(summaries) >= 3:
                break
        return "\n".join(summaries)

    @staticmethod
    def _tool_result_text_for_anchor(content: object) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, ContentBlockText):
                    parts.append(item.text)
                elif isinstance(item, Mapping):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                else:
                    parts.append(str(item))
            return "\n".join(parts)
        return str(content)

    @staticmethod
    def _tool_result_has_failure_signal(text: str) -> bool:
        return bool(Agent._failure_anchor_lines(text))

    @staticmethod
    def _tool_result_has_validation_success_signal(text: str) -> bool:
        lowered = (text or "").lower()
        if not lowered:
            return False
        if "build failure" in lowered or "failed to execute goal" in lowered:
            return False
        return (
            "build success" in lowered
            or "all tests passed" in lowered
            or bool(_CLEAN_TEST_SUMMARY_RE.search(text))
            or bool(_CLEAN_PASSED_FAILED_SUMMARY_RE.search(text))
        )

    @staticmethod
    def _classify_focused_verification_result(result: ToolResult) -> str:
        text = Agent._tool_result_text_for_anchor(result.content)
        if result.is_error or Agent._tool_result_has_failure_signal(text):
            return "failure"
        if (
            Agent._tool_result_has_validation_success_signal(text)
            or _PLAIN_PASSED_SUMMARY_RE.search(text)
        ):
            return "success"
        return "unknown"

    @staticmethod
    def _failure_anchor_lines(text: str) -> list[str]:
        anchors: list[str] = []
        for raw_line in text.splitlines():
            line = " ".join(raw_line.strip().split())
            if not line:
                continue
            lowered = line.lower()
            if (
                _CLEAN_TEST_SUMMARY_RE.search(line)
                or _CLEAN_PASSED_FAILED_SUMMARY_RE.search(line)
                or _CLEAN_ERROR_COUNT_RE.search(line)
                or "no failures" in lowered
                or "no errors" in lowered
            ):
                continue
            if not any(
                marker in lowered
                for marker in (
                    "failed",
                    "failure",
                    "error",
                    "exception",
                    "traceback",
                    "assert",
                    "expected",
                    "actual",
                )
            ):
                continue
            anchors.append(line[:220])
            if len(anchors) >= 6:
                break
        return anchors

    @staticmethod
    def _failure_anchor_signature(summary: str) -> str | None:
        normalized = " ".join((summary or "").strip().lower().split())
        if not normalized:
            return None
        for marker in ("/tmp/", "/var/tmp/"):
            if marker in normalized:
                normalized = normalized.replace(marker, f"{marker}<path>/")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _tool_error_signature(result: ToolResult) -> str:
        tool_name = str(result.tool_name or "tool")
        status: Mapping[str, Any] = result.execution_status or {}
        reason = str(status.get("reason") or "")
        source = str(status.get("source") or "")
        if tool_name in {"apply_patch", "edit_file", "write_file"} and (
            reason in {"retryable_tool_input_error", "invalid_arguments"}
            or "input" in reason
            or source == "tool_runtime"
        ):
            return f"{tool_name}:input_error"
        return f"{tool_name}:{str(result.content)[:160]}"

    async def _stream_provider_events_with_deadline(
        self,
        stream: AsyncIterator[Any],
        *,
        loop: asyncio.AbstractEventLoop,
        total_deadline: float | None,
    ) -> AsyncIterator[Any]:
        stream_iter = stream.__aiter__()
        while True:
            wait_budget = max(0.001, self.config.iteration_timeout)
            total_deadline_limits_wait = False
            if total_deadline is not None:
                remaining_total = total_deadline - loop.time()
                if remaining_total <= 0:
                    await self._close_provider_stream(stream_iter)
                    raise TimeoutError(f"Agent total timeout after {self.config.timeout}s")
                if remaining_total <= wait_budget:
                    wait_budget = remaining_total
                    total_deadline_limits_wait = True

            next_event: asyncio.Future[Any] = asyncio.ensure_future(stream_iter.__anext__())
            try:
                done, _ = await asyncio.wait({next_event}, timeout=wait_budget)
            except (asyncio.CancelledError, GeneratorExit):
                next_event.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                    await next_event
                raise
            if not done:
                next_event.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                    await next_event
                if total_deadline_limits_wait or (
                    total_deadline is not None and loop.time() >= total_deadline
                ):
                    raise TimeoutError(f"Agent total timeout after {self.config.timeout}s")
                raise _IterationStreamTimeoutError
            try:
                yield next_event.result()
            except StopAsyncIteration:
                return

    @staticmethod
    async def _close_provider_stream(stream_iter: AsyncIterator[Any]) -> None:
        aclose = getattr(stream_iter, "aclose", None)
        if not callable(aclose):
            return
        try:
            await aclose()
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask timeout
            logger.debug("provider_stream.close_failed", error=str(exc))

    def _provider_request_messages(
        self,
        messages: list[Message],
        *,
        request_context_message: Message | None,
        request_context_insert_index: int,
        runtime_context_message: Message,
        runtime_context_insert_index: int,
        turn_objective_message: Message | None = None,
    ) -> list[Message]:
        request_messages, _ = self._provider_request_messages_with_sanitize(
            messages,
            request_context_message=request_context_message,
            request_context_insert_index=request_context_insert_index,
            runtime_context_message=runtime_context_message,
            runtime_context_insert_index=runtime_context_insert_index,
            turn_objective_message=turn_objective_message,
        )
        return request_messages

    def _provider_request_messages_with_sanitize(
        self,
        messages: list[Message],
        *,
        request_context_message: Message | None,
        request_context_insert_index: int,
        runtime_context_message: Message,
        runtime_context_insert_index: int,
        turn_objective_message: Message | None = None,
        preview: bool = False,
    ) -> tuple[list[Message], SessionSanitizeResult]:
        capsule_message = self._runtime_state_capsule_provider_message(preview=preview)
        if capsule_message is not None:
            messages = [*messages, capsule_message]
        source_messages = self._with_request_context_messages(
            messages,
            request_context_message,
            request_context_insert_index,
            runtime_context_message,
            runtime_context_insert_index,
            turn_objective_message=turn_objective_message,
        )
        source_messages = self._apply_provider_tool_result_overrides(source_messages)
        source_messages = self._strip_provider_context_marker_replay_for_provider(
            source_messages,
            record=not preview,
        )
        # These provider projections replace content in place and therefore do
        # not affect message cardinality.  Skip them during a count preview:
        # the normal paths update metadata, logs, and snapshot stores.
        if not preview:
            source_messages = self._dedup_repeated_tool_results_for_provider(source_messages)
            source_messages = self._compact_aggregate_tool_results_for_provider(source_messages)
        source_messages = self._sanitize_projected_tool_use_arguments_for_provider(
            source_messages,
            record=not preview,
        )
        source_messages = repair_tool_pairing(source_messages)
        request_messages, sanitize_result = sanitize_session_messages(source_messages)
        if not preview:
            self._remember_provider_visible_tool_results(request_messages)
        return request_messages, sanitize_result

    def _runtime_state_capsule_provider_message(self, *, preview: bool = False) -> Message | None:
        mode = str(getattr(self.config, "runtime_state_capsule_mode", "off") or "off")
        if mode not in {"log", "inject"}:
            return None
        if preview:
            # Count projection needs the exact assembly rule, not capsule
            # contents.  Avoid filesystem reads, metadata writes, and runtime
            # events while preserving the one-message injection cardinality.
            return (
                Message(role="user", content="[runtime state capsule preview]")
                if mode == "inject"
                else None
            )
        ctx = self._tool_context or current_tool_context.get()
        workspace = (
            getattr(ctx, "workspace_dir", None)
            if ctx is not None and getattr(ctx, "workspace_dir", None)
            else self.config.workspace_dir
        )
        capsule = build_runtime_state_capsule(workspace=workspace, tool_context=ctx)
        self.config.metadata["runtime_state_capsule_observed"] = (
            self.config.metadata.get("runtime_state_capsule_observed", 0) + 1
        )
        self._record_runtime_event(
            "runtime_state_capsule.observed",
            feature="runtime_state_capsule",
            mode=mode,
            injected_to_model=mode == "inject",
            capsule=capsule,
        )
        if mode != "inject":
            return None
        self.config.metadata["runtime_state_capsule_injected"] = (
            self.config.metadata.get("runtime_state_capsule_injected", 0) + 1
        )
        return Message(role="user", content=runtime_state_capsule_message(capsule))

    async def _provider_request_messages_with_sanitize_async(
        self,
        messages: list[Message],
        *,
        request_context_message: Message | None,
        request_context_insert_index: int,
        runtime_context_message: Message,
        runtime_context_insert_index: int,
        turn_objective_message: Message | None = None,
    ) -> tuple[list[Message], SessionSanitizeResult]:
        """Off-loop wrapper for :meth:`_provider_request_messages_with_sanitize`.

        The synchronous assembly runs the tool-result compaction snapshot writes,
        each of which does a store-wide ``rglob`` over the shared tool-result
        store (issue #305). Running the whole assembly in a worker thread keeps
        that O(store) filesystem scan off the gateway event loop so per-turn
        latency does not grow with the number of stored results. The assembly
        touches no asyncio primitives, so it is thread-safe to offload.
        """

        def _run() -> tuple[list[Message], SessionSanitizeResult]:
            return self._provider_request_messages_with_sanitize(
                messages,
                request_context_message=request_context_message,
                request_context_insert_index=request_context_insert_index,
                runtime_context_message=runtime_context_message,
                runtime_context_insert_index=runtime_context_insert_index,
                turn_objective_message=turn_objective_message,
            )

        return await asyncio.to_thread(_run)

    async def _provider_request_messages_async(
        self,
        messages: list[Message],
        *,
        request_context_message: Message | None,
        request_context_insert_index: int,
        runtime_context_message: Message,
        runtime_context_insert_index: int,
        turn_objective_message: Message | None = None,
    ) -> list[Message]:
        request_messages, _ = await self._provider_request_messages_with_sanitize_async(
            messages,
            request_context_message=request_context_message,
            request_context_insert_index=request_context_insert_index,
            runtime_context_message=runtime_context_message,
            runtime_context_insert_index=runtime_context_insert_index,
            turn_objective_message=turn_objective_message,
        )
        return request_messages

    def _provider_request_messages_for_count_projection(
        self,
        messages: list[Message],
        *,
        request_context_message: Message | None,
        request_context_insert_index: int,
        runtime_context_message: Message,
        runtime_context_insert_index: int,
        turn_objective_message: Message | None = None,
    ) -> list[Message]:
        """Assemble the provider view without logs, snapshots, or state writes."""

        request_messages, _ = self._provider_request_messages_with_sanitize(
            messages,
            request_context_message=request_context_message,
            request_context_insert_index=request_context_insert_index,
            runtime_context_message=runtime_context_message,
            runtime_context_insert_index=runtime_context_insert_index,
            turn_objective_message=turn_objective_message,
            preview=True,
        )
        return request_messages

    def _project_provider_request_message_count(
        self,
        messages: list[Message],
        *,
        config: ChatConfig,
        identical_request_perturbed: bool,
        request_context_message: Message | None,
        request_context_insert_index: int,
        runtime_context_message: Message,
        runtime_context_insert_index: int,
        turn_objective_message: Message | None,
    ) -> ProviderMessageCountProjection | None:
        request_messages = self._provider_request_messages_for_count_projection(
            messages,
            request_context_message=request_context_message,
            request_context_insert_index=request_context_insert_index,
            runtime_context_message=runtime_context_message,
            runtime_context_insert_index=runtime_context_insert_index,
            turn_objective_message=turn_objective_message,
        )
        if identical_request_perturbed:
            request_messages = self._append_identical_request_loop_nudge(
                request_messages
            )
        return project_provider_message_count(
            self.provider,
            request_messages,
            config,
        )

    @staticmethod
    def _message_count_headroom(limit: int) -> int:
        return min(16, max(2, math.ceil(limit * 0.10)))

    @staticmethod
    def _adjust_index_after_prefix_summary(original_index: int, cut: int) -> int:
        return 2 + max(0, original_index - cut)

    @staticmethod
    def _message_count_safe_prefix_cuts(
        messages: list[Message],
        *,
        protected_turn_start_index: int,
    ) -> list[int]:
        """Return whole-turn cuts that cannot orphan structured tool state."""

        protected_start = max(0, min(protected_turn_start_index, len(messages)))
        cuts: list[int] = []
        for cut in range(1, protected_start + 1):
            if cut >= len(messages):
                break
            first_kept = messages[cut]
            # A historical turn and the protected current-turn prefix both
            # begin with a run of user-side messages.  Require the preceding
            # assistant boundary so skills context, multimodal inputs, and the
            # actual user message cannot be split into separate turns.  A tool
            # result is never a legal boundary even though its role is ``user``.
            if (
                first_kept.role != "user"
                or _message_has_tool_result(first_kept)
                or messages[cut - 1].role != "assistant"
            ):
                continue
            prefix = messages[:cut]
            tail = messages[cut:]
            if repair_tool_pairing(prefix) != prefix:
                continue
            if repair_tool_pairing(tail) != tail:
                continue
            cuts.append(cut)
        return cuts

    @staticmethod
    def _message_count_compaction_entries(messages: list[Message]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message.content, str):
                flat = message.content
                real_tokens = get_approx_tokens(message.content)
            else:
                flat = _flatten_content_blocks(message.content)
                real_tokens = get_approx_tokens(
                    json.dumps(Agent._live_request_jsonable(message.content))
                )
            entries.append(
                {
                    "role": message.role,
                    "content": flat,
                    "token_count": real_tokens,
                }
            )
        return entries

    async def _recover_provider_message_count_limit(
        self,
        messages: list[Message],
        *,
        request_suffix_messages: list[Message],
        proof: ProviderMessageLimitProof,
        config: ChatConfig,
        identical_request_perturbed: bool,
        request_context_message: Message | None,
        request_context_insert_index: int,
        runtime_context_message: Message,
        runtime_context_insert_index: int,
        turn_objective_message: Message | None,
        protected_turn_start_index: int,
    ) -> tuple[_MessageCountRecoveryOutcome | None, str]:
        """Find one safe prefix cut, summarize it once, then re-project.

        This method only returns a request view.  It deliberately does not
        mutate the canonical turn transcript and does not emit a persistent
        ``CompactionEvent``.
        """

        limit = int(proof.limit)
        target = limit - self._message_count_headroom(limit)
        if target <= 0:
            return None, "invalid_recovery_target"

        projected_current = self._project_provider_request_message_count(
            [*messages, *request_suffix_messages],
            config=config,
            identical_request_perturbed=identical_request_perturbed,
            request_context_message=request_context_message,
            request_context_insert_index=request_context_insert_index,
            runtime_context_message=runtime_context_message,
            runtime_context_insert_index=runtime_context_insert_index,
            turn_objective_message=turn_objective_message,
        )
        if projected_current is None:
            return None, "projection_unavailable"
        if projected_current.actual_wire_messages <= limit:
            return None, "local_wire_count_not_over_limit"

        placeholder_summary = Message(
            role="user",
            content="[Context summary]\n[message-count projection placeholder]",
        )
        summary_ack = Message(
            role="assistant",
            content="Understood. Continuing from summary.",
        )
        selected_cut: int | None = None
        selected_projection: ProviderMessageCountProjection | None = None
        selected_request_idx = 0
        selected_runtime_idx = 0
        protected_start = max(0, min(protected_turn_start_index, len(messages)))
        for cut in self._message_count_safe_prefix_cuts(
            messages,
            protected_turn_start_index=protected_start,
        ):
            candidate = [placeholder_summary, summary_ack, *messages[cut:]]
            request_idx = self._adjust_index_after_prefix_summary(
                request_context_insert_index,
                cut,
            )
            runtime_idx = self._adjust_index_after_prefix_summary(
                runtime_context_insert_index,
                cut,
            )
            projection = self._project_provider_request_message_count(
                [*candidate, *request_suffix_messages],
                config=config,
                identical_request_perturbed=identical_request_perturbed,
                request_context_message=request_context_message,
                request_context_insert_index=request_idx,
                runtime_context_message=runtime_context_message,
                runtime_context_insert_index=runtime_idx,
                turn_objective_message=turn_objective_message,
            )
            if projection is None:
                return None, "projection_unavailable"
            if projection.actual_wire_messages <= target:
                selected_cut = cut
                selected_projection = projection
                selected_request_idx = request_idx
                selected_runtime_idx = runtime_idx
                break

        if selected_cut is None or selected_projection is None:
            return None, "no_safe_cut"

        compaction_config = self._build_compaction_config()
        protected_tail_count = len(messages) - protected_start
        compaction_config.protected_recent_messages = max(
            int(compaction_config.protected_recent_messages or 0),
            protected_tail_count,
        )
        request = CompactionRequest(
            session_id="agent-turn-message-count",
            entries=self._message_count_compaction_entries(messages),
            context_window_tokens=self.config.context_window_tokens,
            config=compaction_config,
            forced_prefix_cut=selected_cut,
            trigger="message_count",
            reason="provider_request_message_limit",
            provider_request_correlation=derive_provider_request_correlation(
                self._provider_request_correlation,
                execution_id=uuid.uuid4().hex,
                call_kind="auxiliary.compaction",
            ),
        )
        try:
            result = await compact_context(request)
        except Exception:  # noqa: BLE001 - refusal is surfaced as a stable terminal state
            return None, "summary_failed"
        kept_start_index = int(
            getattr(result, "kept_start_index", result.removed_count)
            or result.removed_count
        )
        if (
            result.removed_count != selected_cut
            or kept_start_index != selected_cut
            or not result.summary
        ):
            return None, str(result.skip_reason or "summary_failed")

        compacted = [
            Message(role="user", content=f"[Context summary]\n{result.summary}"),
            summary_ack,
            *messages[selected_cut:],
        ]
        adjusted_protected_start = self._adjust_index_after_prefix_summary(
            protected_start,
            selected_cut,
        )
        # The protected current turn remains the original structured objects,
        # in the original order.  Refuse rather than repairing or flattening it.
        if compacted[adjusted_protected_start:] != messages[protected_start:]:
            return None, "protected_tail_changed"
        if repair_tool_pairing(compacted) != compacted:
            return None, "tool_pairing_changed"

        verified = self._project_provider_request_message_count(
            [*compacted, *request_suffix_messages],
            config=config,
            identical_request_perturbed=identical_request_perturbed,
            request_context_message=request_context_message,
            request_context_insert_index=selected_request_idx,
            runtime_context_message=runtime_context_message,
            runtime_context_insert_index=selected_runtime_idx,
            turn_objective_message=turn_objective_message,
        )
        if verified is None:
            return None, "projection_unavailable_after_summary"
        if verified.actual_wire_messages > target:
            return None, "projection_above_target_after_summary"

        return (
            _MessageCountRecoveryOutcome(
                messages=compacted,
                request_context_insert_index=selected_request_idx,
                runtime_context_insert_index=selected_runtime_idx,
                protected_turn_start_index=adjusted_protected_start,
                projected_wire_messages=verified.actual_wire_messages,
                removed_count=result.removed_count,
            ),
            "recovered",
        )

    def _apply_provider_tool_result_overrides(self, messages: list[Message]) -> list[Message]:
        if (
            not self._provider_tool_result_overrides
            and not self._provider_tool_result_frozen_overrides
        ):
            return messages

        projected: list[Message] = []
        changed = False
        for message in messages:
            if not isinstance(message.content, list):
                projected.append(message)
                continue
            blocks: list[Any] = []
            message_changed = False
            for block in message.content:
                if isinstance(block, ContentBlockToolResult):
                    override = self._provider_tool_result_overrides.get(
                        block.tool_use_id
                    ) or self._provider_tool_result_frozen_overrides.get(block.tool_use_id)
                    if override is not None:
                        blocks.append(override)
                        message_changed = True
                        continue
                blocks.append(block)
            if message_changed:
                projected.append(
                    Message(
                        role=message.role,
                        content=blocks,
                        reasoning_content=message.reasoning_content,
                    )
                )
                changed = True
            else:
                projected.append(message)
        return projected if changed else messages

    @staticmethod
    def _provider_request_is_smaller(before: list[Message], after: list[Message]) -> bool:
        return len(after) < len(before) or session_payload_chars(after) < session_payload_chars(
            before
        )

    def _runtime_context_block(self) -> str:
        now = datetime.now().astimezone()
        tzinfo = now.tzinfo
        tz_name = getattr(tzinfo, "key", None) or str(tzinfo) if tzinfo is not None else "local"
        lines = [
            "[Runtime context for this turn]",
            f"Current local date/time: {now.isoformat(timespec='minutes')} ({now.strftime('%a')})",
            f"Time zone / location hint: {tz_name}",
            "Use this runtime context for questions about the current date, time, or local "
            "time zone. Do not treat it as a user request.",
        ]
        return "\n".join(lines)

    @staticmethod
    def _runtime_context_message(runtime_context: str) -> Message:
        return Message(role="user", content=runtime_context)

    @staticmethod
    def _request_context_message(request_context: str | None) -> Message | None:
        if not request_context or not request_context.strip():
            return None
        lines = [
            "[Request context for this turn]",
            "This request-scoped context is not a user request and is not transcript history.",
            "Use it only when it is relevant to the current user request.",
            request_context.strip(),
        ]
        return Message(role="user", content="\n".join(lines))

    @staticmethod
    def _turn_objective_message(
        turn_objective: str | None,
        *,
        enabled: bool = True,
        max_chars: int = _TURN_OBJECTIVE_REMINDER_MAX_CHARS,
    ) -> Message | None:
        if not enabled:
            return None
        if not turn_objective or not turn_objective.strip():
            return None
        objective = turn_objective.strip()
        if len(objective) > max_chars:
            objective = objective[:max_chars].rstrip() + "..."
        lines = [
            "[Current user request reminder]",
            "This is the active user request for this same turn, not a new request.",
            "Continue using the tool results above to make progress on:",
            objective,
        ]
        return Message(role="user", content="\n".join(lines))

    @staticmethod
    def _with_request_context_messages(
        messages: list[Message],
        request_context_message: Message | None,
        request_context_insert_index: int,
        runtime_context_message: Message,
        runtime_context_insert_index: int,
        *,
        turn_objective_message: Message | None = None,
    ) -> list[Message]:
        result = list(messages)
        runtime_idx = max(0, min(runtime_context_insert_index, len(result)))
        if request_context_message is not None:
            request_idx = max(0, min(request_context_insert_index, len(result)))
            result.insert(request_idx, request_context_message)
            if request_idx <= runtime_idx:
                runtime_idx += 1
        runtime_idx = max(0, min(runtime_idx, len(result)))
        if runtime_idx < len(result) and result[runtime_idx].role == "user":
            result[runtime_idx] = Agent._append_runtime_context_to_user_message(
                result[runtime_idx],
                runtime_context_message,
            )
        else:
            result.insert(runtime_idx, runtime_context_message)
        if (
            turn_objective_message is not None
            and _message_has_tool_result(result[-1] if result else None)
            and not Agent._has_provider_context_marker_replay(result)
        ):
            result.append(turn_objective_message)
        return result

    @staticmethod
    def _has_provider_context_marker_replay(messages: list[Message]) -> bool:
        for message in messages:
            if not isinstance(message.content, list):
                continue
            for block in message.content:
                if (
                    isinstance(block, ContentBlockToolUse)
                    and Agent._has_provider_context_replay_marker(block.input)
                ):
                    return True
        return False

    @staticmethod
    def _append_runtime_context_to_user_message(
        message: Message,
        runtime_context_message: Message,
    ) -> Message:
        runtime_content = runtime_context_message.content
        if not isinstance(runtime_content, str):
            return runtime_context_message
        if isinstance(message.content, str):
            return Message(
                role=message.role,
                content=f"{message.content}\n\n{runtime_content}",
                reasoning_content=message.reasoning_content,
            )
        if isinstance(message.content, list):
            return Message(
                role=message.role,
                content=[
                    *message.content,
                    ContentBlockText(text=f"\n\n{runtime_content}"),
                ],
                reasoning_content=message.reasoning_content,
            )
        return runtime_context_message

    @staticmethod
    def _cache_breakpoints_without_runtime_context(
        cache_breakpoints: list[dict[str, str]] | None,
    ) -> list[dict[str, str]] | None:
        if not cache_breakpoints:
            return None
        return list(cache_breakpoints)

    def _skills_context_message(self) -> Message | None:
        prompt = self.config.skills_context_prompt
        if not prompt or not prompt.strip():
            return None
        lines = [
            "[Available skills for this turn]",
            "This is runtime-provided context, not a user request.",
            "Use it only to decide whether to call skill_view for the current task.",
            prompt.strip(),
        ]
        return Message(role="user", content="\n".join(lines))

    def _transition(self, to: AgentState) -> StateChangeEvent:
        ev = StateChangeEvent(from_state=self._state, to_state=to)
        self._state = to
        return ev

    @staticmethod
    def _is_turn_yield_result(result: ToolResult) -> bool:
        if result.tool_name != "sessions_yield" or result.is_error:
            return False
        try:
            payload = json.loads(result.content)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict):
            return False
        return payload.get("status") == "yielded"

    @staticmethod
    def _terminal_artifact_delivery_artifacts(
        results: list[ToolResult],
    ) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for result in results:
            if result.tool_name != "publish_artifact" or result.is_error:
                continue
            if result.artifacts:
                artifacts.extend(result.artifacts)
                continue
            try:
                payload = json.loads(result.content)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("status") not in {"published", "already_published"}:
                continue
            artifact = payload.get("artifact")
            artifacts.append(artifact if isinstance(artifact, dict) else {})
        return artifacts

    @staticmethod
    def _artifact_delivery_final_response_text(
        artifacts: list[dict[str, Any]],
    ) -> str:
        names = [
            str(item.get("name") or item.get("filename") or "").strip()
            for item in artifacts
            if isinstance(item, dict)
        ]
        named = [name for name in names if name]
        if named:
            return "The generated file is ready: " + ", ".join(named) + "."
        return "The generated file is ready."

    def _build_compaction_config(self) -> CompactionConfig:
        config = build_compaction_config_from_provider(
            self.provider,
            default_model=self.config.model_id,
        )
        config.compaction_profile = self.config.compaction_profile
        config.protected_recent_messages = self.config.compaction_protected_recent_messages
        return config

    @staticmethod
    def _live_request_jsonable(value: Any) -> Any:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return model_dump(mode="json", exclude_none=True)
            except TypeError:
                return model_dump(mode="json")
        if isinstance(value, list | tuple):
            return [Agent._live_request_jsonable(item) for item in value]
        if isinstance(value, dict):
            return {str(key): Agent._live_request_jsonable(item) for key, item in value.items()}
        if hasattr(value, "__dict__"):
            return {
                str(key): Agent._live_request_jsonable(item)
                for key, item in vars(value).items()
                if not str(key).startswith("_")
            }
        try:
            json.dumps(value)
        except TypeError:
            return repr(value)
        return value

    def _estimate_live_request_tokens(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> int:
        """Estimate the current provider request size without lifetime usage."""

        payload: dict[str, Any] = {
            "messages": [self._live_request_jsonable(message) for message in messages],
        }
        if tools:
            payload["tools"] = [self._live_request_jsonable(tool) for tool in tools]
        if config is not None:
            if config.system:
                payload["system"] = config.system
            config_payload = config.model_dump(
                mode="json",
                exclude_none=True,
                exclude={"system", "model_capabilities"},
            )
            payload.update(config_payload)

        estimated_chars = len(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        return max(1, estimated_chars // 4)

    async def _check_context_overflow(
        self,
        messages: list[Message],
        estimated_context_tokens: int,
        *,
        request_context_insert_index: int | None = None,
        runtime_context_insert_index: int | None = None,
        protected_turn_start_index: int | None = None,
        compaction_window_tokens: int | None = None,
    ) -> CompactionOutcome | None:
        """Check if estimated live context tokens exceed the overflow threshold.

        Uses sub-agent flush instead of prompt injection.
        The flush is re-entrant: it can trigger on every approach to threshold.
        """
        self._last_compaction_refusal_reason = None
        window_tokens = compaction_window_tokens or self.config.context_window_tokens
        threshold = self.config.context_overflow_threshold * window_tokens
        if estimated_context_tokens <= threshold:
            return CompactionOutcome(
                messages=messages,
                request_context_insert_index=request_context_insert_index,
                runtime_context_insert_index=runtime_context_insert_index,
                protected_turn_start_index=protected_turn_start_index,
            )

        compaction_id = new_compaction_id()
        # --- Pre-compaction flush; inline compaction can continue on degraded flush. ---
        flush_task: asyncio.Task | None = None
        self._consume_completed_flush_task()

        async def _await_flush_task() -> Any | None:
            # Give flush a grace period to complete instead of cancelling immediately.
            # Adds up to flush_timeout_seconds (default 15s) of latency, but without
            # this the flush is effectively dead code (always cancelled before finishing).
            if flush_task is not None and not flush_task.done():
                if flush_task is self._flush_wait_timed_out_task:
                    return None
                try:
                    receipt = await asyncio.wait_for(
                        asyncio.shield(flush_task),
                        timeout=self.config.flush_timeout_seconds,
                    )
                    logger.info("memory_flush.completed_after_compaction")
                    self._flush_wait_timed_out_task = None
                    self._mark_flush_task_completed(flush_task)
                    return receipt
                except TimeoutError:
                    self._flush_wait_timed_out_task = flush_task
                    next_retry_seconds = self._record_flush_timeout_backoff()
                    logger.warning(
                        "memory_flush.timed_out",
                        timeout_seconds=self.config.flush_timeout_seconds,
                        next_retry_seconds=next_retry_seconds,
                    )
                except Exception as exc:
                    logger.warning("memory_flush.await_failed", error=str(exc))
                    self._mark_flush_task_completed(flush_task)
                    return None
            if flush_task is not None and flush_task.done():
                try:
                    receipt = flush_task.result()
                    self._flush_wait_timed_out_task = None
                    self._mark_flush_task_completed(flush_task)
                    return receipt
                except Exception as exc:
                    logger.warning("memory_flush.await_failed", error=str(exc))
                    self._flush_wait_timed_out_task = None
                    self._mark_flush_task_completed(flush_task)
                    return None
            return None

        pre_compaction_flush_enabled = flush_trigger_enabled(
            self.config,
            "pre_compaction",
        )

        if not self._flush_done_this_cycle and pre_compaction_flush_enabled:
            try:
                from opensquilla.memory.flush import (
                    resolve_flush_plan,
                    should_flush,
                )

                now = time.monotonic()
                if self._active_flush_task is not None and not self._active_flush_task.done():
                    logger.debug("memory_flush.skipped", reason="already_running")
                    flush_task = self._active_flush_task
                elif now < self._flush_backoff_until:
                    logger.warning(
                        "memory_flush.skipped",
                        reason="backoff",
                        retry_after_seconds=round(self._flush_backoff_until - now, 3),
                    )
                else:
                    transcript_bytes = sum(
                        len(m.content.encode("utf-8")) if isinstance(m.content, str) else 0
                        for m in messages
                    )

                    if should_flush(
                        total_tokens=estimated_context_tokens,
                        threshold_tokens=int(threshold),
                        transcript_bytes=transcript_bytes,
                    ):
                        plan = resolve_flush_plan(
                            workspace_dir=self.config.flush_workspace_dir,
                            archive_max_bytes=self.config.flush_archive_max_bytes,
                        )
                        logger.info(
                            "memory_flush.triggered",
                            path=plan.relative_path,
                            total_tokens=estimated_context_tokens,
                            threshold=int(threshold),
                        )
                        flush_task = asyncio.create_task(self._run_flush(plan, list(messages)))
                        flush_task.add_done_callback(self._on_flush_task_done)
                        self._active_flush_task = flush_task
                        self._flush_done_this_cycle = True
            except Exception:
                logger.debug("memory_flush.skipped", reason="flush module unavailable")

        if pre_compaction_flush_enabled:
            if (
                flush_task is not None
                and not flush_task.done()
                and time.monotonic() < self._flush_backoff_until
            ):
                logger.warning(
                    "memory_flush.skipped",
                    reason="backoff",
                    retry_after_seconds=round(self._flush_backoff_until - time.monotonic(), 3),
                )
                self._flush_done_this_cycle = False
            receipt = await _await_flush_task()
            if not flush_receipt_allows_destructive_compaction(receipt):
                reason = "memory_flush_degraded_before_compaction"
                if flush_task is not None and self._flush_wait_timed_out_task is flush_task:
                    reason = "memory_flush_timeout_before_compaction"
                logger.warning(
                    "memory_flush.degraded_before_compaction",
                    reason=reason,
                    mode=getattr(receipt, "mode", None),
                    integrity_status=getattr(receipt, "integrity_status", None),
                    indexed_chunk_count=getattr(receipt, "indexed_chunk_count", None),
                )
                self._flush_done_this_cycle = False
                if pre_compaction_flush_requires_safe_receipt(self.config):
                    self._last_compaction_refusal_reason = reason
                    if self._session_key:
                        notify_compaction(
                            self._session_key,
                            source="automatic",
                            phase="agent_inline_overflow",
                            status="skipped",
                            reason=reason,
                            tokens_before=estimated_context_tokens,
                            context_window_tokens=window_tokens,
                            **compaction_effect_payload(
                                status="skipped",
                                reason=reason,
                            ),
                            **compaction_lifecycle_payload(
                                compaction_id,
                                COMPACTION_TRIGGERED_EVENT,
                            ),
                        )
                    return None

        # --- Compaction ---
        # Flatten each message for the compaction LLM's *input* text, but size
        # the budget/skip/cut decisions on the ORIGINAL structured content.
        # _flatten_content_blocks clips tool results to 200 chars, so sizing on
        # the flattened view made a tool-heavy (overflowing) context look tiny,
        # so compaction always skipped and the CONTEXT_OVERFLOW retry died with
        # compaction_not_smaller. Attaching a real token_count makes the
        # compactor's estimator (which prefers a persisted token_count) measure
        # the true replay size.
        entries = []
        for m in messages:
            if isinstance(m.content, str):
                flat = m.content
                real_tokens = get_approx_tokens(m.content)
            else:
                flat = _flatten_content_blocks(m.content)
                real_tokens = get_approx_tokens(
                    json.dumps(Agent._live_request_jsonable(m.content))
                )
            entries.append(
                {
                    "role": m.role,
                    "content": flat,
                    "token_count": real_tokens,
                }
            )

        request = CompactionRequest(
            session_id="agent-turn",
            entries=entries,
            context_window_tokens=window_tokens,
            config=self._build_compaction_config(),
            provider_request_correlation=derive_provider_request_correlation(
                self._provider_request_correlation,
                execution_id=uuid.uuid4().hex,
                call_kind="auxiliary.compaction",
            ),
        )

        if self._session_key:
            notify_compaction(
                self._session_key,
                source="automatic",
                phase="agent_inline_overflow",
                status="started",
                tokens_before=estimated_context_tokens,
                context_window_tokens=window_tokens,
                **compaction_effect_payload(status="started"),
                **compaction_lifecycle_payload(
                    compaction_id,
                    COMPACTION_TRIGGERED_EVENT,
                ),
            )

        try:
            result = await compact_context(request)
        except Exception as exc:  # noqa: BLE001
            self._last_compaction_refusal_reason = "compaction_failed"
            if self._session_key:
                notify_compaction(
                    self._session_key,
                    source="automatic",
                    phase="agent_inline_overflow",
                    status="failed",
                    message=str(exc),
                    reason=self._last_compaction_refusal_reason,
                    tokens_before=estimated_context_tokens,
                    context_window_tokens=window_tokens,
                    **compaction_effect_payload(status="failed"),
                    **compaction_lifecycle_payload(
                        compaction_id,
                        COMPACTION_TRIGGERED_EVENT,
                    ),
                )
            return None  # signal failure

        if self._session_key and result.removed_count > 0 and result.summary:
            for event in (
                COMPACTION_CHUNK_SUMMARIZED_EVENT,
                COMPACTION_SUMMARY_VERIFIED_EVENT,
            ):
                observed_payload = compaction_lifecycle_payload(compaction_id, event)
                observed_payload.update(
                    compaction_result_payload(
                        result,
                        tokens_before=estimated_context_tokens,
                    )
                )
                notify_compaction(
                    self._session_key,
                    source="automatic",
                    phase="agent_inline_overflow",
                    status="observed",
                    context_window_tokens=window_tokens,
                    **compaction_effect_payload(status="observed"),
                    **observed_payload,
                )

        # Removing history without a replacement summary is equivalent to
        # bare truncation; reject it so the caller takes the existing
        # compaction failure path instead of silently dropping context.
        if result.removed_count > 0 and not result.summary:
            logger.warning(
                "compaction.empty_summary_rejected",
                removed_count=result.removed_count,
                kept_count=len(result.kept_entries),
            )
            self._last_compaction_refusal_reason = "empty_summary_rejected"
            if self._session_key:
                notify_compaction(
                    self._session_key,
                    source="automatic",
                    phase="agent_inline_overflow",
                    status="failed",
                    reason=self._last_compaction_refusal_reason,
                    tokens_before=estimated_context_tokens,
                    context_window_tokens=window_tokens,
                    removed_count=result.removed_count,
                    kept_count=len(result.kept_entries),
                    **compaction_effect_payload(status="failed"),
                    **compaction_lifecycle_payload(
                        compaction_id,
                        COMPACTION_TRIGGERED_EVENT,
                    ),
                )
            return None

        # A skip (nothing removed, no summary) is a no-op regardless of whether
        # the in-memory history is structured or string-only. Reporting it as
        # compacted=True (the old behavior for string-only history) emits a
        # spurious CompactionEvent that rewrites the durable transcript and
        # corrupts row metadata, so short-circuit every no-op skip here.
        if result.removed_count == 0 and not result.summary:
            has_structured_content = any(not isinstance(m.content, str) for m in messages)
            await _await_flush_task()
            self._flush_done_this_cycle = False
            skip_reason = getattr(result, "skip_reason", None) or (
                "structured_content_noop" if has_structured_content else "noop"
            )
            if self._session_key:
                notify_compaction(
                    self._session_key,
                    source="automatic",
                    phase="agent_inline_overflow",
                    status="skipped",
                    reason=skip_reason,
                    tokens_before=estimated_context_tokens,
                    tokens_after=result.tokens_after,
                    remaining_budget_tokens=result.remaining_budget_tokens,
                    context_window_tokens=window_tokens,
                    **compaction_effect_payload(
                        status="skipped",
                        reason=skip_reason,
                        user_visible=False,
                    ),
                    **compaction_lifecycle_payload(
                        compaction_id,
                        COMPACTION_TRIGGERED_EVENT,
                    ),
                )
            return CompactionOutcome(
                messages=messages,
                request_context_insert_index=request_context_insert_index,
                runtime_context_insert_index=runtime_context_insert_index,
                protected_turn_start_index=protected_turn_start_index,
            )

        # Rebuild message list from compacted entries
        compacted: list[Message] = []
        if result.summary:
            compacted.append(Message(role="user", content=f"[Context summary]\n{result.summary}"))
            compacted.append(
                Message(role="assistant", content="Understood. Continuing from summary.")
            )
        for entry in result.kept_entries:
            compacted.append(Message(role=entry["role"], content=entry["content"]))

        await _await_flush_task()

        # Reset flush flag so it can trigger again after next compaction
        self._flush_done_this_cycle = False

        # Trigger 6: post-compaction sync
        if self._memory_sync_manager is not None:
            self._memory_sync_manager.mark_dirty()

        kept_entries = [{"role": e["role"], "content": e["content"]} for e in result.kept_entries]
        # compact_context is prefix-only.  Use its exact cut instead of
        # content matching: duplicate role/content pairs can otherwise match
        # an older row and move the protected current-turn boundary forward.
        # ``getattr`` keeps older/custom compactor result stubs compatible;
        # prefix-only compaction already guarantees that removed_count is the
        # same boundary when the additive field is absent.
        kept_start_index = int(
            getattr(result, "kept_start_index", result.removed_count)
            or result.removed_count
        )
        adjusted_request_idx = self._adjust_index_after_prefix_compaction(
            request_context_insert_index,
            kept_start_index,
            summary_present=bool(result.summary),
        )
        adjusted_runtime_idx = self._adjust_index_after_prefix_compaction(
            runtime_context_insert_index,
            kept_start_index,
            summary_present=bool(result.summary),
        )
        adjusted_protected_idx = self._adjust_index_after_prefix_compaction(
            protected_turn_start_index,
            kept_start_index,
            summary_present=bool(result.summary),
        )
        return CompactionOutcome(
            messages=compacted,
            compacted=True,
            summary=result.summary,
            kept_entries=kept_entries,
            removed_count=result.removed_count,
            compaction_id=compaction_id,
            request_context_insert_index=adjusted_request_idx,
            runtime_context_insert_index=adjusted_runtime_idx,
            protected_turn_start_index=adjusted_protected_idx,
        )

    def _consume_completed_flush_task(self) -> None:
        task = self._active_flush_task
        if task is None or not task.done():
            return
        self._mark_flush_task_completed(task)

    def _on_flush_task_done(self, task: asyncio.Task) -> None:
        self._mark_flush_task_completed(task)

    def _mark_flush_task_completed(self, task: asyncio.Task) -> None:
        if self._flush_wait_timed_out_task is task:
            self._flush_wait_timed_out_task = None
        if self._active_flush_task is not task:
            return
        try:
            receipt = task.result()
        except asyncio.CancelledError:
            logger.debug("memory_flush.cancelled")
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory_flush.background_failed", error=str(exc))
        else:
            mode = getattr(receipt, "mode", None)
            if not flush_receipt_is_successful_flush(receipt):
                next_retry_seconds = self._ensure_flush_degraded_backoff()
                logger.warning(
                    "memory_flush.degraded",
                    mode=mode,
                    result_status=getattr(receipt, "result_status", None),
                    integrity_status=getattr(receipt, "integrity_status", None),
                    output_coverage_status=getattr(receipt, "output_coverage_status", None),
                    obligation_status=getattr(receipt, "obligation_status", None),
                    raw_reason=getattr(receipt, "raw_reason", None),
                    next_retry_seconds=next_retry_seconds,
                )
            else:
                self._flush_backoff_seconds = 0.0
                self._flush_backoff_until = 0.0
        self._active_flush_task = None

    def _record_flush_timeout_backoff(self) -> float:
        initial = max(0.0, float(self.config.flush_backoff_initial_seconds))
        maximum = max(initial, float(self.config.flush_backoff_max_seconds))
        if initial == 0:
            self._flush_backoff_seconds = 0.0
            self._flush_backoff_until = 0.0
            return 0.0
        if self._flush_backoff_seconds <= 0:
            next_retry_seconds = initial
        else:
            next_retry_seconds = min(self._flush_backoff_seconds * 2, maximum)
        self._flush_backoff_seconds = next_retry_seconds
        self._flush_backoff_until = time.monotonic() + next_retry_seconds
        return next_retry_seconds

    def _ensure_flush_degraded_backoff(self) -> float:
        remaining = self._flush_backoff_until - time.monotonic()
        if remaining > 0:
            return remaining
        return self._record_flush_timeout_backoff()

    @staticmethod
    def _adjust_index_after_prefix_compaction(
        original_index: int | None,
        kept_start_index: int,
        *,
        summary_present: bool,
    ) -> int | None:
        """Map an insertion boundary through an exact prefix compaction cut."""
        if original_index is None:
            return None
        summary_prefix = 2 if summary_present and original_index > 0 else 0
        return summary_prefix + max(0, original_index - kept_start_index)

    async def _run_flush(
        self,
        plan: Any,
        messages: list[Message],
    ) -> Any | None:
        """Run memory flush before compaction; delegates to SessionFlushService.

        When a ``SessionFlushService`` is injected, this method forwards the
        call and returns its receipt. When no service is injected (standalone
        Agent instances in unit tests or legacy paths), it falls back to an
        inline raw-dump so we don't silently drop data.
        """
        service = getattr(self, "_session_flush_service", None)
        if service is not None:
            try:
                from opensquilla.session.keys import parse_agent_id

                sk = getattr(self, "_session_key", None) or "agent:main:legacy"
                return await service.execute(
                    messages,
                    session_key=sk,
                    agent_id=parse_agent_id(sk),
                    timeout=self.config.flush_background_timeout_seconds,
                    message_window=0,
                    segment_mode="auto",
                    provider_request_correlation=derive_provider_request_correlation(
                        self._provider_request_correlation,
                        execution_id=uuid.uuid4().hex,
                        call_kind="auxiliary.session_flush",
                    ),
                )
            except asyncio.CancelledError:
                logger.debug("memory_flush.cancelled")
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("memory_flush.service_failed", error=str(exc))
            return None

        # Legacy fallback — only hit when no service is injected.
        from opensquilla.memory.flush import dump_transcript_excerpt

        if self.provider is None and self.tool_handler is not None:
            excerpt = dump_transcript_excerpt(messages)
            if excerpt.strip():
                from opensquilla.tool_boundary import ToolCall as _FlushToolCall

                await self.tool_handler(
                    _FlushToolCall(
                        tool_use_id="flush-fallback",
                        tool_name="memory_save",
                        arguments={
                            "content": excerpt,
                            "path": plan.relative_path,
                            "mode": "append",
                        },
                    )
                )
        return None

    @staticmethod
    def _has_provider_context_replay_marker(arguments: dict[str, Any]) -> bool:
        if Agent._has_provider_context_argument_marker(arguments):
            return True
        return any(
            isinstance(value, str) and value.startswith(_INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX)
            for value in arguments.values()
        )

    @staticmethod
    def _is_provider_context_projection_reuse_result(result: ToolResult) -> bool:
        status: Mapping[str, Any] = result.execution_status or {}
        return bool(
            result.is_error
            and isinstance(status, dict)
            and status.get("reason") == _PROVIDER_CONTEXT_PROJECTION_REUSED_REASON
        )

    def _strip_provider_context_marker_replay_for_provider(
        self,
        messages: list[Message],
        *,
        record: bool = True,
    ) -> list[Message]:
        blocked_tool_ids: set[str] = set()
        for message in messages:
            if not isinstance(message.content, list):
                continue
            for block in message.content:
                if (
                    isinstance(block, ContentBlockToolUse)
                    and isinstance(block.id, str)
                    and self._has_provider_context_replay_marker(block.input)
                ):
                    blocked_tool_ids.add(block.id)

        if not blocked_tool_ids:
            return messages

        if getattr(self.config, "provider_context_block_feedback", False):
            return self._project_blocked_context_replay_with_feedback(
                messages,
                blocked_tool_ids,
                record=record,
            )

        stripped_messages: list[Message] = []
        stripped_blocks = 0
        for message in messages:
            if not isinstance(message.content, list):
                stripped_messages.append(message)
                continue
            next_content: list[Any] = []
            changed = False
            for block in message.content:
                if isinstance(block, ContentBlockToolUse) and block.id in blocked_tool_ids:
                    stripped_blocks += 1
                    changed = True
                    continue
                if (
                    isinstance(block, ContentBlockToolResult)
                    and block.tool_use_id in blocked_tool_ids
                ):
                    stripped_blocks += 1
                    changed = True
                    continue
                next_content.append(block)
            if not changed:
                stripped_messages.append(message)
                continue
            if not next_content:
                continue
            stripped_messages.append(
                Message(
                    role=message.role,
                    content=next_content,
                    reasoning_content=getattr(message, "reasoning_content", None),
                )
            )

        if stripped_blocks and stripped_messages and stripped_messages[-1].role == "assistant":
            stripped_messages.append(Message(role="user", content=_PROVIDER_CONTEXT_REPAIR_PROMPT))

        if record:
            self.config.metadata["tool_argument_projection_replay_stripped"] = (
                self.config.metadata.get("tool_argument_projection_replay_stripped", 0)
                + stripped_blocks
            )
            self._write_turn_call_log(
                "tool_argument_projection_replay_stripped",
                tool_use_ids=sorted(blocked_tool_ids),
                stripped_blocks=stripped_blocks,
            )
        return stripped_messages

    def _project_blocked_context_replay_with_feedback(
        self,
        messages: list[Message],
        blocked_tool_ids: set[str],
        *,
        record: bool = True,
    ) -> list[Message]:
        """Project blocked compacted-placeholder calls without hiding the rejection.

        Instead of dropping the blocked tool_use and its error tool_result from
        the provider view (which leaves the model with no rejection signal and
        produces byte-identical retry loops), keep the pair: the tool_use input
        becomes the standard compacted-arguments placeholder and the error
        tool_result carrying the rejection text stays visible. When the
        rejection is the most recent event, the repair prompt is appended so
        the model is explicitly told how to recover.
        """
        projected_messages: list[Message] = []
        projected_blocks = 0
        last_blocked_result_index: int | None = None
        for message in messages:
            if not isinstance(message.content, list):
                projected_messages.append(message)
                continue
            next_content: list[Any] = []
            changed = False
            has_blocked_result = False
            for block in message.content:
                if isinstance(block, ContentBlockToolUse) and block.id in blocked_tool_ids:
                    projected_blocks += 1
                    changed = True
                    next_content.append(
                        ContentBlockToolUse(
                            id=block.id,
                            name=block.name,
                            input=self._provider_compacted_arguments_placeholder(
                                block.name,
                                block.input,
                            ),
                        )
                    )
                    continue
                if (
                    isinstance(block, ContentBlockToolResult)
                    and block.tool_use_id in blocked_tool_ids
                ):
                    has_blocked_result = True
                next_content.append(block)
            if changed:
                projected_messages.append(
                    Message(
                        role=message.role,
                        content=next_content,
                        reasoning_content=getattr(message, "reasoning_content", None),
                    )
                )
            else:
                projected_messages.append(message)
            if has_blocked_result:
                last_blocked_result_index = len(projected_messages) - 1

        repair_prompt_appended = (
            last_blocked_result_index is not None
            and last_blocked_result_index == len(projected_messages) - 1
        )
        if repair_prompt_appended:
            projected_messages.append(
                Message(role="user", content=_PROVIDER_CONTEXT_REPAIR_PROMPT)
            )

        if record:
            self.config.metadata["tool_argument_projection_replay_feedback"] = (
                self.config.metadata.get("tool_argument_projection_replay_feedback", 0)
                + projected_blocks
            )
            self._write_turn_call_log(
                "tool_argument_projection_replay_feedback",
                tool_use_ids=sorted(blocked_tool_ids),
                projected_blocks=projected_blocks,
                repair_prompt_appended=repair_prompt_appended,
            )
        return projected_messages

    def _identical_request_loop_break_action(
        self,
        request_messages: list[Message],
        *,
        first_attempt: bool,
    ) -> str | None:
        """Opt-in breaker for consecutive byte-identical provider projections.

        Hashes the projected request before any perturbation is appended, so a
        stuck loop keeps the same base sha and the streak keeps growing across
        iterations: at ``threshold`` the request is perturbed with a loop
        nudge, at ``2 * threshold`` the turn aborts. Provider retry attempts
        (``first_attempt=False``) reuse the current streak without advancing
        it, so retries of one request never count as a loop.
        """
        threshold = self._positive_int(
            getattr(self.config, "identical_request_loop_break_threshold", 0)
        )
        if threshold is None:
            return None
        if first_attempt:
            payload_sha = hashlib.sha256(
                json.dumps(
                    [message.model_dump(mode="json") for message in request_messages],
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            if payload_sha == self._identical_request_last_sha:
                self._identical_request_streak += 1
            else:
                self._identical_request_last_sha = payload_sha
                self._identical_request_streak = 1
        if self._identical_request_streak < threshold:
            return None
        if self._identical_request_streak >= threshold * 2:
            return "abort"
        return "perturb"

    @staticmethod
    def _append_identical_request_loop_nudge(
        request_messages: list[Message],
    ) -> list[Message]:
        """Append the loop-break nudge without producing back-to-back user turns.

        Most providers require strict user/assistant alternation. The request
        being perturbed always ends in a user message (the last tool results,
        or the original prompt), so appending a *new* user message would
        create two consecutive user turns and get rejected or mishandled by
        the provider. Merge the nudge into the existing trailing message
        instead when it is already a user turn.
        """
        if request_messages and request_messages[-1].role == "user":
            last_message = request_messages[-1]
            if isinstance(last_message.content, list):
                merged_content: Any = [
                    *last_message.content,
                    ContentBlockText(text=_IDENTICAL_REQUEST_LOOP_NUDGE),
                ]
            else:
                existing_text = (
                    last_message.content
                    if isinstance(last_message.content, str)
                    else str(last_message.content)
                )
                merged_content = f"{existing_text}\n\n{_IDENTICAL_REQUEST_LOOP_NUDGE}"
            return [
                *request_messages[:-1],
                Message(
                    role="user",
                    content=merged_content,
                    reasoning_content=getattr(last_message, "reasoning_content", None),
                ),
            ]
        return [
            *request_messages,
            Message(role="user", content=_IDENTICAL_REQUEST_LOOP_NUDGE),
        ]

    @staticmethod
    def _parse_tool_argument_projection(value: str) -> dict[str, str] | None:
        if not value.startswith(_TOOL_ARGUMENT_PROJECTION_PREFIX):
            return None
        metadata: dict[str, str] = {}
        for line in value.splitlines()[1:]:
            if line in {"head:", "tail:"}:
                break
            key, separator, raw_value = line.partition(":")
            if not separator:
                continue
            metadata[key.strip()] = raw_value.strip()
        return metadata

    @staticmethod
    def _provider_projection_placeholder(tool_name: str, field: str) -> str:
        return (
            f"[invalid_provider_context_projection:{tool_name}.{field}] "
            "provider-only compacted tool argument omitted; regenerate the real "
            "argument instead of copying provider context."
        )

    @staticmethod
    def _is_provider_context_marker_value(value: Any) -> bool:
        if value is True:
            return True
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "on"}
        return False

    @staticmethod
    def _has_provider_context_argument_marker(arguments: dict[str, Any]) -> bool:
        return Agent._is_provider_context_marker_value(
            arguments.get(_INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY)
        ) or any(
            Agent._is_provider_context_marker_value(arguments.get(marker))
            for marker in _COMPACTED_TOOL_ARGUMENT_MARKERS
        )

    @staticmethod
    def _provider_compacted_arguments_placeholder(
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            _INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY: True,
            "tool": tool_name,
            "reason": "provider_context_omitted",
        }

    def _sanitize_projected_tool_call_arguments(self, tc: ToolCall) -> ToolCall:
        if self._has_provider_context_argument_marker(tc.arguments):
            return ToolCall(
                tool_use_id=tc.tool_use_id,
                tool_name=tc.tool_name,
                arguments=self._provider_compacted_arguments_placeholder(
                    tc.tool_name,
                    tc.arguments,
                ),
                synthetic_from_text=tc.synthetic_from_text,
                origin_trace=tc.origin_trace,
            )
        sanitized = dict(tc.arguments)
        changed = False
        for argument_name, value in tc.arguments.items():
            if find_projected_tool_argument(value, path=argument_name) is None:
                continue
            sanitized[argument_name] = self._provider_projection_placeholder(
                tc.tool_name,
                argument_name,
            )
            changed = True
        if not changed:
            return tc
        return ToolCall(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            arguments=sanitized,
            synthetic_from_text=tc.synthetic_from_text,
            origin_trace=tc.origin_trace,
        )

    def _projection_rehydrate_error(
        self,
        tc: ToolCall,
        *,
        field: str,
        reason: str,
    ) -> ToolResult:
        self.config.metadata["tool_argument_projection_rehydrate_failures"] = (
            self.config.metadata.get(
                "tool_argument_projection_rehydrate_failures",
                0,
            )
            + 1
        )
        self._write_turn_call_log(
            "tool_argument_projection_rehydrate_failed",
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            field=field,
            reason=reason,
        )
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content=(
                f"The {tc.tool_name}.{field} input contains a compaction placeholder "
                '(text like "[provider_request_..._compacted: ...]"). The tool was not '
                "run. That placeholder is not real content and the original text cannot "
                "be recovered by copying or retyping it. Re-read the target file or "
                "re-run the command to obtain the real content, then reissue the tool "
                "call with the argument rebuilt from that output."
            ),
            is_error=True,
            execution_status=runtime_execution_status(
                "error",
                reason=_PROVIDER_CONTEXT_PROJECTION_REUSED_REASON,
            ),
        )

    def _provider_compacted_arguments_error(
        self,
        tc: ToolCall,
        *,
        reason: str,
    ) -> ToolResult:
        self.config.metadata["tool_argument_projection_rehydrate_failures"] = (
            self.config.metadata.get(
                "tool_argument_projection_rehydrate_failures",
                0,
            )
            + 1
        )
        self._write_turn_call_log(
            "tool_argument_projection_rehydrate_failed",
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            reason=reason,
        )
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content=(
                f"The {tc.tool_name} arguments were compacted for provider context and "
                "are not executable. The tool was not run. Do not copy or retype the "
                "compacted placeholder text; re-read the relevant file or re-run the "
                "command to obtain the real content, then reissue the tool call with "
                "complete arguments."
            ),
            is_error=True,
            execution_status=runtime_execution_status(
                "error",
                reason=_PROVIDER_CONTEXT_PROJECTION_REUSED_REASON,
            ),
        )

    def _rehydrate_projected_tool_arguments(
        self,
        tc: ToolCall,
    ) -> ToolCall | ToolResult:
        if self._has_provider_context_argument_marker(tc.arguments):
            return self._provider_compacted_arguments_error(
                tc,
                reason="provider_compacted_arguments_reused",
            )
        projected_match = find_projected_tool_argument(tc.arguments)
        if projected_match is not None:
            return self._projection_rehydrate_error(
                tc,
                field=projected_match.path,
                reason=projected_match.kind,
            )
        return tc

    async def _execute_tool(self, tc: ToolCall) -> ToolResult:
        """Dispatch a tool call to the registered handler."""
        args_hash = hashlib.sha256(
            json.dumps(tc.arguments, ensure_ascii=False, sort_keys=True, default=str).encode(
                "utf-8"
            )
        ).hexdigest()
        failure_signature = (tc.tool_name, args_hash)
        block_threshold = max(
            0,
            int(getattr(self.config, "tool_failure_loop_block_threshold", 0) or 0),
        )
        if (
            block_threshold > 0
            and self._tool_failure_loop_counts.get(failure_signature, 0) >= block_threshold - 1
        ):
            return ToolResult(
                tool_use_id=tc.tool_use_id,
                tool_name=tc.tool_name,
                content=(
                    f"The exact same {tc.tool_name} call has already failed repeatedly. "
                    "Do not retry this exact call unchanged. Use a different approach, "
                    "change the arguments, or explain the blocker to the user."
                ),
                is_error=True,
                execution_status=runtime_execution_status(
                    "error",
                    reason="tool_failure_loop_exhausted",
                ),
            )
        if self.tool_handler is None:
            result = ToolResult(
                tool_use_id=tc.tool_use_id,
                tool_name=tc.tool_name,
                content=f"No tool handler registered for tool '{tc.tool_name}'",
                is_error=True,
                execution_status=runtime_execution_status(
                    "error",
                    reason="runtime_error",
                ),
            )
        else:
            try:
                resolved = self._rehydrate_projected_tool_arguments(tc)
                if isinstance(resolved, ToolResult):
                    result = resolved
                else:
                    tc = resolved
                    result = await self.tool_handler(tc)
            except Exception as exc:  # noqa: BLE001
                result = ToolResult(
                    tool_use_id=tc.tool_use_id,
                    tool_name=tc.tool_name,
                    content=f"Tool '{tc.tool_name}' raised: {exc}",
                    is_error=True,
                    execution_status=runtime_execution_status(
                        "error",
                        reason="runtime_error",
                    ),
                )
        if result.is_error:
            self._tool_failure_loop_counts[failure_signature] = (
                self._tool_failure_loop_counts.get(failure_signature, 0) + 1
            )
        else:
            self._tool_failure_loop_counts.pop(failure_signature, None)
            if tc.tool_name in {
                "apply_patch",
                "background_process",
                "edit_file",
                "execute_code",
                "exec_command",
                "git_commit",
                "install_skill_deps",
                "write_file",
            }:
                self._tool_failure_loop_counts.clear()
        return result

    def _matched_meta_skill_name_from_metadata(self) -> str | None:
        metadata = self.config.metadata or {}
        match = metadata.get("meta_match")
        plan = getattr(match, "plan", None)
        name = getattr(plan, "name", None)
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None

    def _coerce_meta_tool_call(self, tc: ToolCall) -> ToolCall:
        tc = self._coerce_meta_skill_view_tool_call(tc)
        return self._coerce_meta_invoke_tool_call(tc)

    def _coerce_meta_invoke_tool_call(self, tc: ToolCall) -> ToolCall:
        if tc.tool_name != "meta_invoke":
            return tc
        name = tc.arguments.get("name")
        if isinstance(name, str) and name.strip():
            return tc

        raw = tc.arguments.get("_raw")
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                parsed_name = parsed.get("name")
                if isinstance(parsed_name, str) and parsed_name.strip():
                    return ToolCall(
                        tool_use_id=tc.tool_use_id,
                        tool_name="meta_invoke",
                        arguments={"name": parsed_name.strip()},
                        synthetic_from_text=tc.synthetic_from_text,
                        origin_trace=tc.origin_trace,
                    )

        matched_name = self._matched_meta_skill_name_from_metadata()
        if matched_name is None or not (self.config.metadata or {}).get("meta_match_tool_choice"):
            return tc

        logger.info(
            "agent.meta_invoke_arguments_coerced",
            skill=matched_name,
            tool_use_id=tc.tool_use_id,
        )
        return ToolCall(
            tool_use_id=tc.tool_use_id,
            tool_name="meta_invoke",
            arguments={"name": matched_name},
            synthetic_from_text=tc.synthetic_from_text,
            origin_trace=tc.origin_trace,
        )

    def _force_matched_meta_invoke_tool_calls(
        self,
        tool_calls: list[ToolCall],
    ) -> list[ToolCall]:
        metadata = self.config.metadata or {}
        if not metadata.get("meta_match_tool_choice"):
            return tool_calls
        matched_name = self._matched_meta_skill_name_from_metadata()
        if not matched_name:
            return tool_calls
        for tc in tool_calls:
            if (
                tc.tool_name == "meta_invoke"
                and isinstance(tc.arguments.get("name"), str)
                and tc.arguments["name"].strip()
            ):
                return tool_calls
        if not tool_calls:
            return tool_calls

        first = tool_calls[0]
        logger.warning(
            "agent.meta_match_forced_invoke_rewrite",
            skill=matched_name,
            original_tool=first.tool_name,
            tool_use_id=first.tool_use_id,
        )
        return [
            ToolCall(
                tool_use_id=first.tool_use_id,
                tool_name="meta_invoke",
                arguments={"name": matched_name},
                synthetic_from_text=first.synthetic_from_text,
                origin_trace=first.origin_trace,
            )
        ]

    def _coerce_meta_skill_view_tool_call(self, tc: ToolCall) -> ToolCall:
        if tc.tool_name != "skill_view":
            return tc
        name = tc.arguments.get("name")
        if not isinstance(name, str) or not name.strip():
            return tc
        file_path = tc.arguments.get("file_path")
        if file_path not in (None, "", "SKILL.md", "./SKILL.md"):
            return tc

        metadata = self.config.metadata or {}
        skill_loader = metadata.get("skill_loader")
        if skill_loader is None:
            return tc
        try:
            skill_spec = skill_loader.get_by_name(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent.meta_skill_view_coerce_failed", skill=name, error=str(exc))
            return tc

        if (
            skill_spec is None
            or getattr(skill_spec, "kind", "skill") != "meta"
            or getattr(skill_spec, "disable_model_invocation", False)
        ):
            return tc

        logger.info(
            "agent.meta_skill_view_coerced",
            skill=name,
            tool_use_id=tc.tool_use_id,
        )
        return ToolCall(
            tool_use_id=tc.tool_use_id,
            tool_name="meta_invoke",
            arguments={"name": name},
            synthetic_from_text=tc.synthetic_from_text,
            origin_trace=tc.origin_trace,
        )

    def _build_meta_orchestrator(
        self,
        *,
        workspace_dir: Any,
        triggered_by: str,
        skill_loader: Any,
    ) -> tuple[Any, Any, Any]:
        """Construct a MetaOrchestrator wired to this agent's provider/tools.

        Shared by meta launch paths that need the orchestrator plus its runtime
        context dependencies. Only ``triggered_by`` differs between callers.
        """
        from opensquilla.skills.meta.orchestrator import (
            MetaOrchestrator,
            make_agent_runner_from_parent,
            make_llm_chat_from_provider,
            make_tool_invoker_from_handler,
        )

        meta_correlation = derive_provider_request_correlation(
            self._provider_request_correlation,
            execution_id=uuid.uuid4().hex,
            call_kind="auxiliary.meta",
        )
        runner = make_agent_runner_from_parent(
            provider=self.provider,
            base_config=self.config,
            tool_definitions=self.tool_definitions,
            tool_handler=self._raw_tool_handler,
            agent_factory=type(self),
            workspace_dir=str(workspace_dir) if workspace_dir else None,
            usage_tracker=self._usage_tracker,
            session_key=self._session_key,
            usage_event_sink=self._usage_event_sink,
            usage_execution_context=self._usage_execution_context,
            provider_request_correlation=meta_correlation,
        )
        llm_chat = (
            getattr(self, "_test_llm_chat_override", None)
            or (
                make_llm_chat_from_provider(
                    provider=self.provider,
                    base_config=self.config,
                    usage_tracker=self._usage_tracker,
                    session_key=self._session_key,
                    usage_event_sink=self._usage_event_sink,
                    usage_execution_context=self._usage_execution_context,
                    provider_request_correlation=meta_correlation,
                )
                if self.provider is not None
                else None
            )
        )
        tool_invoker = (
            make_tool_invoker_from_handler(
                tool_handler=self._raw_tool_handler,
                provider_request_correlation=meta_correlation,
            )
            if self._raw_tool_handler is not None
            else None
        )
        orch = MetaOrchestrator(
            agent_runner=runner,
            skill_loader=skill_loader,
            llm_chat=llm_chat,
            tool_invoker=tool_invoker,
            workspace_dir=str(workspace_dir) if workspace_dir else None,
            run_writer=self._meta_run_writer,
            triggered_by=triggered_by,
            session_key=getattr(self, "_session_key", None),
            turn_id=getattr(self, "_turn_id", None),
            memory_persist_enabled=True,
            usage_tracker=self._usage_tracker,
        )
        return orch, llm_chat, tool_invoker

    async def _run_one_streaming(
        self,
        tc: ToolCall,
        tool_context: Any,
    ) -> AsyncIterator[AgentEvent | ToolResult]:
        """Stream a meta_invoke tool call inline and return a terminal ToolResult."""

        import opensquilla.skills.creator  # noqa: F401
        from opensquilla.skills.creator.runtime_e2e import make_runtime_e2e_context
        from opensquilla.skills.meta.enabled import is_meta_skill_enabled
        from opensquilla.skills.meta.inputs import (
            make_meta_inputs,
            meta_input_overrides_from_metadata,
        )
        from opensquilla.skills.meta.orchestrator import (
            MetaOrchestrator,
            make_agent_runner_from_parent,
            make_llm_chat_from_provider,
            make_tool_invoker_from_handler,
        )
        from opensquilla.skills.meta.parser import MetaPlanError, parse_meta_plan
        from opensquilla.skills.meta.types import MetaMatch, MetaResult
        from opensquilla.tools.dispatch import preflight_tool_call
        from opensquilla.tools.types import current_tool_context

        if not is_meta_skill_enabled(self.config):
            yield ToolResult(
                tool_use_id=tc.tool_use_id,
                tool_name="meta_invoke",
                content="meta-skill is disabled by configuration",
                is_error=True,
                terminates_turn=False,
            )
            return

        current_depth = _meta_invoke_depth.get()
        turn_count = _meta_invoke_turn_count.get()
        if current_depth >= MAX_META_INVOKE_DEPTH:
            yield ToolResult(
                tool_use_id=tc.tool_use_id,
                tool_name="meta_invoke",
                content=(
                    f"meta_invoke recursion depth limit reached "
                    f"({MAX_META_INVOKE_DEPTH}); refusing nested call to "
                    f"{tc.arguments.get('name', '<unknown>')!r}."
                ),
                is_error=True,
                terminates_turn=False,
            )
            return
        if turn_count >= MAX_META_INVOKE_PER_TURN:
            yield ToolResult(
                tool_use_id=tc.tool_use_id,
                tool_name="meta_invoke",
                content=(
                    f"meta_invoke per-turn invocation limit reached ({MAX_META_INVOKE_PER_TURN})."
                ),
                is_error=True,
                terminates_turn=False,
            )
            return

        depth_token = _meta_invoke_depth.set(current_depth + 1)
        try:
            _meta_invoke_turn_count.set(turn_count + 1)
            if self._tool_registry is None:
                yield ToolResult(
                    tool_use_id=tc.tool_use_id,
                    tool_name="meta_invoke",
                    content="meta_invoke requires Agent to be constructed with tool_registry",
                    is_error=True,
                    terminates_turn=False,
                )
                return

            effective_ctx = current_tool_context.get() or tool_context
            policy_err = await preflight_tool_call(
                registry=self._tool_registry,
                ctx=effective_ctx,
                tool_call=tc,
            )
            if policy_err is not None:
                yield policy_err
                return

            metadata = self.config.metadata or {}
            skill_loader = metadata.get("skill_loader")
            if skill_loader is None:
                yield ToolResult(
                    tool_use_id=tc.tool_use_id,
                    tool_name="meta_invoke",
                    content=(
                        "meta_invoke unavailable: skill_loader missing from AgentConfig.metadata"
                    ),
                    is_error=True,
                    terminates_turn=False,
                )
                return

            workspace_dir = (
                getattr(effective_ctx, "workspace_dir", None)
                or metadata.get("bootstrap_workspace_dir")
                or getattr(self.config, "workspace_dir", None)
            )
            name = tc.arguments.get("name")
            if not isinstance(name, str) or not name:
                yield ToolResult(
                    tool_use_id=tc.tool_use_id,
                    tool_name="meta_invoke",
                    content="meta_invoke requires a non-empty 'name' argument",
                    is_error=True,
                    terminates_turn=False,
                )
                return

            # Spec §10: "New meta_invoke while awaiting | Reject the new
            # invocation". Without this guard the new run hits the
            # partial unique index on (session_key) WHERE
            # status='awaiting_user' deep inside try_claim_awaiting and
            # the user sees an opaque "awaiting claim rejected" error
            # instead of a clear "please finish or cancel the previous
            # form" hint.
            if self._meta_run_writer is not None and self._session_key:
                try:
                    existing_awaiting = await asyncio.to_thread(
                        self._meta_run_writer.peek_awaiting,
                        session_id=self._session_key,
                    )
                except Exception:  # noqa: BLE001 — fail-open
                    existing_awaiting = None
                if existing_awaiting is not None:
                    yield ToolResult(
                        tool_use_id=tc.tool_use_id,
                        tool_name="meta_invoke",
                        content=(
                            f"Previous meta-skill ({existing_awaiting.step_id!r} "
                            f"in run {existing_awaiting.run_id}) is still "
                            "waiting for your answer. Please complete the "
                            "form or reply 'cancel' before starting a new "
                            "meta-skill."
                        ),
                        is_error=True,
                        terminates_turn=True,
                    )
                    return

            skill_spec = skill_loader.get_by_name(name)
            if skill_spec is None or getattr(skill_spec, "kind", "skill") != "meta":
                yield ToolResult(
                    tool_use_id=tc.tool_use_id,
                    tool_name="meta_invoke",
                    content=f"meta_invoke: {name!r} is not a registered meta-skill",
                    is_error=True,
                    terminates_turn=False,
                )
                return
            if getattr(skill_spec, "disable_model_invocation", False):
                yield ToolResult(
                    tool_use_id=tc.tool_use_id,
                    tool_name="meta_invoke",
                    content=f"meta_invoke: {name!r} is not available for model invocation",
                    is_error=True,
                    terminates_turn=False,
                )
                return

            try:
                plan = parse_meta_plan(skill_spec)
            except MetaPlanError as exc:
                yield ToolResult(
                    tool_use_id=tc.tool_use_id,
                    tool_name="meta_invoke",
                    content=f"meta-skill {name!r} plan invalid: {exc}",
                    is_error=True,
                    terminates_turn=False,
                )
                return
            if plan is None:
                yield ToolResult(
                    tool_use_id=tc.tool_use_id,
                    tool_name="meta_invoke",
                    content=f"meta-skill {name!r} parsed to None",
                    is_error=True,
                    terminates_turn=False,
                )
                return

            meta_correlation = derive_provider_request_correlation(
                self._provider_request_correlation,
                execution_id=uuid.uuid4().hex,
                call_kind="auxiliary.meta",
            )
            runner = make_agent_runner_from_parent(
                provider=self.provider,
                base_config=self.config,
                tool_definitions=self.tool_definitions,
                tool_handler=self._raw_tool_handler,
                agent_factory=type(self),
                workspace_dir=str(workspace_dir) if workspace_dir else None,
                usage_tracker=self._usage_tracker,
                session_key=self._session_key,
                usage_event_sink=self._usage_event_sink,
                usage_execution_context=self._usage_execution_context,
                provider_request_correlation=meta_correlation,
            )
            llm_chat = getattr(self, "_test_llm_chat_override", None) or (
                make_llm_chat_from_provider(
                    provider=self.provider,
                    base_config=self.config,
                    usage_tracker=self._usage_tracker,
                    session_key=self._session_key,
                    usage_event_sink=self._usage_event_sink,
                    usage_execution_context=self._usage_execution_context,
                    provider_request_correlation=meta_correlation,
                )
                if self.provider is not None
                else None
            )
            tool_invoker = (
                make_tool_invoker_from_handler(
                    tool_handler=self._raw_tool_handler,
                    provider_request_correlation=meta_correlation,
                )
                if self._raw_tool_handler is not None
                else None
            )

            memory_persist_enabled = True
            orch = MetaOrchestrator(
                agent_runner=runner,
                skill_loader=skill_loader,
                llm_chat=llm_chat,
                tool_invoker=tool_invoker,
                workspace_dir=str(workspace_dir) if workspace_dir else None,
                run_writer=self._meta_run_writer,
                triggered_by="soft_meta_invoke",
                session_key=getattr(self, "_session_key", None),
                turn_id=getattr(self, "_turn_id", None),
                memory_persist_enabled=memory_persist_enabled,
                usage_tracker=self._usage_tracker,
            )

            system_prompt = (
                self._context.system_prompt
                if self._context is not None
                else self.config.system_prompt or ""
            )
            resolved_match = metadata.get("meta_match")
            if (
                isinstance(resolved_match, MetaMatch)
                and getattr(resolved_match.plan, "name", "") == plan.name
            ):
                match_inputs = dict(resolved_match.inputs)
                match_inputs.setdefault("system_prompt", system_prompt)
                match = MetaMatch(
                    plan=plan,
                    inputs=match_inputs,
                    run_id=resolved_match.run_id,
                )
            else:
                match = MetaMatch(
                    plan=plan,
                    inputs=make_meta_inputs(
                        user_message=(
                            getattr(self, "_current_turn_message", "")
                            or metadata.get("user_message", "")
                        ),
                        system_prompt=system_prompt,
                        **meta_input_overrides_from_metadata(metadata),
                    ),
                )

            result: MetaResult | None = None
            from opensquilla.skills.creator.proposer import (
                reset_runtime_e2e_context,
                reset_smoke_fixture_context,
                set_runtime_e2e_context,
                set_smoke_fixture_context,
            )

            runtime_e2e_ctx = make_runtime_e2e_context(
                provider=self.provider,
                base_config=self.config,
                skill_loader=skill_loader,
                tool_definitions=self.tool_definitions,
                tool_handler=self.tool_handler,
                agent_factory=type(self),
                llm_chat=llm_chat,
                tool_invoker=tool_invoker,
                workspace_dir=str(workspace_dir) if workspace_dir else None,
                usage_tracker=self._usage_tracker,
                session_key=getattr(self, "_session_key", None) or "",
                tool_registry=self._tool_registry,
                tool_context=effective_ctx,
                system_prompt=system_prompt,
                baseline_model=getattr(self.config, "model_id", "") or "",
            )
            runtime_e2e_token = set_runtime_e2e_context(runtime_e2e_ctx)
            smoke_fixture_token = set_smoke_fixture_context({"llm_chat": llm_chat})
            try:
                async for ev in orch.iter_events(match):
                    if isinstance(ev, MetaResult):
                        result = ev
                    elif isinstance(ev, TextDeltaEvent):
                        continue
                    else:
                        yield ev
            except Exception as exc:  # noqa: BLE001
                yield ToolResult(
                    tool_use_id=tc.tool_use_id,
                    tool_name="meta_invoke",
                    content=f"meta-skill {name!r} raised: {exc}",
                    is_error=True,
                    terminates_turn=False,
                )
                return
            finally:
                reset_smoke_fixture_context(smoke_fixture_token)
                reset_runtime_e2e_context(runtime_e2e_token)

            if result is None:
                yield ToolResult(
                    tool_use_id=tc.tool_use_id,
                    tool_name="meta_invoke",
                    content="orchestrator produced no MetaResult sentinel",
                    is_error=True,
                    terminates_turn=False,
                )
                return
            # PR7: a paused MetaResult (awaiting user_input) is NOT a
            # failure. Render the form description into assistant text
            # so IM/CLI fallbacks see it; the Web surface has its own
            # rich form card driven by the synthetic ToolResultEvent
            # emitted by the scheduler, so we suppress the text fallback
            # there to avoid the user seeing both a plain-text dump AND
            # the form (the text was leaking out and looking like the
            # "real" reply in review.
            if result.paused:
                from opensquilla.engine.turn_runner.turn_finalizer_stage import (
                    render_paused_outcome,
                )
                from opensquilla.tools.types import CallerKind

                caller_kind = getattr(self._tool_context, "caller_kind", None)
                is_rich_surface = caller_kind is CallerKind.WEB
                if not is_rich_surface:
                    paused_text = render_paused_outcome(result)
                    if paused_text:
                        yield TextDeltaEvent(text=paused_text)
                yield ToolResult(
                    tool_use_id=tc.tool_use_id,
                    tool_name="meta_invoke",
                    content=(f"meta-skill {name!r} paused awaiting user input."),
                    is_error=False,
                    terminates_turn=True,
                )
                return
            if not result.ok:
                yield self._format_meta_invoke_failure(tc, result, plan)
                return
            if not result.final_text:
                result.final_text = _meta_empty_final_text_fallback(name, match.inputs)
            if result.final_text:
                yield TextDeltaEvent(text=result.final_text)
            yield ToolResult(
                tool_use_id=tc.tool_use_id,
                tool_name="meta_invoke",
                content=(
                    f"meta-skill {name!r} completed."
                    if result.final_text
                    else "(meta-skill completed with no output text)"
                ),
                is_error=False,
                terminates_turn=True,
            )
        finally:
            try:
                _meta_invoke_depth.reset(depth_token)
            except ValueError:
                _meta_invoke_depth.set(current_depth)

    async def _run_meta_resume(self, meta_resume: Any) -> AsyncIterator[Any]:
        """Stream a meta-skill resume's events as a single turn.

        ``meta_resume`` is the tuple ``(claim, parsed_fields)`` that
        ``meta_resolution`` stashes on ctx.metadata after a successful
        try_claim_resume CAS. We build a MetaOrchestrator with the same
        wiring ``_run_one_streaming`` uses, then yield every event from
        ``iter_resume_events`` followed by a synthetic DoneEvent so the
        outer stream pipeline can finalize the turn.
        """
        from opensquilla.engine.types import DoneEvent
        from opensquilla.skills.meta.orchestrator import (
            MetaOrchestrator,
            make_agent_runner_from_parent,
            make_llm_chat_from_provider,
            make_tool_invoker_from_handler,
        )
        from opensquilla.skills.meta.types import MetaResult
        from opensquilla.tools.types import current_tool_context

        try:
            claim, parsed = meta_resume
        except (TypeError, ValueError):
            logger.warning("agent.meta_resume_malformed", extra={"value": str(meta_resume)})
            return

        metadata = self.config.metadata or {}
        skill_loader = metadata.get("skill_loader")
        if skill_loader is None or self._meta_run_writer is None:
            logger.warning(
                "agent.meta_resume_missing_deps",
                extra={
                    "has_loader": skill_loader is not None,
                    "has_writer": self._meta_run_writer is not None,
                },
            )
            return

        # Drop the marker so a re-enter through this turn cannot re-resume.
        if isinstance(metadata, dict):
            metadata.pop("meta_resume", None)

        effective_ctx = current_tool_context.get() or None
        workspace_dir = (
            (getattr(effective_ctx, "workspace_dir", None) if effective_ctx else None)
            or metadata.get("bootstrap_workspace_dir")
            or getattr(self.config, "workspace_dir", None)
        )

        meta_correlation = derive_provider_request_correlation(
            self._provider_request_correlation,
            execution_id=uuid.uuid4().hex,
            call_kind="auxiliary.meta",
        )
        runner = make_agent_runner_from_parent(
            provider=self.provider,
            base_config=self.config,
            tool_definitions=self.tool_definitions,
            tool_handler=self._raw_tool_handler,
            agent_factory=type(self),
            workspace_dir=str(workspace_dir) if workspace_dir else None,
            usage_tracker=self._usage_tracker,
            session_key=self._session_key,
            usage_event_sink=self._usage_event_sink,
            usage_execution_context=self._usage_execution_context,
            provider_request_correlation=meta_correlation,
        )
        llm_chat = getattr(self, "_test_llm_chat_override", None) or (
            make_llm_chat_from_provider(
                provider=self.provider,
                base_config=self.config,
                usage_tracker=self._usage_tracker,
                session_key=self._session_key,
                usage_event_sink=self._usage_event_sink,
                usage_execution_context=self._usage_execution_context,
                provider_request_correlation=meta_correlation,
            )
            if self.provider is not None
            else None
        )
        tool_invoker = (
            make_tool_invoker_from_handler(
                tool_handler=self._raw_tool_handler,
                provider_request_correlation=meta_correlation,
            )
            if self._raw_tool_handler is not None
            else None
        )

        orch = MetaOrchestrator(
            agent_runner=runner,
            skill_loader=skill_loader,
            llm_chat=llm_chat,
            tool_invoker=tool_invoker,
            workspace_dir=str(workspace_dir) if workspace_dir else None,
            run_writer=self._meta_run_writer,
            triggered_by="resume",
            session_key=getattr(self, "_session_key", None),
            turn_id=getattr(self, "_turn_id", None),
            memory_persist_enabled=True,
            usage_tracker=self._usage_tracker,
        )

        result: Any = None
        final_text_parts: list[str] = []
        try:
            async for ev in orch.iter_resume_events(
                payload=claim,
                filled_fields=parsed,
            ):
                if isinstance(ev, MetaResult):
                    result = ev
                    continue
                # Stream nested AgentEvents through (TextDelta, ToolUseStart,
                # ToolResult). Capture text deltas so we can render the
                # final assistant text for the transcript / Done event.
                from opensquilla.engine.types import TextDeltaEvent

                if isinstance(ev, TextDeltaEvent) and ev.text:
                    final_text_parts.append(ev.text)
                yield ev
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent.meta_resume_failed", extra={"error": str(exc)})
            yield DoneEvent(text="", input_tokens=0, output_tokens=0, iterations=0)
            return

        # Build the final assistant text. If the DAG re-paused, use the
        # rendered form text; otherwise use the orchestrator's final_text.
        if result is not None:
            if result.paused:
                from opensquilla.engine.turn_runner.turn_finalizer_stage import (
                    render_paused_outcome,
                )

                final_text = render_paused_outcome(result)
            else:
                final_text = result.final_text or "".join(final_text_parts)
            # Emit only a strict suffix that was not already streamed
            # (for example, the re-pause rendering).  A conflicting terminal
            # snapshot must be reconciled by DoneEvent instead of briefly
            # broadcasting an invalid concatenation to streaming surfaces.
            already_streamed = "".join(final_text_parts)
            if final_text.startswith(already_streamed):
                suffix = final_text[len(already_streamed) :]
            else:
                suffix = ""
            if suffix:
                from opensquilla.engine.types import TextDeltaEvent

                yield TextDeltaEvent(text=suffix)
        else:
            final_text = "".join(final_text_parts)

        yield DoneEvent(
            text=final_text,
            input_tokens=0,
            output_tokens=0,
            iterations=1,
            cost_usd=0.0,
            cost_source="none",
            model=self.config.model_id or "",
            text_snapshot=final_text,
        )

    async def _run_meta_launch(self, name: str) -> AsyncIterator[Any]:
        """Run a meta-skill by name from the explicit /meta command.

        Models its streaming/finalization on ``_run_meta_resume`` and reuses the
        resolution + guards from ``_run_one_streaming`` (enabled gate,
        awaiting-guard, kind/disable validation). Yields nested AgentEvents plus
        a terminal DoneEvent so the turn pipeline finalizes normally.
        """
        import opensquilla.skills.creator  # noqa: F401  (registers e2e hooks)
        from opensquilla.engine.types import DoneEvent, TextDeltaEvent
        from opensquilla.skills.creator.proposer import (
            reset_runtime_e2e_context,
            reset_smoke_fixture_context,
            set_runtime_e2e_context,
            set_smoke_fixture_context,
        )
        from opensquilla.skills.creator.runtime_e2e import make_runtime_e2e_context
        from opensquilla.skills.meta.enabled import is_meta_skill_enabled
        from opensquilla.skills.meta.inputs import (
            make_meta_inputs,
            meta_input_overrides_from_metadata,
        )
        from opensquilla.skills.meta.parser import MetaPlanError, parse_meta_plan
        from opensquilla.skills.meta.types import MetaMatch, MetaResult
        from opensquilla.tools.types import current_tool_context

        metadata = self.config.metadata or {}
        # One-shot: drop the marker so a re-enter through this turn cannot re-run.
        if isinstance(metadata, dict):
            metadata.pop("meta_launch", None)

        if not is_meta_skill_enabled(self.config):
            async for ev in self._emit_terminal_text(
                "Meta-skills are disabled by configuration.", iterations=0
            ):
                yield ev
            return

        skill_loader = metadata.get("skill_loader")
        if skill_loader is None or self._meta_run_writer is None:
            async for ev in self._emit_terminal_text(
                f"Cannot run meta-skill {name!r}: runtime is not fully configured.",
                iterations=0,
            ):
                yield ev
            return

        # Awaiting-guard parity with _run_one_streaming: refuse a new launch
        # while a prior run is waiting for input (avoids the opaque CAS error).
        if self._session_key:
            try:
                existing_awaiting = await asyncio.to_thread(
                    self._meta_run_writer.peek_awaiting,
                    session_id=self._session_key,
                )
            except Exception:  # noqa: BLE001 — fail-open
                existing_awaiting = None
            if existing_awaiting is not None:
                async for ev in self._emit_terminal_text(
                    "A previous meta-skill is still waiting for your answer. "
                    "Please complete the form or reply 'cancel' before starting "
                    "a new meta-skill.",
                    iterations=0,
                ):
                    yield ev
                return

        skill_spec = skill_loader.get_by_name(name)
        if skill_spec is None or getattr(skill_spec, "kind", "skill") != "meta":
            async for ev in self._emit_terminal_text(
                f"{name!r} is not a meta-skill. Type /meta to list available "
                "meta-skills.",
                iterations=0,
            ):
                yield ev
            return
        # Parity with _run_one_streaming and meta.list: skills flagged
        # disable_model_invocation are neither listed nor runnable via /meta.
        if getattr(skill_spec, "disable_model_invocation", False):
            async for ev in self._emit_terminal_text(
                f"{name!r} is not available for invocation.", iterations=0
            ):
                yield ev
            return

        try:
            plan = parse_meta_plan(skill_spec)
        except MetaPlanError as exc:
            async for ev in self._emit_terminal_text(
                f"meta-skill {name!r} plan invalid: {exc}", iterations=0
            ):
                yield ev
            return
        if plan is None:
            async for ev in self._emit_terminal_text(
                f"meta-skill {name!r} parsed to None", iterations=0
            ):
                yield ev
            return

        effective_ctx = current_tool_context.get() or None
        workspace_dir = (
            (getattr(effective_ctx, "workspace_dir", None) if effective_ctx else None)
            or metadata.get("bootstrap_workspace_dir")
            or getattr(self.config, "workspace_dir", None)
        )
        system_prompt = (
            self._context.system_prompt
            if self._context is not None
            else self.config.system_prompt or ""
        )

        orch, llm_chat, tool_invoker = self._build_meta_orchestrator(
            workspace_dir=workspace_dir,
            triggered_by="manual_command",
            skill_loader=skill_loader,
        )
        match = MetaMatch(
            plan=plan,
            inputs=make_meta_inputs(
                user_message=(
                    getattr(self, "_current_turn_message", "")
                    or metadata.get("user_message", "")
                ),
                system_prompt=system_prompt,
                **meta_input_overrides_from_metadata(metadata),
            ),
        )

        # Mirror _run_one_streaming: wrap iter_events in the runtime-e2e / smoke
        # ContextVars so a manually launched meta-skill that spawns sub-agents
        # behaves identically to one launched via the meta_invoke tool.
        runtime_e2e_ctx = make_runtime_e2e_context(
            provider=self.provider,
            base_config=self.config,
            skill_loader=skill_loader,
            tool_definitions=self.tool_definitions,
            tool_handler=self.tool_handler,
            agent_factory=type(self),
            llm_chat=llm_chat,
            tool_invoker=tool_invoker,
            workspace_dir=str(workspace_dir) if workspace_dir else None,
            usage_tracker=self._usage_tracker,
            session_key=getattr(self, "_session_key", None) or "",
            tool_registry=self._tool_registry,
            tool_context=effective_ctx,
            system_prompt=system_prompt,
            baseline_model=getattr(self.config, "model_id", "") or "",
        )
        runtime_e2e_token = set_runtime_e2e_context(runtime_e2e_ctx)
        smoke_fixture_token = set_smoke_fixture_context({"llm_chat": llm_chat})

        result: Any = None
        final_text_parts: list[str] = []
        try:
            async for ev in orch.iter_events(match):
                if isinstance(ev, MetaResult):
                    result = ev
                    continue
                if isinstance(ev, TextDeltaEvent) and ev.text:
                    final_text_parts.append(ev.text)
                yield ev
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "agent.meta_launch_failed", extra={"error": str(exc), "name": name}
            )
            yield DoneEvent(text="", input_tokens=0, output_tokens=0, iterations=0)
            return
        finally:
            reset_smoke_fixture_context(smoke_fixture_token)
            reset_runtime_e2e_context(runtime_e2e_token)

        if result is not None and getattr(result, "paused", False):
            from opensquilla.engine.turn_runner.turn_finalizer_stage import (
                render_paused_outcome,
            )

            final_text = render_paused_outcome(result)
        elif result is not None:
            final_text = result.final_text or "".join(final_text_parts)
        else:
            final_text = "".join(final_text_parts)

        already_streamed = "".join(final_text_parts)
        if final_text.startswith(already_streamed):
            suffix = final_text[len(already_streamed) :]
        else:
            suffix = ""
        if suffix:
            yield TextDeltaEvent(text=suffix)

        yield DoneEvent(
            text=final_text,
            input_tokens=0,
            output_tokens=0,
            iterations=1,
            cost_usd=0.0,
            cost_source="none",
            model=self.config.model_id or "",
            text_snapshot=final_text,
        )

    def _read_clarify_outcome(
        self,
        metadata: dict[str, Any],
    ) -> tuple[str, bool] | None:
        """Translate meta_resolution awaiting-branch metadata into the
        user-visible text dictated by spec §10.

        Returns ``(text, terminates)`` on hit, ``None`` when no clarify
        outcome is staged. Pops the consumed keys so the same turn can't
        re-handle them and a re-entry into ``_turn_generator`` won't
        echo a stale outcome.
        """
        # parse-failure (<3 strikes) — show error list + re-render form
        errors = metadata.pop("meta_clarify_errors", None)
        reprompt = metadata.pop("meta_clarify_reprompt", None)
        if errors and reprompt is not None:
            return self._render_clarify_errors(errors, reprompt), True

        cancelled = metadata.pop("meta_clarify_cancelled", None)
        reason = metadata.pop("meta_clarify_cancel_reason", "")
        if cancelled is not None:
            if reason == "parse_failure_limit":
                return "无法解析回复，已取消上一轮收集。", True
            return "好，已取消。", True

        expired = metadata.pop("meta_clarify_expired", None)
        if expired is not None:
            return "上一轮收集已超时，请重新发起。", True

        race_lost = metadata.pop("meta_clarify_race_lost", None)
        if race_lost is not None:
            return "你之前的回答已被处理。", True

        proceed_blocked = metadata.pop("meta_clarify_proceed_blocked", None)
        soft_progress = metadata.pop("meta_clarify_soft_progress", None)
        if proceed_blocked is not None:
            return self._render_clarify_progress(
                proceed_blocked, proceed_blocked=True,
            ), True
        if soft_progress is not None:
            return self._render_clarify_progress(
                soft_progress, proceed_blocked=False,
            ), True

        return None

    def _render_clarify_progress(
        self, payload: Any, *, proceed_blocked: bool,
    ) -> str:
        """Render soft-clarify progress without exposing internal state."""
        data = payload if isinstance(payload, dict) else {}
        filled = data.get("filled")
        filled_summary = self._format_clarify_filled(filled)
        missing = self._coerce_clarify_names(data.get("missing_required"))
        ambiguous = self._format_clarify_ambiguous(
            data.get("ambiguous_fields"),
        )

        lines: list[str] = []
        if proceed_blocked:
            if missing:
                lines.append(
                    "现在还不能开始，还需要补充："
                    + "、".join(missing)
                    + "。"
                )
            else:
                lines.append("现在还不能开始，还需要补充必填信息。")
            if filled_summary:
                lines.append("已记录：" + filled_summary + "。")
        else:
            if filled_summary:
                lines.append("已记录：" + filled_summary + "。")
            else:
                lines.append("已收到补充。")
            if missing:
                lines.append("还需要：" + "、".join(missing) + "。")
            else:
                lines.append("必填信息已补齐，可以回复“开始”继续。")

        if ambiguous:
            lines.append("仍不确定：" + ambiguous + "。")
        lines.append("你可以直接回复缺少字段，或在上面的表单里填写。")
        return "\n".join(lines)

    @staticmethod
    def _coerce_clarify_names(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        names: list[str] = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                names.append(text)
        return names

    def _format_clarify_filled(self, value: Any) -> str:
        if not isinstance(value, dict):
            return ""
        parts: list[str] = []
        for key in sorted(value):
            label = str(key).strip()
            if not label:
                continue
            parts.append(label + "=" + self._format_clarify_value(value[key]))
            if len(parts) >= 6:
                break
        return "，".join(parts)

    @staticmethod
    def _format_clarify_value(value: Any) -> str:
        if isinstance(value, str):
            text = value
        elif isinstance(value, (dict, list, tuple)):
            try:
                text = json.dumps(value, ensure_ascii=False, sort_keys=True)
            except TypeError:
                text = str(value)
        else:
            text = str(value)
        text = " ".join(text.split())
        if len(text) > 80:
            return text[:77] + "..."
        return text

    @staticmethod
    def _format_clarify_ambiguous(value: Any) -> str:
        if not isinstance(value, list):
            return ""
        parts: list[str] = []
        for entry in value:
            if isinstance(entry, dict):
                name = str(entry.get("name") or "").strip()
                reason = str(entry.get("reason") or "").strip()
                if name and reason:
                    parts.append(name + "（" + reason + "）")
                elif name:
                    parts.append(name)
            elif entry is not None:
                text = str(entry).strip()
                if text:
                    parts.append(text)
            if len(parts) >= 4:
                break
        return "，".join(parts)

    def _render_clarify_errors(
        self,
        errors: Any,
        awaiting: Any,
    ) -> str:
        """Build the parse-error feedback block plus a re-rendered form.

        ``errors`` is the ``list[str]`` returned by ``parse_clarify_reply``;
        ``awaiting`` is the ``AwaitingPeek`` row whose ``awaiting_schema_json``
        is reused to render the form a second time.
        """
        from opensquilla.engine.turn_runner.turn_finalizer_stage import (
            _schema_language,
            render_paused_outcome,
        )
        from opensquilla.skills.meta.plan_serde import (
            clarify_config_from_jsonable,
        )
        from opensquilla.skills.meta.types import MetaPaused, MetaResult

        try:
            schema_payload = json.loads(awaiting.awaiting_schema_json or "{}")
            cfg = clarify_config_from_jsonable(schema_payload)
            language = _schema_language(cfg, cfg.intro)
            lines: list[str] = [
                "未能解析回复：" if language == "zh" else "I could not parse your reply:",
            ]
            for err in errors or []:
                lines.append(f"  - {err}")
            synthetic = MetaResult(
                ok=False,
                paused=True,
                paused_payload=MetaPaused(
                    run_id=awaiting.run_id,
                    step_id=awaiting.step_id,
                    schema=cfg,
                    intro=cfg.intro,
                ),
            )
            form_text = render_paused_outcome(synthetic)
            if form_text:
                lines.append("")
                lines.append(form_text)
        except Exception:  # noqa: BLE001 — best-effort re-render
            lines = ["未能解析回复："]
            for err in errors or []:
                lines.append(f"  - {err}")
            lines.append("")
            lines.append("请按上次的表单格式重新回答，或回 '取消' 终止。")
        return "\n".join(lines)

    async def _emit_terminal_text(
        self,
        text: str,
        *,
        iterations: int,
    ) -> AsyncIterator[Any]:
        """Yield ``TextDeltaEvent(text)`` + a minimal ``DoneEvent`` so the
        stream consumer + transcript treat this as a full assistant turn."""
        from opensquilla.engine.types import DoneEvent, TextDeltaEvent

        if text:
            yield TextDeltaEvent(text=text)
        yield DoneEvent(
            text=text,
            input_tokens=0,
            output_tokens=0,
            iterations=iterations,
            cost_usd=0.0,
            cost_source="none",
            model=self.config.model_id or "",
            text_snapshot=text,
        )

    def _format_meta_invoke_failure(
        self,
        tc: ToolCall,
        result: Any,
        plan: Any,
    ) -> ToolResult:
        per_step_cap = 1200
        lines: list[str] = [
            f"Meta-skill `{getattr(plan, 'name', '?')}` failed at step `{result.failed_step_id}`",
            "",
            f"Error: {result.error}",
            "",
            "Partial outputs:",
        ]
        for sid, text in (result.step_outputs or {}).items():
            if sid == result.failed_step_id:
                continue
            snippet = text if len(text) <= per_step_cap else text[:per_step_cap] + "..."
            lines.extend([f"- {sid}:", snippet, ""])
        lines.append(f"Original meta-skill requested: {tc.arguments.get('name', '')}")
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name="meta_invoke",
            content="\n".join(lines),
            is_error=True,
            terminates_turn=False,
        )

    # ------------------------------------------------------------------
    # Subagent factory
    # ------------------------------------------------------------------

    def _make_child_agent(
        self,
        spec: SubagentSpec,
        depth: int,
        execution_id: str | None = None,
    ) -> Agent:
        from opensquilla.sandbox.run_context import (
            RunContext,
            normalize_scope,
            run_context_for_subagent,
        )
        from opensquilla.session.keys import parse_agent_id
        from opensquilla.tools.types import (
            SUBAGENT_TOOL_DENY,
            CallerKind,
            InteractionMode,
            ToolContext,
            current_tool_context,
        )

        parent_session_key = self._session_key or "unknown"
        subagent_label = spec.label or "subagent"
        child_execution_id = execution_id or uuid.uuid4().hex
        child_provider_request_correlation = derive_provider_request_correlation(
            self._provider_request_correlation,
            execution_id=child_execution_id,
            call_kind="subagent.chat",
        )
        parent_ctx = current_tool_context.get() or self._tool_context
        parent_run_context = getattr(parent_ctx, "sandbox_run_context", None)
        if isinstance(parent_run_context, RunContext):
            parent_run_context = run_context_for_subagent(parent_run_context)
        parent_sandbox_mounts = [
            dict(item)
            for item in (getattr(parent_ctx, "sandbox_mounts", None) or [])
            if isinstance(item, dict)
            and normalize_scope(item.get("scope"), "chat") != "once"
        ]
        parent_run_mode = getattr(parent_ctx, "run_mode", None)
        if parent_run_mode is None:
            run_context_mode = getattr(parent_run_context, "run_mode", None)
            parent_run_mode = getattr(run_context_mode, "value", run_context_mode)
        parent_elevated = getattr(parent_ctx, "elevated", None)
        if parent_run_mode not in {"standard", "trusted", "full"}:
            if parent_elevated == "full":
                parent_run_mode = "full"
            elif parent_elevated in {"on", "bypass"}:
                parent_run_mode = "trusted"
            else:
                parent_run_mode = None

        child_usage_context: UsageExecutionContext | None = None
        if self._usage_event_sink is not None:
            parent_usage_context = self._usage_execution_context
            child_usage_context = UsageExecutionContext(
                execution_id=child_execution_id,
                agent_run_id=child_execution_id,
                turn_id=child_execution_id,
                parent_turn_id=(
                    parent_usage_context.turn_id or parent_usage_context.execution_id
                    if parent_usage_context is not None
                    else None
                ),
                session_id=(
                    parent_usage_context.session_id
                    if parent_usage_context is not None
                    else None
                ),
                session_epoch=(
                    parent_usage_context.session_epoch
                    if parent_usage_context is not None
                    else 0
                ),
                agent_id=(
                    parent_usage_context.agent_id
                    if parent_usage_context is not None
                    else parse_agent_id(parent_session_key)
                ),
                run_kind="subagent",
            )

        # Schema-time filtering: subagents cannot see dangerous tools
        filtered_defs = [td for td in self.tool_definitions if td.name not in SUBAGENT_TOOL_DENY]
        subagent_ctx = ToolContext(
            is_owner=True,
            caller_kind=CallerKind.SUBAGENT,
            interaction_mode=InteractionMode.UNATTENDED,
            subagent_depth=depth,
            agent_id=parse_agent_id(parent_session_key),
            workspace_dir=spec.workspace_dir or self.config.workspace_dir,
            session_key=f"subagent:{parent_session_key}",
            channel_kind="subagent",
            channel_id=f"subagent:{parent_session_key}",
            sender_id=parent_session_key,
            denied_tools=set(SUBAGENT_TOOL_DENY),
            run_mode=parent_run_mode,
            sandbox_mounts=parent_sandbox_mounts,
            sandbox_run_context=parent_run_context,
            elevated=parent_elevated if parent_run_mode == "full" else None,
            tool_result_store_dir=self.config.tool_result_store_dir,
            tool_result_store_session_id=(
                self.config.tool_result_store_session_id or parent_session_key
            ),
            source_diff_preservation_mode=self.config.source_diff_preservation_mode,
            source_diff_candidate_mode=self.config.source_diff_candidate_mode,
            tool_run_budget_key=(
                f"subagent:{parent_session_key}:{subagent_label}:{depth}:{uuid.uuid4().hex}"
            ),
            on_runtime_event=self._record_tool_context_runtime_event
            if self.config.runtime_events_path
            else None,
        )

        async def _subagent_tool_handler(tc: ToolCall) -> ToolResult:
            if self._raw_tool_handler is None:
                return ToolResult(
                    tool_use_id=tc.tool_use_id,
                    tool_name=tc.tool_name,
                    content=f"No tool handler registered for tool '{tc.tool_name}'",
                    is_error=True,
                    execution_status=runtime_execution_status(
                        "error",
                        reason="runtime_error",
                    ),
                )
            with bind_provider_request_correlation(
                child_provider_request_correlation,
            ):
                token = current_tool_context.set(subagent_ctx)
                try:
                    return await self._raw_tool_handler(tc)
                finally:
                    current_tool_context.reset(token)

        child_cfg = AgentConfig(
            max_iterations=spec.max_iterations,
            timeout=spec.timeout,
            provider_id=self.config.provider_id,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            top_p=self.config.top_p,
            max_turn_llm_calls=self.config.max_turn_llm_calls,
            max_turn_input_tokens=self.config.max_turn_input_tokens,
            max_turn_output_tokens=self.config.max_turn_output_tokens,
            max_turn_billed_cost_usd=self.config.max_turn_billed_cost_usd,
            max_turn_cost_usd=self.config.max_turn_cost_usd,
            max_turn_tool_errors=self.config.max_turn_tool_errors,
            length_capped_continuations=self.config.length_capped_continuations,
            context_window_tokens=self.config.context_window_tokens,
            workspace_dir=spec.workspace_dir or self.config.workspace_dir,
            flush_enabled=self.config.flush_enabled,
            flush_triggers=list(self.config.flush_triggers),
            flush_pre_compaction=self.config.flush_pre_compaction,
            flush_timeout_seconds=self.config.flush_timeout_seconds,
            flush_background_timeout_seconds=self.config.flush_background_timeout_seconds,
            flush_backoff_initial_seconds=self.config.flush_backoff_initial_seconds,
            flush_backoff_max_seconds=self.config.flush_backoff_max_seconds,
            flush_archive_max_bytes=self.config.flush_archive_max_bytes,
            flush_compaction_requires_safe_receipt=(
                self.config.flush_compaction_requires_safe_receipt
            ),
            flush_compaction_safety_mode=self.config.flush_compaction_safety_mode,
            compaction_profile=self.config.compaction_profile,
            compaction_protected_recent_messages=(self.config.compaction_protected_recent_messages),
            tool_result_projection_max_inline_chars=(
                self.config.tool_result_projection_max_inline_chars
            ),
            tool_result_fresh_diagnostic_policy_enabled=(
                self.config.tool_result_fresh_diagnostic_policy_enabled
            ),
            tool_result_diagnostic_retrieval_gate_enabled=(
                self.config.tool_result_diagnostic_retrieval_gate_enabled
            ),
            tool_result_fresh_diagnostic_inline_max_chars=(
                self.config.tool_result_fresh_diagnostic_inline_max_chars
            ),
            tool_result_dispatch_max_chars=self.config.tool_result_dispatch_max_chars,
            tool_result_dispatch_turn_max_chars=(
                self.config.tool_result_dispatch_turn_max_chars
            ),
            tool_result_provider_request_max_chars=(
                self.config.tool_result_provider_request_max_chars
            ),
            provider_request_proof_max_chars=self.config.provider_request_proof_max_chars,
            tool_use_argument_provider_request_max_chars=(
                self.config.tool_use_argument_provider_request_max_chars
            ),
            tool_use_argument_projection_enabled=(self.config.tool_use_argument_projection_enabled),
            tool_failure_loop_block_threshold=(self.config.tool_failure_loop_block_threshold),
            provider_context_block_feedback=self.config.provider_context_block_feedback,
            identical_request_loop_break_threshold=(
                self.config.identical_request_loop_break_threshold
            ),
            placeholder_escalation_threshold=self.config.placeholder_escalation_threshold,
            deadline_wrapup_margin_seconds=self.config.deadline_wrapup_margin_seconds,
            reasoning_only_thinking_fallback=self.config.reasoning_only_thinking_fallback,
            deadline_thinking_off_margin_seconds=(
                self.config.deadline_thinking_off_margin_seconds
            ),
            reasoning_stream_char_cap=self.config.reasoning_stream_char_cap,
            patch_hygiene_block_mode=self.config.patch_hygiene_block_mode,
            final_diff_salvage=self.config.final_diff_salvage,
            endgame_git_freeze_margin_seconds=(
                self.config.endgame_git_freeze_margin_seconds
            ),
            max_iterations_deadline_extend_seconds=(
                self.config.max_iterations_deadline_extend_seconds
            ),
            final_diff_salvage_veto=self.config.final_diff_salvage_veto,
            endgame_git_freeze_instrumentation_exempt=(
                self.config.endgame_git_freeze_instrumentation_exempt
            ),
            deadline_wrapup_sticky_thinking_off=(
                self.config.deadline_wrapup_sticky_thinking_off
            ),
            endgame_fix_directive_margin_seconds=(
                self.config.endgame_fix_directive_margin_seconds
            ),
            reasoning_only_act_now=self.config.reasoning_only_act_now,
            plan_only_act_now_threshold=self.config.plan_only_act_now_threshold,
            mid_budget_no_diff_nudge=self.config.mid_budget_no_diff_nudge,
            repeated_tool_call_recovery_threshold=(
                self.config.repeated_tool_call_recovery_threshold
            ),
            repeated_tool_call_recovery_extra_tools=(
                self.config.repeated_tool_call_recovery_extra_tools
            ),
            provider_history_dedup_enabled=self.config.provider_history_dedup_enabled,
            provider_history_dedup_min_repeats=(
                self.config.provider_history_dedup_min_repeats
            ),
            projection_signal_hints=self.config.projection_signal_hints,
            progress_watchdog_mode=self.config.progress_watchdog_mode,
            progress_watchdog_repeated_tool_error_threshold=(
                self.config.progress_watchdog_repeated_tool_error_threshold
            ),
            progress_watchdog_repeated_provider_failure_threshold=(
                self.config.progress_watchdog_repeated_provider_failure_threshold
            ),
            progress_watchdog_repeated_failure_anchor_threshold=(
                self.config.progress_watchdog_repeated_failure_anchor_threshold
            ),
            post_write_convergence_enabled=self.config.post_write_convergence_enabled,
            post_write_convergence_warn_threshold=(
                self.config.post_write_convergence_warn_threshold
            ),
            post_write_convergence_finalize_after_warning=(
                self.config.post_write_convergence_finalize_after_warning
            ),
            tool_loop_observer_mode=self.config.tool_loop_observer_mode,
            runtime_recovery_mode=self.config.runtime_recovery_mode,
            runtime_recovery_source_loop_max_nudges=(
                self.config.runtime_recovery_source_loop_max_nudges
            ),
            runtime_state_capsule_mode=self.config.runtime_state_capsule_mode,
            post_tool_empty_recovery_mode=self.config.post_tool_empty_recovery_mode,
            text_only_tool_recovery_mode=self.config.text_only_tool_recovery_mode,
            reasoning_prefill_recovery_mode=self.config.reasoning_prefill_recovery_mode,
            runtime_events_path=self.config.runtime_events_path,
            max_safe_tool_concurrency=self.config.max_safe_tool_concurrency,
            tool_result_external_keep_recent=self.config.tool_result_external_keep_recent,
            tool_result_store_dir=self.config.tool_result_store_dir,
            tool_result_store_session_id=self.config.tool_result_store_session_id,
            tool_result_store_session_key=self.config.tool_result_store_session_key,
            tool_result_store_agent_id=self.config.tool_result_store_agent_id,
            tool_result_store_full_trace=self.config.tool_result_store_full_trace,
            tool_result_store_max_bytes=self.config.tool_result_store_max_bytes,
            tool_result_store_disk_budget_bytes=(self.config.tool_result_store_disk_budget_bytes),
            tool_result_store_retention_seconds=(self.config.tool_result_store_retention_seconds),
        )
        return Agent(
            provider=self.provider,
            config=child_cfg,
            tool_definitions=filtered_defs,
            tool_handler=_subagent_tool_handler,
            subagent_manager=SubagentManager(spawn_depth=depth),
            tool_context=subagent_ctx,
            usage_event_sink=self._usage_event_sink,
            usage_execution_context=child_usage_context,
            provider_request_correlation=child_provider_request_correlation,
        )

    async def spawn_subagent(self, spec: SubagentSpec) -> str:
        """Spawn a subagent and return its run_id."""
        handle = await self.subagent_manager.spawn(spec, self._make_child_agent)
        return handle.run_id
