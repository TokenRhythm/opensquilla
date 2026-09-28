"""Generated wire validation terminates at the durable receipt read adapter."""

from __future__ import annotations

from typing import Any

from opensquilla.contracts.generated.v4.gateway_contract_registry import GATEWAY_METHOD_CONTRACTS
from opensquilla.gateway.adapters._generated_contract_bindings import (
    generated_contract_bindings,
    register_generated_contract_binding,
)


def validate_turn_receipt_params(params: Any) -> None:
    # Keep the original tree for hashing; dumping this model would needlessly
    # duplicate large frozen attachment bodies on an indeterminate send.
    GATEWAY_METHOD_CONTRACTS["turns.receipt.get"].params_model.model_validate(params, strict=True)


def register_turn_receipt_contract(registry: Any, implementation: Any, **kwargs: Any) -> Any:
    method = "turns.receipt.get"
    return register_generated_contract_binding(
        registry, generated_contract_bindings((method,), ValueError), method,
        implementation, **kwargs,
    )
