"""Gateway registration Adapter for generated Session read Contracts.

The generated descriptors own method identity, scope, guest policy and the
wire models.  This Adapter adds only compatibility observation and fail-closed
result validation around the existing Gateway Implementations.  Application
Modules never import generated models.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from pydantic import ValidationError

from opensquilla.contracts.generated.v4.chat_history import (
    ChatHistoryLegacyNonObjectParams,
    ChatHistoryParams,
    ChatHistoryResult,
)
from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
)
from opensquilla.contracts.generated.v4.sessions_messages_hydrate import (
    SessionsMessagesHydrateLegacyNonObjectParams,
    SessionsMessagesHydrateParams,
    SessionsMessagesHydrateResult,
)
from opensquilla.contracts.generated.v4.sessions_messages_snapshot import (
    SessionsMessagesSnapshotLegacyNonObjectParams,
    SessionsMessagesSnapshotParams,
    SessionsMessagesSnapshotResult,
)
from opensquilla.contracts.generated.v4.sessions_messages_subscribe import (
    SessionsMessagesSubscribeLegacyNonObjectParams,
    SessionsMessagesSubscribeParams,
    SessionsMessagesSubscribeResult,
)
from opensquilla.contracts.generated.v4.sessions_messages_unsubscribe import (
    SessionsMessagesUnsubscribeLegacyNonObjectParams,
    SessionsMessagesUnsubscribeParams,
    SessionsMessagesUnsubscribeResult,
)
from opensquilla.contracts.generated.v4.sessions_preview import (
    SessionsPreviewLegacyNonObjectParams,
    SessionsPreviewParams,
    SessionsPreviewResult,
)
from opensquilla.gateway.adapters.contract_method import (
    ErrorFactory,
    GatewayContractBinding,
    GuestAllowedChecker,
    MethodRegistry,
    register_gateway_contract_method,
)

Implementation = Callable[[Any, Any], Awaitable[Any]]


class SessionReadContractError(ValueError):
    """Raised when a Session read result violates its generated Contract."""


def _params_observer(
    params_model: type[Any],
    legacy_model: type[Any],
) -> Callable[[Any], tuple[dict[str, Any], ...]]:
    def observe(params: Any) -> tuple[dict[str, Any], ...]:
        try:
            if isinstance(params, Mapping):
                params_model.model_validate(dict(params))
            elif params is not None:
                legacy_model.model_validate(params)
        except ValidationError as exc:
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
        return ()

    return observe


def _result_validator(
    result_model: type[Any],
    method: str,
) -> Callable[[Any], Any]:
    def validate(value: Any) -> Any:
        try:
            result_model.model_validate(value)
        except ValidationError as exc:
            raise SessionReadContractError(
                f"{method} result violated the generated v4 Contract"
            ) from exc
        return value

    return validate


def _binding(
    method: str,
    params_model: type[Any],
    legacy_model: type[Any],
    result_model: type[Any],
) -> GatewayContractBinding[Any]:
    return GatewayContractBinding(
        descriptor=GATEWAY_METHOD_CONTRACTS[method],
        observe_params=_params_observer(params_model, legacy_model),
        validate_result=_result_validator(result_model, method),
        result_validation_errors=(SessionReadContractError,),
        response_error_message=f"{method} response violated its v4 contract",
        request_mismatch_event=f"{method}.request_contract_mismatch",
        response_violation_event=f"{method}.contract_violation",
    )


_BINDINGS = {
    "chat.history": _binding(
        "chat.history",
        ChatHistoryParams,
        ChatHistoryLegacyNonObjectParams,
        ChatHistoryResult,
    ),
    "sessions.messages.subscribe": _binding(
        "sessions.messages.subscribe",
        SessionsMessagesSubscribeParams,
        SessionsMessagesSubscribeLegacyNonObjectParams,
        SessionsMessagesSubscribeResult,
    ),
    "sessions.messages.hydrate": _binding(
        "sessions.messages.hydrate",
        SessionsMessagesHydrateParams,
        SessionsMessagesHydrateLegacyNonObjectParams,
        SessionsMessagesHydrateResult,
    ),
    "sessions.messages.snapshot": _binding(
        "sessions.messages.snapshot",
        SessionsMessagesSnapshotParams,
        SessionsMessagesSnapshotLegacyNonObjectParams,
        SessionsMessagesSnapshotResult,
    ),
    "sessions.messages.unsubscribe": _binding(
        "sessions.messages.unsubscribe",
        SessionsMessagesUnsubscribeParams,
        SessionsMessagesUnsubscribeLegacyNonObjectParams,
        SessionsMessagesUnsubscribeResult,
    ),
    "sessions.preview": _binding(
        "sessions.preview",
        SessionsPreviewParams,
        SessionsPreviewLegacyNonObjectParams,
        SessionsPreviewResult,
    ),
}


def _register[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    method: str,
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[ResultT]]:
    return register_gateway_contract_method(
        registry,
        cast(GatewayContractBinding[ResultT], _BINDINGS[method]),
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_chat_history_contract[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[ResultT]]:
    return _register(
        registry,
        "chat.history",
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_sessions_messages_subscribe_contract[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[ResultT]]:
    return _register(
        registry,
        "sessions.messages.subscribe",
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_sessions_messages_hydrate_contract[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[ResultT]]:
    return _register(
        registry,
        "sessions.messages.hydrate",
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_sessions_messages_snapshot_contract[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[ResultT]]:
    return _register(
        registry,
        "sessions.messages.snapshot",
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_sessions_messages_unsubscribe_contract[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[ResultT]]:
    return _register(
        registry,
        "sessions.messages.unsubscribe",
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


def register_sessions_preview_contract[ContextT, ResultT](
    registry: MethodRegistry[ContextT],
    implementation: Callable[[Any, ContextT], Awaitable[ResultT]],
    *,
    internal_error: ErrorFactory,
    guest_allowed_checker: GuestAllowedChecker,
) -> Callable[[Any, ContextT], Awaitable[ResultT]]:
    return _register(
        registry,
        "sessions.preview",
        implementation,
        internal_error=internal_error,
        guest_allowed_checker=guest_allowed_checker,
    )


__all__ = [
    "SessionReadContractError",
    "register_chat_history_contract",
    "register_sessions_messages_hydrate_contract",
    "register_sessions_messages_snapshot_contract",
    "register_sessions_messages_subscribe_contract",
    "register_sessions_messages_unsubscribe_contract",
    "register_sessions_preview_contract",
]
