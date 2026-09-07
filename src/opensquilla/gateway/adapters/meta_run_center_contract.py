"""Generated Contract bindings for Meta run recovery and setup methods."""

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

META_RUN_CENTER_CONTRACT_METHODS: Final = (
    "meta.drafts.list",
    "meta.drafts.discard",
    "meta.run",
    "meta.runs.confirm_preflight",
    "meta.runs.recovery",
    "meta.runs.replay",
    "meta.setup.plan",
    "meta.setup.install",
    "meta.setup.status",
)


class MetaRunCenterContractError(ValueError):
    """A Meta run success payload violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    META_RUN_CENTER_CONTRACT_METHODS,
    MetaRunCenterContractError,
)


def register_meta_run_center_contract[ContextT, ResultT](
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
        unsupported_contract="Meta run",
    )


__all__ = [
    "META_RUN_CENTER_CONTRACT_METHODS",
    "MetaRunCenterContractError",
    "register_meta_run_center_contract",
]
