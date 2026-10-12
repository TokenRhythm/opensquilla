"""Structured runtime event sink for observe-only agent diagnostics."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def append_runtime_event(path: str | None, event: dict[str, Any]) -> None:
    """Append one JSON event without affecting agent control flow."""

    if not path:
        return
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(event)
        now = datetime.now(UTC).isoformat()
        payload.setdefault("created_at", now)
        payload.setdefault("timestamp", now)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    except OSError:
        return
