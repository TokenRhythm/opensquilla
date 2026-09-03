from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
import structlog

import opensquilla.gateway.adapters.contract_method as contract_method_adapter
from opensquilla.gateway.adapters.contract_method import (
    GatewayContractBinding,
    register_gateway_contract_method,
)
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, RpcRegistry


class _ContractViolationError(ValueError):
    pass


def _descriptor(
    *,
    guest_allowed: bool = False,
    errors: Any = ({"code": "INTERNAL_ERROR"},),
) -> Any:
    """F1-compatible shape: the registration seam uses only name and scope."""
    return SimpleNamespace(
        name="example.query",
        kind="query",
        scope="operator.read",
        guest_allowed=guest_allowed,
        idempotency="safe",
        timeout=None,
        capability=None,
        errors=errors,
        protocol="opensquilla.gateway.v4",
        wire_version=4,
        request_model=object,
        params_model=object,
        response_model=object,
        result_model=object,
    )


def _binding(
    *,
    observe_params: Any,
    validate_result: Any,
    descriptor: Any | None = None,
    response_error_code: str = "INTERNAL_ERROR",
    result_validation_errors: Any = (_ContractViolationError,),
) -> GatewayContractBinding[Any]:
    return GatewayContractBinding(
        descriptor=descriptor or _descriptor(),
        observe_params=observe_params,
        validate_result=validate_result,
        result_validation_errors=result_validation_errors,
        response_error_message="example.query response violated its v4 contract",
        request_mismatch_event="example.query.request_contract_mismatch",
        response_violation_event="example.query.contract_violation",
        response_error_code=response_error_code,
    )


def _legacy_guest_denied(_method: str) -> bool:
    return False


def test_plain_registry_registration_has_no_generated_contract_provenance() -> None:
    registry = RpcRegistry()

    async def handler(_params: Any, _ctx: Any) -> Any:
        return {"ok": True}

    registry.register("example.query", handler, "operator.read")

    entry = registry.get_entry("example.query")
    assert entry is not None
    assert entry.generated_contract_name is None


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
            validate_result=lambda _result: None,
        ),
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=_legacy_guest_denied,
    )
    ctx = cast(RpcContext, object())
    with structlog.testing.capture_logs() as logs:
        result = await handler(params, ctx)

    entry = registry.get_entry("example.query")
    assert entry is not None
    assert entry.handler is handler
    assert entry.required_scope == "operator.read"
    assert entry.generated_contract_name == "example.query"
    assert implementation_calls == [(params, ctx)]
    assert implementation_calls[0][0] is params
    assert result is expected
    assert [record["event"] for record in logs] == [
        "example.query.request_contract_mismatch"
    ]


def test_registry_rejects_mismatched_generated_contract_marker_before_write() -> None:
    registry = RpcRegistry()

    async def handler(_params: Any, _ctx: Any) -> Any:
        return {"ok": True}

    setattr(handler, "_opensquilla_generated_contract_name", "other.query")

    with pytest.raises(ValueError, match="generated Contract marker"):
        registry.register("example.query", handler, "operator.read")

    assert registry.get_entry("example.query") is None


def test_registration_write_failure_is_not_retried_and_receives_marker() -> None:
    calls: list[tuple[str, str | None, str]] = []

    class FailingRegistry:
        def register(self, name: str, handler: Any, scope: str) -> None:
            calls.append(
                (
                    name,
                    getattr(handler, "_opensquilla_generated_contract_name", None),
                    scope,
                )
            )
            raise RuntimeError("registry write failed")

    async def implementation(_params: Any, _ctx: Any) -> Any:
        return {"ok": True}

    with pytest.raises(RuntimeError, match="registry write failed"):
        register_gateway_contract_method(
            FailingRegistry(),
            _binding(observe_params=lambda _params: (), validate_result=lambda _result: None),
            implementation,
            internal_error=RpcHandlerError,
            guest_allowed_checker=_legacy_guest_denied,
        )

    assert calls == [("example.query", "example.query", "operator.read")]


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
        _binding(observe_params=broken_observer, validate_result=lambda _result: None),
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=_legacy_guest_denied,
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
        guest_allowed_checker=_legacy_guest_denied,
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
        _binding(observe_params=lambda _params: (), validate_result=lambda _result: None),
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=_legacy_guest_denied,
    )

    with pytest.raises(RuntimeError) as error:
        await handler(None, object())

    assert error.value is failure


@pytest.mark.parametrize(
    ("descriptor_allowed", "legacy_allowed"),
    [(True, False), (False, True)],
)
def test_guest_policy_drift_fails_before_registration(
    descriptor_allowed: bool,
    legacy_allowed: bool,
) -> None:
    registry = RpcRegistry()

    async def implementation(_params: Any, _ctx: Any) -> Any:
        return {"ok": True}

    binding = _binding(
        descriptor=_descriptor(guest_allowed=descriptor_allowed),
        observe_params=lambda _params: (),
        validate_result=lambda _result: None,
    )

    with pytest.raises(ValueError, match="disagrees with legacy guest policy"):
        register_gateway_contract_method(
            registry,
            binding,
            implementation,
            internal_error=RpcHandlerError,
            guest_allowed_checker=lambda _method: legacy_allowed,
        )

    assert registry.get_entry("example.query") is None


def test_guest_policy_checker_must_return_a_real_bool() -> None:
    registry = RpcRegistry()

    async def implementation(_params: Any, _ctx: Any) -> Any:
        return {"ok": True}

    with pytest.raises(TypeError, match="must return bool"):
        register_gateway_contract_method(
            registry,
            _binding(observe_params=lambda _params: (), validate_result=lambda _result: None),
            implementation,
            internal_error=RpcHandlerError,
            guest_allowed_checker=lambda _method: cast(Any, 0),
        )


@pytest.mark.parametrize(
    "errors",
    [
        ({"message": "missing code"},),
        ({"code": ""},),
        ({"code": 7},),
        ("not-an-object",),
    ],
)
def test_malformed_generated_error_metadata_fails_fast(errors: Any) -> None:
    with pytest.raises(ValueError, match="descriptor errors"):
        _binding(
            descriptor=_descriptor(errors=errors),
            observe_params=lambda _params: (),
            validate_result=lambda _result: None,
        )


def test_response_error_code_must_be_declared_by_generated_descriptor() -> None:
    with pytest.raises(ValueError, match="is not declared"):
        _binding(
            descriptor=_descriptor(errors=({"code": "UNAVAILABLE"},)),
            observe_params=lambda _params: (),
            validate_result=lambda _result: None,
        )


@pytest.mark.parametrize(
    "error_type",
    [object, BaseException, "not-a-type"],
)
def test_result_validation_errors_require_exception_subclasses(error_type: Any) -> None:
    with pytest.raises(TypeError, match="must contain Exception subclasses"):
        _binding(
            observe_params=lambda _params: (),
            validate_result=lambda _result: None,
            result_validation_errors=(error_type,),
        )


@pytest.mark.asyncio
async def test_validator_return_value_cannot_rewrite_implementation_result() -> None:
    registry = RpcRegistry()
    expected = {"wire": "original"}

    async def implementation(_params: Any, _ctx: Any) -> Any:
        return expected

    handler = register_gateway_contract_method(
        registry,
        _binding(
            observe_params=lambda _params: (),
            validate_result=lambda _result: {"wire": "rewritten"},
        ),
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=_legacy_guest_denied,
    )

    assert await handler(None, object()) is expected


class _BrokenLogger:
    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("warning sink failed")

    def error(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("error sink failed")


@pytest.mark.asyncio
async def test_request_diagnostics_failure_remains_observe_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = RpcRegistry()
    calls: list[Any] = []
    expected = {"unchanged": True}

    async def implementation(params: Any, _ctx: Any) -> Any:
        calls.append(params)
        return expected

    handler = register_gateway_contract_method(
        registry,
        _binding(
            observe_params=lambda _params: ({"type": "legacy_shape"},),
            validate_result=lambda _result: None,
        ),
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=_legacy_guest_denied,
    )
    monkeypatch.setattr(contract_method_adapter, "log", _BrokenLogger())
    params = {"legacy": True}

    assert await handler(params, object()) is expected
    assert calls == [params]


@pytest.mark.asyncio
async def test_response_diagnostics_failure_keeps_stable_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = RpcRegistry()

    def reject_result(_result: Any) -> None:
        raise _ContractViolationError("invalid result")

    async def implementation(_params: Any, _ctx: Any) -> Any:
        return {"invalid": True}

    handler = register_gateway_contract_method(
        registry,
        _binding(observe_params=lambda _params: (), validate_result=reject_result),
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=_legacy_guest_denied,
    )
    monkeypatch.setattr(contract_method_adapter, "log", _BrokenLogger())

    with pytest.raises(RpcHandlerError) as error:
        await handler(None, object())

    assert error.value.code == "INTERNAL_ERROR"
    assert error.value.message == "example.query response violated its v4 contract"
