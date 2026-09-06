"""Generated Contract bindings for channel administration methods."""

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

CHANNEL_ADMINISTRATION_CONTRACT_METHODS: Final = (
    "channels.status",
    "channels.get",
    "channels.probe",
    "channels.logout",
    "channels.restart",
    "channels.pairings",
    "channels.pairing.approve",
    "channels.admin.set",
    "channels.pairing.revoke",
)


class ChannelAdministrationContractError(ValueError):
    """A successful channel response violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    CHANNEL_ADMINISTRATION_CONTRACT_METHODS,
    ChannelAdministrationContractError,
)


def register_channel_administration_contract[ContextT, ResultT](
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
    "CHANNEL_ADMINISTRATION_CONTRACT_METHODS",
    "ChannelAdministrationContractError",
    "register_channel_administration_contract",
]
