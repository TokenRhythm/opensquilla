from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
import structlog

from opensquilla.gateway.adapters.contract_method import (
    GatewayContractBinding,
    register_gateway_contract_method,
)
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, RpcRegistry


class _ContractViolationError(ValueError):
    pass


def _descriptor() -> Any:
    """F1-compatible shape: the registration seam uses only name and scope."""
    return SimpleNamespace(
        name="example.query",
        kind="query",
        scope="operator.read",
        guest_allowed=False,
        idempotency="safe",
        timeout=None,
        capability=None,
        errors=(),
        protocol="opensquilla.gateway.v4",
        wire_version=4,
        request_model=object,
        params_model=object,
        response_model=object,
        result_model=object,
    )


def _binding(*, observe_params: Any, validate_result: Any) -> GatewayContractBinding[Any]:
    return GatewayContractBinding(
        descriptor=_descriptor(),
        observe_params=observe_params,
        validate_result=validate_result,
        result_validation_errors=(_ContractViolationError,),
        response_error_message="example.query response violated its v4 contract",
        request_mismatch_event="example.query.request_contract_mismatch",
        response_violation_event="example.query.contract_violation",
    )


@pytest.mark.asyncio
async def test_registers_one_handler_and_calls_one_implementation_without_rewriting() -> None:
    registry = RpcRegistry()
    params = {"legacy": {"kept": True}}
    expected = {"result": [1, None, "future"]}
    implementation_calls: list[tuple[Any, Any]] = []

    async def implementation(raw_params: Any, ctx: Any) -> Any:
        implementation_calls.append((raw_params, ctx))
        return expected

    handler = register_gateway_contract_method(
        registry,
        _binding(
            observe_params=lambda _params: ({"type": "legacy_shape"},),
            validate_result=lambda result: result,
        ),
        implementation,
        internal_error=RpcHandlerError,
    )
    ctx = cast(RpcContext, object())
    with structlog.testing.capture_logs() as logs:
        result = await handler(params, ctx)

    entry = registry.get_entry("example.query")
    assert entry is not None
    assert entry.handler is handler
    assert entry.required_scope == "operator.read"
    assert implementation_calls == [(params, ctx)]
    assert implementation_calls[0][0] is params
    assert result is expected
    assert [record["event"] for record in logs] == [
        "example.query.request_contract_mismatch"
    ]


@pytest.mark.asyncio
async def test_request_observer_failure_remains_observe_only() -> None:
    registry = RpcRegistry()
    expected = {"unchanged": True}

    def broken_observer(_params: Any) -> tuple[dict[str, Any], ...]:
        raise RuntimeError("observer drift")

    async def implementation(_params: Any, _ctx: Any) -> Any:
        return expected

    handler = register_gateway_contract_method(
        registry,
        _binding(observe_params=broken_observer, validate_result=lambda result: result),
        implementation,
        internal_error=RpcHandlerError,
    )
    with structlog.testing.capture_logs() as logs:
        result = await handler({"legacy": True}, object())

    assert result is expected
    assert logs[0]["event"] == "example.query.request_contract_mismatch"
    assert logs[0]["observer_error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_response_contract_failure_is_fail_closed() -> None:
    registry = RpcRegistry()

    def reject_result(_result: Any) -> Any:
        raise _ContractViolationError("bad result")

    async def implementation(_params: Any, _ctx: Any) -> Any:
        return {"invalid": True}

    handler = register_gateway_contract_method(
        registry,
        _binding(observe_params=lambda _params: (), validate_result=reject_result),
        implementation,
        internal_error=RpcHandlerError,
    )

    with pytest.raises(RpcHandlerError) as error:
        await handler(None, object())

    assert error.value.code == "INTERNAL_ERROR"
    assert error.value.message == "example.query response violated its v4 contract"


@pytest.mark.asyncio
async def test_implementation_exception_is_not_mapped_as_contract_failure() -> None:
    registry = RpcRegistry()
    failure = RuntimeError("implementation failed")

    async def implementation(_params: Any, _ctx: Any) -> Any:
        raise failure

    handler = register_gateway_contract_method(
        registry,
        _binding(observe_params=lambda _params: (), validate_result=lambda result: result),
        implementation,
        internal_error=RpcHandlerError,
    )

    with pytest.raises(RuntimeError) as error:
        await handler(None, object())

    assert error.value is failure
