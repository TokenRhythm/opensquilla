"""Generated Contract bindings for strict execution approval Gateway methods."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from opensquilla.gateway.adapters._generated_contract_bindings import (
    generated_contract_bindings,
    register_generated_contract_binding,
)
from opensquilla.gateway.adapters.contract_method import (
    ErrorFactory,
    GuestAllowedChecker,
    MethodRegistry,
)

APPROVAL_CONTRACT_METHODS: Final = (
    "exec.approval.status",
    "exec.approval.snapshot",
    "exec.approval.resolve",
    "exec.approval.extend",
    "plugin.approval.status",
    "plugin.approval.resolve",
    "plugin.approval.extend",
)


class ApprovalContractError(ValueError):
    """An execution approval success payload violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    APPROVAL_CONTRACT_METHODS,
    ApprovalContractError,
)


def register_approval_contract[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    method: str,
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[ResultT]]:
    return register_generated_contract_binding(
        registry,
        _BINDINGS,
        method,
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
        unsupported_contract="approval",
    )


__all__ = [
    "APPROVAL_CONTRACT_METHODS",
    "ApprovalContractError",
    "register_approval_contract",
]
