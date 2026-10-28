"""Generated Contract bindings for read-only Git Gateway methods."""

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

GIT_CONTRACT_METHODS: Final = (
    "git.status",
    "git.diff",
)


class GitContractError(ValueError):
    """A Git success payload violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    GIT_CONTRACT_METHODS,
    GitContractError,
)


def register_git_contract[ContextT, ResultT](
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
        unsupported_contract="git",
    )


__all__ = [
    "GIT_CONTRACT_METHODS",
    "GitContractError",
    "register_git_contract",
]
