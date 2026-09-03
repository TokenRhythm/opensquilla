from __future__ import annotations

from typing import Any

import pytest

from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
)
from opensquilla.gateway.adapters.platform_configuration_contract import (
    PLATFORM_CONFIGURATION_CONTRACT_METHODS,
    register_platform_configuration_contract,
)
from opensquilla.gateway.adapters.platform_setup_contract import (
    PLATFORM_SETUP_CONTRACT_METHODS,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcHandlerError, RpcRegistry, get_dispatcher

EXPECTED_PLATFORM_CONFIGURATION_METHODS = (
    "config.get",
    "config.effective",
    "config.patch",
    "config.patch.safe",
    "models.list",
    "providers.status",
    "models.routing.get",
)


def test_platform_configuration_contract_owns_the_seven_existing_methods() -> None:
    assert PLATFORM_CONFIGURATION_CONTRACT_METHODS == EXPECTED_PLATFORM_CONFIGURATION_METHODS


@pytest.mark.parametrize("method", EXPECTED_PLATFORM_CONFIGURATION_METHODS)
def test_platform_configuration_registration_uses_generated_identity_and_scope(
    method: str,
) -> None:
    registry = RpcRegistry()

    async def implementation(_params: Any, _ctx: Any) -> Any:
        return None

    handler = register_platform_configuration_contract(
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


def test_onboarding_catalog_is_part_of_the_existing_setup_contract() -> None:
    assert "onboarding.catalog" in PLATFORM_SETUP_CONTRACT_METHODS


@pytest.mark.parametrize(
    "method",
    (*EXPECTED_PLATFORM_CONFIGURATION_METHODS, "onboarding.catalog"),
)
def test_runtime_platform_entries_are_generated_contract_bound(method: str) -> None:
    entry = get_dispatcher().get_entry(method)
    assert entry is not None
    assert entry.generated_contract_name == method
    assert entry.required_scope == GATEWAY_METHOD_CONTRACTS[method].scope
    assert entry.handler.__module__ == "opensquilla.gateway.adapters.contract_method"
    assert entry.handler.__name__ == "handle_contract_method"
