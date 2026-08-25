"""Regression tests for the shared RPC ingress text boundary."""

from __future__ import annotations

from collections import UserDict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, computed_field, field_serializer

from opensquilla.gateway.rpc import ingress
from opensquilla.gateway.rpc.ingress import (
    RPC_INGRESS_MAX_DEPTH,
)
from opensquilla.gateway.rpc.registry import RpcContext, RpcRegistry


def _registry(calls: list[Any]) -> RpcRegistry:
    registry = RpcRegistry()

    async def handler(params: Any, ctx: RpcContext) -> dict[str, bool]:
        calls.append(params)
        return {"handled": True}

    registry.register("test.echo", handler, "operator.write")
    return registry


async def test_dispatch_rejects_non_utf8_text_before_handler() -> None:
    calls: list[Any] = []
    registry = _registry(calls)

    response = await registry.dispatch(
        "request-1",
        "test.echo",
        {"message": "ok", "attachments": [{"metadata": {"bad": "secret-\ud800"}}]},
        RpcContext(conn_id="test"),
    )

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "INVALID_REQUEST"
    assert response.error.message == "RPC request contains text that cannot be encoded as UTF-8"
    assert response.error.details == {"reason": "invalid_utf8_text"}
    assert "secret" not in response.model_dump_json()
    assert calls == []


async def test_dispatch_checks_internal_models_mappings_sequences_and_dataclasses() -> None:
    class Attachment(BaseModel):
        name: str

    class ExtraAttachment(BaseModel):
        model_config = ConfigDict(extra="allow")

    class ComputedAttachment(BaseModel):
        name: str = "safe"

        @computed_field
        @property
        def description(self) -> str:
            return "computed-\udfff"

    class SerializedAttachment(BaseModel):
        name: str = "safe"

        @field_serializer("name")
        def serialize_name(self, value: str) -> str:
            return "serialized-\ud800"

    @dataclass
    class Metadata:
        label: str

    malformed_values: list[Any] = [
        Attachment(name="model-\ud800"),
        ExtraAttachment(extra_name="extra-\ud800"),
        ComputedAttachment(),
        SerializedAttachment(),
        UserDict({"mapping": "\udfff"}),
        ("tuple", ["\ud800"]),
        Metadata(label="dataclass-\udfff"),
    ]

    for malformed in malformed_values:
        calls: list[Any] = []
        response = await _registry(calls).dispatch(
            "request-model",
            "test.echo",
            {"attachment": malformed},
            RpcContext(conn_id="test"),
        )

        assert response.error is not None
        assert response.error.details == {"reason": "invalid_utf8_text"}
        assert calls == []


async def test_dispatch_structures_container_inspection_failures() -> None:
    class BrokenMapping(Mapping[str, Any]):
        def __getitem__(self, key: str) -> Any:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            raise RuntimeError("sensitive mapping failure")

        def __len__(self) -> int:
            return 1

    class BrokenSequence(Sequence[Any]):
        def __getitem__(self, index: int) -> Any:
            raise IndexError("sensitive sequence failure")

        def __len__(self) -> int:
            return 1

        def __iter__(self) -> Iterator[Any]:
            raise RuntimeError("sensitive sequence failure")

    @dataclass
    class BrokenDataclass:
        value: str = "safe"

        def __getattribute__(self, name: str) -> Any:
            if name == "value":
                raise AttributeError("sensitive dataclass failure")
            return object.__getattribute__(self, name)

    for malformed in (BrokenMapping(), BrokenSequence(), BrokenDataclass()):
        calls: list[Any] = []
        response = await _registry(calls).dispatch(
            "request-broken",
            "test.echo",
            {"value": malformed},
            RpcContext(conn_id="test"),
        )

        assert response.error is not None
        assert response.error.code == "INVALID_REQUEST"
        assert response.error.details == {"reason": "uninspectable_value"}
        assert "sensitive" not in response.model_dump_json()
        assert calls == []


async def test_dispatch_rejects_cyclic_internal_params_without_recursing_forever() -> None:
    calls: list[Any] = []
    registry = _registry(calls)
    params: dict[str, Any] = {}
    params["self"] = params

    response = await registry.dispatch(
        "request-2", "test.echo", params, RpcContext(conn_id="test")
    )

    assert response.ok is False
    assert response.error is not None
    assert response.error.details == {"reason": "cyclic_structure"}
    assert calls == []


async def test_dispatch_rejects_inputs_beyond_depth_and_node_limits(monkeypatch: Any) -> None:
    calls: list[Any] = []
    registry = _registry(calls)
    nested: Any = None
    for _ in range(RPC_INGRESS_MAX_DEPTH + 1):
        nested = [nested]

    too_deep = await registry.dispatch(
        "request-depth", "test.echo", nested, RpcContext(conn_id="test")
    )
    monkeypatch.setattr(ingress, "RPC_INGRESS_MAX_NODES", 8)
    too_large = await registry.dispatch(
        "request-nodes",
        "test.echo",
        [None] * 8,
        RpcContext(conn_id="test"),
    )

    assert too_deep.error is not None
    assert too_deep.error.details == {"reason": "structure_too_deep"}
    assert too_large.error is not None
    assert too_large.error.details == {"reason": "too_many_nodes"}
    assert calls == []


async def test_dispatch_accepts_shared_containers_and_normal_unicode() -> None:
    calls: list[Any] = []
    registry = _registry(calls)
    shared = ["😀", "中文"]
    params = {"first": shared, "second": shared}

    response = await registry.dispatch(
        "request-3", "test.echo", params, RpcContext(conn_id="test")
    )

    assert response.ok is True
    assert response.payload == {"handled": True}
    assert calls == [params]


async def test_dispatch_accepts_dense_params_within_wire_payload_limit() -> None:
    calls: list[Any] = []
    registry = _registry(calls)
    params = {"items": [None] * 100_000}

    response = await registry.dispatch(
        "request-dense", "test.echo", params, RpcContext(conn_id="test")
    )

    assert response.ok is True
    assert calls == [params]
