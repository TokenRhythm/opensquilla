"""Registration Adapter tests for generated SandboxRuntime Contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

import opensquilla.gateway.rpc_sandbox as rpc_sandbox
from opensquilla.application.sandbox_runtime import SandboxCapability
from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
)
from opensquilla.gateway.adapters.sandbox_runtime_contract import (
    SANDBOX_RUNTIME_CONTRACT_METHODS,
    register_sandbox_runtime_contract,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc.registry import RpcHandlerError, get_dispatcher


class RecordingRegistry:
    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[Any, object], Awaitable[Any]]] = {}
        self.scopes: dict[str, str] = {}

    def register(
        self,
        name: str,
        handler: Callable[[Any, object], Awaitable[Any]],
        scope: str,
    ) -> None:
        self.handlers[name] = handler
        self.scopes[name] = scope


POLICY = {
    "schemaVersion": 2,
    "policyVersion": 0,
    "files": {
        "customDenyWritePaths": [],
        "recursiveDeleteBackupEnabled": True,
        "backupQuotaBytes": 1024,
    },
    "commands": {
        "requireApprovalPrefixes": [],
        "autoAllowPrefixes": [],
        "systemTools": "auto",
    },
    "network": {"blockAllNetwork": False, "allowDomains": [], "denyDomains": []},
    "runtimes": {"enabled": True, "python": True, "node": True, "gitBash": True},
}

RUNTIME_STATUS = {
    "schemaVersion": 1,
    "managementSupported": True,
    "target": "linux-x64",
    "catalogVersion": "2026-08-21.2",
    "sourceOrder": ["oss", "github"],
    "components": [],
    "nextPollAfterMs": 5_000,
}

OPERATION = {
    "operationId": "operation-1",
    "componentId": "python",
    "kind": "install",
    "state": "queued",
    "downloadedBytes": 0,
    "totalBytes": 100,
    "progressPercent": 0,
    "source": None,
    "startedAtMs": 1,
    "updatedAtMs": 1,
    "error": None,
}

VALID_RESULTS: dict[str, Any] = {
    "sandbox.setup.status": {
        "state": "ready",
        "platform": "linux",
        "message": "Sandbox initialized.",
        "requiresAdmin": False,
    },
    "sandbox.setup.ensure": {
        "state": "ready",
        "platform": "linux",
        "message": "Sandbox initialized.",
        "requiresAdmin": False,
    },
    "sandbox.capability.status": {
        "available": True,
        "backend": "bubblewrap",
        "platform": "linux",
        "code": "ready",
        "reason": "ready",
        "setupSupported": True,
        "restartRequired": False,
        "probeVersion": 1,
        "capabilities": ["process"],
    },
    "sandbox.policy.get": POLICY,
    "sandbox.policy.defaults": {
        "builtinDenyWritePaths": ["/etc"],
        "runtimeTarget": "linux-x64",
        "runtimeVersions": {},
    },
    "sandbox.policy.update": POLICY,
    "sandbox.run_mode.preference.get": {"runMode": "full", "source": "default"},
    "sandbox.run_mode.preference.set": {"runMode": "safe", "source": "preference"},
    "sandbox.runtime.status": RUNTIME_STATUS,
    "sandbox.runtime.install": {"operation": OPERATION},
    "sandbox.runtime.cancel": {"operation": OPERATION},
    "sandbox.runtime.remove": {"operation": {**OPERATION, "kind": "remove"}},
    "sandbox.runtime.discard_download": {"status": RUNTIME_STATUS},
    "sandbox.resume": {
        "sessionKey": "agent:main:webchat:contract",
        "resumed": True,
        "autonomousPaused": False,
    },
}

VALID_PARAMS: dict[str, Any] = {
    "sandbox.setup.status": ["legacy", "ignored"],
    "sandbox.setup.ensure": "legacy ignored params",
    "sandbox.capability.status": {"refresh": True},
    "sandbox.policy.get": None,
    "sandbox.policy.defaults": {},
    "sandbox.policy.update": {"basePolicyVersion": 0, "policy": POLICY},
    "sandbox.run_mode.preference.get": None,
    "sandbox.run_mode.preference.set": {"runMode": "trusted"},
    "sandbox.runtime.status": {},
    "sandbox.runtime.install": {"componentId": "python"},
    "sandbox.runtime.cancel": {"componentId": "python", "operationId": "operation-1"},
    "sandbox.runtime.remove": {"componentId": "python"},
    "sandbox.runtime.discard_download": {"componentId": "python"},
    "sandbox.resume": {"sessionKey": "agent:main:webchat:contract"},
}

PRODUCTION_HANDLER_NAMES = {
    "sandbox.setup.status": "_handle_sandbox_setup_status",
    "sandbox.setup.ensure": "_handle_sandbox_setup_ensure",
    "sandbox.capability.status": "_handle_sandbox_capability_status",
    "sandbox.policy.get": "_handle_sandbox_policy_get",
    "sandbox.policy.defaults": "_handle_sandbox_policy_defaults",
    "sandbox.policy.update": "_handle_sandbox_policy_update",
    "sandbox.run_mode.preference.get": "_handle_run_mode_preference_get",
    "sandbox.run_mode.preference.set": "_handle_run_mode_preference_set",
    "sandbox.runtime.status": "_handle_sandbox_runtime_status",
    "sandbox.runtime.install": "_handle_sandbox_runtime_install",
    "sandbox.runtime.cancel": "_handle_sandbox_runtime_cancel",
    "sandbox.runtime.remove": "_handle_sandbox_runtime_remove",
    "sandbox.runtime.discard_download": "_handle_sandbox_runtime_discard_download",
    "sandbox.resume": "_handle_sandbox_resume",
}


def test_production_registry_uses_contract_wrappers_without_surface_drift() -> None:
    registry = get_dispatcher()

    assert len(registry.list_methods()) == 306
    assert tuple(PRODUCTION_HANDLER_NAMES) == SANDBOX_RUNTIME_CONTRACT_METHODS
    for method, implementation_name in PRODUCTION_HANDLER_NAMES.items():
        entry = registry.get_entry(method)
        implementation = getattr(rpc_sandbox, implementation_name)
        registered = getattr(rpc_sandbox, f"{implementation_name}_contract")

        assert entry is not None
        assert entry.required_scope == GATEWAY_METHOD_CONTRACTS[method].scope
        assert entry.handler is registered
        assert entry.handler is not implementation


@pytest.mark.asyncio
async def test_production_wrapper_fails_closed_on_invalid_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidCapabilityApplication:
        async def inspect_capability(self, *, refresh: bool) -> SandboxCapability:
            assert refresh is False
            return SandboxCapability(
                available=True,
                backend="test",
                platform="test",
                code="ready",
                reason="ready",
                setup_supported=True,
                restart_required=False,
                probe_version=-1,
                capabilities=frozenset(),
            )

    monkeypatch.setattr(
        rpc_sandbox,
        "_sandbox_application",
        lambda _ctx: InvalidCapabilityApplication(),
    )
    entry = get_dispatcher().get_entry("sandbox.capability.status")
    assert entry is not None

    with pytest.raises(RpcHandlerError) as exc_info:
        await entry.handler({}, object())

    assert exc_info.value.code == "INTERNAL_ERROR"
    assert "violated its v4 contract" in str(exc_info.value)


@pytest.mark.asyncio
async def test_generic_factory_registers_all_methods_and_preserves_implementation_calls() -> None:
    registry = RecordingRegistry()
    calls: list[tuple[str, Any, object]] = []

    for method in SANDBOX_RUNTIME_CONTRACT_METHODS:
        async def implementation(
            params: Any,
            context: object,
            *,
            _method: str = method,
        ) -> Any:
            calls.append((_method, params, context))
            return VALID_RESULTS[_method]

        register_sandbox_runtime_contract(
            registry,
            method,
            implementation,
            internal_error=RpcHandlerError,
            guest_allowed_checker=is_guest_rpc_method_allowed,
        )

    assert tuple(registry.handlers) == SANDBOX_RUNTIME_CONTRACT_METHODS
    assert registry.scopes["sandbox.runtime.install"] == "operator.admin"
    assert registry.scopes["sandbox.policy.update"] == "operator.write"
    assert registry.scopes["sandbox.setup.status"] == "operator.read"

    context = object()
    for method, handler in registry.handlers.items():
        assert await handler(VALID_PARAMS[method], context) == VALID_RESULTS[method]

    assert [method for method, _params, _context in calls] == list(
        SANDBOX_RUNTIME_CONTRACT_METHODS
    )
    assert all(call_context is context for _method, _params, call_context in calls)


@pytest.mark.asyncio
async def test_request_mismatch_is_observe_only() -> None:
    registry = RecordingRegistry()
    observed: list[Any] = []

    async def implementation(params: Any, _context: object) -> dict[str, Any]:
        observed.append(params)
        return VALID_RESULTS["sandbox.runtime.install"]

    register_sandbox_runtime_contract(
        registry,
        "sandbox.runtime.install",
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )

    invalid = {"componentId": "ruby"}
    assert await registry.handlers["sandbox.runtime.install"](invalid, object()) == (
        VALID_RESULTS["sandbox.runtime.install"]
    )
    assert observed == [invalid]


@pytest.mark.asyncio
async def test_invalid_success_result_fails_closed() -> None:
    registry = RecordingRegistry()

    async def implementation(_params: Any, _context: object) -> dict[str, Any]:
        return {"available": True}

    register_sandbox_runtime_contract(
        registry,
        "sandbox.capability.status",
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )

    with pytest.raises(RpcHandlerError) as exc_info:
        await registry.handlers["sandbox.capability.status"]({}, object())

    assert exc_info.value.code == "INTERNAL_ERROR"
    assert "violated its v4 contract" in str(exc_info.value)


def test_generic_factory_rejects_non_sandbox_method() -> None:
    registry = RecordingRegistry()

    async def implementation(_params: Any, _context: object) -> object:
        return object()

    with pytest.raises(ValueError, match="unsupported SandboxRuntime Contract method"):
        register_sandbox_runtime_contract(
            registry,
            "sandbox.status",
            implementation,
            internal_error=RpcHandlerError,
            guest_allowed_checker=is_guest_rpc_method_allowed,
        )
