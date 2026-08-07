"""Context budget coordination around provider request proof."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import asdict, dataclass
from typing import Any, Literal

from opensquilla.provider.request_proof import (
    CHAT_REQUEST_ENVELOPE,
    ProviderRequestBudgetExceededError,
    ProviderRequestEnvelopeShape,
    prove_or_compact_provider_payload,
)

ContextBudgetAction = Literal[
    "send",
    "send_compacted",
    "budget_limited",
    "invalid_request",
]


@dataclass(frozen=True)
class ContextBudgetDecision:
    action: ContextBudgetAction
    payload: dict[str, Any] | None
    proof: dict[str, Any] | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def coordinate_provider_context_budget(
    payload: dict[str, Any],
    *,
    projection_adapter: str,
    proof_budget: int,
    status_projection_mode: str = "native_or_none",
    fallback_reason: str | None = None,
    envelope_shape: ProviderRequestEnvelopeShape = CHAT_REQUEST_ENVELOPE,
    active_user_message_index: int | None = None,
    protected_tool_result_indexes: Collection[int] | None = None,
) -> ContextBudgetDecision:
    """Reuse the provider proof path as the single budget decision point."""

    try:
        final_payload, proof = prove_or_compact_provider_payload(
            payload,
            projection_adapter=projection_adapter,
            proof_budget=proof_budget,
            status_projection_mode=status_projection_mode,
            fallback_reason=fallback_reason,
            envelope_shape=envelope_shape,
            active_user_message_index=active_user_message_index,
            protected_tool_result_indexes=protected_tool_result_indexes,
        )
    except ProviderRequestBudgetExceededError as exc:
        return ContextBudgetDecision(
            action="budget_limited",
            payload=None,
            proof=exc.proof,
            reason="provider_request_budget_exhausted",
        )
    except (TypeError, ValueError, RecursionError):
        return ContextBudgetDecision(
            action="invalid_request",
            payload=None,
            proof=None,
            reason="provider_request_serialization_failed",
        )
    compacted = bool(proof and proof.get("compact_needed"))
    return ContextBudgetDecision(
        action="send_compacted" if compacted else "send",
        payload=final_payload,
        proof=proof,
        reason="compact_needed" if compacted else "fits",
    )
