"""Content-free runtime facts emitted by authoritative engine boundaries.

This module intentionally knows nothing about telemetry identity, consent,
storage, or transport.  The engine can therefore publish a small, closed fact
object while the gateway-owned adapter remains responsible for constructing a
consented telemetry event.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Final

from opensquilla.telemetry.contracts.common import (
    ClientSurface,
    ExecutionMode,
    ResultOutcome,
)
from opensquilla.telemetry.contracts.reliability import (
    ToolCategory,
    ToolErrorCode,
    ToolOutcome,
    TurnErrorCode,
    TurnFailureStage,
)

STALL_THRESHOLD_MS: Final = 15_000


@dataclass(frozen=True, slots=True)
class ClientRuntimeDimensions:
    surface: ClientSurface
    execution_mode: ExecutionMode


_CLIENT_RUNTIME_DIMENSIONS: ContextVar[ClientRuntimeDimensions | None] = ContextVar(
    "opensquilla_client_runtime_dimensions",
    default=None,
)


def current_client_runtime_dimensions() -> ClientRuntimeDimensions | None:
    return _CLIENT_RUNTIME_DIMENSIONS.get()


def set_client_runtime_dimensions(
    surface: ClientSurface,
    execution_mode: ExecutionMode,
) -> Token[ClientRuntimeDimensions | None]:
    if not isinstance(surface, ClientSurface) or not isinstance(execution_mode, ExecutionMode):
        raise TypeError("client runtime dimensions must use closed enums")
    return _CLIENT_RUNTIME_DIMENSIONS.set(
        ClientRuntimeDimensions(surface=surface, execution_mode=execution_mode)
    )


def reset_client_runtime_dimensions(
    token: Token[ClientRuntimeDimensions | None],
) -> None:
    _CLIENT_RUNTIME_DIMENSIONS.reset(token)


_CURRENT_TURN_FAILURE_STAGE: ContextVar[TurnFailureStage | None] = ContextVar(
    "opensquilla_current_turn_failure_stage",
    default=None,
)


def current_turn_failure_stage() -> TurnFailureStage | None:
    return _CURRENT_TURN_FAILURE_STAGE.get()


def set_current_turn_failure_stage(
    stage: TurnFailureStage,
) -> Token[TurnFailureStage | None]:
    if not isinstance(stage, TurnFailureStage):
        raise TypeError("turn failure stage must use the closed enum")
    return _CURRENT_TURN_FAILURE_STAGE.set(stage)


def mark_current_turn_failure_stage(stage: TurnFailureStage) -> None:
    if not isinstance(stage, TurnFailureStage):
        raise TypeError("turn failure stage must use the closed enum")
    _CURRENT_TURN_FAILURE_STAGE.set(stage)


def reset_current_turn_failure_stage(token: Token[TurnFailureStage | None]) -> None:
    _CURRENT_TURN_FAILURE_STAGE.reset(token)


@dataclass(frozen=True, slots=True)
class TurnReliabilityFacts:
    """Whitelist-only terminal facts for one public agent turn."""

    outcome: ResultOutcome
    error_code: TurnErrorCode | None
    failure_stage: TurnFailureStage | None
    duration_ms: int
    ttft_ms: int | None
    stall_count: int
    stall_threshold_ms: int = STALL_THRESHOLD_MS

    def __post_init__(self) -> None:
        _require_non_negative("duration_ms", self.duration_ms)
        _require_non_negative("stall_count", self.stall_count)
        if self.ttft_ms is not None:
            _require_non_negative("ttft_ms", self.ttft_ms)
            if self.ttft_ms > self.duration_ms:
                raise ValueError("ttft_ms cannot exceed duration_ms")
        if self.stall_threshold_ms != STALL_THRESHOLD_MS:
            raise ValueError("stall_threshold_ms must use the protocol threshold")
        _require_error_pair(self.outcome is ResultOutcome.SUCCESS, self.error_code)
        if self.outcome is ResultOutcome.SUCCESS:
            if self.failure_stage is not None:
                raise ValueError("successful turn cannot include failure_stage")
        elif self.failure_stage is None:
            raise ValueError("non-success turn requires failure_stage")


@dataclass(frozen=True, slots=True)
class ToolCallReliabilityFacts:
    """Whitelist-only terminal facts for one logical tool call."""

    tool_category: ToolCategory
    outcome: ToolOutcome
    error_code: ToolErrorCode | None
    duration_ms: int
    retry_count: int

    def __post_init__(self) -> None:
        _require_non_negative("duration_ms", self.duration_ms)
        _require_non_negative("retry_count", self.retry_count)
        _require_error_pair(self.outcome is ToolOutcome.SUCCESS, self.error_code)


TurnReliabilitySink = Callable[[TurnReliabilityFacts], object]
ToolReliabilitySink = Callable[[ToolCallReliabilityFacts], object]
GrowthMilestoneSink = Callable[[], object]


@dataclass(slots=True)
class TurnFactAccumulator:
    """Monotonic, content-free measurement state for one public stream."""

    started_at: float
    first_text_at: float | None = None
    last_progress_at: float | None = None
    stall_count: int = 0
    _stall_open: bool = False

    def observe_text(self, now: float) -> None:
        """Observe a non-empty public text delta without retaining its text."""

        self.observe_progress(now)
        if self.first_text_at is None:
            self.first_text_at = max(self.started_at, now)
            self.last_progress_at = self.first_text_at

    def observe_progress(self, now: float) -> None:
        """Observe public progress, closing any currently open stall."""

        now = max(self.started_at, now)
        self._count_stall_if_due(now)
        if self.first_text_at is not None:
            self.last_progress_at = now
            self._stall_open = False

    def observe_heartbeat(self, now: float, *, idle_ms: int) -> None:
        """Use a public heartbeat solely as evidence of an ongoing idle gap."""

        if self.first_text_at is None or self._stall_open:
            return
        idle_seconds = max(0, idle_ms) / 1000
        last_progress = self.last_progress_at or self.first_text_at
        if idle_ms >= STALL_THRESHOLD_MS or now - last_progress >= idle_seconds:
            self._count_stall_if_due(max(now, last_progress + idle_seconds))

    def finish(
        self,
        now: float,
        *,
        outcome: ResultOutcome,
        error_code: TurnErrorCode | None,
        failure_stage: TurnFailureStage | None,
    ) -> TurnReliabilityFacts:
        now = max(self.started_at, now)
        self._count_stall_if_due(now)
        duration_ms = _elapsed_ms(self.started_at, now)
        ttft_ms = (
            None
            if self.first_text_at is None
            else min(duration_ms, _elapsed_ms(self.started_at, self.first_text_at))
        )
        return TurnReliabilityFacts(
            outcome=outcome,
            error_code=error_code,
            failure_stage=failure_stage,
            duration_ms=duration_ms,
            ttft_ms=ttft_ms,
            stall_count=self.stall_count,
        )

    def _count_stall_if_due(self, now: float) -> None:
        if self.first_text_at is None or self._stall_open:
            return
        last_progress = self.last_progress_at or self.first_text_at
        if now - last_progress >= STALL_THRESHOLD_MS / 1000:
            self.stall_count += 1
            self._stall_open = True


_TURN_CODE_ALIASES: Final[dict[str, TurnErrorCode]] = {
    "total_timeout": TurnErrorCode.HARD_DEADLINE,
    "iteration_timeout": TurnErrorCode.PROVIDER_TIMEOUT,
    "document_mutation_provider_timeout": TurnErrorCode.PROVIDER_TIMEOUT,
    "provider_request_too_large": TurnErrorCode.REQUEST_TOO_LARGE,
    "provider_request_message_limit_exhausted": TurnErrorCode.REQUEST_TOO_LARGE,
    "provider_output_truncated": TurnErrorCode.OUTPUT_TRUNCATED,
    "context_overflow": TurnErrorCode.CONTEXT_LIMIT,
    "compaction_exhausted": TurnErrorCode.CONTEXT_LIMIT,
    "turn_llm_call_budget_exceeded": TurnErrorCode.BUDGET_EXHAUSTED,
    "turn_input_token_budget_exceeded": TurnErrorCode.BUDGET_EXHAUSTED,
    "turn_output_token_budget_exceeded": TurnErrorCode.BUDGET_EXHAUSTED,
    "turn_billed_cost_budget_exceeded": TurnErrorCode.BUDGET_EXHAUSTED,
    "turn_cost_budget_exceeded": TurnErrorCode.BUDGET_EXHAUSTED,
    "turn_tool_error_budget_exceeded": TurnErrorCode.BUDGET_EXHAUSTED,
    "platform_validation": TurnErrorCode.PLATFORM_VALIDATION,
    "platform_safety": TurnErrorCode.SAFETY_BLOCKED,
}

_FAILURE_KIND_CODES: Final[dict[str, TurnErrorCode]] = {
    "rate_limited": TurnErrorCode.PROVIDER_RATE_LIMITED,
    "provider_overloaded": TurnErrorCode.PROVIDER_UNAVAILABLE,
    "auth_invalid": TurnErrorCode.PROVIDER_AUTH,
    "context_overflow": TurnErrorCode.CONTEXT_LIMIT,
    "model_not_found": TurnErrorCode.PROVIDER_UNAVAILABLE,
    "transport_transient": TurnErrorCode.PROVIDER_UNAVAILABLE,
    "policy_refusal": TurnErrorCode.SAFETY_BLOCKED,
    "bad_request": TurnErrorCode.PLATFORM_VALIDATION,
}


def classify_turn_error(
    *,
    code: object = None,
    failure_kind: object = None,
) -> tuple[ResultOutcome, TurnErrorCode]:
    """Map only closed engine signals; raw messages are never accepted."""

    normalized_code = code if isinstance(code, str) else ""
    normalized_kind = failure_kind if isinstance(failure_kind, str) else ""
    error_code: TurnErrorCode | None
    try:
        error_code = TurnErrorCode(normalized_code)
    except ValueError:
        error_code = _TURN_CODE_ALIASES.get(normalized_code)
    if error_code is None:
        error_code = _FAILURE_KIND_CODES.get(
            normalized_kind,
            TurnErrorCode.INTERNAL_ERROR,
        )
    if error_code in {TurnErrorCode.PROVIDER_TIMEOUT, TurnErrorCode.HARD_DEADLINE}:
        return ResultOutcome.TIMEOUT, error_code
    if error_code in {
        TurnErrorCode.CANCELLED_BY_USER,
        TurnErrorCode.CANCELLED_BEFORE_START,
        TurnErrorCode.SHUTDOWN,
    }:
        return ResultOutcome.CANCEL, error_code
    return ResultOutcome.FAIL, error_code


def classify_control_terminal(reason: object) -> tuple[ResultOutcome, TurnErrorCode]:
    normalized = str(reason) if isinstance(reason, str) else ""
    if normalized == "hard_deadline":
        return ResultOutcome.TIMEOUT, TurnErrorCode.HARD_DEADLINE
    if normalized == "shutdown":
        return ResultOutcome.CANCEL, TurnErrorCode.SHUTDOWN
    if normalized == "platform_validation":
        return ResultOutcome.FAIL, TurnErrorCode.PLATFORM_VALIDATION
    if normalized == "platform_safety":
        return ResultOutcome.FAIL, TurnErrorCode.SAFETY_BLOCKED
    if normalized == "cancel":
        return ResultOutcome.CANCEL, TurnErrorCode.CANCELLED_BY_USER
    return ResultOutcome.CANCEL, TurnErrorCode.UNKNOWN


_FILESYSTEM_READ_TOOLS: Final = frozenset(
    {"read_file", "list_dir", "glob", "find_files", "read_scratch"}
)
_FILESYSTEM_WRITE_TOOLS: Final = frozenset(
    {"apply_patch", "edit_file", "edit_source", "write_file", "write_scratch"}
)
_SHELL_TOOLS: Final = frozenset({"exec_command", "write_stdin", "shell"})
_CODE_TOOLS: Final = frozenset({"execute_code", "python", "javascript"})
_WEB_TOOLS: Final = frozenset({"web_fetch", "open_url"})
_SEARCH_TOOLS: Final = frozenset({"web_search", "search"})
_BROWSER_TOOLS: Final = frozenset(
    {"browser", "browser_open", "browser_click", "browser_type", "browser_screenshot"}
)
_COMPUTER_USE_TOOLS: Final = frozenset({"computer_use"})
_ARTIFACT_TOOLS: Final = frozenset({"publish_artifact"})
_COLLABORATION_TOOLS: Final = frozenset(
    {
        "create_goal",
        "update_goal",
        "request_user_input",
        "spawn_agent",
        "send_message",
        "send_message_to_agent",
        "wait_agent",
        "meta_invoke",
    }
)


def tool_category_for_name(tool_name: object) -> ToolCategory:
    """Reduce an ephemeral tool name to the closed analytics taxonomy."""

    name = tool_name if isinstance(tool_name, str) else ""
    if name in _FILESYSTEM_READ_TOOLS:
        return ToolCategory.FILESYSTEM_READ
    if name in _FILESYSTEM_WRITE_TOOLS:
        return ToolCategory.FILESYSTEM_WRITE
    if name in _SHELL_TOOLS:
        return ToolCategory.SHELL
    if name in _CODE_TOOLS:
        return ToolCategory.CODE_EXECUTION
    if name in _WEB_TOOLS:
        return ToolCategory.WEB
    if name in _SEARCH_TOOLS:
        return ToolCategory.SEARCH
    if name in _BROWSER_TOOLS:
        return ToolCategory.BROWSER
    if name in _COMPUTER_USE_TOOLS:
        return ToolCategory.COMPUTER_USE
    if name in _ARTIFACT_TOOLS:
        return ToolCategory.ARTIFACT
    if name.startswith("mcp_"):
        return ToolCategory.MCP_EXTENSION
    if name.startswith("document_"):
        return ToolCategory.DOCUMENT
    if name in _COLLABORATION_TOOLS:
        return ToolCategory.COLLABORATION
    return ToolCategory.OTHER


_INVALID_ARGUMENT_REASONS: Final = frozenset(
    {
        "invalid_arguments",
        "invalid_tool_arguments",
        "schema_validation_failed",
        "patch_snapshot_failed",
    }
)
_POLICY_DENIED_REASONS: Final = frozenset(
    {
        "approval_denied",
        "policy_denied",
        "sandbox_denied",
        "workspace_edit_gate_blocked",
    }
)
_UNAVAILABLE_REASONS: Final = frozenset(
    {"tool_unavailable", "unknown_tool", "handler_missing", "git_unavailable"}
)
_BUDGET_REASONS: Final = frozenset(
    {"budget_exhausted", "tool_budget_exhausted", "turn_tool_error_budget_exceeded"}
)


def classify_tool_result(
    *,
    status: object,
    reason: object,
    is_error: bool,
) -> tuple[ToolOutcome, ToolErrorCode | None]:
    """Classify a settled result without inspecting its content or arguments."""

    normalized_status = status if isinstance(status, str) else ""
    normalized_reason = reason if isinstance(reason, str) else ""
    if normalized_status == "timeout":
        return ToolOutcome.TIMEOUT, ToolErrorCode.TOOL_TIMEOUT
    if normalized_status == "cancelled":
        if normalized_reason in _POLICY_DENIED_REASONS:
            return ToolOutcome.DENIED, ToolErrorCode.POLICY_DENIED
        return ToolOutcome.CANCEL, ToolErrorCode.CANCELLED
    if normalized_reason in _POLICY_DENIED_REASONS:
        return ToolOutcome.DENIED, ToolErrorCode.POLICY_DENIED
    if normalized_reason in _INVALID_ARGUMENT_REASONS:
        return ToolOutcome.FAIL, ToolErrorCode.INVALID_ARGUMENTS
    if normalized_reason in _UNAVAILABLE_REASONS:
        return ToolOutcome.FAIL, ToolErrorCode.TOOL_UNAVAILABLE
    if normalized_reason in _BUDGET_REASONS:
        return ToolOutcome.FAIL, ToolErrorCode.BUDGET_EXHAUSTED
    if normalized_reason == "injection_rejected":
        return ToolOutcome.FAIL, ToolErrorCode.INJECTION_REJECTED
    if normalized_reason == "finalization_error":
        return ToolOutcome.FAIL, ToolErrorCode.FINALIZATION_ERROR
    if not is_error and normalized_status not in {"error", "timeout", "cancelled"}:
        return ToolOutcome.SUCCESS, None
    return ToolOutcome.FAIL, ToolErrorCode.HANDLER_ERROR


def _elapsed_ms(started_at: float, ended_at: float) -> int:
    return max(0, int((ended_at - started_at) * 1000))


def _require_non_negative(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_error_pair(success: bool, error_code: object | None) -> None:
    if success != (error_code is None):
        raise ValueError("success requires no error_code; failures require one")


__all__ = [
    "ClientRuntimeDimensions",
    "STALL_THRESHOLD_MS",
    "ToolCallReliabilityFacts",
    "ToolReliabilitySink",
    "GrowthMilestoneSink",
    "TurnFactAccumulator",
    "TurnReliabilityFacts",
    "TurnReliabilitySink",
    "classify_control_terminal",
    "classify_tool_result",
    "classify_turn_error",
    "current_client_runtime_dimensions",
    "current_turn_failure_stage",
    "mark_current_turn_failure_stage",
    "reset_client_runtime_dimensions",
    "reset_current_turn_failure_stage",
    "set_client_runtime_dimensions",
    "set_current_turn_failure_stage",
    "tool_category_for_name",
]
