"""Runtime recovery decisions for incomplete provider responses."""

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
