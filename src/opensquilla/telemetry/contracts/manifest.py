"""Stable, language-neutral telemetry protocol manifest and fingerprint."""

from __future__ import annotations

import hashlib
import json
from types import MappingProxyType
from typing import Any, Final

from opensquilla.telemetry.contracts.batch import (
    MAX_GROWTH_BATCH_EVENTS,
    MAX_RELIABILITY_BATCH_EVENTS,
)
from opensquilla.telemetry.contracts.common import BATCH_VERSION

MAX_RELIABILITY_BATCH_BYTES: Final = 128 * 1024
MAX_GROWTH_BATCH_BYTES: Final = 64 * 1024
MAX_TELEMETRY_EVENT_BYTES: Final = 16 * 1024
MAX_TELEMETRY_NESTING_DEPTH: Final = 16

CURRENT_NOTICE_VERSION_BY_SCOPE = MappingProxyType(
    {
        "growth": "growth-v1",
        "reliability": "reliability-v1",
    }
)

# Keep this tuple explicit and ordered.  Other language implementations can
# pin the exported JSON fingerprint without importing Python model internals.
_EVENT_SPECS: Final = (
    ("app_start_result", 1, "reliability"),
    ("gateway_start_result", 1, "reliability"),
    ("app_crash_detected", 1, "reliability"),
    ("turn_result", 1, "reliability"),
    ("turn_result", 2, "reliability"),
    ("turn_result", 3, "reliability"),
    ("tool_call_result", 1, "reliability"),
    ("tool_call_result", 2, "reliability"),
    ("file_parse_result", 1, "reliability"),
    ("file_parse_result", 2, "reliability"),
    ("update_result", 1, "reliability"),
    ("performance_summary", 1, "reliability"),
    ("landing_view", 1, "growth"),
    ("download_click", 1, "growth"),
    ("download_served", 1, "growth"),
    ("install_started", 1, "growth"),
    ("install_result", 1, "growth"),
    ("registration_started", 1, "growth"),
    ("registration_result", 1, "growth"),
    ("onboarding_result", 1, "growth"),
    ("first_app_ready", 1, "growth"),
    ("first_turn_started", 1, "growth"),
    ("first_turn_result", 1, "growth"),
    ("client_launch", 1, "growth"),
)


def _build_manifest() -> dict[str, Any]:
    return {
        "batch_limits": {
            "growth": {
                "max_bytes": MAX_GROWTH_BATCH_BYTES,
                "max_events": MAX_GROWTH_BATCH_EVENTS,
            },
            "reliability": {
                "max_bytes": MAX_RELIABILITY_BATCH_BYTES,
                "max_events": MAX_RELIABILITY_BATCH_EVENTS,
            },
        },
        "batch_version": BATCH_VERSION,
        "events": [
            {
                "event_name": event_name,
                "event_version": event_version,
                "scope": scope,
            }
            for event_name, event_version, scope in _EVENT_SPECS
        ],
        "manifest_version": 1,
        "notice_versions": dict(CURRENT_NOTICE_VERSION_BY_SCOPE),
        "wire_limits": {
            "max_event_bytes": MAX_TELEMETRY_EVENT_BYTES,
            "max_nesting_depth": MAX_TELEMETRY_NESTING_DEPTH,
        },
    }


TELEMETRY_PROTOCOL_MANIFEST_JSON: Final = json.dumps(
    _build_manifest(),
    ensure_ascii=False,
    allow_nan=False,
    sort_keys=True,
    separators=(",", ":"),
)
TELEMETRY_PROTOCOL_FINGERPRINT_SHA256: Final = hashlib.sha256(
    TELEMETRY_PROTOCOL_MANIFEST_JSON.encode("utf-8")
).hexdigest()


def telemetry_protocol_manifest() -> dict[str, Any]:
    """Return a detached JSON-compatible copy of the protocol manifest."""

    value = json.loads(TELEMETRY_PROTOCOL_MANIFEST_JSON)
    if not isinstance(value, dict):  # pragma: no cover - constant invariant
        raise RuntimeError("telemetry protocol manifest must be an object")
    return value


__all__ = [
    "CURRENT_NOTICE_VERSION_BY_SCOPE",
    "MAX_GROWTH_BATCH_BYTES",
    "MAX_RELIABILITY_BATCH_BYTES",
    "MAX_TELEMETRY_EVENT_BYTES",
    "MAX_TELEMETRY_NESTING_DEPTH",
    "TELEMETRY_PROTOCOL_FINGERPRINT_SHA256",
    "TELEMETRY_PROTOCOL_MANIFEST_JSON",
    "telemetry_protocol_manifest",
]
