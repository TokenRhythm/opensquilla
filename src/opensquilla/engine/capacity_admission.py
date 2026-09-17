"""Fail-closed model admission for turns carrying attachment context."""

from __future__ import annotations

from opensquilla.context_budget import ContextBudgetGovernor
from opensquilla.provider.model_catalog import (
    resolve_effective_context_window,
    shared_catalog,
)
from opensquilla.provider.request_proof import effective_proof_token_budget

NON_MATERIAL_INPUT_HEADROOM_TOKENS = 8_192
MAX_THINKING_BUDGET_TOKENS = 50_000
CAPACITY_CONFIGURATION_HINT = (
    "For a custom or catalog-unknown model, set llm.context_window_tokens "
    "to the deployment's verified context limit."
)


class LargeContextCapacityError(RuntimeError):
    """An attachment turn has no deployment with proven request capacity."""


def model_has_request_capacity(
    *,
    provider: str,
    model: str,
    material_tokens: int,
    thinking_budget_tokens: int,
    request_input_tokens: int = 0,
    context_window_override_tokens: int = 0,
    max_output_override_tokens: int = 0,
    provider_request_proof_max_chars: int = 0,
    api_key: str = "",
    base_url: str = "",
    proxy: str = "",
) -> bool:
    """Return whether definite catalog limits prove a conservative request fits.

    ``request_input_tokens`` is the preferred path: callers that can measure the
    assembled request pass the complete input estimate. ``material_tokens`` plus
    the historical fixed reserve remains only for compatibility with older
    internal callers that have not reached an assembled-request boundary.
    """

    provider_id = str(provider or "").strip()
    model_id = str(model or "").strip()
    resolved_request_tokens = max(0, int(request_input_tokens))
    resolved_material_tokens = max(0, int(material_tokens))
    if not provider_id or not model_id or (
        resolved_request_tokens <= 0 and resolved_material_tokens <= 0
    ):
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
        provider_request_proof_max_chars=provider_request_proof_max_chars,
    ).snapshot()
    safe_input_tokens, _headroom = effective_proof_token_budget(budget.usable_tokens)
    # This prefilter receives token estimates, not the serialized request.
    # Its character cap is enforced separately by the final adapter proof;
    # converting that cap to tokens would conflate two independent limits.
    required_input_tokens = (
        resolved_request_tokens
        if resolved_request_tokens > 0
        else resolved_material_tokens + NON_MATERIAL_INPUT_HEADROOM_TOKENS
    )
    return required_input_tokens <= safe_input_tokens


__all__ = [
    "CAPACITY_CONFIGURATION_HINT",
    "LargeContextCapacityError",
    "MAX_THINKING_BUDGET_TOKENS",
    "NON_MATERIAL_INPUT_HEADROOM_TOKENS",
    "model_has_request_capacity",
]
