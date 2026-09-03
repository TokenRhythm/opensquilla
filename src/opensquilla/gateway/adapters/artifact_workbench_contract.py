"""Generated Contract registration for Artifact Workbench methods."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final, cast

from pydantic import ValidationError

from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
    GatewayMethodContract,
)
from opensquilla.gateway.adapters.contract_method import (
    ErrorFactory,
    GatewayContractBinding,
    GuestAllowedChecker,
    MethodRegistry,
    register_gateway_contract_method,
)

ARTIFACT_WORKBENCH_CONTRACT_METHODS: Final = (
    "artifacts.list",
    "artifacts.get",
    "artifacts.edit.capabilities",
    "artifacts.documents.open",
    "artifacts.documents.list",
    "artifacts.documents.get",
    "artifacts.documents.rename",
    "artifacts.documents.close",
    "documents.editSessions.start",
    "documents.editSessions.heartbeat",
    "documents.editSessions.close",
    "artifacts.revisions.list",
    "artifacts.revisions.restore",
    "artifacts.changes.list",
    "artifacts.changes.get",
    "artifacts.changes.revert",
    "artifacts.prompt_annotations.list",
    "artifacts.prompt_annotations.create",
    "artifacts.prompt_annotations.focus",
    "artifacts.prompt_annotations.update",
    "artifacts.prompt_annotations.discard",
    "artifacts.source.read",
    "artifacts.source.patch",
    "workbench.resources.list",
    "workbench.resources.get",
    "artifacts.mutations.resolve",
    "workbench.resources.open",
    "workbench.previews.create",
    "documents.import",
    "documents.publish",
)


class ArtifactWorkbenchContractError(ValueError):
    """A successful Workbench response violated its generated Contract."""


def _errors(exc: ValidationError) -> tuple[dict[str, Any], ...]:
    return tuple(
        cast(
            list[dict[str, Any]],
            exc.errors(include_url=False, include_context=False, include_input=False),
        )
    )


def _binding(method: str) -> GatewayContractBinding[Any]:
    descriptor: GatewayMethodContract = GATEWAY_METHOD_CONTRACTS[method]

    def observe(params: Any) -> tuple[dict[str, Any], ...]:
        try:
            descriptor.request_model.model_validate(
                {
                    "type": "req",
                    "id": "contract-observer",
                    "method": method,
                    "params": params,
                }
            )
        except ValidationError as exc:
            return _errors(exc)
        return ()

    def validate(result: Any) -> Any:
        try:
            descriptor.result_model.model_validate(result)
        except ValidationError as exc:
            raise ArtifactWorkbenchContractError(
                f"{method} result violated the generated v4 Contract"
            ) from exc
        return result

    return GatewayContractBinding(
        descriptor=descriptor,
        observe_params=observe,
        validate_result=validate,
        result_validation_errors=(ArtifactWorkbenchContractError,),
        response_error_message=f"{method} response violated its v4 contract",
        request_mismatch_event=f"{method}.request_contract_mismatch",
        response_violation_event=f"{method}.contract_violation",
    )


_BINDINGS: Final = {method: _binding(method) for method in ARTIFACT_WORKBENCH_CONTRACT_METHODS}


def register_artifact_workbench_contract[ContextT, ResultT](
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


__all__ = [
    "ARTIFACT_WORKBENCH_CONTRACT_METHODS",
    "ArtifactWorkbenchContractError",
    "register_artifact_workbench_contract",
]
