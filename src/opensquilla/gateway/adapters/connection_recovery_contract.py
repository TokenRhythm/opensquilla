"""Strict, additive connection recovery Contract registration."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from opensquilla.contracts.generated.v4.gateway_contract_registry import GATEWAY_METHOD_CONTRACTS
from opensquilla.gateway.adapters._generated_contract_bindings import (
    generated_contract_bindings,
    register_generated_contract_binding,
)


def validate_recovery_params(method: str, params: Any) -> None:
    # Unlike legacy observe-only adapters, these additive methods have no
    # historical malformed-input compatibility to preserve.
    try:
        GATEWAY_METHOD_CONTRACTS[method].params_model.model_validate(params)
    except ValidationError as exc:
        raise ValueError("Invalid connection recovery parameters") from exc


def register_connection_recovery_contract(
    registry: Any, method: str, implementation: Any, **kwargs: Any
) -> Any:
    bindings = generated_contract_bindings((method,), ValueError)
    return register_generated_contract_binding(
        registry, bindings, method, implementation, **kwargs
    )
