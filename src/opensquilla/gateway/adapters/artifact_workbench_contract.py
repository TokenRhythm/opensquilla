"""Generated Contract registration for Artifact Workbench methods."""

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


_BINDINGS: Final = generated_contract_bindings(
    ARTIFACT_WORKBENCH_CONTRACT_METHODS,
    ArtifactWorkbenchContractError,
)


def register_artifact_workbench_contract[ContextT, ResultT](
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
    "ARTIFACT_WORKBENCH_CONTRACT_METHODS",
    "ArtifactWorkbenchContractError",
    "register_artifact_workbench_contract",
]
