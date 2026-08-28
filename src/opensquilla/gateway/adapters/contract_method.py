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
ResultValidator = Callable[[Any], None]
ErrorFactory = Callable[[str, str], Exception]
GuestAllowedChecker = Callable[[str], bool]


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

    @property
    def guest_allowed(self) -> bool: ...

    @property
    def errors(self) -> tuple[dict[str, Any], ...]: ...


class MethodRegistry[ContextT](Protocol):
    """Minimal registry port; no dependency on Gateway dispatcher internals."""

    def register(
        self,
        name: str,
        handler: RegisteredHandler[ContextT, Any],
        scope: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class GatewayContractBinding[ResultT]:
    descriptor: GatewayMethodDescriptor
    observe_params: ParamsObserver
    validate_result: ResultValidator
    result_validation_errors: tuple[type[Exception], ...]
    response_error_message: str
    request_mismatch_event: str
    response_violation_event: str
    response_error_code: str = "INTERNAL_ERROR"

    def __post_init__(self) -> None:
        if not self.result_validation_errors:
            raise ValueError("result_validation_errors must be explicit")
        declared_error_codes: set[str] = set()
        for error in self.descriptor.errors:
            if not isinstance(error, dict):
                raise ValueError("descriptor errors must be objects")
            code = error.get("code")
            if not isinstance(code, str) or not code:
                raise ValueError("descriptor errors must declare a non-empty string code")
            declared_error_codes.add(code)
        if self.response_error_code not in declared_error_codes:
            raise ValueError(
                f"response_error_code {self.response_error_code!r} is not declared "
                f"by {self.descriptor.name!r}"
            )


def _best_effort_log(level: str, event: str, **values: Any) -> None:
    """Emit Contract diagnostics without changing request behavior."""

    try:
        getattr(log, level)(event, **values)
    except Exception:
        # Logging processors and sinks are replaceable infrastructure.  A
        # diagnostics failure must not suppress an Implementation call or
        # replace the stable response-contract error returned to clients.
        pass


def _safe_exception_text(exc: Exception) -> str:
    try:
        return str(exc)
    except Exception:
        return type(exc).__name__


def register_gateway_contract_method[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    binding: GatewayContractBinding[ResultT],
    implementation: Implementation[ContextT, ResultT],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> RegisteredHandler[ContextT, ResultT]:
    """Register one validated handler around one existing Implementation."""

    legacy_guest_allowed = guest_allowed_checker(binding.descriptor.name)
    if type(legacy_guest_allowed) is not bool:
        raise TypeError("guest_allowed_checker must return bool")
    if binding.descriptor.guest_allowed is not legacy_guest_allowed:
        raise ValueError(
            f"generated guest_allowed={binding.descriptor.guest_allowed!r} for "
            f"{binding.descriptor.name!r} disagrees with legacy guest policy "
            f"{legacy_guest_allowed!r}"
        )

    async def handle_contract_method(params: Any, ctx: ContextT) -> ResultT:
        try:
            request_errors = binding.observe_params(params)
        except Exception as exc:  # observation must never change v4 behavior
            _best_effort_log(
                "warning",
                binding.request_mismatch_event,
                method=binding.descriptor.name,
                params_type=type(params).__name__,
                observer_error=type(exc).__name__,
                exc_info=exc,
            )
        else:
            if request_errors:
                _best_effort_log(
                    "warning",
                    binding.request_mismatch_event,
                    method=binding.descriptor.name,
                    params_type=type(params).__name__,
                    errors=request_errors,
                )

        result = await implementation(params, ctx)
        try:
            binding.validate_result(result)
        except binding.result_validation_errors as exc:
            _best_effort_log(
                "error",
                binding.response_violation_event,
                method=binding.descriptor.name,
                error=_safe_exception_text(exc),
            )
            raise internal_error(
                binding.response_error_code,
                binding.response_error_message,
            ) from exc
        return result

    registered = cast(RegisteredHandler[ContextT, ResultT], handle_contract_method)
    registry.register(binding.descriptor.name, registered, binding.descriptor.scope)
    return registered
