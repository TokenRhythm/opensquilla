"""Fail-closed model admission for turns carrying attachment context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from opensquilla.context_budget import CHARS_PER_TOKEN, ContextBudgetGovernor
from opensquilla.provider.model_catalog import (
    resolve_effective_context_window,
    shared_catalog,
)

NON_MATERIAL_INPUT_HEADROOM_TOKENS = 8_192
MAX_THINKING_BUDGET_TOKENS = 50_000
CAPACITY_CONFIGURATION_HINT = (
    "For a custom or catalog-unknown model, set llm.context_window_tokens "
    "to the deployment's verified context limit."
)
CAPACITY_REDUCTION_HINT = (
    "Reduce the attachment or session context, run /compact, or start a new "
    "session before retrying."
)
CAPACITY_TOO_LARGE_ERROR_CODE = "attachment_capacity_too_large"
CAPACITY_UNKNOWN_ERROR_CODE = "attachment_capacity_unknown"

CapacityAdmissionStatus = Literal[
    "fits",
    "known_capacity_request_too_large",
    "capacity_unknown",
]


class LargeContextCapacityError(RuntimeError):
    """An attachment turn has no deployment with proven request capacity."""

    def __init__(
        self,
        message: str,
        *,
        status: CapacityAdmissionStatus | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = (
            CAPACITY_TOO_LARGE_ERROR_CODE
            if status == "known_capacity_request_too_large"
            else CAPACITY_UNKNOWN_ERROR_CODE
            if status == "capacity_unknown"
            else "attachment_capacity_unavailable"
        )


@dataclass(frozen=True, slots=True)
class ModelRequestCapacityAssessment:
    """Structured proof result for one physical model deployment."""

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
    """Classify whether definite deployment limits prove a request fits."""

    provider_id = str(provider or "").strip()
    model_id = str(model or "").strip()
    resolved_request_tokens = max(0, int(request_input_tokens))
    resolved_material_tokens = max(0, int(material_tokens))
    required_input_tokens = (
        resolved_request_tokens
        if resolved_request_tokens > 0
        else (
            resolved_material_tokens + NON_MATERIAL_INPUT_HEADROOM_TOKENS
            if resolved_material_tokens > 0
            else 0
        )
    )
    unknown = ModelRequestCapacityAssessment(
        status="capacity_unknown",
        required_input_tokens=required_input_tokens,
        safe_input_tokens=None,
    )
    if not provider_id or not model_id or required_input_tokens <= 0:
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
        if window_source not in {"catalog", "config", "override"}:
            return unknown
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
    except Exception:  # noqa: BLE001 - invalid/missing capability fails closed
        return unknown

    return ModelRequestCapacityAssessment(
        status=(
            "fits"
            if required_input_tokens <= safe_input_tokens
            else "known_capacity_request_too_large"
        ),
        required_input_tokens=required_input_tokens,
        safe_input_tokens=safe_input_tokens,
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
    """Return whether definite catalog limits prove a conservative request fits.

    ``request_input_tokens`` is the preferred path: callers that can measure the
    assembled request pass the complete input estimate. ``material_tokens`` plus
    the historical fixed reserve remains only for compatibility with older
    internal callers that have not reached an assembled-request boundary.
    """

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
    "CAPACITY_TOO_LARGE_ERROR_CODE",
    "CAPACITY_UNKNOWN_ERROR_CODE",
    "CapacityAdmissionStatus",
    "LargeContextCapacityError",
    "MAX_THINKING_BUDGET_TOKENS",
    "ModelRequestCapacityAssessment",
    "NON_MATERIAL_INPUT_HEADROOM_TOKENS",
    "assess_model_request_capacity",
    "model_has_request_capacity",
]
