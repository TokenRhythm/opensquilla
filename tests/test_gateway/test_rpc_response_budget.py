from __future__ import annotations

import pytest

from opensquilla.gateway.protocol import make_ok_res
from opensquilla.gateway.rpc import BudgetedRpcResult, RpcContext
from opensquilla.gateway.rpc.registry import RpcRegistry

_MIN_RESPONSE_BUDGET = 64 * 1024


def _wire_bytes(frame: object) -> int:
    return len(frame.model_dump_json().encode("utf-8"))  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_budgeted_result_reports_exact_final_res_frame_bytes() -> None:
    registry = RpcRegistry()

    async def _bounded(params, ctx):
        return BudgetedRpcResult(
            {
                "content": '中文🙂 "quoted" \\ path',
                "wire_bytes": 0,
            },
            _MIN_RESPONSE_BUDGET,
        )

    registry.register("test.bounded", _bounded, "operator.read")
    request_id = '请求-🙂-"quoted"-\\'

    response = await registry.dispatch(
        request_id,
        "test.bounded",
        {},
        RpcContext(conn_id="test"),
    )

    assert response.ok is True
    assert response.payload["wire_bytes"] == _wire_bytes(response)
    assert response.payload["wire_bytes"] <= _MIN_RESPONSE_BUDGET


@pytest.mark.asyncio
async def test_budgeted_result_does_not_add_wire_bytes_to_undeclared_payload() -> None:
    registry = RpcRegistry()

    async def _bounded(params, ctx):
        return BudgetedRpcResult({"content": "small"}, _MIN_RESPONSE_BUDGET)

    registry.register("test.bounded", _bounded, "operator.read")

    response = await registry.dispatch(
        "req-1",
        "test.bounded",
        {},
        RpcContext(conn_id="test"),
    )

    assert response.ok is True
    assert response.payload == {"content": "small"}


@pytest.mark.asyncio
async def test_budget_exceeded_returns_bounded_error_and_dispatch_stays_usable() -> None:
    registry = RpcRegistry()
    oversized_content = '中文🙂 "quoted" \\ path\n' * 10_000

    async def _oversized(params, ctx):
        return BudgetedRpcResult(
            {"content": oversized_content, "wire_bytes": 0},
            _MIN_RESPONSE_BUDGET,
        )

    async def _small(params, ctx):
        return {"status": "ok"}

    registry.register("test.oversized", _oversized, "operator.read")
    registry.register("test.small", _small, "operator.read")
    ctx = RpcContext(conn_id="test")

    response = await registry.dispatch("req-large", "test.oversized", {}, ctx)

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "RESPONSE_BUDGET_EXCEEDED"
    assert response.error.details is not None
    assert response.error.details["byte_budget"] == _MIN_RESPONSE_BUDGET
    attempted_wire_bytes = response.error.details["wire_bytes"]
    attempted = make_ok_res(
        "req-large",
        {"content": oversized_content, "wire_bytes": attempted_wire_bytes},
    )
    assert attempted_wire_bytes == _wire_bytes(attempted)
    assert attempted_wire_bytes > _MIN_RESPONSE_BUDGET
    assert _wire_bytes(response) <= _MIN_RESPONSE_BUDGET

    follow_up = await registry.dispatch("req-small", "test.small", {}, ctx)
    assert follow_up.ok is True
    assert follow_up.payload == {"status": "ok"}


@pytest.mark.asyncio
async def test_pathological_request_id_cannot_expand_budget_error() -> None:
    registry = RpcRegistry()

    async def _oversized(params, ctx):
        return BudgetedRpcResult({"content": "z" * 100_000}, _MIN_RESPONSE_BUDGET)

    registry.register("test.oversized", _oversized, "operator.read")
    response = await registry.dispatch(
        "request-" + ("x" * 70_000),
        "test.oversized",
        {},
        RpcContext(conn_id="test"),
    )

    assert response.ok is False
    assert response.id == ""
    assert response.error is not None
    assert response.error.code == "RESPONSE_BUDGET_EXCEEDED"
    assert _wire_bytes(response) <= _MIN_RESPONSE_BUDGET


@pytest.mark.asyncio
async def test_v2_pre_wrapper_error_is_still_bounded() -> None:
    registry = RpcRegistry()

    async def _raises_before_wrapper(params, ctx):
        raise ValueError("invalid-" + ("界" * 100_000))

    async def _small(params, ctx):
        return {"status": "ok"}

    registry.register("sessions.bootstrap.v2", _raises_before_wrapper, "operator.read")
    registry.register("test.small", _small, "operator.read")
    ctx = RpcContext(conn_id="test")

    response = await registry.dispatch(
        "pre-wrapper",
        "sessions.bootstrap.v2",
        {"maxResponseBytes": _MIN_RESPONSE_BUDGET},
        ctx,
    )

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "RESPONSE_BUDGET_EXCEEDED"
    assert _wire_bytes(response) <= _MIN_RESPONSE_BUDGET

    follow_up = await registry.dispatch("after-pre-wrapper", "test.small", {}, ctx)
    assert follow_up.ok is True


@pytest.mark.asyncio
async def test_legacy_payload_wire_bytes_field_is_not_rewritten() -> None:
    registry = RpcRegistry()

    async def _legacy(params, ctx):
        return {"content": "legacy", "wire_bytes": 7}

    registry.register("test.legacy", _legacy, "operator.read")

    response = await registry.dispatch(
        "req-legacy",
        "test.legacy",
        {},
        RpcContext(conn_id="test"),
    )

    assert response.ok is True
    assert response.payload == {"content": "legacy", "wire_bytes": 7}
