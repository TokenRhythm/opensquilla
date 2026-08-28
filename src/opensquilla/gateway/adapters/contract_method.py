"""Small registration seam for generated Gateway method Contracts.

The generated descriptor owns method identity and scope.  A per-method
binding supplies only the compatibility behavior that cannot be generated:
observe-only request drift and fail-closed response validation.  Business
Implementations still receive the original params/context and are invoked
exactly once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast

import structlog

log = structlog.get_logger(__name__)

ContextT = TypeVar("ContextT")
ResultT = TypeVar("ResultT")

Implementation = Callable[[Any, ContextT], Awaitable[ResultT]]
RegisteredHandler = Callable[[Any, ContextT], Coroutine[Any, Any, ResultT]]
ParamsObserver = Callable[[Any], tuple[dict[str, Any], ...]]
ResultValidator = Callable[[Any], ResultT]
ErrorFactory = Callable[[str, str], Exception]


class GatewayMethodDescriptor(Protocol):
    """Subset of the generated ``GatewayMethodContract`` used at runtime.

    F1's generated descriptor has additional kind/idempotency/timeout/error
    and Pydantic model fields.  Keeping this port narrow avoids reflecting
    over those fields while accepting that generated object structurally.
    """

    @property
    def name(self) -> str: ...

    @property
    def scope(self) -> str: ...


class MethodRegistry[ContextT](Protocol):
    """Minimal registry port; no dependency on Gateway dispatcher internals."""

    def register(
        self,
        name: str,
        handler: RegisteredHandler[ContextT, Any],
        scope: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class StaticGatewayMethodDescriptor:
    """Temporary bridge for methods whose generated F1 descriptor is absent."""

    name: str
    scope: str


@dataclass(frozen=True, slots=True)
class GatewayContractBinding[ResultT]:
    descriptor: GatewayMethodDescriptor
    observe_params: ParamsObserver
    validate_result: ResultValidator[ResultT]
    result_validation_errors: tuple[type[Exception], ...]
    response_error_message: str
    request_mismatch_event: str
    response_violation_event: str
    response_error_code: str = "INTERNAL_ERROR"

    def __post_init__(self) -> None:
        if not self.result_validation_errors:
            raise ValueError("result_validation_errors must be explicit")


def register_gateway_contract_method[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    binding: GatewayContractBinding[ResultT],
    implementation: Implementation[ContextT, ResultT],
    *,
    internal_error: ErrorFactory,
) -> RegisteredHandler[ContextT, ResultT]:
    """Register one validated handler around one existing Implementation."""

    async def handle_contract_method(params: Any, ctx: ContextT) -> ResultT:
        try:
            request_errors = binding.observe_params(params)
        except Exception as exc:  # observation must never change v4 behavior
            log.warning(
                binding.request_mismatch_event,
                method=binding.descriptor.name,
                params_type=type(params).__name__,
                observer_error=type(exc).__name__,
                exc_info=exc,
            )
        else:
            if request_errors:
                log.warning(
                    binding.request_mismatch_event,
                    method=binding.descriptor.name,
                    params_type=type(params).__name__,
                    errors=request_errors,
                )

        result = await implementation(params, ctx)
        try:
            return binding.validate_result(result)
        except binding.result_validation_errors as exc:
            log.error(
                binding.response_violation_event,
                method=binding.descriptor.name,
                error=str(exc),
            )
            raise internal_error(
                binding.response_error_code,
                binding.response_error_message,
            ) from exc

    registered = cast(RegisteredHandler[ContextT, ResultT], handle_contract_method)
    registry.register(binding.descriptor.name, registered, binding.descriptor.scope)
    return registered
