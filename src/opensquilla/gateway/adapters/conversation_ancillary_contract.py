"""Generated Contract registration for ancillary conversation methods."""

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

CONVERSATION_ANCILLARY_CONTRACT_METHODS: Final = (
    "usage.status",
    "usage.query",
    "usage.cost",
    "commands.list_for_surface",
    "router.feedback.submit",
    "sessions.promptCacheKeepalive.status",
    "sessions.promptCacheKeepalive.set",
    "chat.clarify_submit",
)


class ConversationAncillaryContractError(ValueError):
    """A successful ancillary response violated its generated Contract."""


_BINDINGS: Final = generated_contract_bindings(
    CONVERSATION_ANCILLARY_CONTRACT_METHODS,
    ConversationAncillaryContractError,
)


def register_conversation_ancillary_contract[ContextT, ResultT](
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
    "CONVERSATION_ANCILLARY_CONTRACT_METHODS",
    "ConversationAncillaryContractError",
    "register_conversation_ancillary_contract",
]
