"""Reasoning-dialect registry for OpenAI-compatible thinking payloads.

Every OpenAI-compatible upstream spells its extended-thinking switch
differently: OpenRouter takes ``reasoning={"effort": ...}``, OpenAI takes a
bare ``reasoning_effort``, DeepSeek and the zai/moonshot/volcengine family
take a ``thinking`` object, DashScope takes ``enable_thinking`` plus
``thinking_budget``. ``DIALECTS`` maps each ``reasoning_format`` value to the
payload mutations for turning thinking on and off, so adding a dialect is one
registry entry instead of another elif in the request builder.

Gating stays with the caller (``openai.py``): which model or capability
profile triggers a payload at all — ``supports_reasoning``,
``thinking_toggle_model_ids``, the ``default_reasoning_format`` fallback — is
policy knowledge. This module only knows how each dialect spells the result.
A ``reasoning_format`` absent from ``DIALECTS`` ("none", "think_tags",
unknown values) deliberately produces no payload in either direction.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opensquilla.engine.types import ThinkingLevel


def _resolve_reasoning_effort(level: ThinkingLevel | None, budget: int) -> str:
    """Map ThinkingLevel to OpenRouter/DeepSeek effort string."""
    from opensquilla.engine.types import (
        ThinkingLevel,  # local: avoids circular import at module load
    )

    _level_map = {
        ThinkingLevel.MINIMAL: "minimal",
        ThinkingLevel.LOW: "low",
        ThinkingLevel.MEDIUM: "medium",
        ThinkingLevel.HIGH: "high",
        ThinkingLevel.XHIGH: "xhigh",
    }
    if level and level in _level_map:
        return _level_map[level]
    if budget <= 1024:
        return "low"
    elif budget <= 10000:
        return "medium"
    else:
        return "high"


def _resolve_deepseek_reasoning_effort(level: ThinkingLevel | None) -> str:
    """Map OpenSquilla thinking levels to DeepSeek V4's documented effort values."""
    from opensquilla.engine.types import (
        ThinkingLevel,  # local: avoids circular import at module load
    )

    if level == ThinkingLevel.XHIGH:
        return "max"
    return "high"


def _resolve_tokenhub_reasoning_effort(level: ThinkingLevel | None) -> str:
    """Map OpenSquilla thinking levels to TokenHub's documented effort values.

    TokenHub's hy3 family accepts exactly ``low`` and ``high`` — anything
    else is undocumented, so the five-level ladder collapses onto those two.
    """
    from opensquilla.engine.types import (
        ThinkingLevel,  # local: avoids circular import at module load
    )

    if level in (ThinkingLevel.MINIMAL, ThinkingLevel.LOW):
        return "low"
    return "high"


def _gemini_supports_reasoning_none(model: str) -> bool:
    """Return True for Gemini OpenAI-compatible models with documented off control."""
    model_name = model.rsplit("/", 1)[-1].strip().lower()
    return model_name.startswith("gemini-2.5-flash")


@dataclass(frozen=True)
class ReasoningEnableArgs:
    """Inputs a dialect may consume when turning thinking on."""

    thinking_level: ThinkingLevel | None
    thinking_budget_tokens: int
    model: str = ""
    thinking_budget_explicit: bool = False
    reasoning_effort_override: str | None = None

    @property
    def effort(self) -> str:
        """Effort string resolved from level/budget for effort-style dialects."""
        return _resolve_reasoning_effort(self.thinking_level, self.thinking_budget_tokens)


@dataclass(frozen=True)
class ReasoningDisableArgs:
    """Inputs a dialect may consume when turning thinking off.

    ``model`` is the raw configured model id; dialects that gate their off
    payload on the model apply their own normalization, exactly as the
    request-builder ladder did.
    """

    model: str
    disable_reasoning_by_default_models: frozenset[str] = frozenset()


def _enable_openrouter(payload: dict[str, Any], args: ReasoningEnableArgs) -> None:
    payload["reasoning"] = {"effort": args.effort}


def _enable_reasoning_effort(payload: dict[str, Any], args: ReasoningEnableArgs) -> None:
    payload["reasoning_effort"] = args.effort


def _enable_deepseek(payload: dict[str, Any], args: ReasoningEnableArgs) -> None:
    payload["thinking"] = {"type": "enabled"}
    payload["reasoning_effort"] = (
        args.reasoning_effort_override
        or _resolve_deepseek_reasoning_effort(args.thinking_level)
    )


def _enable_tencent_tokenhub(payload: dict[str, Any], args: ReasoningEnableArgs) -> None:
    payload["thinking"] = {"type": "enabled"}
    payload["reasoning_effort"] = _resolve_tokenhub_reasoning_effort(args.thinking_level)


def _enable_thinking_object(payload: dict[str, Any], args: ReasoningEnableArgs) -> None:
    payload["thinking"] = {"type": "enabled"}


def _enable_dashscope(payload: dict[str, Any], args: ReasoningEnableArgs) -> None:
    payload["enable_thinking"] = True
    payload["thinking_budget"] = args.thinking_budget_tokens


def _enable_qwen_token_plan(payload: dict[str, Any], args: ReasoningEnableArgs) -> None:
    payload["enable_thinking"] = True


def _enable_qwen_token_plan_qwen(
    payload: dict[str, Any], args: ReasoningEnableArgs
) -> None:
    payload["enable_thinking"] = True
    if args.model.rsplit("/", 1)[-1].strip().lower() == "qwen3.8-max-preview":
        if args.thinking_budget_explicit:
            payload["thinking_budget"] = args.thinking_budget_tokens
        else:
            from opensquilla.engine.types import ThinkingLevel

            if args.thinking_level in (ThinkingLevel.MINIMAL, ThinkingLevel.LOW):
                payload["reasoning_effort"] = "low"
            elif args.thinking_level in (ThinkingLevel.MEDIUM, ThinkingLevel.HIGH):
                payload["reasoning_effort"] = "medium"
            else:
                payload["reasoning_effort"] = "xhigh"
        return
    payload["thinking_budget"] = args.thinking_budget_tokens


def _enable_qwen_token_plan_deepseek(
    payload: dict[str, Any], args: ReasoningEnableArgs
) -> None:
    payload["enable_thinking"] = True
    payload["reasoning_effort"] = _resolve_deepseek_reasoning_effort(args.thinking_level)


def _enable_qwen_token_plan_glm(
    payload: dict[str, Any], args: ReasoningEnableArgs
) -> None:
    payload["enable_thinking"] = True
    payload["reasoning_effort"] = _resolve_deepseek_reasoning_effort(
        args.thinking_level
    )


def _disable_qwen_token_plan(
    payload: dict[str, Any], args: ReasoningDisableArgs
) -> None:
    payload["enable_thinking"] = False


def _disable_thinking_object(payload: dict[str, Any], args: ReasoningDisableArgs) -> None:
    payload["thinking"] = {"type": "disabled"}


def _disable_gemini(payload: dict[str, Any], args: ReasoningDisableArgs) -> None:
    # Only Gemini models with a documented off control accept the explicit
    # "none"; every other Gemini model omits the field when thinking is off.
    if _gemini_supports_reasoning_none(args.model):
        payload["reasoning_effort"] = "none"


def _disable_dashscope(payload: dict[str, Any], args: ReasoningDisableArgs) -> None:
    payload["enable_thinking"] = False


def _disable_openrouter(payload: dict[str, Any], args: ReasoningDisableArgs) -> None:
    # OpenRouter's reasoning controls are model/provider-specific: only the
    # models in the policy's disable set are stabilized by an explicit off
    # payload; other reasoning endpoints reject it.
    if args.model.strip().lower() in args.disable_reasoning_by_default_models:
        payload["reasoning"] = {"enabled": False}


@dataclass(frozen=True)
class ReasoningDialect:
    """How one ``reasoning_format`` spells its thinking on/off payload."""

    name: str
    enable: Callable[[dict[str, Any], ReasoningEnableArgs], None]
    # None = the dialect has no off payload: thinking-off omits every field.
    disable: Callable[[dict[str, Any], ReasoningDisableArgs], None] | None


# "none" (and any unknown format such as "think_tags") is deliberately absent:
# those formats serialize with no reasoning field in either direction.
# moonshot and volcengine share one wire spelling but stay separate entries so
# each reasoning_format resolves through exactly one key.
DIALECTS: dict[str, ReasoningDialect] = {
    "openrouter": ReasoningDialect(
        name="openrouter",
        enable=_enable_openrouter,
        disable=_disable_openrouter,
    ),
    "openai": ReasoningDialect(
        name="openai",
        enable=_enable_reasoning_effort,
        disable=None,
    ),
    "deepseek": ReasoningDialect(
        name="deepseek",
        enable=_enable_deepseek,
        disable=_disable_thinking_object,
    ),
    "gemini": ReasoningDialect(
        name="gemini",
        enable=_enable_reasoning_effort,
        disable=_disable_gemini,
    ),
    "zai": ReasoningDialect(
        name="zai",
        enable=_enable_thinking_object,
        disable=_disable_thinking_object,
    ),
    "dashscope": ReasoningDialect(
        name="dashscope",
        enable=_enable_dashscope,
        disable=_disable_dashscope,
    ),
    "qwen_token_plan": ReasoningDialect(
        name="qwen_token_plan",
        enable=_enable_qwen_token_plan,
        disable=_disable_qwen_token_plan,
    ),
    "qwen_token_plan_qwen": ReasoningDialect(
        name="qwen_token_plan_qwen",
        enable=_enable_qwen_token_plan_qwen,
        disable=_disable_qwen_token_plan,
    ),
    "qwen_token_plan_deepseek": ReasoningDialect(
        name="qwen_token_plan_deepseek",
        enable=_enable_qwen_token_plan_deepseek,
        disable=_disable_qwen_token_plan,
    ),
    "qwen_token_plan_glm": ReasoningDialect(
        name="qwen_token_plan_glm",
        enable=_enable_qwen_token_plan_glm,
        disable=_disable_qwen_token_plan,
    ),
    "qwen_token_plan_kimi": ReasoningDialect(
        name="qwen_token_plan_kimi",
        enable=_enable_qwen_token_plan,
        disable=_disable_qwen_token_plan,
    ),
    "moonshot": ReasoningDialect(
        name="moonshot",
        enable=_enable_thinking_object,
        disable=_disable_thinking_object,
    ),
    "volcengine": ReasoningDialect(
        name="volcengine",
        enable=_enable_thinking_object,
        disable=_disable_thinking_object,
    ),
    # TokenHub (hy3 family) documents thinking={"type": "enabled"} plus
    # reasoning_effort low|high, but no off payload — thinking-off omits
    # every field and the endpoint applies its own default.
    "tencent_tokenhub": ReasoningDialect(
        name="tencent_tokenhub",
        enable=_enable_tencent_tokenhub,
        disable=None,
    ),
}


def apply_reasoning_enable(
    payload: dict[str, Any],
    reasoning_format: str,
    args: ReasoningEnableArgs,
) -> None:
    """Apply the thinking-on payload for a dialect; unknown formats are a no-op."""
    dialect = DIALECTS.get(reasoning_format)
    if dialect is not None:
        dialect.enable(payload, args)


def apply_reasoning_disable(
    payload: dict[str, Any],
    reasoning_format: str,
    args: ReasoningDisableArgs,
) -> None:
    """Apply the thinking-off payload for a dialect; no-op when there is none."""
    dialect = DIALECTS.get(reasoning_format)
    if dialect is not None and dialect.disable is not None:
        dialect.disable(payload, args)
