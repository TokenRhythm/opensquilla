"""Best-effort bridge from the synchronous code-task runner to Growth telemetry."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

log = logging.getLogger(__name__)

CODING_MODE_ACTIVE_ENV = "OPENSQUILLA_CODING_MODE_ACTIVE"
CODING_MODE_CONFIG_PATH_ENV = "OPENSQUILLA_CODING_MODE_CONFIG_PATH"


def _coding_mode_snapshot_from_env() -> bool | None:
    value = os.environ.get(CODING_MODE_ACTIVE_ENV)
    if value == "1":
        return True
    if value == "0":
        return False
    return None


async def record_current_profile_coding_mode_usage(
    run_id: str,
    *,
    occurred_at: datetime | None = None,
    coding_mode_active: bool | None = None,
    config_path: str | None = None,
) -> bool:
    """Enqueue one Coding Mode use against the operator's primary profile.

    The code-task child agent runs with an isolated disposable profile, so the
    observation must be written by its parent process before that isolation is
    applied. This function performs local queue I/O only; a Gateway uploader
    sends the event later through the normal batched Growth endpoint.
    """

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.telemetry.growth_sink import GrowthEventSink
    from opensquilla.telemetry.runtime import ScopedTelemetryRuntime

    if coding_mode_active is False:
        return False
    config = GatewayConfig.load(
        config_path
        or os.environ.get(CODING_MODE_CONFIG_PATH_ENV)
        or os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"),
        read_only=True,
    )
    if coding_mode_active is None:
        skills_config = getattr(config, "skills", None)
        if not bool(getattr(skills_config, "coding_mode", False)):
            return False
    runtime = ScopedTelemetryRuntime(config=config)
    sink = GrowthEventSink(runtime, config=config)
    try:
        return await sink.record_coding_mode_usage(
            run_id,
            occurred_at or datetime.now(UTC),
        )
    finally:
        await sink.close()
        await runtime.close()


def observe_current_profile_coding_mode_usage(run_id: str) -> None:
    """Durably enqueue one actual use without performing network I/O.

    The callback runs only after the coding Agent process exists. Capturing the
    timestamp and the effective Coding Mode gate here keeps the event on the
    correct UTC day and avoids a later config-toggle race. Local persistence is
    completed before returning so normal CLI exit cannot discard the record.
    """

    if not isinstance(run_id, str) or not run_id:
        return

    try:
        occurred_at = datetime.now(UTC)
        asyncio.run(
            record_current_profile_coding_mode_usage(
                run_id,
                occurred_at=occurred_at,
                coding_mode_active=_coding_mode_snapshot_from_env(),
                config_path=os.environ.get(CODING_MODE_CONFIG_PATH_ENV),
            )
        )
    except Exception:
        # Telemetry remains non-load-bearing: a queue/config failure never
        # changes the already-started coding task.
        log.debug("coding mode usage enqueue failed", exc_info=True)


__all__ = [
    "CODING_MODE_ACTIVE_ENV",
    "CODING_MODE_CONFIG_PATH_ENV",
    "observe_current_profile_coding_mode_usage",
    "record_current_profile_coding_mode_usage",
]
