"""Runtime recovery decisions for empty or no-progress agent turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from opensquilla.provider.types import ModelCapabilities

RuntimeRecoveryMode = Literal["off", "log", "warn_model"]
ReasoningPrefillRecoveryMode = Literal["off", "log", "recover"]
RuntimeRecoveryAction = Literal["observe", "prefill", "nudge"]

_RUNTIME_RECOVERY_MODES = frozenset({"off", "log", "warn_model"})
_REASONING_PREFILL_RECOVERY_MODES = frozenset({"off", "log", "recover"})

POST_TOOL_EMPTY_RECOVERY_MESSAGE = (
    "[Runtime recovery]\n"
    "The previous response after tool results had no visible content. Process the "
    "tool results above and continue with the next concrete step."
)

REASONING_ONLY_CONTINUATION_MESSAGE = (
    "[Runtime recovery]\n"
    "The previous response contained private reasoning but no visible answer or "
    "tool call. Continue now with the next concrete tool call or a concise "
    "visible response. Do not repeat the private reasoning."
)

SOURCE_LOOP_RECOVERY_MESSAGE = (
    "[Runtime recovery]\n"
    "You have a source diff and repeated evidence around the same failure or "
    "unchanged verification result. Re-check whether the current diff addresses "
    "that evidence. If it does not, change the source patch before repeating the "
    "same verification."
)

_SOURCE_LOOP_RECOVERY_REASONS = frozenset(
    {
        "repeated_failure_anchor",
        "repeated_source_read_after_write",
        "repeated_verification_without_diff_change",
        "edit_churn_after_failure",
        "finish_error_with_non_empty_diff",
    }
)


@dataclass(frozen=True)
class RuntimeRecoveryDecision:
    action: RuntimeRecoveryAction
    mechanism: str
    reason: str
    mode: str
    injected_to_model: bool = False
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def normalize_runtime_recovery_mode(
    value: str | None,
    *,
    default: str = "log",
) -> RuntimeRecoveryMode:
    raw = (value or default).strip().lower()
    if raw in _RUNTIME_RECOVERY_MODES:
        return raw  # type: ignore[return-value]
    if default in _RUNTIME_RECOVERY_MODES:
        return default  # type: ignore[return-value]
    return "log"


def normalize_reasoning_prefill_recovery_mode(
    value: str | None,
    *,
    default: str = "log",
) -> ReasoningPrefillRecoveryMode:
    raw = (value or default).strip().lower()
    if raw in _REASONING_PREFILL_RECOVERY_MODES:
        return raw  # type: ignore[return-value]
    if default in _REASONING_PREFILL_RECOVERY_MODES:
        return default  # type: ignore[return-value]
    return "log"


def supports_reasoning_prefill_replay(
    *,
    model_capabilities: ModelCapabilities | None,
    reasoning_content: str | None,
    thinking_signature: str | None,
) -> bool:
    if not reasoning_content or not reasoning_content.strip():
        return False
    if thinking_signature:
        return True
    if not model_capabilities or not model_capabilities.supports_reasoning:
        return False
    return model_capabilities.reasoning_format in {"openrouter", "deepseek"}


def reasoning_prefill_decision(
    *,
    global_mode: RuntimeRecoveryMode,
    mode: ReasoningPrefillRecoveryMode,
    attempt_kind: str,
    attempted: bool,
    supports_replay: bool,
    reasoning_chars: int,
    reasoning_tokens: int,
) -> RuntimeRecoveryDecision | None:
    if global_mode == "off" or mode == "off":
        return None
    if attempt_kind != "reasoning_only" or attempted or not supports_replay:
        return None
    injected = mode == "recover"
    return RuntimeRecoveryDecision(
        action="prefill" if injected else "observe",
        mechanism="reasoning_prefill_recovery",
        reason="reasoning_only_prefill_continuation",
        mode=mode,
        injected_to_model=injected,
        details={
            "reasoning_chars": reasoning_chars,
            "reasoning_tokens": reasoning_tokens,
            "supports_replay": supports_replay,
        },
    )


def reasoning_continuation_decision(
    *,
    global_mode: RuntimeRecoveryMode,
    mode: ReasoningPrefillRecoveryMode,
    attempt_kind: str,
    attempted: bool,
    supports_replay: bool,
    provider_reasoning_format: str | None,
    reasoning_chars: int,
    reasoning_tokens: int,
) -> RuntimeRecoveryDecision | None:
    """Decide whether to recover reasoning-only output without replaying reasoning.

    DashScope/Qwen does not accept provider-specific reasoning-content replay in
    the OpenAI-compatible history. For that shape, use a single visible nudge so
    the model can continue from its own prior turn without us echoing private
    reasoning back into the prompt.
    """

    if global_mode == "off" or mode == "off":
        return None
    if attempt_kind != "reasoning_only" or attempted or supports_replay:
        return None
    if (provider_reasoning_format or "").strip().lower() != "dashscope":
        return None
    injected = mode == "recover"
    return RuntimeRecoveryDecision(
        action="nudge" if injected else "observe",
        mechanism="reasoning_continuation_recovery",
        reason="reasoning_only_visible_continuation",
        mode=mode,
        injected_to_model=injected,
        message=REASONING_ONLY_CONTINUATION_MESSAGE if injected else None,
        details={
            "reasoning_chars": reasoning_chars,
            "reasoning_tokens": reasoning_tokens,
            "supports_replay": supports_replay,
            "provider_reasoning_format": provider_reasoning_format,
        },
    )


def post_tool_empty_decision(
    *,
    global_mode: RuntimeRecoveryMode,
    mode: RuntimeRecoveryMode,
    attempt_kind: str,
    post_tool_turn: bool,
    attempted: bool,
    reasoning_present: bool,
) -> RuntimeRecoveryDecision | None:
    if global_mode == "off" or mode == "off":
        return None
    if attempted or not post_tool_turn:
        return None
    if attempt_kind != "malformed_empty" or reasoning_present:
        return None
    injected = mode == "warn_model"
    return RuntimeRecoveryDecision(
        action="nudge" if injected else "observe",
        mechanism="post_tool_empty_recovery",
        reason="empty_response_after_tool_results",
        mode=mode,
        injected_to_model=injected,
        message=POST_TOOL_EMPTY_RECOVERY_MESSAGE if injected else None,
        details={"post_tool_turn": post_tool_turn},
    )


def source_loop_recovery_decision(
    *,
    global_mode: RuntimeRecoveryMode,
    diagnostic_events: list[dict[str, Any]],
    attempted: bool,
    attempted_event_keys: set[str] | frozenset[str] | None = None,
    max_nudges: int = 1,
) -> RuntimeRecoveryDecision | None:
    """Decide whether repeated source-loop diagnostics warrant one recovery nudge."""

    if global_mode == "off":
        return None
    max_nudges = max(1, int(max_nudges or 1))
    if attempted_event_keys is None:
        if attempted:
            return None
        attempted_event_keys = frozenset()
    elif len(attempted_event_keys) >= max_nudges:
        return None
    event = _first_source_loop_event(diagnostic_events, attempted_event_keys)
    if event is None:
        return None
    event_key = source_loop_recovery_event_key(event)
    injected = global_mode == "warn_model"
    reason = str(event.get("reason") or "source_loop_after_diff")
    details = {
        "trigger_reason": reason,
        "recovery_event_key": event_key,
        "source_loop_recovery_count": len(attempted_event_keys) + 1,
        "source_loop_recovery_max_nudges": max_nudges,
        "trigger_count": event.get("trigger_count"),
        "command_family": event.get("command_family"),
        "normalized_path": event.get("normalized_path"),
        "failure_anchor_hash": event.get("failure_anchor_hash"),
        "failure_anchor_excerpt": event.get("failure_anchor_excerpt"),
        "changed_files": _source_paths_from_event(event, "changed_files"),
        "diff_paths": _source_paths_from_event(event, "diff_paths"),
        "diff_fingerprint_before": event.get("diff_fingerprint_before"),
        "diff_fingerprint_after": event.get("diff_fingerprint_after"),
    }
    return RuntimeRecoveryDecision(
        action="nudge" if injected else "observe",
        mechanism="source_loop_recovery",
        reason="source_loop_after_diff",
        mode=global_mode,
        injected_to_model=injected,
        message=SOURCE_LOOP_RECOVERY_MESSAGE if injected else None,
        details=details,
    )


def source_loop_recovery_event_key(event: dict[str, Any]) -> str:
    """Return a stable key for source-loop recovery de-duplication."""

    payload = {
        "reason": event.get("reason"),
        "trigger_key_hash": event.get("trigger_key_hash"),
        "diff_fingerprint_after": event.get("diff_fingerprint_after"),
        "command_family": event.get("command_family"),
        "normalized_path": event.get("normalized_path"),
        "failure_anchor_hash": event.get("failure_anchor_hash"),
    }
    return "|".join(str(payload.get(key) or "") for key in sorted(payload))


def _first_source_loop_event(
    events: list[dict[str, Any]],
    attempted_event_keys: set[str] | frozenset[str] | None = None,
) -> dict[str, Any] | None:
    attempted_event_keys = attempted_event_keys or frozenset()
    for event in events:
        reason = event.get("reason")
        if reason not in _SOURCE_LOOP_RECOVERY_REASONS:
            continue
        if not _event_has_source_diff(event):
            continue
        if source_loop_recovery_event_key(event) in attempted_event_keys:
            continue
        return event
    return None


def _event_has_source_diff(event: dict[str, Any]) -> bool:
    path_classes = event.get("path_classes") if isinstance(event, dict) else None
    if isinstance(path_classes, dict):
        for key in ("diff_paths", "changed_files"):
            classified = path_classes.get(key)
            if isinstance(classified, dict) and any(
                value == "source" for value in classified.values()
            ):
                return True
    for key in ("diff_paths", "changed_files"):
        if _source_paths_from_event(event, key):
            return True
    return False


def _source_paths_from_event(event: dict[str, Any], key: str) -> list[str]:
    raw_paths = event.get(key)
    if not isinstance(raw_paths, list):
        return []
    path_classes = event.get("path_classes")
    classified: dict[str, Any] = {}
    if isinstance(path_classes, dict) and isinstance(path_classes.get(key), dict):
        classified = path_classes[key]
    paths: list[str] = []
    for value in raw_paths:
        if not isinstance(value, str) or not value:
            continue
        if classified:
            if classified.get(value) == "source":
                paths.append(value)
            continue
        if _path_looks_source(value):
            paths.append(value)
    return paths[:8]


def _path_looks_source(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./").casefold()
    if not normalized:
        return False
    non_source_markers = (
        "/test/",
        "/tests/",
        "__tests__/",
        ".test.",
        ".spec.",
        "docs/",
        "doc/",
        "target/",
        "build/",
        "dist/",
        "generated/",
        "tmp/",
        "scratch/",
        "debug",
    )
    return not any(marker in normalized for marker in non_source_markers)
