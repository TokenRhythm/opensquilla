"""Fail-closed model admission for turns carrying attachment context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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
CAPACITY_REDUCTION_HINT = (
    "Reduce the attachment or session context, run /compact, or start a new session "
    "before retrying."
)
CapacityAdmissionStatus = Literal[
    "fits", "known_capacity_request_too_large", "capacity_unknown",
]


class LargeContextCapacityError(RuntimeError):
    """An attachment turn has no deployment with proven request capacity."""

    def __init__(self, message: str, *, status: CapacityAdmissionStatus | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = {
            "known_capacity_request_too_large": "attachment_capacity_too_large",
            "capacity_unknown": "attachment_capacity_unknown",
        }.get(status or "", "attachment_capacity_unavailable")


@dataclass(frozen=True, slots=True)
class ModelRequestCapacityAssessment:
    """Capacity proof for a physical deployment, before request serialization."""

    status: CapacityAdmissionStatus
    required_input_tokens: int
    safe_input_tokens: int | None

    @property
    def fits(self) -> bool:
        return self.status == "fits"


def assess_model_request_capacity(
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
) -> ModelRequestCapacityAssessment:
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
    required_input_tokens = (
        resolved_request_tokens
        if resolved_request_tokens > 0
        else resolved_material_tokens + NON_MATERIAL_INPUT_HEADROOM_TOKENS
        if resolved_material_tokens > 0
        else 0
    )
    unknown = ModelRequestCapacityAssessment("capacity_unknown", required_input_tokens, None)
    if not provider_id or not model_id or (
        resolved_request_tokens <= 0 and resolved_material_tokens <= 0
    ):
        return unknown
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
        return unknown
    if window_source not in {"catalog", "config", "override"}:
        return unknown
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
    return ModelRequestCapacityAssessment(
        (
            "fits" if required_input_tokens <= safe_input_tokens
            else "known_capacity_request_too_large"
        ),
        required_input_tokens,
        safe_input_tokens,
    )


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
    """Keep the boolean admission contract for ordinary routing and fallbacks."""

    return assess_model_request_capacity(
        provider=provider,
        model=model,
        material_tokens=material_tokens,
        thinking_budget_tokens=thinking_budget_tokens,
        request_input_tokens=request_input_tokens,
        context_window_override_tokens=context_window_override_tokens,
        max_output_override_tokens=max_output_override_tokens,
        provider_request_proof_max_chars=provider_request_proof_max_chars,
        api_key=api_key,
        base_url=base_url,
        proxy=proxy,
    ).fits


__all__ = [
    "CAPACITY_CONFIGURATION_HINT",
    "CAPACITY_REDUCTION_HINT",
    "CapacityAdmissionStatus",
    "LargeContextCapacityError",
    "MAX_THINKING_BUDGET_TOKENS",
    "ModelRequestCapacityAssessment",
    "NON_MATERIAL_INPUT_HEADROOM_TOKENS",
    "assess_model_request_capacity",
    "model_has_request_capacity",
]
