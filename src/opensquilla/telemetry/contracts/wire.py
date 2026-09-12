"""Fail-closed parsing for untrusted telemetry wire payloads.

Network and file ingress must call :func:`parse_telemetry_wire` instead of
passing raw input directly to a Pydantic JSON adapter.  The preflight phase
rejects JSON ambiguities and resource-limit violations before schema parsing.
"""

from __future__ import annotations

import codecs
import json
import math
from enum import StrEnum
from typing import Any, Literal, overload

from pydantic import ValidationError

from opensquilla.telemetry.contracts.batch import (
    GrowthEventBatch,
    ReliabilityEventBatch,
)
from opensquilla.telemetry.contracts.growth import GROWTH_EVENT_ADAPTER, GrowthEvent
from opensquilla.telemetry.contracts.manifest import (
    MAX_GROWTH_BATCH_BYTES,
    MAX_RELIABILITY_BATCH_BYTES,
    MAX_TELEMETRY_EVENT_BYTES,
    MAX_TELEMETRY_NESTING_DEPTH,
)
from opensquilla.telemetry.contracts.reliability import (
    RELIABILITY_EVENT_ADAPTER,
    ReliabilityEvent,
)


class TelemetryWireTarget(StrEnum):
    RELIABILITY_BATCH = "reliability_batch"
    GROWTH_BATCH = "growth_batch"
    RELIABILITY_EVENT = "reliability_event"
    GROWTH_EVENT = "growth_event"


class TelemetryWireErrorCode(StrEnum):
    INVALID_INPUT_TYPE = "invalid_input_type"
    INVALID_TARGET = "invalid_target"
    BODY_TOO_LARGE = "body_too_large"
    INVALID_UTF8 = "invalid_utf8"
    UTF8_BOM = "utf8_bom"
    INVALID_JSON = "invalid_json"
    DUPLICATE_KEY = "duplicate_key"
    NON_FINITE_NUMBER = "non_finite_number"
    NESTING_TOO_DEEP = "nesting_too_deep"
    SCHEMA_INVALID = "schema_invalid"


_ERROR_MESSAGES = {
    TelemetryWireErrorCode.INVALID_INPUT_TYPE: "telemetry wire input must be bytes or text",
    TelemetryWireErrorCode.INVALID_TARGET: "telemetry wire target is invalid",
    TelemetryWireErrorCode.BODY_TOO_LARGE: "telemetry wire payload exceeds its byte limit",
    TelemetryWireErrorCode.INVALID_UTF8: "telemetry wire payload must be valid UTF-8",
    TelemetryWireErrorCode.UTF8_BOM: "telemetry wire payload must not contain a UTF-8 BOM",
    TelemetryWireErrorCode.INVALID_JSON: "telemetry wire payload must be valid JSON",
    TelemetryWireErrorCode.DUPLICATE_KEY: "telemetry wire payload contains a duplicate key",
    TelemetryWireErrorCode.NON_FINITE_NUMBER: (
        "telemetry wire payload contains a non-finite number"
    ),
    TelemetryWireErrorCode.NESTING_TOO_DEEP: ("telemetry wire payload exceeds its nesting limit"),
    TelemetryWireErrorCode.SCHEMA_INVALID: "telemetry wire payload does not match its schema",
}


class TelemetryWireError(ValueError):
    """Sanitized failure raised for every untrusted wire rejection."""

    def __init__(self, code: TelemetryWireErrorCode) -> None:
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


type TelemetryWireModel = ReliabilityEventBatch | GrowthEventBatch | ReliabilityEvent | GrowthEvent


class _DuplicateKeyError(ValueError):
    pass


class _NonFiniteNumberError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _reject_json_constant(_token: str) -> None:
    raise _NonFiniteNumberError


def _parse_finite_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise _NonFiniteNumberError
    return value


def _wire_bytes(raw: bytes | str, *, max_bytes: int) -> bytes:
    if isinstance(raw, str):
        # Every Unicode code point occupies at least one UTF-8 byte.  This
        # cheap lower-bound check avoids allocating another oversized copy.
        if len(raw) > max_bytes:
            raise TelemetryWireError(TelemetryWireErrorCode.BODY_TOO_LARGE)
        try:
            encoded = raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise TelemetryWireError(TelemetryWireErrorCode.INVALID_UTF8) from None
    elif isinstance(raw, bytes):
        encoded = raw
    else:
        raise TelemetryWireError(TelemetryWireErrorCode.INVALID_INPUT_TYPE)

    if len(encoded) > max_bytes:
        raise TelemetryWireError(TelemetryWireErrorCode.BODY_TOO_LARGE)
    if encoded.startswith(codecs.BOM_UTF8):
        raise TelemetryWireError(TelemetryWireErrorCode.UTF8_BOM)
    return encoded


def _decode_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise TelemetryWireError(TelemetryWireErrorCode.INVALID_UTF8) from None

    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
            parse_float=_parse_finite_float,
        )
    except _DuplicateKeyError:
        raise TelemetryWireError(TelemetryWireErrorCode.DUPLICATE_KEY) from None
    except _NonFiniteNumberError:
        raise TelemetryWireError(TelemetryWireErrorCode.NON_FINITE_NUMBER) from None
    except RecursionError:
        raise TelemetryWireError(TelemetryWireErrorCode.NESTING_TOO_DEEP) from None
    except (json.JSONDecodeError, UnicodeError, ValueError):
        raise TelemetryWireError(TelemetryWireErrorCode.INVALID_JSON) from None

    _require_bounded_nesting(value)
    return value


def _require_bounded_nesting(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, container_depth = stack.pop()
        if isinstance(current, dict):
            next_depth = container_depth + 1
            if next_depth > MAX_TELEMETRY_NESTING_DEPTH:
                raise TelemetryWireError(TelemetryWireErrorCode.NESTING_TOO_DEEP)
            stack.extend((child, next_depth) for child in current.values())
        elif isinstance(current, list):
            next_depth = container_depth + 1
            if next_depth > MAX_TELEMETRY_NESTING_DEPTH:
                raise TelemetryWireError(TelemetryWireErrorCode.NESTING_TOO_DEEP)
            stack.extend((child, next_depth) for child in current)


def _normalized_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        # ASCII escaping also keeps lone JSON surrogate escapes from causing
        # an unhandled UnicodeEncodeError before strict schema rejection.
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")


def _target_byte_limit(target: TelemetryWireTarget) -> int:
    if target is TelemetryWireTarget.RELIABILITY_BATCH:
        return MAX_RELIABILITY_BATCH_BYTES
    if target is TelemetryWireTarget.GROWTH_BATCH:
        return MAX_GROWTH_BATCH_BYTES
    return MAX_TELEMETRY_EVENT_BYTES


@overload
def parse_telemetry_wire(
    raw: bytes | str,
    *,
    target: Literal[TelemetryWireTarget.RELIABILITY_BATCH],
) -> ReliabilityEventBatch: ...


@overload
def parse_telemetry_wire(
    raw: bytes | str,
    *,
    target: Literal[TelemetryWireTarget.GROWTH_BATCH],
) -> GrowthEventBatch: ...


@overload
def parse_telemetry_wire(
    raw: bytes | str,
    *,
    target: Literal[TelemetryWireTarget.RELIABILITY_EVENT],
) -> ReliabilityEvent: ...


@overload
def parse_telemetry_wire(
    raw: bytes | str,
    *,
    target: Literal[TelemetryWireTarget.GROWTH_EVENT],
) -> GrowthEvent: ...


def parse_telemetry_wire(
    raw: bytes | str,
    *,
    target: TelemetryWireTarget,
) -> TelemetryWireModel:
    """Parse one untrusted payload using the explicitly selected wire schema."""

    if not isinstance(target, TelemetryWireTarget):
        raise TelemetryWireError(TelemetryWireErrorCode.INVALID_TARGET)
    encoded = _wire_bytes(raw, max_bytes=_target_byte_limit(target))
    value = _decode_json(encoded)
    normalized = _normalized_json_bytes(value)

    try:
        if target is TelemetryWireTarget.RELIABILITY_BATCH:
            return ReliabilityEventBatch.model_validate_json(normalized, strict=True)
        if target is TelemetryWireTarget.GROWTH_BATCH:
            return GrowthEventBatch.model_validate_json(normalized, strict=True)
        if target is TelemetryWireTarget.RELIABILITY_EVENT:
            return RELIABILITY_EVENT_ADAPTER.validate_json(normalized, strict=True)
        return GROWTH_EVENT_ADAPTER.validate_json(normalized, strict=True)
    except ValidationError:
        raise TelemetryWireError(TelemetryWireErrorCode.SCHEMA_INVALID) from None


__all__ = [
    "TelemetryWireError",
    "TelemetryWireErrorCode",
    "TelemetryWireModel",
    "TelemetryWireTarget",
    "parse_telemetry_wire",
]
