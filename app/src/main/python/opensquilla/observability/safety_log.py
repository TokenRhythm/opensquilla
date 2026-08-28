"""Independent safety-event log stream.

Safety events are written to ``~/.opensquilla/logs/safety-YYYYMMDD.jsonl`` — a
separate file from the decision log. Schemas never merge: a reader of one
file does not need to parse the other.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from opensquilla.paths import default_opensquilla_home


class SafetyEventType(StrEnum):
    """Closed enum of safety-event categories."""

    REFUSED_TOOL = "refused_tool"
    TRUNCATED_OUTPUT = "truncated_output"
    RATE_LIMIT = "rate_limit"
    INJECTION_BLOCKED = "injection_blocked"
    TIER_DENIED = "tier_denied"
    SANDBOX_VIOLATION = "sandbox_violation"


@dataclass
class SafetyEvent:
    """One row in the safety-event log."""

    event_type: SafetyEventType
    session_id: str
    reason: str
    ts: str
    tool_name: str | None = None


def _default_log_dir() -> Path:
    """Resolve the safety-log directory (shares env override with decisions)."""

    return Path(os.environ.get("OPENSQUILLA_LOG_DIR", str(default_opensquilla_home() / "logs")))


def write_safety_event(
    event: SafetyEvent,
    log_dir: Path | None = None,
) -> Path:
    """Append ``event`` to the safety-log file; return the path written to."""

    log_dir = log_dir or _default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(UTC).strftime("%Y%m%d")
    path = log_dir / f"safety-{day}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
    return path
