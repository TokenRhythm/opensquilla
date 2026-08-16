"""Fail-closed model admission for turns carrying large material context."""

from __future__ import annotations

from opensquilla.context_budget import CHARS_PER_TOKEN, ContextBudgetGovernor
from opensquilla.provider.model_catalog import (
    resolve_effective_context_window,
    shared_catalog,
)

NON_MATERIAL_INPUT_HEADROOM_TOKENS = 8_192
MAX_THINKING_BUDGET_TOKENS = 50_000


class LargeContextCapacityError(RuntimeError):
    """A large-material turn has no deployment with proven request capacity."""


def model_has_request_capacity(
    *,
    provider: str,
    model: str,
    material_tokens: int,
    thinking_budget_tokens: int,
    context_window_override_tokens: int = 0,
    max_output_override_tokens: int = 0,
    provider_request_proof_max_chars: int = 0,
    api_key: str = "",
    base_url: str = "",
    proxy: str = "",
) -> bool:
    """Return whether definite catalog limits prove a conservative request fits."""

    provider_id = str(provider or "").strip()
    model_id = str(model or "").strip()
    if not provider_id or not model_id or material_tokens <= 0:
        return False
    catalog = shared_catalog()
    try:
        window, window_source = resolve_effective_context_window(
            catalog,
            model_id,
            provider=provider_id,
            global_override=max(0, int(context_window_override_tokens)),
        )
        max_output, _output_source = catalog.resolve_max_tokens_with_source(
            model_id,
            user_override=max(0, int(max_output_override_tokens)),
            provider=provider_id,
        )
        deployment_resolver = getattr(catalog, "resolve_deployment_limits", None)
        if callable(deployment_resolver):
            deployment_limits = deployment_resolver(
                model_id,
                provider=provider_id,
                api_key=api_key,
                base_url=base_url,
                proxy=proxy,
                logical_max_tokens_override=max(
                    0,
                    int(max_output_override_tokens),
                ),
            )
            window = min(window, int(deployment_limits.context_window))
            if deployment_limits.max_output_tokens_known:
                max_output = min(
                    max_output,
                    int(deployment_limits.max_output_tokens),
                )
    except Exception:  # noqa: BLE001 - invalid/missing capability fails closed
        return False
    if window_source not in {"catalog", "config", "override"}:
        return False
    budget = ContextBudgetGovernor.from_values(
        context_window_tokens=window,
        max_output_tokens=max_output,
        thinking_budget_tokens=max(0, int(thinking_budget_tokens)),
        context_overflow_threshold=0.85,
    ).snapshot()
    safe_input_tokens = budget.provider_request_max_chars // CHARS_PER_TOKEN
    if provider_request_proof_max_chars > 0:
        safe_input_tokens = min(
            safe_input_tokens,
            int(provider_request_proof_max_chars) // CHARS_PER_TOKEN,
        )
    return (
        material_tokens + NON_MATERIAL_INPUT_HEADROOM_TOKENS
        <= safe_input_tokens
    )


__all__ = [
    "LargeContextCapacityError",
    "MAX_THINKING_BUDGET_TOKENS",
    "NON_MATERIAL_INPUT_HEADROOM_TOKENS",
    "model_has_request_capacity",
]
