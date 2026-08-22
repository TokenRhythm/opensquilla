"""Tool dispatch orchestrator.

This module exposes :func:`build_tool_handler`, the single entry point used
by every caller (gateway, CLI, cron, channel adapters). The pipeline is:

1. Ingress injection guard — before registry lookup.
2. Registry lookup — before any policy check.
3. Optional ``ToolHook.before_tool`` fan-out.
4. Policy chain (:func:`opensquilla.tools.policy.run_chain_with_emit`) —
   first denial wins; chain log emission flows through one site.
5. Handler dispatch inside ``current_tool_context.set(effective_ctx)``.
6. Optional ``ToolHook.after_tool`` fan-out with the raw outcome.
7. Single finalisation point (:func:`opensquilla.tools.policy.finalize.finalize`).
8. ``current_tool_context.reset(token)`` in ``finally``.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import time
import weakref
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import structlog

from opensquilla.artifact_session.mutation_attempts import (
    ArtifactMutationCleanupAmbiguousError,
)
from opensquilla.engine.hooks import ToolHook, ToolHookCall, ToolHookResult
from opensquilla.execution_status import normalize_execution_status
from opensquilla.result_budget import (
    DEFAULT_TOOL_RESULT_BUDGET_POLICY,
    DEFAULT_TOOL_RUN_BUDGET_POLICY,
    DuplicateRetrievalInFlightError,
    TerminalRetrievalReplayError,
    ToolResultBudgetPolicy,
    ToolResultBudgetTracker,
    ToolRunBudgetExceededError,
    ToolRunBudgetPolicy,
    ToolRunBudgetReservation,
    ToolRunBudgetTracker,
    clamp_tool_arguments,
)
from opensquilla.safety.injection_guard import (
    REFUSAL_REASON_TOOL_CALL_IN_UNTRUSTED,
    extract_tool_call_refusal_reason,
)
from opensquilla.sandbox.operation_runtime import (
    prepare_tool_operation_guard,
    record_tool_operation_success,
    run_tool_handler_with_operation_guard,
)
from opensquilla.search_tool_outcome import parse_web_tool_outcome
from opensquilla.tool_boundary import (
    AgentToolHandler,
    ToolCall,
    ToolEffectOutcome,
    ToolResult,
)
from opensquilla.tools.argument_normalization import (
    canonicalize_tool_arguments,
    format_alias_conflicts,
)
from opensquilla.tools.builtin.document_format_adapters import DocumentMutationError
from opensquilla.tools.envelope import build_tool_failure_envelope
from opensquilla.tools.plan_access import preflight_plan_access
from opensquilla.tools.policy import DispatchInput, finalize, run_chain_with_emit
from opensquilla.tools.projected_arguments import find_projected_tool_argument
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.schema_validation import (
    tool_spec_schema_parts,
    validate_tool_arguments,
)
from opensquilla.tools.types import (
    CallerKind,
    InvalidToolArgumentsError,
    ProjectedToolArgumentsError,
    ToolContext,
    current_tool_context,
)

log = structlog.get_logger("opensquilla.tools.dispatch")

__all__ = ["build_tool_handler", "preflight_tool_call"]

_PROVIDER_REPLAY_ARGUMENT_PREFIX = "_opensquilla_replay_"
_MISSING_REQUIRED_ARGUMENT_SHAPE_GUIDANCE_ENV = (
    "OPENSQUILLA_MISSING_REQUIRED_ARGUMENT_SHAPE_GUIDANCE"
)
_REPEATED_CALL_NOTICE_ENV = "OPENSQUILLA_REPEATED_CALL_NOTICE"
# Repeat-tracking state per handler closure holds keys and hashes only, never
# result content.
_REPEATED_CALL_NOTICE_MAX_ENTRIES = 1024
_PROMPT_ANNOTATION_WRITERS = frozenset({"document_apply", "document_patch"})
_PROMPT_ANNOTATION_LOOP_CONTROL = frozenset(
    {
        "document_browser_act",
        "document_browser_inspect",
        "document_browser_reload",
        "document_browser_screenshot",
        "document_finish",
    }
)

# Runtime resource guards are deliberately terminal even inside an autonomous
# candidate loop.  These controls mean the platform has exhausted a global
# budget (rather than the model having made a correctable browser/source
# mistake); the Agent must enter its tools-disabled wrap-up path and the outer
# turn cleanup will discard any uncommitted candidate.
_CANDIDATE_GLOBAL_CONTROL_REASONS = frozenset(
    {
        "tool_run_budget_exhausted",
        "turn_llm_call_budget_exceeded",
        "turn_input_token_budget_exceeded",
        "turn_output_token_budget_exceeded",
        "turn_billed_cost_budget_exceeded",
        "turn_cost_budget_exceeded",
        "turn_tool_error_budget_exceeded",
        "agent_runtime_timeout",
        "total_timeout",
    }
)


def _prompt_annotation_candidate_enabled(
    tool_call: ToolCall,
    ctx: ToolContext | None,
) -> bool:
    """Whether this call belongs to the draft-candidate agent loop."""

    return bool(
        ctx is not None
        and getattr(ctx, "artifact_candidate_loop_controller", None) is not None
        and tool_call.tool_name
        in (_PROMPT_ANNOTATION_WRITERS | _PROMPT_ANNOTATION_LOOP_CONTROL)
        and (
            (exclusive := getattr(ctx, "exclusive_tools", None)) is None
            or tool_call.tool_name in exclusive
        )
    )


def _prompt_annotation_agent_loop_enabled() -> bool:
    """Return whether this bound turn can continue after a durable writer.

    The candidate controller may later replace the compatibility writer path,
    but the public signal is intentionally the surfaced loop-control tool set.
    Ordinary document turns and legacy PromptAnnotation clients retain their
    existing terminal-writer behavior.
    """

    ctx = current_tool_context.get()
    if ctx is None:
        return False
    surfaced = getattr(ctx, "surfaced_tools", None)
    exclusive = getattr(ctx, "exclusive_tools", None)
    names = exclusive if exclusive is not None else surfaced
    return bool(names is not None and _PROMPT_ANNOTATION_LOOP_CONTROL <= set(names))
# ToolSpec carries no mutating/read-only flag, so hash-compare only tools whose
# results are pure functions of their arguments and observed state. Execution
# and write tools stay excluded: byte-identical output from them is no proof
# the call had no effect.
_REPEATED_CALL_NOTICE_SAFE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "read_file",
        "read_source",
        "read_spreadsheet",
        "list_dir",
        "glob_search",
        "grep_search",
        "source_symbols",
        "git_status",
        "git_diff",
        "git_log",
    }
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _plan_access_preflight(
    tool_call: ToolCall,
    registered: Any,
    ctx: ToolContext | None,
) -> ToolResult | None:
    denial = preflight_plan_access(tool_call, registered, ctx)
    if denial is None:
        return None
    log.warning(
        "dispatch.defense_in_depth_block",
        tool=tool_call.tool_name,
        reason="plan_mode_denied",
        tool_use_id=tool_call.tool_use_id,
        agent_id=ctx.agent_id if ctx else None,
        session_key=ctx.session_key if ctx else None,
    )
    return denial


def _resolve_budget_policy(ctx: ToolContext | None) -> ToolResultBudgetPolicy:
    policy = getattr(ctx, "tool_result_budget_policy", None) if ctx is not None else None
    if isinstance(policy, ToolResultBudgetPolicy):
        return policy
    return DEFAULT_TOOL_RESULT_BUDGET_POLICY


def _build_budget_tracker(ctx: ToolContext | None) -> ToolResultBudgetTracker:
    factory = getattr(ctx, "tool_result_budget_tracker_factory", None) if ctx else None
    if callable(factory):
        tracker = factory()
        if isinstance(tracker, ToolResultBudgetTracker):
            return tracker
    return ToolResultBudgetTracker(_resolve_budget_policy(ctx))


def _resolve_run_budget_policy(ctx: ToolContext | None) -> ToolRunBudgetPolicy:
    policy = getattr(ctx, "tool_run_budget_policy", None) if ctx is not None else None
    if isinstance(policy, ToolRunBudgetPolicy):
        return policy
    return DEFAULT_TOOL_RUN_BUDGET_POLICY


def _build_run_budget_tracker(ctx: ToolContext | None) -> ToolRunBudgetTracker:
    factory = getattr(ctx, "tool_run_budget_tracker_factory", None) if ctx else None
    if callable(factory):
        tracker = factory()
        if isinstance(tracker, ToolRunBudgetTracker):
            return tracker
    return ToolRunBudgetTracker(_resolve_run_budget_policy(ctx))


def _build_envelope_result(
    tool_call: ToolCall,
    *,
    exc: Exception,
    policy_denial: bool = False,
    error_class_override: str | None = None,
    user_message_override: str | None = None,
    reason_override: str | None = None,
) -> ToolResult:
    status = {
        "version": 1,
        "status": "error",
        "exit_code": None,
        "timed_out": False,
        "truncated": False,
        "reason": reason_override or ("denied" if policy_denial else "runtime_error"),
        "source": "tool_runtime",
        "preservation_class": "diagnostic",
    }
    return ToolResult(
        tool_use_id=tool_call.tool_use_id,
        tool_name=tool_call.tool_name,
        content=json.dumps(
            build_tool_failure_envelope(
                exc,
                tool_call.tool_name,
                policy_denial=policy_denial,
                error_class_override=error_class_override,
                user_message_override=user_message_override,
            )
        ),
        is_error=True,
        execution_status=normalize_execution_status(status),
    )


def _build_invalid_attempt_result(
    tool_call: ToolCall,
    *,
    reason_code: str,
    user_message: str,
    missing_keys: list[str] | None = None,
    valid_shapes: list[list[str]] | None = None,
) -> ToolResult:
    payload: dict[str, Any] = {
        "status": "rejected",
        "reason_code": reason_code,
        "tool": tool_call.tool_name,
        "received_keys": sorted(str(name) for name in tool_call.arguments if str(name)),
        "retry_allowed": True,
        "user_message": user_message,
        "error_class": "InvalidToolArgumentsError",
    }
    if missing_keys is not None:
        payload["missing_keys"] = missing_keys
    if valid_shapes is not None:
        payload["valid_shapes"] = valid_shapes

    status = {
        "version": 1,
        "status": "error",
        "exit_code": None,
        "timed_out": False,
        "truncated": False,
        "reason": "invalid_tool_arguments",
        "source": "tool_runtime",
        "preservation_class": "diagnostic",
    }
    execution_status: dict[str, Any] = dict(normalize_execution_status(status))
    execution_status["preflight_rejected"] = True
    execution_status["reason_code"] = reason_code
    return ToolResult(
        tool_use_id=tool_call.tool_use_id,
        tool_name=tool_call.tool_name,
        content=json.dumps(payload),
        is_error=True,
        execution_status=execution_status,  # type: ignore[arg-type]
    )


async def _emit_web_retrieval_tool_run_diagnostics(
    *,
    tool_call: ToolCall,
    effective_ctx: ToolContext | None,
    reservation: ToolRunBudgetReservation,
    run_budget_tracker: ToolRunBudgetTracker,
    started_at: float,
    raw_result: Any,
    exception: BaseException | None,
) -> None:
    if not reservation.counted_as_external_text:
        return
    snapshot = await run_budget_tracker.snapshot()
    web_outcome = (
        parse_web_tool_outcome(tool_call.tool_name, raw_result)
        if exception is None
        else None
    )
    if web_outcome is not None:
        status = "error"
    elif exception is None:
        status = "ok"
    elif isinstance(exception, ToolRunBudgetExceededError):
        status = "budget_exhausted"
    else:
        status = "error"
    result_chars = 0
    if raw_result is not None:
        result_chars = len(raw_result if isinstance(raw_result, str) else str(raw_result))
    log.debug(
        "dispatch.web_retrieval_tool_run_diagnostics",
        tool=tool_call.tool_name,
        tool_use_id=tool_call.tool_use_id,
        agent_id=effective_ctx.agent_id if effective_ctx else None,
        session_key=effective_ctx.session_key if effective_ctx else None,
        status=status,
        tool_wall_time_ms=round((time.monotonic() - started_at) * 1000, 3),
        result_chars=result_chars,
        reserved_external_text_chars=reservation.reserved_external_text_chars,
        counted_as_search=reservation.counted_as_search,
        counted_as_fetch=reservation.counted_as_fetch,
        error_kind=web_outcome.error_kind if web_outcome is not None else None,
        **snapshot,
    )


def _build_run_budget_control_result(
    tool_call: ToolCall,
    exc: ToolRunBudgetExceededError,
) -> ToolResult:
    payload = {
        "status": "control",
        "tool": tool_call.tool_name,
        "reason": "tool_run_budget_exhausted",
        "user_message": (
            "The tool was skipped by a runtime resource guard. Continue with "
            "available evidence or choose a smaller request."
        ),
        "retry_allowed": False,
    }
    status = {
        "version": 1,
        "status": "unknown",
        "exit_code": None,
        "timed_out": False,
        "truncated": False,
        "reason": "tool_run_budget_exhausted",
        "source": "tool_runtime",
        "preservation_class": "ephemeral",
    }
    log.info(
        "dispatch.tool_run_budget_exhausted",
        tool=tool_call.tool_name,
        tool_use_id=tool_call.tool_use_id,
        message=str(exc),
    )
    return ToolResult(
        tool_use_id=tool_call.tool_use_id,
        tool_name=tool_call.tool_name,
        content=json.dumps(payload),
        is_error=False,
        execution_status=normalize_execution_status(status),
    )


def _build_duplicate_retrieval_control_result(
    tool_call: ToolCall,
    exc: DuplicateRetrievalInFlightError,
) -> ToolResult:
    payload = {
        "status": "control",
        "tool": tool_call.tool_name,
        "reason": "duplicate_search_in_flight",
        "user_message": (
            "An equivalent search is already running in this turn. Continue with "
            "other work and use the original result when it completes."
        ),
        "retry_allowed": False,
    }
    status = {
        "version": 1,
        "status": "unknown",
        "exit_code": None,
        "timed_out": False,
        "truncated": False,
        "reason": "duplicate_search_in_flight",
        "source": "tool_runtime",
        "preservation_class": "ephemeral",
    }
    log.info(
        "dispatch.duplicate_search_in_flight",
        tool=tool_call.tool_name,
        tool_use_id=tool_call.tool_use_id,
        message=str(exc),
    )
    return ToolResult(
        tool_use_id=tool_call.tool_use_id,
        tool_name=tool_call.tool_name,
        content=json.dumps(payload),
        is_error=False,
        execution_status=normalize_execution_status(status),
    )


def _build_terminal_retrieval_replay_result(
    tool_call: ToolCall,
    exc: TerminalRetrievalReplayError,
) -> ToolResult:
    payload = {
        "status": "error",
        "tool": tool_call.tool_name,
        "reason": "terminal_search_failure_replay",
        "error_kind": exc.error_kind,
        "user_message": (
            "An equivalent search already ended in a non-retryable failure this turn. "
            "Change the query, mode, recency, or domain filters before searching "
            "again, or continue with available evidence."
        ),
        "retry_allowed": False,
    }
    status = {
        "version": 1,
        "status": "error",
        "exit_code": None,
        "timed_out": False,
        "truncated": False,
        "reason": "terminal_search_failure_replay",
        "source": "tool_runtime",
        "preservation_class": "diagnostic",
    }
    log.info(
        "dispatch.terminal_search_failure_replay",
        tool=tool_call.tool_name,
        tool_use_id=tool_call.tool_use_id,
        error_kind=exc.error_kind,
        message=str(exc),
    )
    return ToolResult(
        tool_use_id=tool_call.tool_use_id,
        tool_name=tool_call.tool_name,
        content=json.dumps(payload),
        is_error=True,
        execution_status=normalize_execution_status(status),
    )


def _notify_after_tool_hooks(
    hooks: Sequence[ToolHook],
    hook_call: ToolHookCall | None,
    result: ToolResult,
) -> None:
    if hook_call is None:
        return
    for hook in hooks:
        try:
            hook.after_tool(hook_call, ToolHookResult(result=result))
        except Exception as hook_exc:  # noqa: BLE001 - hooks must not break dispatch
            log.warning(
                "dispatch.tool_hook_failed",
                hook=getattr(hook, "name", type(hook).__name__),
                phase="after_tool",
                error=str(hook_exc),
            )


async def _reserve_tool_call_with_runtime_guards(
    *,
    tracker: ToolRunBudgetTracker,
    tool_call: ToolCall,
    arguments: dict[str, Any],
    ctx: ToolContext | None,
) -> ToolRunBudgetReservation | ToolResult:
    try:
        policy = _resolve_run_budget_policy(ctx)
        return await tracker.reserve_tool_call(
            tool_name=tool_call.tool_name,
            arguments=clamp_tool_arguments(
                tool_call.tool_name,
                arguments,
                policy,
            ),
        )
    except DuplicateRetrievalInFlightError as exc:
        return _build_duplicate_retrieval_control_result(tool_call, exc)
    except TerminalRetrievalReplayError as exc:
        return _build_terminal_retrieval_replay_result(tool_call, exc)
    except ToolRunBudgetExceededError as exc:
        return _build_run_budget_control_result(tool_call, exc)


def _check_injection_guard(
    tool_call: ToolCall, effective_ctx: ToolContext | None
) -> ToolResult | None:
    origin = tool_call.origin_trace
    if not origin:
        return None
    reason = extract_tool_call_refusal_reason(origin)
    if reason != REFUSAL_REASON_TOOL_CALL_IN_UNTRUSTED:
        return None
    log.warning(
        "dispatch.injection_refused",
        tool=tool_call.tool_name,
        reason=reason,
        tool_use_id=tool_call.tool_use_id,
        agent_id=effective_ctx.agent_id if effective_ctx else None,
        session_key=effective_ctx.session_key if effective_ctx else None,
    )
    return _build_envelope_result(
        tool_call,
        exc=ValueError("dispatch injection refused"),
        policy_denial=True,
        error_class_override="InjectionRefused",
        user_message_override=str(reason),
    )


def _record_invalid_tool_arguments_event(
    effective_ctx: ToolContext | None,
    tool_call: ToolCall,
    *,
    reason: str,
    missing: list[str] | None = None,
    required: list[str] | None = None,
    errors: list[str] | None = None,
    shape_guidance_enabled: bool | None = None,
    example_guidance_emitted: bool | None = None,
) -> None:
    if effective_ctx is None or effective_ctx.on_runtime_event is None:
        return
    event: dict[str, Any] = {
        "feature": "tool_arguments",
        "name": "dispatch.invalid_tool_arguments",
        "tool": tool_call.tool_name,
        "tool_name": tool_call.tool_name,
        "tool_use_id": tool_call.tool_use_id,
        "reason": reason,
        "argument_keys": sorted(
            key for key in (str(name) for name in tool_call.arguments) if key
        ),
        "executed": False,
        "agent_id": effective_ctx.agent_id,
        "session_key": effective_ctx.session_key,
    }
    if missing is not None:
        event["missing"] = missing
    if required is not None:
        event["required"] = required
    if errors is not None:
        event["errors"] = errors
    if shape_guidance_enabled is not None:
        event["shape_guidance_enabled"] = shape_guidance_enabled
    if example_guidance_emitted is not None:
        event["example_guidance_emitted"] = example_guidance_emitted
    try:
        effective_ctx.on_runtime_event(event)
    except Exception:
        return


def _closest_tool_names(
    target: str,
    candidates: list[str],
    *,
    limit: int = 3,
    cutoff: float = 0.6,
) -> list[str]:
    """Return up to ``limit`` registered tool names closest to ``target``.

    Used to offer an advisory "did you mean" hint on a registry miss so the
    model can recover a mistyped or glued tool name instead of blindly
    retrying an unavailable one. Pure stdlib difflib; returns an empty list
    when nothing clears the similarity ``cutoff``.
    """
    if not target or not candidates:
        return []
    return difflib.get_close_matches(target, candidates, n=limit, cutoff=cutoff)


def _record_registry_miss_event(
    ctx: ToolContext | None,
    tool_call: ToolCall,
    *,
    is_skill: bool,
    untrusted: bool,
    suggestions: list[str],
) -> None:
    if ctx is None or ctx.on_runtime_event is None:
        return
    event: dict[str, Any] = {
        "feature": "tool_dispatch",
        "name": "dispatch.registry_miss",
        "tool": tool_call.tool_name,
        "tool_name": tool_call.tool_name,
        "tool_use_id": tool_call.tool_use_id,
        "is_skill": is_skill,
        "untrusted_caller": untrusted,
        "suggestions": suggestions,
        "suggestion_emitted": bool(suggestions),
        "executed": False,
        "agent_id": ctx.agent_id,
        "session_key": ctx.session_key,
    }
    try:
        ctx.on_runtime_event(event)
    except Exception:
        return


def _unwrap_nested_json_arguments(
    tool_call: ToolCall,
    registered: Any,
    effective_ctx: ToolContext | None,
) -> ToolCall:
    """Normalize model-emitted OpenAI wire-style nested arguments.

    Some OpenAI-compatible providers/models occasionally put the function-call
    wire field itself into the tool arguments object, e.g.
    ``{"arguments": "{\"path\":\"...\"}"}``. Only unwrap when ``arguments`` is
    not a declared tool parameter and no other executable fields are present.
    """

    arguments = tool_call.arguments
    nested_key = "arguments"
    nested_arguments = arguments.get(nested_key)
    if not isinstance(nested_arguments, str):
        nested_key = "_raw"
        nested_arguments = arguments.get(nested_key)
    if not isinstance(nested_arguments, str):
        return tool_call

    spec = getattr(registered, "spec", None)
    parameters = getattr(spec, "parameters", None) or {}
    if nested_key != "_raw" and isinstance(parameters, dict) and nested_key in parameters:
        return tool_call

    non_replay_keys = {
        key
        for key in arguments
        if not key.startswith(_PROVIDER_REPLAY_ARGUMENT_PREFIX)
    }
    if non_replay_keys != {nested_key}:
        return tool_call

    try:
        parsed_arguments = json.loads(nested_arguments)
    except json.JSONDecodeError:
        return tool_call
    if not isinstance(parsed_arguments, dict):
        return tool_call

    log.warning(
        "dispatch.nested_json_arguments_unwrapped",
        tool=tool_call.tool_name,
        tool_use_id=tool_call.tool_use_id,
        agent_id=effective_ctx.agent_id if effective_ctx else None,
        session_key=effective_ctx.session_key if effective_ctx else None,
        source_key=nested_key,
    )
    return ToolCall(
        tool_use_id=tool_call.tool_use_id,
        tool_name=tool_call.tool_name,
        arguments=parsed_arguments,
        synthetic_from_text=tool_call.synthetic_from_text,
        origin_trace=tool_call.origin_trace,
        continuation=tool_call.continuation,
    )


def _check_non_executable_arguments(
    tool_call: ToolCall,
    effective_ctx: ToolContext | None,
) -> ToolResult | None:
    arguments = tool_call.arguments
    if set(arguments) == {"_raw"} and isinstance(arguments.get("_raw"), str):
        log.warning(
            "dispatch.invalid_tool_arguments",
            tool=tool_call.tool_name,
            tool_use_id=tool_call.tool_use_id,
            agent_id=effective_ctx.agent_id if effective_ctx else None,
            session_key=effective_ctx.session_key if effective_ctx else None,
            reason="unparsed_raw_arguments",
        )
        _record_invalid_tool_arguments_event(
            effective_ctx,
            tool_call,
            reason="unparsed_raw_arguments",
        )
        return _build_invalid_attempt_result(
            tool_call,
            reason_code="unparsed_raw_arguments",
            user_message=InvalidToolArgumentsError.user_message,
        )

    projected_match = find_projected_tool_argument(arguments)
    if projected_match is not None:
        log.warning(
            "dispatch.projected_tool_arguments_refused",
            tool=tool_call.tool_name,
            tool_use_id=tool_call.tool_use_id,
            agent_id=effective_ctx.agent_id if effective_ctx else None,
            session_key=effective_ctx.session_key if effective_ctx else None,
            reason=projected_match.kind,
            field=projected_match.path,
        )
        return _build_envelope_result(
            tool_call,
            exc=ProjectedToolArgumentsError(),
            reason_override="provider_context_projection_reused",
        )

    return None


def _invalid_argument_guidance(
    tool_name: str,
    *,
    missing: list[str],
    effective_ctx: ToolContext | None = None,
) -> str:
    def tool_visible(name: str) -> bool:
        if effective_ctx is None:
            return True
        if name in effective_ctx.denied_tools:
            return False
        if effective_ctx.allowed_tools is not None and name not in effective_ctx.allowed_tools:
            return False
        return True

    missing_set = set(missing)
    if tool_name == "edit_file":
        details = (
            " Valid edit_file shapes: single edit "
            '{"path":"...","old_text":"...","new_text":"..."}; '
            "multi edit "
            '{"path":"...","edits":[{"old_text":"...","new_text":"..."}]}.'
        )
        if "old_text" in missing_set:
            details += " new_text alone cannot identify where to edit."
        if tool_visible("apply_patch"):
            details += (
                " For complex or large edits, prefer apply_patch with a small "
                "unified diff instead of retrying malformed edit_file JSON."
            )
        else:
            details += (
                " For complex or large edits, split the edit into smaller "
                "edit_file calls with complete JSON arguments."
            )
        return details
    if tool_name == "write_file":
        details = ' Valid write_file shape: {"path":"...","content":"..."}.'
        alternatives = [
            name
            for name in ("edit_file", "apply_patch")
            if tool_visible(name)
        ]
        if alternatives:
            details += (
                f" For existing source files, prefer {' or '.join(alternatives)} "
                "so the replacement region stays explicit."
            )
        else:
            details += (
                " For existing source files, only rewrite the full file when the "
                "complete replacement content is intended."
            )
        return details
    if tool_name == "apply_patch":
        return (
            ' Valid apply_patch shape: {"patch":"*** Begin Patch\\n'
            "*** Update File: ...\\n@@ ...\\n*** End Patch\"}."
        )
    if tool_name == "exec_command":
        return (
            ' Valid exec_command shape: {"command":"..."}. '
            "Do not put shell text in `new_text`, `path`, or `_raw`."
        )
    if tool_name == "execute_code":
        return (
            ' Valid execute_code shape: {"code":"..."}. '
            "Use exec_command for shell commands."
        )
    return ""


_EDIT_FILE_OLD_TEXT_KEYS = ("old_text", "oldText", "old_string", "oldString")
_EDIT_FILE_NEW_TEXT_KEYS = ("new_text", "newText", "new_string", "newString")


def _non_blank_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _non_empty_edit_old_text(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _first_string_field(arguments: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str):
            return value
    return None


def _valid_edit_file_shape(arguments: Mapping[str, Any]) -> bool:
    has_single = _non_empty_edit_old_text(arguments.get("old_text")) and isinstance(
        arguments.get("new_text"),
        str,
    )
    if has_single:
        return True
    edits = arguments.get("edits")
    if not isinstance(edits, list) or not edits:
        return False
    for item in edits:
        if not isinstance(item, Mapping):
            return False
        edit_old_text = _first_string_field(item, _EDIT_FILE_OLD_TEXT_KEYS)
        edit_new_text = _first_string_field(item, _EDIT_FILE_NEW_TEXT_KEYS)
        if not _non_empty_edit_old_text(edit_old_text):
            return False
        if not isinstance(edit_new_text, str):
            return False
    return True


def _valid_apply_patch_shape(arguments: Mapping[str, Any]) -> bool:
    return _non_blank_string(arguments.get("patch")) or _non_blank_string(
        arguments.get("path")
    )


def _executable_shape_guidance(tool_name: str) -> str:
    if tool_name == "edit_file":
        return (
            "Valid executable edit_file shapes are "
            '{"path":"...","old_text":"...","new_text":"..."} or '
            '{"path":"...","edits":[{"old_text":"...","new_text":"..."}]}.'
        )
    if tool_name == "apply_patch":
        return (
            "Valid executable apply_patch shapes are "
            '{"patch":"*** Begin Patch\\n..."} or {"path":"scratch/patch.txt"}.'
        )
    return "The tool call is missing an executable argument shape."


def _executable_valid_shapes(tool_name: str) -> list[list[str]]:
    if tool_name == "edit_file":
        return [["path", "old_text", "new_text"], ["path", "edits"]]
    if tool_name == "apply_patch":
        return [["patch"], ["path"]]
    return []


def _check_executable_tool_shape(
    tool_call: ToolCall,
    effective_ctx: ToolContext | None,
) -> ToolResult | None:
    if tool_call.tool_name == "edit_file":
        valid = _valid_edit_file_shape(tool_call.arguments)
    elif tool_call.tool_name == "apply_patch":
        valid = _valid_apply_patch_shape(tool_call.arguments)
    else:
        return None
    if valid:
        return None

    guidance = _executable_shape_guidance(tool_call.tool_name)
    log.warning(
        "dispatch.invalid_tool_arguments",
        tool=tool_call.tool_name,
        tool_use_id=tool_call.tool_use_id,
        agent_id=effective_ctx.agent_id if effective_ctx else None,
        session_key=effective_ctx.session_key if effective_ctx else None,
        reason="missing_executable_shape",
        argument_keys=sorted(str(name) for name in tool_call.arguments if str(name)),
    )
    _record_invalid_tool_arguments_event(
        effective_ctx,
        tool_call,
        reason="missing_executable_shape",
        errors=[guidance],
    )
    return _build_invalid_attempt_result(
        tool_call,
        reason_code="missing_executable_shape",
        user_message=guidance,
        valid_shapes=_executable_valid_shapes(tool_call.tool_name),
    )


def _missing_required_argument_shape_guidance_enabled(
    effective_ctx: ToolContext | None,
) -> bool:
    if (
        effective_ctx is not None
        and effective_ctx.missing_required_argument_shape_guidance
    ):
        return True
    raw = os.environ.get(_MISSING_REQUIRED_ARGUMENT_SHAPE_GUIDANCE_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _format_argument_names(names: list[str], *, limit: int = 8) -> str:
    visible = [name for name in names if name][:limit]
    rendered = ", ".join(f"`{name}`" for name in visible)
    hidden = len(names) - len(visible)
    if hidden > 0:
        suffix = f", and {hidden} more"
        return f"{rendered}{suffix}" if rendered else f"{hidden} argument(s)"
    return rendered


def _missing_required_argument_shape_guidance(
    tool_call: ToolCall,
    *,
    missing: list[str],
) -> str:
    supplied = sorted(str(name) for name in tool_call.arguments if str(name))
    missing_text = _format_argument_names(missing)
    if supplied:
        supplied_text = _format_argument_names(supplied)
        return (
            f" You supplied argument(s): {supplied_text}. "
            f"Missing argument(s): {missing_text}."
        )
    return f" You supplied no arguments. Missing argument(s): {missing_text}."


def _check_required_arguments(
    tool_call: ToolCall,
    registered: Any,
    effective_ctx: ToolContext | None,
) -> ToolResult | None:
    spec = getattr(registered, "spec", None)
    required = getattr(spec, "required", None) or []
    required_names = [str(name) for name in required if isinstance(name, str) and name]
    if not required_names:
        return None

    missing = [name for name in required_names if name not in tool_call.arguments]
    if not missing:
        return None

    missing_text = ", ".join(f"`{name}`" for name in missing)
    required_text = ", ".join(f"`{name}`" for name in required_names)
    user_message = (
        f"The {tool_call.tool_name} tool call is missing required argument(s): "
        f"{missing_text}. Reissue the tool call with complete JSON arguments. "
        f"Required arguments: {required_text}."
    )
    guidance = _invalid_argument_guidance(
        tool_call.tool_name,
        missing=missing,
        effective_ctx=effective_ctx,
    )
    shape_guidance_enabled = _missing_required_argument_shape_guidance_enabled(
        effective_ctx
    )
    if shape_guidance_enabled:
        user_message = (
            f"{user_message}"
            f"{_missing_required_argument_shape_guidance(tool_call, missing=missing)}"
        )
    if guidance:
        user_message = f"{user_message}{guidance}"
    log.warning(
        "dispatch.invalid_tool_arguments",
        tool=tool_call.tool_name,
        tool_use_id=tool_call.tool_use_id,
        agent_id=effective_ctx.agent_id if effective_ctx else None,
        session_key=effective_ctx.session_key if effective_ctx else None,
        reason="missing_required_arguments",
        missing=missing,
        required=required_names,
        argument_keys=sorted(str(name) for name in tool_call.arguments if str(name)),
        shape_guidance_enabled=shape_guidance_enabled,
    )
    _record_invalid_tool_arguments_event(
        effective_ctx,
        tool_call,
        reason="missing_required_arguments",
        missing=missing,
        required=required_names,
        shape_guidance_enabled=shape_guidance_enabled,
    )
    return _build_invalid_attempt_result(
        tool_call,
        reason_code="missing_required_arguments",
        user_message=user_message,
        missing_keys=missing,
    )


def _check_schema_valid_arguments(
    tool_call: ToolCall,
    registered: Any,
    effective_ctx: ToolContext | None,
) -> ToolResult | None:
    spec = getattr(registered, "spec", None)
    properties, required, additional_properties = tool_spec_schema_parts(spec)
    errors = validate_tool_arguments(
        tool_call.arguments,
        properties=properties,
        required=required,
        additional_properties=additional_properties,
    )
    if not errors:
        return None
    user_message = (
        f"The {tool_call.tool_name} tool call arguments did not match the tool "
        f"schema: {'; '.join(errors[:5])}. Reissue the tool call with corrected "
        "JSON arguments."
    )
    guidance = _invalid_argument_guidance(
        tool_call.tool_name,
        missing=[],
        effective_ctx=effective_ctx,
    )
    if guidance:
        user_message = f"{user_message}{guidance}"
    log.warning(
        "dispatch.invalid_tool_arguments",
        tool=tool_call.tool_name,
        tool_use_id=tool_call.tool_use_id,
        agent_id=effective_ctx.agent_id if effective_ctx else None,
        session_key=effective_ctx.session_key if effective_ctx else None,
        reason="schema_validation_failed",
        errors=errors[:5],
        argument_keys=sorted(str(name) for name in tool_call.arguments if str(name)),
        example_guidance_emitted=bool(guidance),
    )
    _record_invalid_tool_arguments_event(
        effective_ctx,
        tool_call,
        reason="schema_validation_failed",
        errors=errors[:5],
        example_guidance_emitted=bool(guidance),
    )
    return _build_invalid_attempt_result(
        tool_call,
        reason_code="schema_validation_failed",
        user_message=user_message,
    )


def _strip_provider_replay_arguments(tool_call: ToolCall) -> ToolCall:
    """Remove provider-history-only replay fields before live tool execution."""

    if not any(key.startswith(_PROVIDER_REPLAY_ARGUMENT_PREFIX) for key in tool_call.arguments):
        return tool_call
    return ToolCall(
        tool_use_id=tool_call.tool_use_id,
        tool_name=tool_call.tool_name,
        arguments={
            key: value
            for key, value in tool_call.arguments.items()
            if not key.startswith(_PROVIDER_REPLAY_ARGUMENT_PREFIX)
        },
        synthetic_from_text=tool_call.synthetic_from_text,
        origin_trace=tool_call.origin_trace,
        continuation=tool_call.continuation,
    )


def _normalize_common_tool_argument_aliases(
    tool_call: ToolCall,
    effective_ctx: ToolContext | None,
) -> tuple[ToolCall, ToolResult | None]:
    """Map common coding-agent argument names to OpenSquilla canonical names."""

    result = canonicalize_tool_arguments(tool_call.tool_name, tool_call.arguments)
    if result.conflicts:
        conflict_messages = format_alias_conflicts(result.conflicts)
        user_message = (
            f"The {tool_call.tool_name} tool call arguments contained conflicting "
            f"aliases: {'; '.join(conflict_messages[:5])}. Reissue the tool call "
            "with only canonical JSON arguments."
        )
        log.warning(
            "dispatch.tool_arguments_alias_conflict",
            tool=tool_call.tool_name,
            tool_use_id=tool_call.tool_use_id,
            agent_id=effective_ctx.agent_id if effective_ctx else None,
            session_key=effective_ctx.session_key if effective_ctx else None,
            conflicts=conflict_messages[:5],
            argument_keys=sorted(str(name) for name in tool_call.arguments if str(name)),
        )
        _record_invalid_tool_arguments_event(
            effective_ctx,
            tool_call,
            reason="alias_conflict",
            errors=conflict_messages[:5],
        )
        return tool_call, _build_invalid_attempt_result(
            tool_call,
            reason_code="alias_conflict",
            user_message=user_message,
        )
    if result.aliases_applied:
        log.info(
            "dispatch.tool_arguments_aliases_applied",
            tool=tool_call.tool_name,
            tool_use_id=tool_call.tool_use_id,
            agent_id=effective_ctx.agent_id if effective_ctx else None,
            session_key=effective_ctx.session_key if effective_ctx else None,
            aliases=result.aliases_applied,
        )
    if not result.changed:
        return tool_call, None
    return ToolCall(
        tool_use_id=tool_call.tool_use_id,
        tool_name=tool_call.tool_name,
        arguments=result.arguments,
        synthetic_from_text=tool_call.synthetic_from_text,
        origin_trace=tool_call.origin_trace,
        continuation=tool_call.continuation,
    ), None


def _repeated_call_notice_threshold() -> int:
    raw = os.environ.get(_REPEATED_CALL_NOTICE_ENV, "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _record_repeated_call_notice_event(
    effective_ctx: ToolContext | None,
    tool_call: ToolCall,
    *,
    arguments_sha256: str,
    result_sha256: str,
    repeat_count: int,
    threshold: int,
) -> None:
    if effective_ctx is None or effective_ctx.on_runtime_event is None:
        return
    event: dict[str, Any] = {
        "feature": "repeated_call_notice",
        "name": "dispatch.repeated_call_notice",
        "tool": tool_call.tool_name,
        "tool_name": tool_call.tool_name,
        "tool_use_id": tool_call.tool_use_id,
        "arguments_sha256": arguments_sha256,
        "result_sha256": result_sha256,
        "repeat_count": repeat_count,
        "threshold": threshold,
        "injected_to_model": True,
        "agent_id": effective_ctx.agent_id,
        "session_key": effective_ctx.session_key,
    }
    try:
        effective_ctx.on_runtime_event(event)
    except Exception:
        return


def _inject_repeated_call_notice(content: str, notice: str) -> str:
    # Downstream consumers json.loads structured tool results (the
    # result_truncated wrapper); those must stay parseable, so the wrapper
    # gets the notice as a key. Anything else — including file bodies that
    # happen to be JSON — must keep its exact bytes, so it gets a text prefix.
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and payload.get("result_truncated") is True:
        payload["repeated_call_notice"] = notice
        return json.dumps(payload, ensure_ascii=False)
    return f"{notice}\n{content}"


def _maybe_apply_repeated_call_notice(
    final_result: ToolResult,
    *,
    tool_call: ToolCall,
    effective_ctx: ToolContext | None,
    raw_result: Any,
    exception: BaseException | None,
    seen: OrderedDict[tuple[str, str, str], tuple[int, str]],
) -> ToolResult:
    threshold = _repeated_call_notice_threshold()
    if threshold <= 0:
        return final_result
    if (
        exception is not None
        or final_result.is_error
        or tool_call.tool_name not in _REPEATED_CALL_NOTICE_SAFE_TOOL_NAMES
        or not isinstance(raw_result, str)
    ):
        return final_result
    arguments_payload = json.dumps(
        tool_call.arguments,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    args_sha = hashlib.sha256(arguments_payload.encode("utf-8")).hexdigest()
    # Hash the pre-budget result: the per-turn budget tracker is stateful, so
    # finalized content is not byte-stable across identical calls.
    result_sha = hashlib.sha256(raw_result.encode("utf-8")).hexdigest()
    # Subagents share the handler closure via current_tool_context; keying on
    # session_key keeps their counters independent of the parent's. Callers
    # with no session identity are never counted: an id()-derived fallback
    # aliases (id reuse after GC; every None ctx shares one id) and could
    # merge unrelated callers into one counter.
    if effective_ctx is None or not effective_ctx.session_key:
        return final_result
    scope_key = effective_ctx.session_key
    key = (scope_key, tool_call.tool_name, args_sha)
    previous = seen.get(key)
    if previous is not None and previous[1] == result_sha:
        count = previous[0] + 1
    else:
        count = 1
    seen[key] = (count, result_sha)
    seen.move_to_end(key)
    while len(seen) > _REPEATED_CALL_NOTICE_MAX_ENTRIES:
        seen.popitem(last=False)
    if count < threshold:
        return final_result
    notice = (
        f"[repeated_call_notice] This exact {tool_call.tool_name} call has "
        f"already been run {count} times this session and returned an "
        "identical result each time."
    )
    final_result.content = _inject_repeated_call_notice(final_result.content, notice)
    _record_repeated_call_notice_event(
        effective_ctx,
        tool_call,
        arguments_sha256=args_sha,
        result_sha256=result_sha,
        repeat_count=count,
        threshold=threshold,
    )
    return final_result


def _is_untrusted_caller(ctx: ToolContext | None) -> bool:
    """Return True when the caller cannot be trusted with tool-name disclosure.

    Untrusted callers (CHANNEL surfaces without owner standing, or anonymous
    callers with no ``ToolContext`` at all) must receive an opaque envelope on
    a registry miss so they cannot enumerate the tool catalogue by probing
    names. Owner CHANNEL traffic is treated as trusted because owner promotion
    happens upstream and the owner already sees the full tool surface.
    """
    if ctx is None:
        return True
    return ctx.caller_kind is CallerKind.CHANNEL and not ctx.is_owner


def _resolve_registry_miss(
    tool_call: ToolCall,
    known_skill_names: frozenset[str],
    ctx: ToolContext | None,
    registry: ToolRegistry,
) -> ToolResult:
    untrusted = _is_untrusted_caller(ctx)
    is_skill = tool_call.tool_name in known_skill_names

    # Advisory "did you mean" recovery hint. Only computed for trusted callers
    # on the generic path: untrusted callers receive an opaque envelope (below)
    # and must not be able to enumerate the catalogue, skills have their own
    # redirect, and ``bash`` has a targeted exec_command redirect.
    suggestions: list[str] = []
    if not untrusted and not is_skill and tool_call.tool_name != "bash":
        suggestions = _closest_tool_names(tool_call.tool_name, registry.list_names())

    # Always record the actual tool name in the structured log so operators
    # retain debug visibility regardless of what the caller is allowed to see.
    log.warning(
        "dispatch.registry_miss",
        tool=tool_call.tool_name,
        tool_use_id=tool_call.tool_use_id,
        is_skill=is_skill,
        untrusted_caller=untrusted,
        agent_id=ctx.agent_id if ctx else None,
        session_key=ctx.session_key if ctx else None,
    )
    _record_registry_miss_event(
        ctx,
        tool_call,
        is_skill=is_skill,
        untrusted=untrusted,
        suggestions=suggestions,
    )

    if untrusted:
        # Opaque envelope: do NOT echo tool_call.tool_name. A bare CHANNEL
        # caller could otherwise enumerate the registry by probing names and
        # observing which ones come back as ToolNotFound vs. UnsupportedSurface.
        return _build_envelope_result(
            tool_call,
            exc=PermissionError("tool unavailable for this surface"),
            policy_denial=True,
            error_class_override="PolicyDenied",
            user_message_override="Tool unavailable for this surface.",
        )

    if is_skill:
        skill_name = tool_call.tool_name
        user_message = (
            f"{skill_name} is a skill, not a tool. Do not call skill names as tools. "
            f'Use skill_view(name="{skill_name}") to read the skill instructions, '
            "then continue using only tools listed in Available Tools."
        )
        return _build_envelope_result(
            tool_call,
            exc=ValueError("skill call mismatch"),
            policy_denial=True,
            error_class_override="UnsupportedSurface",
            user_message_override=user_message,
        )
    if tool_call.tool_name == "bash":
        user_message = (
            "Tool not found: bash. Use exec_command with a command string instead; "
            "do not retry bash as a tool."
        )
    else:
        user_message = (
            f"Tool not found: {tool_call.tool_name}. Do not retry unavailable tools; "
            "use only tools listed in Available Tools."
        )
        if suggestions:
            user_message += " Did you mean: " + ", ".join(suggestions) + "?"
    return _build_envelope_result(
        tool_call,
        exc=KeyError(tool_call.tool_name),
        policy_denial=True,
        error_class_override="ToolNotFound",
        user_message_override=user_message,
    )


async def preflight_tool_call(
    *,
    registry: ToolRegistry,
    ctx: ToolContext | None,
    tool_call: ToolCall,
    known_skill_names: set[str] | frozenset[str] | None = None,
) -> ToolResult | None:
    """Return a denial envelope when a tool call fails dispatch preflight."""
    known = frozenset(known_skill_names or ())
    injection_envelope = _check_injection_guard(tool_call, ctx)
    if injection_envelope is not None:
        return injection_envelope

    exclusive_denial = _exclusive_tool_ceiling_preflight(tool_call, ctx)
    if exclusive_denial is not None:
        return exclusive_denial

    registered = registry.get(tool_call.tool_name)
    if registered is None:
        return _resolve_registry_miss(tool_call, known, ctx, registry)

    plan_access_denial = _plan_access_preflight(tool_call, registered, ctx)
    if plan_access_denial is not None:
        return plan_access_denial

    tool_call = _unwrap_nested_json_arguments(tool_call, registered, ctx)
    injection_envelope = _check_injection_guard(tool_call, ctx)
    if injection_envelope is not None:
        return injection_envelope

    non_executable_arguments = _check_non_executable_arguments(tool_call, ctx)
    if non_executable_arguments is not None:
        return non_executable_arguments
    tool_call = _strip_provider_replay_arguments(tool_call)
    tool_call, alias_normalization_error = _normalize_common_tool_argument_aliases(
        tool_call,
        ctx,
    )
    if alias_normalization_error is not None:
        return alias_normalization_error
    missing_required_arguments = _check_required_arguments(tool_call, registered, ctx)
    if missing_required_arguments is not None:
        return missing_required_arguments
    schema_valid_arguments = _check_schema_valid_arguments(tool_call, registered, ctx)
    if schema_valid_arguments is not None:
        return schema_valid_arguments
    executable_shape = _check_executable_tool_shape(tool_call, ctx)
    if executable_shape is not None:
        return executable_shape

    dispatch_input = DispatchInput(
        tool_call=tool_call,
        ctx=ctx,
        registered=registered,
        known_skill_names=known,
        registry=registry,
    )

    def _emit_policy_log(log_event: dict) -> None:
        event = log_event.get("event", "dispatch.policy_block")
        fields = {k: v for k, v in log_event.items() if k != "event"}
        log.warning(event, **fields)

    decision = run_chain_with_emit(dispatch_input, emit=_emit_policy_log)
    if not decision.allowed:
        if decision.envelope is None:
            raise RuntimeError("PolicyCheck returned a denial without an envelope")
        return decision.envelope
    return None


def _prompt_annotation_writer_is_guarded(
    tool_call: ToolCall,
    ctx: ToolContext | None,
) -> bool:
    """Return whether this server-bound writer needs a durable receipt."""

    return bool(
        tool_call.tool_name in _PROMPT_ANNOTATION_WRITERS
        and ctx is not None
        and (
            ctx.artifact_mutation_attempt_controller is not None
            or getattr(ctx, "artifact_candidate_loop_controller", None) is not None
        )
        and ctx.surfaced_tools is not None
        and tool_call.tool_name in ctx.surfaced_tools
        and (
            ctx.exclusive_tools is None
            or tool_call.tool_name in ctx.exclusive_tools
        )
    )


def _mutation_attempt_status(attempt: Any) -> str:
    status = getattr(attempt, "status", "")
    value = getattr(status, "value", status)
    return str(value or "").strip().lower()


def _document_mutation_details(
    *,
    status: str,
    phase: str,
    retry_policy: str,
    code: str,
    attempt: Any | None = None,
    extra_outcome: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "version": 1,
        "status": status,
        "phase": phase,
        "retryPolicy": retry_policy,
        "code": code,
    }
    if retry_policy == "refresh":
        outcome["refreshRequired"] = True
    if extra_outcome:
        outcome.update(extra_outcome)
    if attempt is not None:
        optional_fields = {
            "attemptId": "mutation_attempt_id",
            "documentId": "document_id",
            "baseRevisionId": "base_revision_id",
            "resultRevisionId": "revision_id",
            "changeSetId": "change_set_id",
            "stateRevision": "state_revision",
        }
        for public_name, attribute_name in optional_fields.items():
            value = getattr(attempt, attribute_name, None)
            if isinstance(value, (str, int)) and value not in {"", 0}:
                outcome[public_name] = value
    return {"documentMutationOutcome": outcome}


def _mutation_result_code(result: ToolResult, fallback: str) -> str:
    try:
        payload = json.loads(result.content)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        for key in ("user_message", "message", "reason"):
            value = payload.get(key)
            if not isinstance(value, str):
                continue
            match = re.match(r"^([A-Z][A-Z0-9_]{3,}):", value.strip())
            if match is not None:
                return match.group(1)
    return fallback


def _proposal_failure_policy(
    *,
    failure_code: str,
    stable_code: str,
    exception: BaseException | None = None,
) -> tuple[str, str, str]:
    """Map a pure proposal rejection to retry, loop, and public status."""

    if isinstance(exception, DocumentMutationError):
        if exception.retry_policy == "forbidden":
            return "never", "finalize_without_tools", "not_attempted"
        if exception.retry_policy == "refresh":
            return "refresh", "finalize_without_tools", "not_attempted"
        return "same_turn", "continue", "not_attempted"

    forbidden_dispatch = {
        "writer_injection_rejected",
        "writer_arguments_rejected",
        "writer_exclusive_denied",
        "writer_plan_denied",
        "writer_policy_denied",
        "writer_unregistered",
        "writer_runtime_only_argument",
        "writer_non_executable_arguments",
        "writer_continuation_mismatch",
        "writer_runtime_guard_denied",
    }
    upper = stable_code.upper()
    if failure_code in forbidden_dispatch or any(
        marker in upper
        for marker in ("UNSAFE", "AUTHORITY", "FORBIDDEN", "SCOPE", "QUERY_LIMIT")
    ):
        return "never", "finalize_without_tools", "not_attempted"
    if any(
        marker in upper
        for marker in (
            "CONFLICT",
            "STALE",
            "SESSION",
            "TOKEN_INVALID",
            "TOKEN_USED",
            "CURSOR_INVALID",
        )
    ):
        return "refresh", "finalize_without_tools", "not_attempted"
    return "same_turn", "continue", "not_attempted"


def _set_mutation_payload_control(
    result: ToolResult,
    *,
    retry_policy: str,
    outcome_code: str,
) -> None:
    """Append bounded loop guidance to an already-sanitized tool result."""

    try:
        payload = json.loads(result.content)
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        return
    payload["retry_allowed"] = retry_policy == "same_turn"
    payload["retry_policy"] = retry_policy
    payload["outcome_code"] = outcome_code
    result.content = json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _candidate_finish_has_durable_attempt(context: Any | None) -> bool:
    """Return whether ``document_finish`` crossed its durable receipt boundary.

    The candidate controller keeps this identity turn-local while a finish
    request reserves/registers its mutation attempt.  A failure after that
    point cannot safely be treated like a normal validation error: the receipt
    may be RESERVED, APPLIED, or unreadable.  Support the public property used
    by newer controllers and the private field retained by older in-process
    controllers so rolling upgrades remain fail-closed.
    """

    controller = getattr(context, "artifact_candidate_loop_controller", None)
    if controller is None:
        return False
    for name in (
        "mutation_attempt_id",
        "_mutation_attempt_id",
        "mutation_attempt_tool_use_id",
        "_mutation_attempt_tool_use_id",
    ):
        value = getattr(controller, name, None)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _candidate_finish_is_ambiguous(
    *,
    tool_call: ToolCall,
    payload: Mapping[str, Any] | None,
    exception: BaseException | None,
    context: Any | None,
) -> bool:
    """Classify only durable finish uncertainty as an ambiguous outcome.

    Browser/runtime failures and pre-commit validation errors are recoverable
    observations for the model.  The exception is different only when a
    ``document_finish`` call has already reserved a durable mutation attempt;
    retrying another writer could then create a second side effect.  Handlers
    may also mark an explicitly ambiguous result, useful for a lost response
    after an atomic transaction.
    """

    if tool_call.tool_name != "document_finish":
        return False
    if bool(getattr(exception, "candidate_commit_ambiguous", False)):
        return True
    if isinstance(payload, Mapping):
        if str(payload.get("status") or "").strip().lower() == "ambiguous":
            return True
        for key in ("outcome_code", "outcomeCode", "reason", "code"):
            value = payload.get(key)
            if isinstance(value, str) and value in {
                "document_finish_commit_ambiguous",
                "DOCUMENT_FINISH_COMMIT_AMBIGUOUS",
                "mutation_attempt_ambiguous",
            }:
                return True
    return _candidate_finish_has_durable_attempt(context)


async def _candidate_terminal_receipt(
    *,
    tool_call: ToolCall,
    context: Any | None,
) -> ToolResult | None:
    """Project a terminal candidate state after a late dispatch exception."""

    if tool_call.tool_name != "document_finish" or context is None:
        return None
    controller = getattr(context, "artifact_candidate_loop_controller", None)
    if controller is None:
        return None
    state = getattr(controller, "state", None)
    status = str(getattr(state, "status", "") or "")
    if status not in {"committed", "discarded"}:
        # A response can be lost immediately after the repository transaction
        # and before the controller updates its local state.  Reconcile once
        # before declaring the late exception unrecoverable.
        reconcile = getattr(controller, "reconcile", None)
        if callable(reconcile):
            try:
                await reconcile()
            except Exception:  # noqa: BLE001 - preserve the original failure
                return None
            state = getattr(controller, "state", None)
            status = str(getattr(state, "status", "") or "")
    if status == "committed":
        return _mutation_receipt_result(
            tool_call,
            status="applied",
            reason="document_mutation_applied_after_dispatch_failure",
        )
    if status == "discarded":
        result = ToolResult(
            tool_use_id=tool_call.tool_use_id,
            tool_name=tool_call.tool_name,
            content=json.dumps(
                {
                    "status": "discarded",
                    "reason": "document_mutation_discarded_after_dispatch_failure",
                    "retry_allowed": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            is_error=False,
        )
        return await _candidate_loop_effect_result(result, tool_call=tool_call)
    return None


def _mutation_effect_result(
    result: ToolResult,
    *,
    effect_state: str,
    retry_policy: str,
    loop_action: str,
    outcome_code: str,
    mutation_status: str,
    phase: str,
    attempt: Any | None = None,
    extra_outcome: Mapping[str, Any] | None = None,
) -> ToolResult:
    # A PromptAnnotation autonomous loop keeps the model in control after the
    # first durable compatibility writer so it can inspect the bound preview
    # and explicitly call document_finish.  Only a confirmed committed result
    # is converted; ambiguous/failed receipts still close the side-effect
    # boundary fail-closed.
    if (
        loop_action == "finalize_without_tools"
        and effect_state == "committed"
        and result.tool_name in _PROMPT_ANNOTATION_WRITERS
        and _prompt_annotation_agent_loop_enabled()
    ):
        loop_action = "continue"
        retry_policy = "same_turn"
    result.terminates_turn = False
    result.terminal_response_text = None
    result.effect_outcome = ToolEffectOutcome(
        effect_state=effect_state,
        retry_policy=retry_policy,
        loop_action=loop_action,
        outcome_code=outcome_code,
        safe_details=_document_mutation_details(
            status=mutation_status,
            phase=phase,
            retry_policy=retry_policy,
            code=outcome_code,
            attempt=attempt,
            extra_outcome=extra_outcome,
        ),
    )
    _set_mutation_payload_control(
        result,
        retry_policy=retry_policy,
        outcome_code=outcome_code,
    )
    return result


async def _candidate_loop_effect_result(
    result: ToolResult,
    *,
    tool_call: ToolCall,
    exception: BaseException | None = None,
) -> ToolResult:
    """Attach loop semantics to candidate/browser/finish results.

    Candidate writers and browser observations are deliberately non-terminal;
    only a durable ``document_finish`` decision closes the loop.  Keeping this
    projection in dispatch makes the same contract apply to schema failures,
    policy denials, and handler exceptions.
    """

    context = current_tool_context.get()
    invalidate = getattr(
        getattr(context, "artifact_candidate_loop_controller", None),
        "invalidate_verification",
        None,
    )
    if tool_call.tool_name in _PROMPT_ANNOTATION_WRITERS or tool_call.tool_name in {
        "document_browser_act",
        "document_browser_reload",
    }:
        # A failed proposal/action must not leave an earlier receipt usable.
        # Keep this cleanup best-effort: the public result remains the stable
        # model-facing error even if the draft was already closed elsewhere.
        if context is not None:
            setattr(context, "_artifact_browser_verification_token", None)
            setattr(context, "_artifact_browser_verification_sha256", None)
        if callable(invalidate):
            try:
                await invalidate(reason="candidate_loop_operation")
            except Exception:  # noqa: BLE001 - stale/closed candidates fail closed below
                pass
    try:
        payload = json.loads(result.content)
    except (TypeError, ValueError):
        payload = None
    status = str(payload.get("status") or "") if isinstance(payload, dict) else ""
    candidate_preview_unavailable = (
        tool_call.tool_name in _PROMPT_ANNOTATION_WRITERS
        and isinstance(payload, dict)
        and payload.get("preview") == "unavailable"
    )
    # ``document_finish`` crosses the durable revision boundary before tool
    # accounting/telemetry is finalized.  A budget tracker or hook can fail
    # after that commit and hand us an error-shaped result; never project that
    # as ``not_applied`` or ask the model to retry.  The controller's terminal
    # state is the authoritative local receipt for this still-running turn.
    candidate_controller = getattr(context, "artifact_candidate_loop_controller", None)
    candidate_state = getattr(candidate_controller, "state", None)
    if (
        tool_call.tool_name == "document_finish"
        and getattr(candidate_state, "status", None) in {"committed", "discarded"}
        and (exception is not None or status != "applied" or result.is_error)
    ):
        terminal_status = (
            "applied"
            if getattr(candidate_state, "status", None) == "committed"
            else "discarded"
        )
        return _mutation_receipt_result(
            tool_call,
            status=terminal_status,
            reason=(
                "document_mutation_applied_after_accounting_failure"
                if terminal_status == "applied"
                else "document_mutation_discarded_after_accounting_failure"
            ),
        )
    if candidate_preview_unavailable:
        # Without a bindable preview the candidate can never produce the
        # verification receipt required by document_finish(commit).  Keep the
        # lifecycle tool available so the model remains responsible for the
        # explicit discard decision; a plain-text stop is nudged back into the
        # loop, while the global deadline/cost/call fuses still reject an
        # abandoned draft during turn cleanup.
        result.effect_outcome = ToolEffectOutcome(
            effect_state="started",
            retry_policy="same_turn",
            loop_action="continue",
            outcome_code="document_preview_unavailable",
            safe_details={
                "documentMutationOutcome": {
                    "version": 1,
                    "status": "not_applied",
                    "phase": "candidate",
                    "retryPolicy": "same_turn",
                    "code": "document_preview_unavailable",
                }
            },
        )
        _set_mutation_payload_control(
            result,
            retry_policy="same_turn",
            outcome_code="document_preview_unavailable",
        )
        result.terminates_turn = False
        result.terminal_response_text = None
        return result
    # The ordinary tool-run budget is a global circuit breaker, not a model
    # correction opportunity.  Close the loop here so an open candidate is
    # discarded by the turn cleanup and the tools-disabled finalizer can report
    # a truthful not-applied outcome.  All other candidate/browser failures are
    # intentionally fed back to the model: it may inspect again, repair with a
    # different writer, or explicitly call document_finish(discard).
    budget_exhausted = isinstance(exception, ToolRunBudgetExceededError) or (
        isinstance(payload, dict)
        and payload.get("status") == "control"
        and (
            payload.get("reason") in _CANDIDATE_GLOBAL_CONTROL_REASONS
            or payload.get("retry_allowed") is False
        )
    )
    if budget_exhausted:
        result.effect_outcome = ToolEffectOutcome(
            effect_state="none",
            retry_policy="new_turn",
            loop_action="finalize_without_tools",
            outcome_code="document_tool_budget_exhausted",
            safe_details={
                "documentMutationOutcome": {
                    "version": 1,
                    "status": "not_applied",
                    "phase": "verification"
                    if tool_call.tool_name.startswith("document_browser_")
                    else "candidate",
                    "retryPolicy": "new_turn",
                    "code": "document_tool_budget_exhausted",
                }
            },
        )
        _set_mutation_payload_control(
            result,
            retry_policy="new_turn",
            outcome_code="document_tool_budget_exhausted",
        )
        result.terminates_turn = False
        result.terminal_response_text = None
        return result
    # Runtime control results (for example a browser tool budget exhaustion)
    # are intentionally ``is_error=False`` so generic tool accounting does not
    # treat them as handler failures.  Only the explicit global-control reasons
    # above close the loop; ordinary retry_allowed=false envelopes are still
    # model feedback in this autonomous turn.
    payload_retry_disallowed = (
        isinstance(payload, dict) and payload.get("retry_allowed") is False
    )
    browser_preview_unavailable = (
        tool_call.tool_name.startswith("document_browser_")
        and (
            "DOCUMENT_BROWSER_UNAVAILABLE" in result.content
            or "DOCUMENT_BROWSER_PREVIEW_UNAVAILABLE" in result.content
        )
    )
    if browser_preview_unavailable:
        # A missing/v3 bridge cannot be repaired by another browser iteration,
        # but it is still the model's decision to abandon the candidate.  Feed
        # the failure back with document_finish(discard) still available.
        result.effect_outcome = ToolEffectOutcome(
            effect_state="none",
            retry_policy="same_turn",
            loop_action="continue",
            outcome_code="document_preview_unavailable",
            safe_details={
                "documentMutationOutcome": {
                    "version": 1,
                    "status": "not_applied",
                    "phase": "verification",
                    "retryPolicy": "same_turn",
                    "code": "document_preview_unavailable",
                }
            },
        )
        _set_mutation_payload_control(
            result,
            retry_policy="same_turn",
            outcome_code="document_preview_unavailable",
        )
        result.terminates_turn = False
        result.terminal_response_text = None
        return result
    durable_ambiguous = _candidate_finish_is_ambiguous(
        tool_call=tool_call,
        payload=payload if isinstance(payload, Mapping) else None,
        exception=exception,
        context=context,
    )
    if durable_ambiguous and status not in {"applied", "discarded"}:
        # A finish request that already reserved a durable mutation receipt is
        # not safe to replay as another writer.  Keep the outcome explicitly
        # ambiguous and enter the no-tools finalizer; the receipt/reconcile
        # path remains authoritative for restart recovery.
        result.effect_outcome = ToolEffectOutcome(
            effect_state="unknown",
            retry_policy="reconcile",
            loop_action="finalize_without_tools",
            outcome_code="document_finish_commit_ambiguous",
            safe_details={
                "documentMutationOutcome": {
                    "version": 1,
                    "status": "ambiguous",
                    "phase": "commit",
                    "retryPolicy": "reconcile",
                    "code": "document_finish_commit_ambiguous",
                }
            },
        )
        _set_mutation_payload_control(
            result,
            retry_policy="reconcile",
            outcome_code="document_finish_commit_ambiguous",
        )
        result.terminates_turn = False
        result.terminal_response_text = None
        return result
    if exception is not None or result.is_error or payload_retry_disallowed:
        # ``SafeToolError`` is deliberately conservative in the generic
        # envelope (many classes set retry_allowed=false), but that bit does
        # not mean the autonomous document loop must terminate.  A browser
        # runtime exception, stale anchor, unavailable preview, or rejected
        # writer is useful feedback for the model and is safe to retry under
        # the global deadline/cost/call guards.
        retry_policy = "same_turn"
        loop_action = "continue"
        outcome_code = (
            "document_browser_verification_failed"
            if tool_call.tool_name.startswith("document_browser_")
            else "document_finish_retry"
            if tool_call.tool_name == "document_finish"
            else "document_candidate_retry"
        )
        status = (
            "verification_failed"
            if tool_call.tool_name.startswith("document_browser_")
            else "not_applied"
        )
        phase = (
            "verification"
            if tool_call.tool_name.startswith("document_browser_")
            else "commit"
            if tool_call.tool_name == "document_finish"
            else "candidate"
        )
        result.effect_outcome = ToolEffectOutcome(
            effect_state="started" if tool_call.tool_name != "document_finish" else "none",
            retry_policy=retry_policy,
            loop_action=loop_action,
            outcome_code=outcome_code,
            safe_details={
                "documentMutationOutcome": {
                    "version": 1,
                    "status": status,
                    "phase": phase,
                    "retryPolicy": retry_policy,
                    "code": outcome_code,
                }
            },
        )
        _set_mutation_payload_control(
            result,
            retry_policy=retry_policy,
            outcome_code=outcome_code,
        )
        result.terminates_turn = False
        result.terminal_response_text = None
        return result

    if tool_call.tool_name == "document_finish" and status in {"applied", "discarded"}:
        committed = status == "applied"
        result.effect_outcome = ToolEffectOutcome(
            effect_state="committed" if committed else "none",
            retry_policy="never",
            loop_action="finalize_without_tools",
            outcome_code=(
                "document_mutation_applied"
                if committed
                else "document_mutation_discarded"
            ),
            safe_details={
                "documentMutationOutcome": {
                    "version": 1,
                    "status": status,
                    "phase": "commit" if committed else "discard",
                    "retryPolicy": "never",
                    "code": (
                        "document_mutation_applied"
                        if committed
                        else "document_mutation_discarded"
                    ),
                }
            },
        )
        result.terminates_turn = False
        result.terminal_response_text = None
        return result

    phase = "candidate" if tool_call.tool_name in _PROMPT_ANNOTATION_WRITERS else "verification"
    if status:
        mutation_status = status
    elif tool_call.tool_name == "document_browser_inspect":
        # Only inspect creates a verification receipt.  Diagnostic/action
        # tools must not be projected as proof that a candidate is healthy.
        mutation_status = "verification_passed"
    elif tool_call.tool_name == "document_browser_screenshot":
        mutation_status = "screenshot_observed"
    elif tool_call.tool_name == "document_browser_act":
        mutation_status = "action_applied"
    elif tool_call.tool_name == "document_browser_reload":
        mutation_status = "reload_observed"
    else:
        mutation_status = "candidate_staged"
    extra: dict[str, Any] = {}
    if isinstance(payload, dict):
        for key in ("candidateSha256", "candidateEpoch", "verificationToken", "generation"):
            if key in payload:
                extra[key] = payload[key]
    result.effect_outcome = ToolEffectOutcome(
        effect_state="started",
        retry_policy="same_turn",
        loop_action="continue",
        outcome_code=(
            "document_preview_unavailable"
            if candidate_preview_unavailable
            else (
                "document_candidate_staged"
                if phase == "candidate"
                else "document_verification_observed"
            )
        ),
        safe_details={
            "documentMutationOutcome": {
                "version": 1,
                "status": mutation_status,
                "phase": phase,
                "retryPolicy": "same_turn",
                "code": (
                    "document_preview_unavailable"
                    if candidate_preview_unavailable
                    else (
                        "document_candidate_staged"
                        if phase == "candidate"
                        else "document_verification_observed"
                    )
                ),
                **extra,
            }
        },
    )
    _set_mutation_payload_control(
        result,
        retry_policy="same_turn",
        outcome_code=(
            "document_preview_unavailable"
            if candidate_preview_unavailable
            else (
                "document_candidate_staged"
                if phase == "candidate"
                else "document_verification_observed"
            )
        ),
    )
    result.terminates_turn = False
    result.terminal_response_text = None
    return result


def _mutation_receipt_result(
    tool_call: ToolCall,
    *,
    status: str,
    reason: str,
    attempt: Any | None = None,
) -> ToolResult:
    """Build a bounded deterministic receipt without exposing durable ids."""

    if status == "applied":
        payload = {
            "status": "applied",
            "tool": tool_call.tool_name,
            "reason": reason,
            "retry_allowed": False,
        }
        execution_state = "success"
        preservation_class = "normal"
        effect_state = "committed"
        retry_policy = "never"
        mutation_status = "applied"
    elif status == "discarded":
        payload = {
            "status": "discarded",
            "tool": tool_call.tool_name,
            "reason": reason,
            "retry_allowed": False,
        }
        execution_state = "success"
        preservation_class = "normal"
        effect_state = "none"
        retry_policy = "never"
        mutation_status = "discarded"
    elif status == "ambiguous":
        payload = {
            "status": "ambiguous",
            "tool": tool_call.tool_name,
            "reason": reason,
            "retry_allowed": False,
        }
        execution_state = "error"
        preservation_class = "diagnostic"
        effect_state = "unknown"
        retry_policy = "reconcile"
        mutation_status = "ambiguous"
    else:
        payload = {
            "status": "error",
            "tool": tool_call.tool_name,
            "reason": reason,
            "retry_allowed": False,
        }
        execution_state = "error"
        preservation_class = "diagnostic"
        effect_state = "started"
        retry_policy = (
            "refresh" if "conflict" in reason or "stale" in reason else "new_turn"
        )
        mutation_status = "conflict" if retry_policy == "refresh" else "not_applied"
    result = ToolResult(
        tool_use_id=tool_call.tool_use_id,
        tool_name=tool_call.tool_name,
        content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        is_error=status not in {"applied", "discarded"},
        execution_status=normalize_execution_status(
            {
                "version": 1,
                "status": execution_state,
                "exit_code": None,
                "timed_out": False,
                "truncated": False,
                "reason": reason,
                "source": "replay" if status in {"applied", "discarded"} else "tool_runtime",
                "preservation_class": preservation_class,
            }
        ),
    )
    return _mutation_effect_result(
        result,
        effect_state=effect_state,
        retry_policy=retry_policy,
        loop_action="finalize_without_tools",
        outcome_code=reason,
        mutation_status=mutation_status,
        phase="commit",
        attempt=attempt,
    )


async def _finish_artifact_mutation_failure(
    controller: Any | None,
    tool_call: ToolCall,
    result: ToolResult,
    *,
    failure_code: str,
    proposal_failures: OrderedDict[int, list[str]] | None = None,
    exception: BaseException | None = None,
) -> ToolResult:
    if bool(getattr(controller, "is_candidate_loop", False)):
        return await _candidate_loop_effect_result(
            result,
            tool_call=tool_call,
            exception=exception,
        )
    if controller is None:
        ctx = current_tool_context.get()
        if _prompt_annotation_candidate_enabled(tool_call, ctx):
            return await _candidate_loop_effect_result(
                result,
                tool_call=tool_call,
                exception=exception,
            )
        return result
    stable_code = (
        exception.code
        if isinstance(exception, DocumentMutationError)
        else _mutation_result_code(result, failure_code)
    )
    if bool(getattr(controller, "is_replay_conflict", lambda _value: False)(
        tool_call.tool_use_id
    )):
        try:
            attempt = await controller.replay_conflict_attempt(tool_call.tool_use_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - an unreadable receipt stays unknown
            return _mutation_effect_result(
                result,
                effect_state="unknown",
                retry_policy="reconcile",
                loop_action="finalize_without_tools",
                outcome_code=stable_code,
                mutation_status="ambiguous",
                phase="commit",
            )
        durable_status = _mutation_attempt_status(attempt)
        if durable_status == "applied":
            return _mutation_effect_result(
                result,
                effect_state="committed",
                retry_policy="never",
                loop_action="finalize_without_tools",
                outcome_code=stable_code,
                mutation_status="applied",
                phase="commit",
                attempt=attempt,
            )
        if durable_status == "failed":
            durable_code = str(getattr(attempt, "failure_code", "") or stable_code)
            mutation_status = (
                "conflict"
                if durable_code == "DOCUMENT_MUTATION_CONFLICT"
                or "conflict" in durable_code.lower()
                else "not_applied"
            )
            return _mutation_effect_result(
                result,
                effect_state="started",
                retry_policy=("refresh" if mutation_status == "conflict" else "new_turn"),
                loop_action="finalize_without_tools",
                outcome_code=durable_code,
                mutation_status=mutation_status,
                phase="commit",
                attempt=attempt,
            )
        return _mutation_effect_result(
            result,
            effect_state="unknown",
            retry_policy="reconcile",
            loop_action="finalize_without_tools",
            outcome_code=(str(getattr(attempt, "failure_code", "") or stable_code)),
            mutation_status="ambiguous",
            phase="commit",
            attempt=attempt,
        )
    if not bool(controller.owns_commit(tool_call.tool_use_id)):
        try:
            await controller.reject_proposal(tool_call.tool_use_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - no durable side effect exists yet
            log.warning(
                "dispatch.document_proposal_rejection_failed",
                tool_use_id=tool_call.tool_use_id,
                failure_code=failure_code,
                exc_info=True,
            )
        digest = hashlib.sha256(
            json.dumps(
                tool_call.arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        history: list[str] = []
        if proposal_failures is not None:
            controller_key = id(controller)
            history = proposal_failures.setdefault(controller_key, [])
            proposal_failures.move_to_end(controller_key)
            while len(proposal_failures) > 1024:
                proposal_failures.popitem(last=False)
        no_progress = digest in history
        history.append(digest)
        retry_policy, loop_action, mutation_status = _proposal_failure_policy(
            failure_code=failure_code,
            stable_code=stable_code,
            exception=exception,
        )
        outcome_code = stable_code
        # Only a byte-identical correctable proposal is a no-progress stop.
        # Refresh/forbidden failures already close the side-effect boundary for
        # their own policy reason and retain their stable code.
        if no_progress and loop_action == "continue":
            retry_policy = "new_turn"
            loop_action = "finalize_without_tools"
            outcome_code = "document_proposal_no_progress"
        return _mutation_effect_result(
            result,
            effect_state="none",
            retry_policy=retry_policy,
            loop_action=loop_action,
            outcome_code=outcome_code,
            mutation_status=mutation_status,
            phase="proposal",
        )
    try:
        attempt = await controller.reconcile(tool_call.tool_use_id)
        status = _mutation_attempt_status(attempt)
        if status == "applied":
            return _mutation_receipt_result(
                tool_call,
                status="applied",
                reason="mutation_response_reconciled",
                attempt=attempt,
            )
        if status == "ambiguous":
            return _mutation_receipt_result(
                tool_call,
                status="ambiguous",
                reason="mutation_attempt_ambiguous",
                attempt=attempt,
            )
        if status == "failed":
            durable_code = str(getattr(attempt, "failure_code", "") or stable_code)
            durable_status = (
                "conflict"
                if durable_code == "DOCUMENT_MUTATION_CONFLICT"
                or "conflict" in durable_code.lower()
                else "not_applied"
            )
            return _mutation_effect_result(
                result,
                effect_state="started",
                retry_policy=("refresh" if durable_status == "conflict" else "new_turn"),
                loop_action="finalize_without_tools",
                outcome_code=durable_code,
                mutation_status=durable_status,
                phase="commit",
                attempt=attempt,
            )
        attempt = await controller.mark_failed(tool_call.tool_use_id, stable_code)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - uncertain receipts must fail closed
        log.warning(
            "dispatch.artifact_mutation_failure_receipt_failed",
            tool_use_id=tool_call.tool_use_id,
            failure_code=failure_code,
            exc_info=True,
        )
        try:
            recovered = await controller.reconcile(tool_call.tool_use_id)
        except Exception:  # noqa: BLE001 - continue to the ambiguity fence
            recovered = None
        recovered_status = _mutation_attempt_status(recovered)
        if recovered_status == "applied":
            return _mutation_receipt_result(
                tool_call,
                status="applied",
                reason="mutation_response_reconciled",
                attempt=recovered,
            )
        if recovered_status == "failed":
            durable_code = str(
                getattr(recovered, "failure_code", "") or stable_code
            )
            durable_status = (
                "conflict"
                if durable_code == "DOCUMENT_MUTATION_CONFLICT"
                or "conflict" in durable_code.lower()
                else "not_applied"
            )
            return _mutation_effect_result(
                result,
                effect_state="started",
                retry_policy=("refresh" if durable_status == "conflict" else "new_turn"),
                loop_action="finalize_without_tools",
                outcome_code=durable_code,
                mutation_status=durable_status,
                phase="commit",
                attempt=recovered,
            )
        try:
            await controller.mark_ambiguous(
                tool_call.tool_use_id,
                "writer_failure_receipt_unknown",
            )
        except Exception:  # noqa: BLE001 - existing receipt remains a write fence
            pass
        return _mutation_receipt_result(
            tool_call,
            status="ambiguous",
            reason="mutation_attempt_ambiguous",
        )
    mutation_status = (
        "conflict"
        if stable_code == "DOCUMENT_MUTATION_CONFLICT"
        or "conflict" in stable_code.lower()
        else "not_applied"
    )
    return _mutation_effect_result(
        result,
        effect_state="started",
        retry_policy="refresh" if mutation_status == "conflict" else "new_turn",
        loop_action="finalize_without_tools",
        outcome_code=stable_code,
        mutation_status=mutation_status,
        phase="commit",
        attempt=attempt,
    )


async def _finish_artifact_mutation_cleanup_ambiguity(
    controller: Any | None,
    tool_call: ToolCall,
) -> ToolResult:
    """Preserve a failed candidate for deterministic restart reconciliation."""

    if bool(getattr(controller, "is_candidate_loop", False)):
        result = ToolResult(
            tool_use_id=tool_call.tool_use_id,
            tool_name=tool_call.tool_name,
            content=json.dumps(
                {"status": "verification_failed", "retry_allowed": True},
                ensure_ascii=False,
                sort_keys=True,
            ),
            is_error=True,
        )
        return await _candidate_loop_effect_result(result, tool_call=tool_call)
    if controller is None:
        return _mutation_receipt_result(
            tool_call,
            status="ambiguous",
            reason="mutation_attempt_ambiguous",
        )
    try:
        attempt = await controller.reconcile(tool_call.tool_use_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - mark below provides a second durable fence
        attempt = None
    status = _mutation_attempt_status(attempt)
    if status == "applied":
        return _mutation_receipt_result(
            tool_call,
            status="applied",
            reason="mutation_response_reconciled",
            attempt=attempt,
        )
    if status == "failed":
        return _mutation_receipt_result(
            tool_call,
            status="failed",
            reason="mutation_attempt_failed",
            attempt=attempt,
        )
    if status != "ambiguous":
        try:
            attempt = await controller.mark_ambiguous(
                tool_call.tool_use_id,
                "writer_candidate_cleanup_failed",
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - RESERVED remains restart-recoverable
            log.warning(
                "dispatch.artifact_mutation_cleanup_receipt_failed",
                tool_use_id=tool_call.tool_use_id,
                exc_info=True,
            )
            try:
                attempt = await controller.reconcile(tool_call.tool_use_id)
            except Exception:  # noqa: BLE001 - bounded receipt hides durable details
                attempt = None
        status = _mutation_attempt_status(attempt)
        if status == "applied":
            return _mutation_receipt_result(
                tool_call,
                status="applied",
                reason="mutation_response_reconciled",
                attempt=attempt,
            )
        if status == "failed":
            return _mutation_receipt_result(
                tool_call,
                status="failed",
                reason="mutation_attempt_failed",
                attempt=attempt,
            )
    return _mutation_receipt_result(
        tool_call,
        status="ambiguous",
        reason="mutation_attempt_ambiguous",
        attempt=attempt,
    )


async def _finish_artifact_mutation_success(
    controller: Any | None,
    tool_call: ToolCall,
    result: ToolResult,
) -> ToolResult:
    if controller is None:
        return result
    try:
        attempt = await controller.reconcile(tool_call.tool_use_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - successful response without receipt is unsafe
        attempt = None
    status = _mutation_attempt_status(attempt)
    if status == "applied":
        proposal_rejections = int(
            getattr(controller, "proposal_rejection_count", 0) or 0
        )
        return _mutation_effect_result(
            result,
            effect_state="committed",
            retry_policy="never",
            loop_action="finalize_without_tools",
            outcome_code="document_mutation_applied",
            mutation_status="applied",
            phase="commit",
            attempt=attempt,
            extra_outcome=(
                {
                    "corrected": True,
                    "proposalAttempts": proposal_rejections + 1,
                }
                if proposal_rejections
                else None
            ),
        )
    if status == "failed":
        return _mutation_receipt_result(
            tool_call,
            status="failed",
            reason="mutation_attempt_failed",
            attempt=attempt,
        )
    try:
        await controller.mark_ambiguous(
            tool_call.tool_use_id,
            "writer_success_receipt_unknown",
        )
    except Exception:  # noqa: BLE001 - existing receipt remains a write fence
        pass
    return _mutation_receipt_result(
        tool_call,
        status="ambiguous",
        reason="mutation_attempt_ambiguous",
    )


async def _mark_artifact_mutation_cancelled(
    controller: Any | None,
    tool_call: ToolCall,
) -> None:
    if controller is None:
        return
    if bool(getattr(controller, "is_candidate_loop", False)):
        # A cancellation can arrive after the candidate commit transaction has
        # crossed the durable CAS but before ``ArtifactCandidateLoopController``
        # updates its in-memory status.  Reconcile first; otherwise the outer
        # turn cleanup may try to reject an already-applied ChangeSet and lose
        # the authoritative result.  A failed reconciliation remains a safe
        # open candidate and is invalidated below for a later recovery pass.
        reconcile = getattr(controller, "reconcile", None)
        if callable(reconcile):
            try:
                await reconcile()
            except asyncio.CancelledError:
                # We are already handling the original cancellation.  Keep
                # the cleanup best-effort without masking that cause.
                pass
            except Exception:  # noqa: BLE001 - recovery remains fail-closed
                log.warning(
                    "dispatch.candidate_cancel_reconcile_failed",
                    tool_use_id=tool_call.tool_use_id,
                    exc_info=True,
                )
        state = getattr(controller, "state", None)
        if str(getattr(state, "status", "") or "") in {"committed", "discarded"}:
            return
        invalidate = getattr(controller, "invalidate_verification", None)
        if callable(invalidate):
            try:
                await invalidate(reason="writer_cancelled")
            except Exception:  # noqa: BLE001 - cancellation keeps original cause
                pass
        return
    try:
        if not bool(controller.owns_commit(tool_call.tool_use_id)):
            await controller.reject_proposal(tool_call.tool_use_id)
            return
        attempt = await controller.reconcile(tool_call.tool_use_id)
        if _mutation_attempt_status(attempt) == "applied":
            return
        await controller.mark_ambiguous(
            tool_call.tool_use_id,
            "writer_cancelled",
        )
    except BaseException:  # noqa: BLE001 - cancellation must retain its original cause
        log.warning(
            "dispatch.artifact_mutation_cancel_receipt_failed",
            tool_use_id=tool_call.tool_use_id,
            exc_info=True,
        )


def _exclusive_tool_ceiling_preflight(
    tool_call: ToolCall,
    ctx: ToolContext | None,
) -> ToolResult | None:
    """Deny a restricted-turn probe before lookup or argument validation.

    This keeps hidden tool existence, schemas, aliases, and validation details
    outside the PromptAnnotation provider boundary. The policy-chain check is
    retained as a second guard for callers that invoke the chain directly.
    """

    if (
        ctx is None
        or ctx.exclusive_tools is None
        or tool_call.tool_name in ctx.exclusive_tools
    ):
        return None
    log.warning(
        "dispatch.defense_in_depth_block",
        tool=tool_call.tool_name,
        reason="exclusive_ceiling",
        tool_use_id=tool_call.tool_use_id,
        agent_id=ctx.agent_id,
        session_key=ctx.session_key,
    )
    return _build_envelope_result(
        tool_call,
        exc=PermissionError("tool outside exclusive ceiling"),
        policy_denial=True,
        error_class_override="PolicyDenied",
        user_message_override="Tool unavailable for this restricted turn.",
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def build_tool_handler(
    registry: ToolRegistry,
    ctx: ToolContext | None = None,
    *,
    known_skill_names: set[str] | None = None,
    tool_hooks: Sequence[ToolHook] | None = None,
) -> AgentToolHandler:
    """Build an async tool handler from a :class:`ToolRegistry`.

    The returned handler:

    1. Injection-guard check before registry lookup.
    2. Registry lookup; returns structured error on miss.
    3. ``ToolHook.before_tool`` fan-out (no-op if ``tool_hooks`` is empty).
    4. Policy chain; first denial returns immediately.
    5. Reserves run budget, including external call counts and text caps.
    6. Dispatches to the registered handler inside the request-scoped contextvar.
    7. Commits or aborts the run-budget reservation.
    8. ``ToolHook.after_tool`` fan-out with the raw outcome.
    9. Finalises the result (execution status, budget, artefacts) via
       :func:`opensquilla.tools.policy.finalize`.
    10. Resets ``current_tool_context`` unconditionally in ``finally``.

    ``tool_hooks`` defaults to empty so callers that do not pass hooks are
    bit-for-bit equivalent to the legacy path.
    """
    if ctx is not None:
        ctx.validate_path_roots()
    known = frozenset(known_skill_names or ())
    hooks: tuple[ToolHook, ...] = tuple(tool_hooks or ())
    fallback_budget_tracker = _build_budget_tracker(ctx)
    scoped_budget_trackers: dict[
        int,
        tuple[weakref.ReferenceType[ToolContext], ToolResultBudgetTracker],
    ] = {}
    keyed_run_budget_trackers: dict[str, ToolRunBudgetTracker] = {}
    # (scope_key, tool_name, args_sha256) -> (count, last_result_sha256).
    # One instance per built tool surface; bounded by
    # _REPEATED_CALL_NOTICE_MAX_ENTRIES and only populated while the
    # OPENSQUILLA_REPEATED_CALL_NOTICE gate is armed.
    repeated_call_seen: OrderedDict[tuple[str, str, str], tuple[int, str]] = OrderedDict()
    # Turn-scoped controllers are short-lived. Keep only bounded proposal
    # digests so a repeated invalid proposal is recognized as no progress
    # without creating durable mutation state.
    mutation_proposal_failures: OrderedDict[int, list[str]] = OrderedDict()

    def _budget_tracker_for(effective_ctx: ToolContext | None) -> ToolResultBudgetTracker:
        if effective_ctx is None or effective_ctx is ctx:
            return fallback_budget_tracker
        key = id(effective_ctx)
        entry = scoped_budget_trackers.get(key)
        if entry is not None:
            context_ref, tracker = entry
            if context_ref() is effective_ctx:
                return tracker
        tracker = _build_budget_tracker(effective_ctx)
        scoped_budget_trackers[key] = (weakref.ref(effective_ctx), tracker)
        return tracker

    def _run_budget_tracker_for(
        effective_ctx: ToolContext | None,
    ) -> ToolRunBudgetTracker:
        run_budget_key = (
            getattr(effective_ctx, "tool_run_budget_key", None)
            if effective_ctx is not None
            else None
        )
        if isinstance(run_budget_key, str) and run_budget_key:
            tracker = keyed_run_budget_trackers.get(run_budget_key)
            if tracker is not None:
                return tracker
            tracker = _build_run_budget_tracker(effective_ctx)
            keyed_run_budget_trackers[run_budget_key] = tracker
            return tracker
        tracker = _build_run_budget_tracker(effective_ctx)
        return tracker

    async def _handler(tool_call: ToolCall) -> ToolResult:  # type: ignore[return]
        effective_ctx = current_tool_context.get() or ctx
        mutation_controller = None
        if _prompt_annotation_candidate_enabled(tool_call, effective_ctx):
            mutation_controller = getattr(
                effective_ctx,
                "artifact_candidate_loop_controller",
                None,
            )
        elif _prompt_annotation_writer_is_guarded(tool_call, effective_ctx):
            mutation_controller = getattr(
                effective_ctx,
                "artifact_candidate_loop_controller",
                None,
            ) or (
                getattr(effective_ctx, "artifact_mutation_attempt_controller", None)
                if effective_ctx is not None
                else None
            )

        # 1. Ingress injection guard.
        injection_envelope = _check_injection_guard(tool_call, effective_ctx)
        if injection_envelope is not None:
            return await _finish_artifact_mutation_failure(
                mutation_controller,
                tool_call,
                injection_envelope,
                failure_code="writer_injection_rejected",
                proposal_failures=mutation_proposal_failures,
            )

        exclusive_denial = _exclusive_tool_ceiling_preflight(
            tool_call,
            effective_ctx,
        )
        if exclusive_denial is not None:
            return await _finish_artifact_mutation_failure(
                mutation_controller,
                tool_call,
                exclusive_denial,
                failure_code="writer_exclusive_denied",
                proposal_failures=mutation_proposal_failures,
            )

        # 2. Registry lookup.
        registered = registry.get(tool_call.tool_name)
        if registered is None:
            registry_miss = _resolve_registry_miss(
                tool_call,
                known,
                effective_ctx,
                registry,
            )
            return await _finish_artifact_mutation_failure(
                mutation_controller,
                tool_call,
                registry_miss,
                failure_code="writer_unregistered",
                proposal_failures=mutation_proposal_failures,
            )

        plan_access_denial = _plan_access_preflight(
            tool_call,
            registered,
            effective_ctx,
        )
        if plan_access_denial is not None:
            return await _finish_artifact_mutation_failure(
                mutation_controller,
                tool_call,
                plan_access_denial,
                failure_code="writer_plan_denied",
                proposal_failures=mutation_proposal_failures,
            )

        # The unwrap preserves the immutable origin trace, so the authoritative
        # ingress injection decision above cannot change after normalization.
        tool_call = _unwrap_nested_json_arguments(tool_call, registered, effective_ctx)

        runtime_only_supplied = sorted(
            set(tool_call.arguments) & registered.spec.runtime_only_arguments
        )
        if tool_call.continuation is None and runtime_only_supplied:
            invalid_result = _build_invalid_attempt_result(
                tool_call,
                reason_code="runtime_only_tool_argument",
                user_message=(
                    "Runtime-only continuation arguments cannot be supplied by the model: "
                    + ", ".join(runtime_only_supplied)
                ),
            )
            return await _finish_artifact_mutation_failure(
                mutation_controller,
                tool_call,
                invalid_result,
                failure_code="writer_runtime_only_argument",
                proposal_failures=mutation_proposal_failures,
            )

        non_executable_arguments = _check_non_executable_arguments(tool_call, effective_ctx)
        if non_executable_arguments is not None:
            return await _finish_artifact_mutation_failure(
                mutation_controller,
                tool_call,
                non_executable_arguments,
                failure_code="writer_non_executable_arguments",
                proposal_failures=mutation_proposal_failures,
            )
        tool_call = _strip_provider_replay_arguments(tool_call)
        tool_call, alias_normalization_error = _normalize_common_tool_argument_aliases(
            tool_call,
            effective_ctx,
        )
        if alias_normalization_error is not None:
            return await _finish_artifact_mutation_failure(
                mutation_controller,
                tool_call,
                alias_normalization_error,
                failure_code="writer_alias_conflict",
                proposal_failures=mutation_proposal_failures,
            )
        missing_required_arguments = _check_required_arguments(
            tool_call,
            registered,
            effective_ctx,
        )
        if missing_required_arguments is not None:
            return await _finish_artifact_mutation_failure(
                mutation_controller,
                tool_call,
                missing_required_arguments,
                failure_code="writer_missing_arguments",
                proposal_failures=mutation_proposal_failures,
            )
        schema_valid_arguments = _check_schema_valid_arguments(
            tool_call,
            registered,
            effective_ctx,
        )
        if schema_valid_arguments is not None:
            return await _finish_artifact_mutation_failure(
                mutation_controller,
                tool_call,
                schema_valid_arguments,
                failure_code="writer_schema_invalid",
                proposal_failures=mutation_proposal_failures,
            )
        executable_shape = _check_executable_tool_shape(tool_call, effective_ctx)
        if executable_shape is not None:
            return await _finish_artifact_mutation_failure(
                mutation_controller,
                tool_call,
                executable_shape,
                failure_code="writer_shape_invalid",
                proposal_failures=mutation_proposal_failures,
            )

        # 3. ToolHook.before_tool — optional observability hook.
        hook_call = ToolHookCall(tool_call=tool_call, ctx=effective_ctx) if hooks else None
        if hook_call is not None:
            for hook in hooks:
                try:
                    hook.before_tool(hook_call)
                except Exception as exc:  # noqa: BLE001 - hooks must not break dispatch
                    log.warning(
                        "dispatch.tool_hook_failed",
                        hook=getattr(hook, "name", type(hook).__name__),
                        phase="before_tool",
                        error=str(exc),
                    )

        # 4. Policy chain — first denial wins. Single emission site via run_chain_with_emit.
        dispatch_input = DispatchInput(
            tool_call=tool_call,
            ctx=effective_ctx,
            registered=registered,
            known_skill_names=known,
            registry=registry,
        )

        def _emit_policy_log(log_event: dict) -> None:
            event = log_event.get("event", "dispatch.policy_block")
            fields = {k: v for k, v in log_event.items() if k != "event"}
            log.warning(event, **fields)

        decision = run_chain_with_emit(dispatch_input, emit=_emit_policy_log)
        if not decision.allowed:
            if decision.envelope is None:
                raise RuntimeError("PolicyCheck returned a denial without an envelope")
            if hook_call is not None:
                for hook in hooks:
                    try:
                        hook.after_tool(
                            hook_call,
                            ToolHookResult(result=decision.envelope),
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "dispatch.tool_hook_failed",
                            hook=getattr(hook, "name", type(hook).__name__),
                            phase="after_tool",
                            error=str(exc),
                        )
            return await _finish_artifact_mutation_failure(
                mutation_controller,
                tool_call,
                decision.envelope,
                failure_code="writer_policy_denied",
                proposal_failures=mutation_proposal_failures,
            )

        # 5. Handler dispatch inside the request-scoped contextvar. Runtime
        # continuation authority is injected only after model-argument schema
        # and policy checks, so it never becomes provider-visible input.
        if tool_call.continuation is not None and not tool_call.continuation.matches(
            tool_use_id=tool_call.tool_use_id,
            session_key=(effective_ctx.session_key if effective_ctx is not None else None),
        ):
            invalid_result = _build_invalid_attempt_result(
                tool_call,
                reason_code="approval_continuation_mismatch",
                user_message=(
                    "The approved continuation does not belong to this tool call "
                    "and session."
                ),
            )
            return await _finish_artifact_mutation_failure(
                mutation_controller,
                tool_call,
                invalid_result,
                failure_code="writer_continuation_mismatch",
                proposal_failures=mutation_proposal_failures,
            )
        execution_arguments = dict(tool_call.arguments)
        if "_tool_use_id" in registered.spec.runtime_only_arguments:
            execution_arguments["_tool_use_id"] = tool_call.tool_use_id
        if (
            tool_call.continuation is not None
            and "approval_id" in registered.spec.runtime_only_arguments
        ):
            execution_arguments.setdefault(
                "approval_id",
                tool_call.continuation.approval_id,
            )
        run_budget_tracker = _run_budget_tracker_for(effective_ctx)
        reservation_or_control = await _reserve_tool_call_with_runtime_guards(
            tracker=run_budget_tracker,
            tool_call=tool_call,
            arguments=execution_arguments,
            ctx=effective_ctx,
        )
        if isinstance(reservation_or_control, ToolResult):
            _notify_after_tool_hooks(hooks, hook_call, reservation_or_control)
            return await _finish_artifact_mutation_failure(
                mutation_controller,
                tool_call,
                reservation_or_control,
                failure_code="writer_runtime_guard_denied",
                proposal_failures=mutation_proposal_failures,
            )
        reservation = reservation_or_control

        token = current_tool_context.set(effective_ctx)
        tool_started_at = time.monotonic()
        raw_result: Any = None
        exception: BaseException | None = None
        artifact_start = (
            len(effective_ctx.published_artifacts) if effective_ctx is not None else 0
        )
        try:
            sandbox_descriptor = registered.spec.sandbox
            if sandbox_descriptor.enforce:
                workspace = (
                    Path(effective_ctx.workspace_dir)
                    if effective_ctx is not None and effective_ctx.workspace_dir
                    else None
                )
                sandbox_guard = await prepare_tool_operation_guard(
                    sandbox_descriptor,
                    tool_name=tool_call.tool_name,
                    arguments=reservation.arguments,
                    workspace=workspace,
                    run_mode=getattr(effective_ctx, "run_mode", None),
                )
                raw_result = await run_tool_handler_with_operation_guard(
                    registered.handler,
                    reservation.arguments,
                    sandbox_guard,
                )
                if sandbox_guard.denial_payload is None and sandbox_guard.record_payload:
                    try:
                        await record_tool_operation_success(sandbox_guard, raw_result)
                    except Exception:  # pragma: no cover - cache failures should not fail tools
                        log.exception(
                            "dispatch.sandbox_record_success_failed",
                            tool=tool_call.tool_name,
                        )
            else:
                raw_result = await registered.handler(**reservation.arguments)
            await run_budget_tracker.commit_tool_result(reservation, raw_result)
        except asyncio.CancelledError as exc:
            exception = exc
            await run_budget_tracker.abort_tool_result(reservation)
            await _mark_artifact_mutation_cancelled(
                mutation_controller,
                tool_call,
            )
            raise
        except ToolRunBudgetExceededError as exc:
            exception = exc
            if raw_result is None:
                await run_budget_tracker.abort_tool_result(reservation)
        except Exception as exc:  # noqa: BLE001
            exception = exc
            await run_budget_tracker.abort_tool_result(reservation)
        finally:
            try:
                # 6. ToolHook.after_tool — observability seam.
                if hook_call is not None:
                    outcome = ToolHookResult(result=raw_result, exception=exception)
                    for hook in hooks:
                        try:
                            hook.after_tool(hook_call, outcome)
                        except Exception as hook_exc:  # noqa: BLE001
                            log.warning(
                                "dispatch.tool_hook_failed",
                                hook=getattr(hook, "name", type(hook).__name__),
                                phase="after_tool",
                                error=str(hook_exc),
                            )
                if not isinstance(exception, asyncio.CancelledError):
                    await _emit_web_retrieval_tool_run_diagnostics(
                        tool_call=tool_call,
                        effective_ctx=effective_ctx,
                        reservation=reservation,
                        run_budget_tracker=run_budget_tracker,
                        started_at=tool_started_at,
                        raw_result=raw_result,
                        exception=exception,
                    )
                    # 7. Single finalisation point.
                    candidate_loop = _prompt_annotation_candidate_enabled(
                        tool_call,
                        effective_ctx,
                    )
                    try:
                        final_result = await finalize(
                            tool_call,
                            effective_ctx,
                            raw_result,
                            exception,
                            artifact_start,
                            _budget_tracker_for(effective_ctx),
                            registered,
                        )
                    except Exception as finalize_exception:
                        if candidate_loop:
                            recovered_result = await _candidate_terminal_receipt(
                                tool_call=tool_call,
                                context=effective_ctx,
                            )
                            if recovered_result is not None:
                                return _maybe_apply_repeated_call_notice(
                                    recovered_result,
                                    tool_call=tool_call,
                                    effective_ctx=effective_ctx,
                                    raw_result=raw_result,
                                    exception=finalize_exception,
                                    seen=repeated_call_seen,
                                )
                        raise finalize_exception
                    if candidate_loop:
                        final_result = await _candidate_loop_effect_result(
                            final_result,
                            tool_call=tool_call,
                            exception=exception,
                        )
                    elif mutation_controller is not None:
                        if isinstance(
                            exception,
                            ArtifactMutationCleanupAmbiguousError,
                        ):
                            final_result = (
                                await _finish_artifact_mutation_cleanup_ambiguity(
                                    mutation_controller,
                                    tool_call,
                                )
                            )
                        elif final_result.is_error:
                            final_result = await _finish_artifact_mutation_failure(
                                mutation_controller,
                                tool_call,
                                final_result,
                                failure_code="writer_handler_failed",
                                proposal_failures=mutation_proposal_failures,
                                exception=exception,
                            )
                        else:
                            final_result = await _finish_artifact_mutation_success(
                                mutation_controller,
                                tool_call,
                                final_result,
                            )
                    # Applied after finalize so the notice survives every
                    # ToolResultBudgetPolicy cap; hooks above still observe
                    # the unmodified raw outcome.
                    return _maybe_apply_repeated_call_notice(
                        final_result,
                        tool_call=tool_call,
                        effective_ctx=effective_ctx,
                        raw_result=raw_result,
                        exception=exception,
                        seen=repeated_call_seen,
                    )
            finally:
                current_tool_context.reset(token)

    # Agent-side lossy projection is only safe when the callable can actually
    # dispatch the provider-visible recovery tool.  Keep this capability on
    # the handler itself so embedded Agents and wrapped Meta children do not
    # mistake an arbitrary non-null callback for a retrieval implementation.
    setattr(
        _handler,
        "_opensquilla_available_tools",
        frozenset(registry.list_names()),
    )
    return _handler
