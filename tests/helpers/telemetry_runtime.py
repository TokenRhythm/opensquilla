"""Offline telemetry data shared by in-process and isolated shutdown tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from opensquilla.telemetry.consent import (
    CURRENT_RELIABILITY_NOTICE_VERSION,
    resolve_scope_consent,
)
from opensquilla.telemetry.contracts import TELEMETRY_EVENT_ADAPTER
from opensquilla.telemetry.coordination import scope_consent_coordinator_for


def runtime_config(state_dir: Path, *, disabled: bool = False):
    config = SimpleNamespace(
        state_dir=str(state_dir),
        privacy=SimpleNamespace(disable_network_observability=disabled),
    )
    scope_consent_coordinator_for(
        config,
        state_provider=lambda scope: resolve_scope_consent(scope, config=config, env={}),
    )
    return config


def turn_event(number: int = 1):
    return TELEMETRY_EVENT_ADAPTER.validate_json(
        json.dumps(
            {
                "event_name": "turn_result",
                "event_version": 1,
                "event_id": f"00000000-0000-4000-8000-{number:012d}",
                "occurred_at_utc": "2026-09-02T01:02:03.456Z",
                "source": "gateway",
                "app_version": "1.2.3",
                "platform": "linux",
                "outcome": "success",
                "error_code": None,
                "duration_ms": 120,
                "consent_scope": "reliability",
                "notice_version": CURRENT_RELIABILITY_NOTICE_VERSION,
                "sample_rate": 1.0,
                "app_session_id": "00000000-0000-4000-8000-000000000900",
                "ttft_ms": 40,
                "stall_count": 0,
                "stall_threshold_ms": 15_000,
            }
        ),
        strict=True,
    )
