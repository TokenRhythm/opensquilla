"""Gateway Contract registration tests for the R1 Goal/Plan command seam."""

from __future__ import annotations

from typing import Any, cast

import pytest
import structlog

from opensquilla.gateway.adapters.goals_contract import (
    register_goals_clear_contract,
    register_goals_edit_contract,
    register_goals_pause_contract,
    register_goals_resume_contract,
)
from opensquilla.gateway.adapters.plans_contract import (
    register_plans_cancel_run_contract,
    register_plans_implement_contract,
    register_plans_revise_contract,
    register_plans_set_mode_contract,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcHandlerError, RpcRegistry


@pytest.mark.parametrize(
    ("register", "method"),
    [
        (register_goals_edit_contract, "goals.edit"),
        (register_goals_pause_contract, "goals.pause"),
        (register_goals_resume_contract, "goals.resume"),
        (register_goals_clear_contract, "goals.clear"),
        (register_plans_set_mode_contract, "plans.setMode"),
        (register_plans_revise_contract, "plans.revise"),
        (register_plans_implement_contract, "plans.implement"),
        (register_plans_cancel_run_contract, "plans.cancelRun"),
    ],
)
@pytest.mark.asyncio
async def test_r1_adapter_registers_one_handler_and_preserves_wire_result(
    register: Any,
    method: str,
) -> None:
    registry = RpcRegistry()
    expected = {"accepted": True, "future": {"kept": True}}
    seen: list[Any] = []

    async def implementation(params: Any, _ctx: Any) -> dict[str, Any]:
        seen.append(params)
        return expected

    handler = register(
        registry,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
    params = {"legacy": True}
    result = await handler(params, cast(Any, object()))

    assert result is expected
    assert seen == [params]
    entry = registry.get_entry(method)
    assert entry is not None
    assert entry.handler is handler
    assert entry.required_scope == "operator.write"


@pytest.mark.asyncio
async def test_r1_adapters_keep_invalid_request_observation_non_blocking() -> None:
    registry = RpcRegistry()

    async def implementation(_params: Any, _ctx: Any) -> dict[str, Any]:
        return {"accepted": True}

    handler = register_goals_edit_contract(
        registry,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )

    with structlog.testing.capture_logs() as logs:
        result = await handler({"planRevisionId": 1}, cast(Any, object()))

    assert result == {"accepted": True}
    assert any(record.get("event") == "goals.edit.request_contract_mismatch" for record in logs)


@pytest.mark.asyncio
async def test_r1_adapters_map_non_object_result_to_internal_error() -> None:
    registry = RpcRegistry()

    async def implementation(_params: Any, _ctx: Any) -> Any:
        return None

    handler = register_goals_clear_contract(
        registry,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )

    with pytest.raises(RpcHandlerError) as error:
        await handler({}, cast(Any, object()))

    assert error.value.code == "INTERNAL_ERROR"
    assert error.value.message == "goals.clear response violated its v4 contract"
