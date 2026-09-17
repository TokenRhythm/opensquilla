"""Bounded validation for text entering the gateway RPC boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from typing import Any, Final

from pydantic import BaseModel

from opensquilla.gateway.protocol import MAX_PAYLOAD_BYTES

RPC_INGRESS_MAX_DEPTH: Final = 128
# Every node in JSON source consumes at least one byte. Matching the public
# wire-size limit prevents this structural guard from rejecting a frame that
# otherwise satisfies the existing 25 MiB payload contract. Internal callers
# receive the same finite traversal bound even though they do not have raw
# wire bytes.
RPC_INGRESS_MAX_NODES: Final = MAX_PAYLOAD_BYTES

_ERROR_MESSAGES: Final[dict[str, str]] = {
    "invalid_utf8_text": "RPC request contains text that cannot be encoded as UTF-8",
    "structure_too_deep": "RPC request exceeds the maximum nesting depth",
    "too_many_nodes": "RPC request exceeds the maximum structural complexity",
    "cyclic_structure": "RPC request contains a cyclic structure",
    "uninspectable_value": "RPC request contains a value that cannot be safely inspected",
}


class RpcIngressValidationError(ValueError):
    """A safe, structured RPC ingress rejection without request content."""

    def __init__(self, reason: str) -> None:
        super().__init__(_ERROR_MESSAGES[reason])
        self.reason = reason


def is_utf8_encodable(value: str) -> bool:
    """Return whether strict UTF-8 encoding accepts every code point."""

    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def validate_rpc_ingress(*values: Any) -> None:
    """Reject non-UTF-8 text in JSON-like values with bounded traversal.

    Mapping keys and values are both inspected. Pydantic models, dataclasses,
    and non-text sequences are included for internal callers; decoded JSON
    itself only produces dictionaries and lists. The traversal is linear in
    the admitted node count, uses at most ``RPC_INGRESS_MAX_DEPTH`` Python
    frames, and rejects container cycles.
    """

    nodes_seen = 0
    active_containers: set[int] = set()

    def visit(value: Any, depth: int) -> None:
        nonlocal nodes_seen
        if depth > RPC_INGRESS_MAX_DEPTH:
            raise RpcIngressValidationError("structure_too_deep")

        nodes_seen += 1
        if nodes_seen > RPC_INGRESS_MAX_NODES:
            raise RpcIngressValidationError("too_many_nodes")

        if isinstance(value, str):
            if not is_utf8_encodable(value):
                raise RpcIngressValidationError("invalid_utf8_text")
            return

        is_dataclass_instance = is_dataclass(value) and not isinstance(value, type)
        if not isinstance(value, Mapping | Sequence | BaseModel) and not is_dataclass_instance:
            return

        container_id = id(value)
        if container_id in active_containers:
            raise RpcIngressValidationError("cyclic_structure")
        active_containers.add(container_id)
        try:
            if isinstance(value, BaseModel):
                try:
                    exported = value.model_dump(mode="json")
                except Exception as exc:
                    raise RpcIngressValidationError("uninspectable_value") from exc
                visit(exported, depth + 1)
            elif is_dataclass_instance:
                for dataclass_field in fields(value):
                    visit(dataclass_field.name, depth + 1)
                    visit(getattr(value, dataclass_field.name), depth + 1)
            elif isinstance(value, Mapping):
                for key, item in value.items():
                    visit(key, depth + 1)
                    visit(item, depth + 1)
            elif not isinstance(value, str | bytes | bytearray | memoryview):
                for item in value:
                    visit(item, depth + 1)
        finally:
            active_containers.remove(container_id)

    for value in values:
        try:
            visit(value, 0)
        except RpcIngressValidationError:
            raise
        except Exception as exc:
            raise RpcIngressValidationError("uninspectable_value") from exc
