"""Gateway registration adapters for the typed Goal query/command seam."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import ValidationError

from opensquilla.contracts.adapters.goals_contract import (
    GOALS_CAPABILITIES_METHOD,
    GOALS_REATTACH_METHOD,
    GOALS_SET_METHOD,
    GOALS_STATUS_METHOD,
    GoalsContractError,
    goals_capabilities_params_contract_errors,
    goals_reattach_params_contract_errors,
    goals_set_params_contract_errors,
    goals_status_params_contract_errors,
    validate_goals_capabilities_result,
    validate_goals_reattach_result,
    validate_goals_set_result,
    validate_goals_status_result,
)
from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
)
from opensquilla.contracts.generated.v4.goals_clear import (
    Params as GoalsClearParams,
)
from opensquilla.contracts.generated.v4.goals_clear import (
    Result as GoalsClearResult,
)
from opensquilla.contracts.generated.v4.goals_clear_metadata import GOALS_CLEAR_METHOD
from opensquilla.contracts.generated.v4.goals_edit import (
    Params as GoalsEditParams,
)
from opensquilla.contracts.generated.v4.goals_edit import (
    Result as GoalsEditResult,
)
from opensquilla.contracts.generated.v4.goals_edit_metadata import GOALS_EDIT_METHOD
from opensquilla.contracts.generated.v4.goals_pause import (
    Params as GoalsPauseParams,
)
from opensquilla.contracts.generated.v4.goals_pause import (
    Result as GoalsPauseResult,
)
from opensquilla.contracts.generated.v4.goals_pause_metadata import GOALS_PAUSE_METHOD
from opensquilla.contracts.generated.v4.goals_resume import (
    Params as GoalsResumeParams,
)
from opensquilla.contracts.generated.v4.goals_resume import (
    Result as GoalsResumeResult,
)
from opensquilla.contracts.generated.v4.goals_resume_metadata import GOALS_RESUME_METHOD
from opensquilla.gateway.adapters.contract_method import (
    GatewayContractBinding,
    GuestAllowedChecker,
    MethodRegistry,
    register_gateway_contract_method,
)

ErrorFactory = Callable[[str, str], Exception]

_GOALS_STATUS_BINDING: GatewayContractBinding[dict[str, Any]] = GatewayContractBinding(
    descriptor=GATEWAY_METHOD_CONTRACTS[GOALS_STATUS_METHOD],
    observe_params=goals_status_params_contract_errors,
    validate_result=validate_goals_status_result,
    result_validation_errors=(GoalsContractError,),
    response_error_message="goals.status response violated its v4 contract",
    request_mismatch_event="goals.status.request_contract_mismatch",
    response_violation_event="goals.status.contract_violation",
)

_GOALS_SET_BINDING: GatewayContractBinding[dict[str, Any]] = GatewayContractBinding(
    descriptor=GATEWAY_METHOD_CONTRACTS[GOALS_SET_METHOD],
    observe_params=goals_set_params_contract_errors,
    validate_result=validate_goals_set_result,
    result_validation_errors=(GoalsContractError,),
    response_error_message="goals.set response violated its v4 contract",
    request_mismatch_event="goals.set.request_contract_mismatch",
    response_violation_event="goals.set.contract_violation",
)

_GOALS_CAPABILITIES_BINDING: GatewayContractBinding[dict[str, Any]] = GatewayContractBinding(
    descriptor=GATEWAY_METHOD_CONTRACTS[GOALS_CAPABILITIES_METHOD],
    observe_params=goals_capabilities_params_contract_errors,
    validate_result=validate_goals_capabilities_result,
    result_validation_errors=(GoalsContractError,),
    response_error_message="goals.capabilities response violated its v4 contract",
    request_mismatch_event="goals.capabilities.request_contract_mismatch",
    response_violation_event="goals.capabilities.contract_violation",
)

_GOALS_REATTACH_BINDING: GatewayContractBinding[dict[str, Any]] = GatewayContractBinding(
    descriptor=GATEWAY_METHOD_CONTRACTS[GOALS_REATTACH_METHOD],
    observe_params=goals_reattach_params_contract_errors,
    validate_result=validate_goals_reattach_result,
    result_validation_errors=(GoalsContractError,),
    response_error_message="goals.reattach response violated its v4 contract",
    request_mismatch_event="goals.reattach.request_contract_mismatch",
    response_violation_event="goals.reattach.contract_violation",
)


class GoalMutationContractError(ValueError):
    """A Goal mutation response did not satisfy its generated Contract."""


def _mutation_params_errors(params: Any, model: type[Any]) -> tuple[dict[str, Any], ...]:
    if not isinstance(params, Mapping):
        return ()
    try:
        model.model_validate(_canonicalize_mutation_aliases(params))
    except ValidationError as exc:
        return tuple(
            {
                "type": error.get("type", "value_error"),
                "loc": error.get("loc", ()),
                "msg": error.get("msg", "invalid value"),
            }
            for error in exc.errors(include_url=False, include_context=False)
        )
    return ()


def _canonicalize_mutation_aliases(params: Mapping[str, Any]) -> dict[str, Any]:
    values = dict(params)
    for canonical, aliases in {
        "sessionKey": ("session_key", "key"),
        "expectedGoalId": ("expected_goal_id",),
        "expectedStateRevision": ("expected_state_revision",),
        "clientRequestId": ("client_request_id",),
        "sourceKind": ("source_kind",),
    }.items():
        if canonical not in values:
            for alias in aliases:
                if alias in values:
                    values[canonical] = values[alias]
                    break
    return values


def _validate_mutation_result(
    payload: Any,
    model: type[Any],
    method: str,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise GoalMutationContractError(f"{method} result must be an object")
    try:
        model.model_validate(dict(payload))
    except ValidationError as exc:
        raise GoalMutationContractError(f"{method} result violated Contract") from exc
    return dict(payload)


def _mutation_binding(
    method: str,
    params_model: type[Any],
    result_model: type[Any],
) -> GatewayContractBinding[dict[str, Any]]:
    return GatewayContractBinding(
        descriptor=GATEWAY_METHOD_CONTRACTS[method],
        observe_params=lambda params: _mutation_params_errors(params, params_model),
        validate_result=lambda payload: _validate_mutation_result(payload, result_model, method),
        result_validation_errors=(GoalMutationContractError,),
        response_error_message=f"{method} response violated its v4 contract",
        request_mismatch_event=f"{method}.request_contract_mismatch",
        response_violation_event=f"{method}.contract_violation",
    )


_GOALS_EDIT_BINDING = _mutation_binding(GOALS_EDIT_METHOD, GoalsEditParams, GoalsEditResult)
_GOALS_PAUSE_BINDING = _mutation_binding(GOALS_PAUSE_METHOD, GoalsPauseParams, GoalsPauseResult)
_GOALS_RESUME_BINDING = _mutation_binding(GOALS_RESUME_METHOD, GoalsResumeParams, GoalsResumeResult)
_GOALS_CLEAR_BINDING = _mutation_binding(GOALS_CLEAR_METHOD, GoalsClearParams, GoalsClearResult)


def register_goals_status_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    """Register one validated wrapper around the existing status handler."""

    return register_gateway_contract_method(
        registry,
        _GOALS_STATUS_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_goals_set_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    """Register one validated wrapper around the existing set handler."""

    return register_gateway_contract_method(
        registry,
        _GOALS_SET_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_goals_capabilities_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    """Register the read-only capability query around its existing handler."""

    return register_gateway_contract_method(
        registry,
        _GOALS_CAPABILITIES_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_goals_reattach_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    """Register the lease handoff boundary around the existing handler."""

    return register_gateway_contract_method(
        registry,
        _GOALS_REATTACH_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def _register_goal_mutation[ContextT](
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


def register_goals_edit_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    return _register_goal_mutation(
        registry,
        _GOALS_EDIT_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_goals_pause_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    return _register_goal_mutation(
        registry,
        _GOALS_PAUSE_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_goals_resume_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    return _register_goal_mutation(
        registry,
        _GOALS_RESUME_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_goals_clear_contract[ContextT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[dict[str, Any]]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[dict[str, Any]]]:
    return _register_goal_mutation(
        registry,
        _GOALS_CLEAR_BINDING,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


__all__ = [
    "register_goals_capabilities_contract",
    "register_goals_clear_contract",
    "register_goals_edit_contract",
    "register_goals_pause_contract",
    "register_goals_reattach_contract",
    "register_goals_resume_contract",
    "register_goals_set_contract",
    "register_goals_status_contract",
]
