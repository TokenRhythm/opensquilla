"""Focused registration tests for strict generated Gateway method Contracts."""

from __future__ import annotations

from typing import Any

import pytest

from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
)
from opensquilla.gateway.adapters.approval_contract import (
    APPROVAL_CONTRACT_METHODS,
    register_approval_contract,
)
from opensquilla.gateway.adapters.memory_profile_import_contract import (
    MEMORY_PROFILE_IMPORT_CONTRACT_METHODS,
    register_memory_profile_import_contract,
)
from opensquilla.gateway.adapters.session_control_contract import (
    SESSION_CONTROL_CONTRACT_METHODS,
    register_session_control_contract,
)
from opensquilla.gateway.adapters.workspace_catalog_contract import (
    WORKSPACE_CATALOG_CONTRACT_METHODS,
    register_workspace_catalog_contract,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcHandlerError, RpcRegistry, get_dispatcher

EXPECTED_APPROVAL_METHODS = (
    "exec.approval.status",
    "exec.approval.snapshot",
    "exec.approval.resolve",
    "exec.approval.extend",
    "plugin.approval.status",
    "plugin.approval.resolve",
    "plugin.approval.extend",
)
EXPECTED_MEMORY_PROFILE_IMPORT_METHODS = (
    "memory.import.info",
    "memory.import.start",
    "memory.import.status",
    "memory.import.retry",
    "memory.import.cancel",
    "memory.import.apply",
    "memory.import.undo",
    "memory.import.discard",
)
EXPECTED_SESSION_CONTROL_METHODS = (
    "sessions.subscribe",
    "sessions.unsubscribe",
    "sessions.routing.get",
    "sessions.routing.set",
)
EXPECTED_WORKSPACE_METHODS = (
    "workspaces.list",
    "sandbox.path.list",
    "sandbox.path.create-directory",
    "sandbox.path.pick",
)


def test_strict_contract_groups_own_the_expected_methods() -> None:
    assert APPROVAL_CONTRACT_METHODS == EXPECTED_APPROVAL_METHODS
    assert MEMORY_PROFILE_IMPORT_CONTRACT_METHODS == EXPECTED_MEMORY_PROFILE_IMPORT_METHODS
    assert SESSION_CONTROL_CONTRACT_METHODS == EXPECTED_SESSION_CONTROL_METHODS
    assert WORKSPACE_CATALOG_CONTRACT_METHODS == EXPECTED_WORKSPACE_METHODS


@pytest.mark.parametrize(
    ("methods", "register"),
    (
        (EXPECTED_APPROVAL_METHODS, register_approval_contract),
        (
            EXPECTED_MEMORY_PROFILE_IMPORT_METHODS,
            register_memory_profile_import_contract,
        ),
        (EXPECTED_SESSION_CONTROL_METHODS, register_session_control_contract),
        (EXPECTED_WORKSPACE_METHODS, register_workspace_catalog_contract),
    ),
)
def test_strict_contract_factories_use_generated_identity_scope_and_provenance(
    methods: tuple[str, ...],
    register: Any,
) -> None:
    registry = RpcRegistry()

    async def implementation(_params: Any, _ctx: Any) -> Any:
        return None

    for method in methods:
        handler = register(
            registry,
            method,
            implementation,
            internal_error=RpcHandlerError,
            guest_allowed_checker=is_guest_rpc_method_allowed,
        )
        entry = registry.get_entry(method)
        assert entry is not None
        assert entry.handler is handler
        assert entry.required_scope == GATEWAY_METHOD_CONTRACTS[method].scope
        assert entry.generated_contract_name == method


@pytest.mark.parametrize(
    "method",
    (
        "models.routing.set",
        *EXPECTED_APPROVAL_METHODS,
        *EXPECTED_MEMORY_PROFILE_IMPORT_METHODS,
        *EXPECTED_SESSION_CONTROL_METHODS,
        *EXPECTED_WORKSPACE_METHODS,
    ),
)
def test_runtime_entries_are_generated_contract_bound(method: str) -> None:
    entry = get_dispatcher().get_entry(method)
    assert entry is not None
    assert entry.generated_contract_name == method
    assert entry.required_scope == GATEWAY_METHOD_CONTRACTS[method].scope
    assert entry.handler.__module__ == "opensquilla.gateway.adapters.contract_method"
    assert entry.handler.__name__ == "handle_contract_method"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "result"),
    (
        (
            "plugin.approval.status",
            {"id": "plugin-approval-1"},
            {
                "found": False,
                "id": "plugin-approval-1",
                "namespace": "plugin",
                "pending": False,
                "resolutionInProgress": False,
                "resolved": False,
            },
        ),
        (
            "plugin.approval.resolve",
            {"id": "plugin-approval-1", "approved": True},
            {
                "id": "plugin-approval-1",
                "mode": "prompt",
                "approved": True,
                "resolved": True,
                "resolution": "approved",
                "deadline": None,
                "consumed": False,
                "pending": False,
            },
        ),
        (
            "plugin.approval.extend",
            {"id": "plugin-approval-1", "seconds": 60},
            {
                "id": "plugin-approval-1",
                "mode": "prompt",
                "approved": False,
                "resolved": False,
                "resolution": "pending",
                "deadline": 60.0,
                "consumed": False,
                "pending": True,
            },
        ),
    ),
)
async def test_legacy_plugin_aliases_call_their_matching_implementation_once(
    method: str,
    params: dict[str, Any],
    result: dict[str, Any],
) -> None:
    registry = RpcRegistry()
    calls: list[tuple[Any, Any]] = []
    context = object()

    async def implementation(actual_params: Any, actual_context: Any) -> Any:
        calls.append((actual_params, actual_context))
        return result

    handler = register_approval_contract(
        registry,
        method,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )

    actual = await handler(params, context)

    assert actual is result
    assert calls == [(params, context)]
