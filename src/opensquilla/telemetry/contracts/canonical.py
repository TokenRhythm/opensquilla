"""Canonical JSON serialization for already-validated metrics models."""

from __future__ import annotations

import json

from opensquilla.telemetry.contracts.common import StrictTelemetryModel
from opensquilla.telemetry.privacy import assert_no_forbidden_fields


def canonical_json_bytes(value: StrictTelemetryModel) -> bytes:
    """Serialize a validated model into stable compact UTF-8 JSON.

    Historical null fields remain explicit. Optional device identity is
    omitted when unavailable, preserving hashes of pre-device queued events.
    Callers must validate raw input before invoking this function.
    """

    validated = type(value).model_validate(value, strict=True)
    payload = validated.model_dump(mode="json", exclude_none=False, by_alias=True)
    assert_no_forbidden_fields(payload)
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")


def canonical_json(value: StrictTelemetryModel) -> str:
    """Return :func:`canonical_json_bytes` decoded as UTF-8 text."""

    return canonical_json_bytes(value).decode("utf-8")


__all__ = ["canonical_json", "canonical_json_bytes"]
