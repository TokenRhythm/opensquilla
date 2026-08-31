"""Gateway registration adapters for the Plan command Contract seam.

The existing plan handlers remain the single business implementation.  This
module owns only generated identity/metadata plus observe-only request and
fail-closed response validation, so v4 clients keep their current JSON tree.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import ValidationError

from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
)
from opensquilla.contracts.generated.v4.plans_cancel_run import (
    Params as PlansCancelRunParams,
)
from opensquilla.contracts.generated.v4.plans_cancel_run import (
    Result as PlansCancelRunResult,
)
from opensquilla.contracts.generated.v4.plans_cancel_run_metadata import (
    PLANS_CANCEL_RUN_METHOD,
)
from opensquilla.contracts.generated.v4.plans_implement import (
    Params as PlansImplementParams,
)
from opensquilla.contracts.generated.v4.plans_implement import (
    Result as PlansImplementResult,
)
from opensquilla.contracts.generated.v4.plans_implement_metadata import (
    PLANS_IMPLEMENT_METHOD,
)
from opensquilla.contracts.generated.v4.plans_revise import (
    Params as PlansReviseParams,
)
from opensquilla.contracts.generated.v4.plans_revise import (
    Result as PlansReviseResult,
)
from opensquilla.contracts.generated.v4.plans_revise_metadata import (
    PLANS_REVISE_METHOD,
)
from opensquilla.contracts.generated.v4.plans_set_mode import (
    Params as PlansSetModeParams,
)
from opensquilla.contracts.generated.v4.plans_set_mode import (
    Result as PlansSetModeResult,
)
from opensquilla.contracts.generated.v4.plans_set_mode_metadata import (
    PLANS_SET_MODE_METHOD,
)
from opensquilla.gateway.adapters.contract_method import (
    GatewayContractBinding,
    GuestAllowedChecker,
    MethodRegistry,
    register_gateway_contract_method,
)

ErrorFactory = Callable[[str, str], Exception]


class PlansContractError(ValueError):
    """Payload crossed the Plans adapter without satisfying its Contract."""


def _errors(exc: Exception) -> tuple[dict[str, Any], ...]:
    if not isinstance(exc, ValidationError):
        return ({"type": "value_error", "loc": (), "msg": str(exc)},)
    return tuple(
        {
            "type": error.get("type", "value_error"),
            "loc": error.get("loc", ()),
            "msg": error.get("msg", "invalid value"),
        }
        for error in exc.errors(include_url=False, include_context=False)
    )


def _observe(params: Any, model: type[Any]) -> tuple[dict[str, Any], ...]:
    # Observation is deliberately best-effort.  Existing handlers own legacy
    # aliases and exact error messages; the Contract must never change them.
    if not isinstance(params, Mapping):
        return ()
    try:
        model.model_validate(dict(params))
    except (ValidationError, ValueError) as exc:
        return _errors(exc)
    return ()


def _validate_result(payload: Any, model: type[Any], method: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise PlansContractError(f"{method} result must be an object")
    try:
        model.model_validate(dict(payload))
    except ValidationError as exc:
        raise PlansContractError(f"{method} result violated Contract: {_errors(exc)}") from exc
    return dict(payload)


_SET_MODE_BINDING: GatewayContractBinding[dict[str, Any]] = GatewayContractBinding(
    descriptor=GATEWAY_METHOD_CONTRACTS[PLANS_SET_MODE_METHOD],
    observe_params=lambda params: _observe(params, PlansSetModeParams),
    validate_result=lambda payload: _validate_result(
        payload, PlansSetModeResult, PLANS_SET_MODE_METHOD
    ),
    result_validation_errors=(PlansContractError,),
    response_error_message="plans.setMode response violated its v4 contract",
    request_mismatch_event="plans.setMode.request_contract_mismatch",
    response_violation_event="plans.setMode.contract_violation",
)
_REVISE_BINDING: GatewayContractBinding[dict[str, Any]] = GatewayContractBinding(
    descriptor=GATEWAY_METHOD_CONTRACTS[PLANS_REVISE_METHOD],
    observe_params=lambda params: _observe(params, PlansReviseParams),
    validate_result=lambda payload: _validate_result(
        payload, PlansReviseResult, PLANS_REVISE_METHOD
    ),
    result_validation_errors=(PlansContractError,),
    response_error_message="plans.revise response violated its v4 contract",
    request_mismatch_event="plans.revise.request_contract_mismatch",
    response_violation_event="plans.revise.contract_violation",
)
_IMPLEMENT_BINDING: GatewayContractBinding[dict[str, Any]] = GatewayContractBinding(
    descriptor=GATEWAY_METHOD_CONTRACTS[PLANS_IMPLEMENT_METHOD],
    observe_params=lambda params: _observe(params, PlansImplementParams),
    validate_result=lambda payload: _validate_result(
        payload, PlansImplementResult, PLANS_IMPLEMENT_METHOD
    ),
    result_validation_errors=(PlansContractError,),
    response_error_message="plans.implement response violated its v4 contract",
    request_mismatch_event="plans.implement.request_contract_mismatch",
    response_violation_event="plans.implement.contract_violation",
)
_CANCEL_RUN_BINDING: GatewayContractBinding[dict[str, Any]] = GatewayContractBinding(
    descriptor=GATEWAY_METHOD_CONTRACTS[PLANS_CANCEL_RUN_METHOD],
    observe_params=lambda params: _observe(params, PlansCancelRunParams),
    validate_result=lambda payload: _validate_result(
        payload, PlansCancelRunResult, PLANS_CANCEL_RUN_METHOD
    ),
    result_validation_errors=(PlansContractError,),
    response_error_message="plans.cancelRun response violated its v4 contract",
    request_mismatch_event="plans.cancelRun.request_contract_mismatch",
    response_violation_event="plans.cancelRun.contract_violation",
)


def _register[ContextT](
    registry: MethodRegistry[ContextT],
    binding: GatewayContractBinding[dict[str, Any]],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    return register_gateway_contract_method(
        registry,
        binding,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_plans_set_mode_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    return _register(
        registry,
        _SET_MODE_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_plans_revise_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    return _register(
        registry,
        _REVISE_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_plans_implement_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    return _register(
        registry,
        _IMPLEMENT_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_plans_cancel_run_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    return _register(
        registry,
        _CANCEL_RUN_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


__all__ = [
    "register_plans_cancel_run_contract",
    "register_plans_implement_contract",
    "register_plans_revise_contract",
    "register_plans_set_mode_contract",
]
