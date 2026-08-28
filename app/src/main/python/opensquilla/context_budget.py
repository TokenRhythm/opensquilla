"""Shared context-budget derivation for provider and agent views."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

CHARS_PER_TOKEN = 4
LARGE_CONTEXT_MIN_TOKENS = 64_000
CONTEXT_RESERVE_FLOOR_TOKENS = 20_000
SMALL_CONTEXT_MIN_PROOF_CHARS = 4_000
SMALL_CONTEXT_MAX_PROOF_CHARS = 32_000
LARGE_CONTEXT_MIN_ARGUMENT_CHARS = 64_000
LARGE_CONTEXT_MAX_ARGUMENT_CHARS = 512_000
LARGE_CONTEXT_MIN_RESULT_CHARS = 128_000
LARGE_CONTEXT_MAX_RESULT_CHARS = 160_000


class ContextBudgetClass(StrEnum):
    EXTERNAL = "external"
    LOCAL = "local"
    ARTIFACT = "artifact"
    ERROR = "error"
    CONTROL = "control"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ContextBudgetSnapshot:
    context_window_tokens: int
    reserved_tokens: int
    usable_tokens: int
    threshold: float
    provider_request_max_chars: int
    default_tool_argument_max_chars: int
    external_tool_argument_max_chars: int
    default_tool_result_provider_max_chars: int
    external_tool_result_provider_max_chars: int


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _threshold(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.85
    return min(max(parsed, 0.1), 0.95)


def _context_class(value: ContextBudgetClass | str | None) -> ContextBudgetClass:
    if isinstance(value, ContextBudgetClass):
        return value
    if isinstance(value, str):
        try:
            return ContextBudgetClass(value)
        except ValueError:
            return ContextBudgetClass.UNKNOWN
    return ContextBudgetClass.UNKNOWN


class ContextBudgetGovernor:
    """Derive related provider-context budgets from one model window."""

    def __init__(
        self,
        snapshot: ContextBudgetSnapshot,
        *,
        explicit_tool_argument_max_chars: int | None = None,
        explicit_tool_result_provider_max_chars: int | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._explicit_tool_argument_max_chars = explicit_tool_argument_max_chars
        self._explicit_tool_result_provider_max_chars = (
            explicit_tool_result_provider_max_chars
        )

    @classmethod
    def from_config(cls, config: Any) -> ContextBudgetGovernor:
        thinking_budget_tokens = 0
        try:
            thinking_enabled, resolved_thinking_budget = config.resolve_thinking(None)
        except Exception:  # noqa: BLE001 - tests may pass lightweight config doubles
            thinking_enabled = False
            resolved_thinking_budget = 0
        if thinking_enabled:
            thinking_budget_tokens = max(0, int(resolved_thinking_budget or 0))
        return cls.from_values(
            context_window_tokens=getattr(config, "context_window_tokens", 0),
            max_output_tokens=getattr(config, "max_tokens", 0),
            thinking_budget_tokens=thinking_budget_tokens,
            context_overflow_threshold=getattr(
                config,
                "context_overflow_threshold",
                0.85,
            ),
            provider_request_proof_max_chars=getattr(
                config,
                "provider_request_proof_max_chars",
                0,
            ),
            tool_use_argument_provider_request_max_chars=getattr(
                config,
                "tool_use_argument_provider_request_max_chars",
                0,
            ),
            tool_result_provider_request_max_chars=getattr(
                config,
                "tool_result_provider_request_max_chars",
                0,
            ),
        )

    @classmethod
    def from_values(
        cls,
        *,
        context_window_tokens: Any,
        max_output_tokens: Any,
        thinking_budget_tokens: Any,
        context_overflow_threshold: Any,
        provider_request_proof_max_chars: Any = 0,
        tool_use_argument_provider_request_max_chars: Any = 0,
        tool_result_provider_request_max_chars: Any = 0,
    ) -> ContextBudgetGovernor:
        context_tokens = max(1, int(context_window_tokens or 0))
        max_output = max(0, int(max_output_tokens or 0))
        thinking_budget = max(0, int(thinking_budget_tokens or 0))
        threshold = _threshold(context_overflow_threshold)

        max_reserve = max(1, context_tokens // 2)
        output_reserve = min(max_output + thinking_budget, max_reserve)
        context_reserve = (
            CONTEXT_RESERVE_FLOOR_TOKENS
            if context_tokens >= LARGE_CONTEXT_MIN_TOKENS
            else max(512, context_tokens // 8)
        )
        reserved_tokens = min(
            max(context_tokens - 1, 1),
            output_reserve + context_reserve,
        )
        usable_tokens = max(1, context_tokens - reserved_tokens)

        explicit_proof = _positive_int(provider_request_proof_max_chars)
        derived_provider_chars = int(usable_tokens * threshold * CHARS_PER_TOKEN)
        if context_tokens < LARGE_CONTEXT_MIN_TOKENS:
            derived_provider_chars = min(
                SMALL_CONTEXT_MAX_PROOF_CHARS,
                max(SMALL_CONTEXT_MIN_PROOF_CHARS, derived_provider_chars),
            )
        provider_chars = explicit_proof or max(1, derived_provider_chars)

        explicit_argument = _positive_int(tool_use_argument_provider_request_max_chars)
        explicit_result = _positive_int(tool_result_provider_request_max_chars)

        default_arg = cls._derive_tool_argument_chars(
            context_tokens=context_tokens,
            provider_chars=provider_chars,
            explicit=explicit_argument,
        )
        external_arg = cls._derive_external_tool_argument_chars(
            context_tokens=context_tokens,
            provider_chars=provider_chars,
            default_chars=default_arg,
            explicit=explicit_argument,
        )
        default_result = cls._derive_tool_result_provider_chars(
            context_tokens=context_tokens,
            provider_chars=provider_chars,
            explicit=explicit_result,
        )
        external_result = cls._derive_external_tool_result_provider_chars(
            context_tokens=context_tokens,
            provider_chars=provider_chars,
            default_chars=default_result,
            explicit=explicit_result,
        )

        return cls(
            ContextBudgetSnapshot(
                context_window_tokens=context_tokens,
                reserved_tokens=reserved_tokens,
                usable_tokens=usable_tokens,
                threshold=threshold,
                provider_request_max_chars=provider_chars,
                default_tool_argument_max_chars=default_arg,
                external_tool_argument_max_chars=external_arg,
                default_tool_result_provider_max_chars=default_result,
                external_tool_result_provider_max_chars=external_result,
            ),
            explicit_tool_argument_max_chars=explicit_argument,
            explicit_tool_result_provider_max_chars=explicit_result,
        )

    @staticmethod
    def _derive_tool_argument_chars(
        *,
        context_tokens: int,
        provider_chars: int,
        explicit: int | None,
    ) -> int:
        if explicit is not None:
            return explicit
        if context_tokens < LARGE_CONTEXT_MIN_TOKENS:
            return max(2_000, min(16_000, provider_chars // 2))
        return max(
            LARGE_CONTEXT_MIN_ARGUMENT_CHARS,
            min(LARGE_CONTEXT_MAX_ARGUMENT_CHARS, int(provider_chars * 0.16)),
        )

    @staticmethod
    def _derive_external_tool_argument_chars(
        *,
        context_tokens: int,
        provider_chars: int,
        default_chars: int,
        explicit: int | None,
    ) -> int:
        if explicit is not None:
            return max(1, min(default_chars, explicit))
        if context_tokens < LARGE_CONTEXT_MIN_TOKENS:
            return max(1_000, min(default_chars, provider_chars // 3))
        return max(8_000, min(default_chars, int(provider_chars * 0.05), 32_000))

    @staticmethod
    def _derive_tool_result_provider_chars(
        *,
        context_tokens: int,
        provider_chars: int,
        explicit: int | None,
    ) -> int:
        if explicit is not None:
            return explicit
        if context_tokens < LARGE_CONTEXT_MIN_TOKENS:
            return max(4_000, min(32_000, int(provider_chars * 0.75)))
        return max(
            LARGE_CONTEXT_MIN_RESULT_CHARS,
            min(LARGE_CONTEXT_MAX_RESULT_CHARS, int(provider_chars * 0.50)),
        )

    @staticmethod
    def _derive_external_tool_result_provider_chars(
        *,
        context_tokens: int,
        provider_chars: int,
        default_chars: int,
        explicit: int | None,
    ) -> int:
        if explicit is not None:
            return max(1, min(default_chars, explicit))
        if context_tokens < LARGE_CONTEXT_MIN_TOKENS:
            return max(2_000, min(default_chars, int(provider_chars * 0.50)))
        return max(96_000, min(default_chars, int(provider_chars * 0.25)))

    def snapshot(self) -> ContextBudgetSnapshot:
        return self._snapshot

    def tool_argument_chars_for(
        self,
        budget_class: ContextBudgetClass | str | None,
    ) -> int:
        resolved = _context_class(budget_class)
        if resolved is ContextBudgetClass.EXTERNAL:
            return self._snapshot.external_tool_argument_max_chars
        return self._snapshot.default_tool_argument_max_chars

    def tool_result_provider_chars_for(
        self,
        budget_class: ContextBudgetClass | str | None,
    ) -> int:
        resolved = _context_class(budget_class)
        if resolved is ContextBudgetClass.EXTERNAL:
            return self._snapshot.external_tool_result_provider_max_chars
        return self._snapshot.default_tool_result_provider_max_chars
