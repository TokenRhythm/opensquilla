"""Generated Contract registration for ancillary conversation methods."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final, cast

from pydantic import ValidationError

from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
    GatewayMethodContract,
)
from opensquilla.gateway.adapters.contract_method import (
    ErrorFactory,
    GatewayContractBinding,
    GuestAllowedChecker,
    MethodRegistry,
    register_gateway_contract_method,
)

CONVERSATION_ANCILLARY_CONTRACT_METHODS: Final = (
    "usage.status",
    "usage.query",
    "usage.cost",
    "commands.list_for_surface",
    "router.feedback.submit",
    "sessions.promptCacheKeepalive.status",
    "sessions.promptCacheKeepalive.set",
    "chat.clarify_submit",
)


class ConversationAncillaryContractError(ValueError):
    """A successful ancillary response violated its generated Contract."""


def _errors(exc: ValidationError) -> tuple[dict[str, Any], ...]:
    return tuple(
        cast(
            list[dict[str, Any]],
            exc.errors(include_url=False, include_context=False, include_input=False),
        )
    )


def _binding(method: str) -> GatewayContractBinding[Any]:
    descriptor: GatewayMethodContract = GATEWAY_METHOD_CONTRACTS[method]

    def observe(params: Any) -> tuple[dict[str, Any], ...]:
        try:
            descriptor.request_model.model_validate(
                {"type": "req", "id": "contract-observer", "method": method, "params": params}
            )
        except ValidationError as exc:
            return _errors(exc)
        return ()

    def validate(result: Any) -> Any:
        try:
            descriptor.result_model.model_validate(result)
        except ValidationError as exc:
            raise ConversationAncillaryContractError(
                f"{method} result violated the generated v4 Contract"
            ) from exc
        return result

    return GatewayContractBinding(
        descriptor=descriptor,
        observe_params=observe,
        validate_result=validate,
        result_validation_errors=(ConversationAncillaryContractError,),
        response_error_message=f"{method} response violated its v4 contract",
        request_mismatch_event=f"{method}.request_contract_mismatch",
        response_violation_event=f"{method}.contract_violation",
    )


_BINDINGS: Final = {
    method: _binding(method) for method in CONVERSATION_ANCILLARY_CONTRACT_METHODS
}


def register_conversation_ancillary_contract[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    method: str,
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[ResultT]]:
    return register_gateway_contract_method(
        registry,
        cast(GatewayContractBinding[ResultT], _BINDINGS[method]),
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


__all__ = [
    "CONVERSATION_ANCILLARY_CONTRACT_METHODS",
    "ConversationAncillaryContractError",
    "register_conversation_ancillary_contract",
]
