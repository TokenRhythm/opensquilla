"""RPC registration shell for durable Workbench resource operations."""

from __future__ import annotations

from opensquilla.gateway.adapters.artifact_workbench import (
    GatewayArtifactWorkbenchAdapter,
)
from opensquilla.gateway.adapters.artifact_workbench_contract import (
    register_artifact_workbench_contract,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcHandlerError, get_dispatcher
from opensquilla.gateway.workbench_resource_runtime import (
    _WorkbenchResourceRuntimePort,
    adopt_generated_deliverable_if_editable,
    resolve_recovery_import_source,
)

_d = get_dispatcher()

_WORKBENCH_RESOURCE_METHODS = (
    "workbench.resources.list",
    "workbench.resources.get",
    "artifacts.mutations.resolve",
    "workbench.resources.open",
    "workbench.previews.create",
    "documents.import",
    "documents.publish",
)

(
    _handle_resources_list,
    _handle_resources_get,
    _handle_mutation_resolve,
    _handle_resources_open,
    _handle_preview_create,
    _handle_documents_import,
    _handle_documents_publish,
) = tuple(
    GatewayArtifactWorkbenchAdapter.bind(method, _WorkbenchResourceRuntimePort)
    for method in _WORKBENCH_RESOURCE_METHODS
)

for _artifact_method, _artifact_implementation in zip(
    _WORKBENCH_RESOURCE_METHODS,
    (
        _handle_resources_list,
        _handle_resources_get,
        _handle_mutation_resolve,
        _handle_resources_open,
        _handle_preview_create,
        _handle_documents_import,
        _handle_documents_publish,
    ),
    strict=True,
):
    register_artifact_workbench_contract(
        _d,
        _artifact_method,
        _artifact_implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )


__all__ = [
    "_handle_documents_import",
    "_handle_documents_publish",
    "_handle_preview_create",
    "_handle_resources_get",
    "_handle_resources_list",
    "_handle_resources_open",
    "adopt_generated_deliverable_if_editable",
    "resolve_recovery_import_source",
]
