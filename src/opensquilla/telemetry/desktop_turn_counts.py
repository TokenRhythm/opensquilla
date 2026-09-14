"""Local terminal-turn counters shared with the Desktop session summary."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from opensquilla.profile_operation_lock import ProfileOperationLock
from opensquilla.telemetry.contracts import CURRENT_NOTICE_VERSION_BY_SCOPE
from opensquilla.telemetry.contracts.common import StrictTelemetryModel
from opensquilla.telemetry.desktop_state import (
    _fsync_directory_best_effort,
    _require_real_directory,
    desktop_early_spool_root,
)

SESSION_MARKER_NAME = ".desktop-reliability-session.tmp"
TURN_COUNTS_PREFIX = ".desktop-reliability-turns-"
MAX_COUNTER = 2**31 - 1
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def _read(path: Path) -> dict[str, Any] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 32 * 1024:
        raise ValueError("invalid desktop turn counter state")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("invalid desktop turn counter state")
    return value


def _counter(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_COUNTER


def record_desktop_turn(
    state_dir: Path,
    event: StrictTelemetryModel,
) -> None:
    """Count an accepted terminal observation under its caller's consent permit.

    The active marker belongs to Electron; only this separate checkpoint is
    Gateway-written. A new Gateway process resumes the same totals, while a
    new Desktop session gets an independent file. No content or turn IDs are
    retained. Queue replay never calls this function for duplicate events.
    """

    if getattr(event, "event_name", None) != "turn_result":
        return
    occurred_at = getattr(event, "occurred_at_utc", None)
    stall_count = getattr(event, "stall_count", None)
    if not isinstance(occurred_at, datetime) or not _counter(stall_count):
        return
    assert stall_count is not None
    observed_at_ms = int(occurred_at.timestamp() * 1000)
    directory = desktop_early_spool_root(state_dir) / "reliability"
    # Do not create a Desktop profile or follow a redirected telemetry scope.
    for path in (directory.parent.parent, directory.parent, directory):
        _require_real_directory(path)
    marker_path = directory / SESSION_MARKER_NAME
    with ProfileOperationLock(marker_path, timeout=0.05):
        marker = _read(marker_path)
        mirror = _read(directory.parent.parent / "desktop-consent-mirror.json")
        consent = mirror.get("reliability") if mirror is not None else None
        if (
            marker is None
            or not isinstance(consent, dict)
            or consent.get("enabled") is not True
            or consent.get("forced_off") is not False
            or consent.get("notice_version") != CURRENT_NOTICE_VERSION_BY_SCOPE["reliability"]
            or not isinstance(consent.get("consented_at_utc"), str)
            or marker.get("consent_generation")
            != (f"{consent['notice_version']}\n{consent['consented_at_utc']}")
            or marker.get("marker_kind") != "desktop_reliability_session"
            or marker.get("clean_exit") is not False
            or marker.get("performance_summary_emitted") is not False
            or marker.get("gateway_turn_counts_applied") is True
            or not isinstance(marker.get("app_session_id"), str)
            or _UUID_RE.fullmatch(str(marker["app_session_id"])) is None
            or type(marker.get("started_at_ms")) is not int
            or observed_at_ms < int(marker["started_at_ms"])
        ):
            return
        app_session_id = str(marker["app_session_id"])
        target = directory / f"{TURN_COUNTS_PREFIX}{app_session_id}.tmp"
        counts = _read(target)
        if counts is None:
            # One bounded checkpoint consumes one existing spool quota slot.
            if sum(1 for _ in directory.iterdir()) >= 512:
                return
            counts = {
                "schema_version": 1,
                "app_session_id": app_session_id,
                "turn_count": 0,
                "stalled_turn_count": 0,
                "stall_count": 0,
                "last_observed_at_ms": observed_at_ms,
            }
        if (
            set(counts)
            != {
                "schema_version",
                "app_session_id",
                "turn_count",
                "stalled_turn_count",
                "stall_count",
                "last_observed_at_ms",
            }
            or counts["schema_version"] != 1
            or counts["app_session_id"] != app_session_id
            or not all(
                _counter(counts[key])
                for key in (
                    "turn_count",
                    "stalled_turn_count",
                    "stall_count",
                )
            )
            or type(counts["last_observed_at_ms"]) is not int
            or counts["stalled_turn_count"] > counts["turn_count"]
            or counts["stalled_turn_count"] > counts["stall_count"]
        ):
            raise ValueError("invalid desktop turn counter state")
        counts["turn_count"] = min(MAX_COUNTER, int(counts["turn_count"]) + 1)
        counts["stalled_turn_count"] = min(
            MAX_COUNTER,
            int(counts["stalled_turn_count"]) + int(stall_count > 0),
        )
        counts["stall_count"] = min(MAX_COUNTER, int(counts["stall_count"]) + stall_count)
        counts["last_observed_at_ms"] = max(
            observed_at_ms,
            int(counts["last_observed_at_ms"]),
        )
        descriptor, temporary = tempfile.mkstemp(
            prefix=".desktop-turn-write-", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(counts, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            # Withdrawal detaches the whole scope; never recreate its old path.
            _require_real_directory(directory)
            latest = _read(marker_path)
            if latest is None or any(
                latest.get(key) != marker.get(key)
                for key in (
                    "app_session_id",
                    "consent_generation",
                    "started_at_ms",
                    "clean_exit",
                    "performance_summary_emitted",
                    "gateway_turn_counts_applied",
                )
            ):
                return
            os.replace(temporary, target)
            _fsync_directory_best_effort(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
