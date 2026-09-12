"""Validation and projection for custom OpenAI-compatible request fields."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

type ExtraBodyValue = (
    None
    | bool
    | int
    | float
    | str
    | list["ExtraBodyValue"]
    | dict[str, "ExtraBodyValue"]
)


# OpenSquilla owns these top-level request fields.  Custom request extensions
# must never replace conversation, streaming, tool, budget, or reasoning
# policy selected by the runtime.
RESERVED_EXTRA_BODY_FIELDS = frozenset(
    {
        "cache_control",
        "disable_fallbacks",
        "enable_thinking",
        "max_completion_tokens",
        "max_output_tokens",
        "max_tokens",
        "messages",
        "model",
        "parallel_tool_calls",
        "preserve_thinking",
        "provider",
        "reasoning",
        "reasoning_effort",
        "response_format",
        "stop",
        "stream",
        "stream_options",
        "temperature",
        "thinking",
        "thinking_budget",
        "tool_choice",
        "tool_stream",
        "tools",
        "top_p",
        "usage",
    }
)


def _normalize_value(value: Any, *, path: str) -> ExtraBodyValue:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"extra_body value at {path} must be a finite JSON number")
        return value
    if isinstance(value, list):
        return [
            _normalize_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        result: dict[str, ExtraBodyValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"extra_body object key at {path} must be a non-empty string")
            result[key] = _normalize_value(item, path=f"{path}.{key}")
        return result
    raise ValueError(f"extra_body value at {path} must be JSON-compatible")


def normalize_extra_body(value: Mapping[str, Any] | None) -> dict[str, ExtraBodyValue]:
    """Return an isolated JSON-compatible body after enforcing owned keys."""

    if not value:
        return {}
    result: dict[str, ExtraBodyValue] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("extra_body top-level keys must be non-empty strings")
        normalized_key = key.strip().lower()
        if normalized_key in RESERVED_EXTRA_BODY_FIELDS:
            raise ValueError(f"extra_body field is reserved by OpenSquilla: {key}")
        result[key] = _normalize_value(item, path=f"extra_body.{key}")
    return result


def extra_body_identity(value: Mapping[str, Any] | None) -> str:
    """Return a stable, key-order-independent deployment identity fragment."""

    normalized = normalize_extra_body(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def merge_extra_body(
    payload: dict[str, Any], value: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Shallow-merge validated extensions without allowing runtime collisions."""

    normalized = normalize_extra_body(value)
    if not normalized:
        return payload
    payload_keys = {str(key).strip().lower() for key in payload}
    collisions = sorted(key for key in normalized if key.strip().lower() in payload_keys)
    if collisions:
        joined = ", ".join(collisions)
        raise ValueError(f"extra_body fields conflict with the generated request: {joined}")
    payload.update(deepcopy(normalized))
    return payload


__all__ = [
    "ExtraBodyValue",
    "RESERVED_EXTRA_BODY_FIELDS",
    "extra_body_identity",
    "merge_extra_body",
    "normalize_extra_body",
]
