"""Generated Contract registration for scheduled-job methods."""

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

CRON_SCHEDULER_CONTRACT_METHODS: Final = (
    "cron.list", "cron.status", "cron.add", "cron.create", "cron.update",
    "cron.remove", "cron.run", "cron.runs", "cron.subscribe", "cron.unsubscribe",
)


class CronSchedulerContractError(ValueError):
    """A successful scheduled-job response violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    CRON_SCHEDULER_CONTRACT_METHODS,
    CronSchedulerContractError,
)


def register_cron_scheduler_contract[ContextT, ResultT](
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
    )


__all__ = [
    "CRON_SCHEDULER_CONTRACT_METHODS",
    "CronSchedulerContractError",
    "register_cron_scheduler_contract",
]
