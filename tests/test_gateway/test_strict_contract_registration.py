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
from opensquilla.gateway.adapters.meta_run_center_contract import (
    META_RUN_CENTER_CONTRACT_METHODS,
    register_meta_run_center_contract,
)
from opensquilla.gateway.adapters.migration_operations_contract import (
    MIGRATION_OPERATIONS_CONTRACT_METHODS,
    register_migration_operations_contract,
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
    "workspaces.open",
    "workspaces.update",
    "workspaces.pin",
    "workspaces.remove",
    "workspaces.history.delete",
    "sandbox.path.list",
    "sandbox.path.create-directory",
    "sandbox.path.pick",
)
EXPECTED_META_RUN_CENTER_METHODS = (
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
EXPECTED_MIGRATION_OPERATIONS_METHODS = (
    "migration.sources.list",
    "migration.sources.preview",
)


def test_strict_contract_groups_own_the_expected_methods() -> None:
    assert APPROVAL_CONTRACT_METHODS == EXPECTED_APPROVAL_METHODS
    assert MEMORY_PROFILE_IMPORT_CONTRACT_METHODS == EXPECTED_MEMORY_PROFILE_IMPORT_METHODS
    assert SESSION_CONTROL_CONTRACT_METHODS == EXPECTED_SESSION_CONTROL_METHODS
    assert WORKSPACE_CATALOG_CONTRACT_METHODS == EXPECTED_WORKSPACE_METHODS
    assert META_RUN_CENTER_CONTRACT_METHODS == EXPECTED_META_RUN_CENTER_METHODS
    assert MIGRATION_OPERATIONS_CONTRACT_METHODS == EXPECTED_MIGRATION_OPERATIONS_METHODS


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
        (EXPECTED_META_RUN_CENTER_METHODS, register_meta_run_center_contract),
        (
            EXPECTED_MIGRATION_OPERATIONS_METHODS,
            register_migration_operations_contract,
        ),
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
        *EXPECTED_META_RUN_CENTER_METHODS,
        *EXPECTED_MIGRATION_OPERATIONS_METHODS,
    ),
)
def test_runtime_entries_are_generated_contract_bound(method: str) -> None:
    entry = get_dispatcher().get_entry(method)
    assert entry is not None
    assert entry.generated_contract_name == method
    assert entry.required_scope == GATEWAY_METHOD_CONTRACTS[method].scope
    assert entry.handler.__module__ == "opensquilla.gateway.adapters.contract_method"
    assert entry.handler.__name__ == "handle_contract_method"


_WORKSPACE = {
    "id": "workspace-1",
    "name": "Synthetic workspace",
    "path": "/synthetic/workspace",
    "taskCount": 0,
    "pinned": False,
    "available": True,
}

_VALID_REGISTRATION_RESULTS: dict[str, dict[str, Any]] = {
    "workspaces.open": {"workspace": _WORKSPACE},
    "workspaces.update": {"workspace": _WORKSPACE},
    "workspaces.pin": {"workspace": _WORKSPACE},
    "workspaces.remove": {
        "removed": True,
        "workspaceId": "workspace-1",
        "pausedCronJobIds": [],
        "pausedCronJobCount": 0,
    },
    "workspaces.history.delete": {
        "workspaceId": "workspace-1",
        "deletedTaskCount": 0,
        "deletedSessionKeys": [],
    },
    "migration.sources.list": {
        "schemaVersion": 1,
        "mode": "preview_only",
        "capabilities": {
            "discover": False,
            "preview": False,
            "apply": False,
            "manualSource": False,
        },
        "candidates": [],
    },
    "migration.sources.preview": {
        "schemaVersion": 1,
        "mode": "preview_only",
        "candidate": {
            "candidateId": "candidate-1",
            "sourceKind": "opensquilla",
            "version": None,
            "estimatedActivityAt": None,
            "sessionCount": None,
            "sizeBytes": None,
            "previouslyImported": False,
        },
        "previewStatus": "available",
        "targetAction": "copy",
        "summary": {
            "sessionCount": None,
            "itemCounts": {"planned": 0, "skipped": 0, "error": 0},
            "pausedJobCount": 0,
            "diskRequiredBytes": 0,
            "diskFreeBytes": 0,
        },
        "blockers": [],
        "notices": [],
        "execution": {"canApply": False, "supportedBy": ["desktop"]},
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("methods", "register"),
    (
        (EXPECTED_WORKSPACE_METHODS[1:6], register_workspace_catalog_contract),
        (EXPECTED_META_RUN_CENTER_METHODS, register_meta_run_center_contract),
        (
            EXPECTED_MIGRATION_OPERATIONS_METHODS,
            register_migration_operations_contract,
        ),
    ),
)
async def test_final_contract_bindings_call_each_implementation_exactly_once(
    methods: tuple[str, ...],
    register: Any,
) -> None:
    for method in methods:
        registry = RpcRegistry()
        calls: list[tuple[Any, Any]] = []
        context = object()
        result = _VALID_REGISTRATION_RESULTS.get(method, {"ok": True})

        async def implementation(actual_params: Any, actual_context: Any) -> Any:
            calls.append((actual_params, actual_context))
            return result

        handler = register(
            registry,
            method,
            implementation,
            internal_error=RpcHandlerError,
            guest_allowed_checker=is_guest_rpc_method_allowed,
        )
        params = {"synthetic": method}

        actual = await handler(params, context)

        assert actual is result
        assert calls == [(params, context)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("methods", "register"),
    (
        (EXPECTED_WORKSPACE_METHODS[1:6], register_workspace_catalog_contract),
        (EXPECTED_META_RUN_CENTER_METHODS, register_meta_run_center_contract),
        (
            EXPECTED_MIGRATION_OPERATIONS_METHODS,
            register_migration_operations_contract,
        ),
    ),
)
async def test_final_contract_bindings_fail_closed_on_invalid_success_payload(
    methods: tuple[str, ...],
    register: Any,
) -> None:
    for method in methods:
        registry = RpcRegistry()

        async def implementation(_params: Any, _context: Any) -> Any:
            return None

        handler = register(
            registry,
            method,
            implementation,
            internal_error=RpcHandlerError,
            guest_allowed_checker=is_guest_rpc_method_allowed,
        )

        with pytest.raises(RpcHandlerError) as raised:
            await handler({}, object())

        assert raised.value.code == "INTERNAL_ERROR"


def test_final_contract_bindings_preserve_non_guest_policy() -> None:
    for method in (
        *EXPECTED_WORKSPACE_METHODS[1:6],
        *EXPECTED_META_RUN_CENTER_METHODS,
        *EXPECTED_MIGRATION_OPERATIONS_METHODS,
    ):
        assert GATEWAY_METHOD_CONTRACTS[method].guest_allowed is False
        assert is_guest_rpc_method_allowed(method) is False


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
