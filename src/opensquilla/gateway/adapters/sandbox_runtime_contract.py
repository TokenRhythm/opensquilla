"""Generated Contract bindings for the existing SandboxRuntime RPC handlers.

The generated descriptors own method identity, scope, guest policy and wire
models.  This Adapter observes request drift without changing legacy handler
behavior and rejects success payloads that violate their generated Contract.
It deliberately does not import or receive ``RpcContext``.
"""

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

SANDBOX_RUNTIME_CONTRACT_METHODS: Final = (
    "sandbox.setup.status",
    "sandbox.setup.ensure",
    "sandbox.capability.status",
    "sandbox.policy.get",
    "sandbox.policy.defaults",
    "sandbox.policy.update",
    "sandbox.run_mode.preference.get",
    "sandbox.run_mode.preference.set",
    "sandbox.runtime.status",
    "sandbox.runtime.install",
    "sandbox.runtime.cancel",
    "sandbox.runtime.remove",
    "sandbox.runtime.discard_download",
    "sandbox.resume",
)

_IGNORES_ALL_PARAMS: Final = frozenset(
    {
        "sandbox.setup.status",
        "sandbox.setup.ensure",
    }
)
_ALLOWS_NULL_PARAMS: Final = frozenset(
    {
        "sandbox.capability.status",
        "sandbox.policy.get",
        "sandbox.policy.defaults",
        "sandbox.run_mode.preference.get",
        "sandbox.runtime.status",
    }
)


class SandboxRuntimeContractError(ValueError):
    """A SandboxRuntime success payload violated its generated Contract."""


def _validation_errors(exc: ValidationError) -> tuple[dict[str, Any], ...]:
    return tuple(
        cast(
            list[dict[str, Any]],
            exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            ),
        )
    )


def _params_observer(
    method: str,
    descriptor: GatewayMethodContract,
) -> Callable[[Any], tuple[dict[str, Any], ...]]:
    def observe(params: Any) -> tuple[dict[str, Any], ...]:
        if method in _IGNORES_ALL_PARAMS:
            return ()
        if params is None and method in _ALLOWS_NULL_PARAMS:
            return ()
        try:
            descriptor.params_model.model_validate(params)
        except ValidationError as exc:
            return _validation_errors(exc)
        return ()

    return observe


def _result_validator(
    method: str,
    descriptor: GatewayMethodContract,
) -> Callable[[Any], Any]:
    def validate(result: Any) -> Any:
        try:
            descriptor.result_model.model_validate(result)
        except ValidationError as exc:
            raise SandboxRuntimeContractError(
                f"{method} result violated the generated v4 Contract"
            ) from exc
        return result

    return validate


def _binding(method: str) -> GatewayContractBinding[Any]:
    if method not in SANDBOX_RUNTIME_CONTRACT_METHODS:
        raise ValueError(f"unsupported SandboxRuntime Contract method: {method}")
    descriptor = GATEWAY_METHOD_CONTRACTS[method]
    return GatewayContractBinding(
        descriptor=descriptor,
        observe_params=_params_observer(method, descriptor),
        validate_result=_result_validator(method, descriptor),
        result_validation_errors=(SandboxRuntimeContractError,),
        response_error_message=f"{method} response violated its v4 contract",
        request_mismatch_event=f"{method}.request_contract_mismatch",
        response_violation_event=f"{method}.contract_violation",
    )


_BINDINGS: Final = {
    method: _binding(method)
    for method in SANDBOX_RUNTIME_CONTRACT_METHODS
}


def register_sandbox_runtime_contract[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    method: str,
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[ResultT]]:
    """Register one Contract wrapper around one existing Sandbox handler."""

    try:
        binding = _BINDINGS[method]
    except KeyError as exc:
        raise ValueError(f"unsupported SandboxRuntime Contract method: {method}") from exc
    return register_gateway_contract_method(
        registry,
        cast(GatewayContractBinding[ResultT], binding),
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


__all__ = [
    "SANDBOX_RUNTIME_CONTRACT_METHODS",
    "SandboxRuntimeContractError",
    "register_sandbox_runtime_contract",
]
