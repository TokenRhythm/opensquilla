"""Fail-closed model admission for turns carrying large material context."""

from __future__ import annotations

from opensquilla.context_budget import CHARS_PER_TOKEN, ContextBudgetGovernor
from opensquilla.provider.model_catalog import shared_catalog

NON_MATERIAL_INPUT_HEADROOM_TOKENS = 8_192
MAX_THINKING_BUDGET_TOKENS = 50_000


def model_has_request_capacity(
    *,
    provider: str,
    model: str,
    material_tokens: int,
    thinking_budget_tokens: int,
) -> bool:
    """Return whether definite catalog limits prove a conservative request fits."""

    provider_id = str(provider or "").strip()
    model_id = str(model or "").strip()
    if not provider_id or not model_id or material_tokens <= 0:
        return False
    catalog = shared_catalog()
    try:
        window, window_source = catalog.resolve_context_window_with_source(
            model_id,
            provider_id,
        )
        max_output, _output_source = catalog.resolve_max_tokens_with_source(
            model_id,
            user_override=0,
            provider=provider_id,
        )
    except Exception:  # noqa: BLE001 - invalid/missing capability fails closed
        return False
    if window_source not in {"catalog", "override"}:
        return False
    budget = ContextBudgetGovernor.from_values(
        context_window_tokens=window,
        max_output_tokens=max_output,
        thinking_budget_tokens=max(0, int(thinking_budget_tokens)),
        context_overflow_threshold=0.85,
    ).snapshot()
    safe_input_tokens = budget.provider_request_max_chars // CHARS_PER_TOKEN
    return (
        material_tokens + NON_MATERIAL_INPUT_HEADROOM_TOKENS
        <= safe_input_tokens
    )


__all__ = [
    "MAX_THINKING_BUDGET_TOKENS",
    "NON_MATERIAL_INPUT_HEADROOM_TOKENS",
    "model_has_request_capacity",
]
