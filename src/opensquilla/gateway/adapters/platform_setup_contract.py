"""Generated Contract bindings for Platform setup Gateway methods."""

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

PLATFORM_SETUP_CONTRACT_METHODS: Final = (
    "onboarding.status",
    "onboarding.catalog",
    "onboarding.provider.configure",
    "onboarding.provider.probe",
    "onboarding.models.discover",
    "onboarding.imageGeneration.models.discover",
    "onboarding.provider.credential.reveal",
    "onboarding.provider.credential.clear",
    "onboarding.llmProfile.upsert",
    "onboarding.llmProfile.activate",
    "onboarding.llmProfile.remove",
    "onboarding.llmProfile.active.remove",
    "onboarding.llmProfile.credential.clear",
    "onboarding.llmProfile.probe",
    "onboarding.llmProfile.draft.probe",
    "onboarding.llmProfile.models.discover",
    "onboarding.llmProfile.draft.models.discover",
    "onboarding.router.configure",
    "onboarding.ensemble.configure",
    "onboarding.search.configure",
    "onboarding.imageGeneration.configure",
    "onboarding.memory_embedding.configure",
    "onboarding.audio.configure",
    "onboarding.capability.reset",
    "onboarding.channel.probe",
    "onboarding.channel.upsert",
    "onboarding.channel.remove",
    "onboarding.channel.enable",
    "onboarding.channel.disable",
)


class PlatformSetupContractError(ValueError):
    """A setup success payload violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    PLATFORM_SETUP_CONTRACT_METHODS,
    PlatformSetupContractError,
)


def register_platform_setup_contract[ContextT, ResultT](
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
        unsupported_contract="Platform setup",
    )


__all__ = [
    "PLATFORM_SETUP_CONTRACT_METHODS",
    "PlatformSetupContractError",
    "register_platform_setup_contract",
]
