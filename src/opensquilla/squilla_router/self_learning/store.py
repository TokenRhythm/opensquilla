"""Per-agent event store for router self-learning samples.

Layout (all under the user-level OpenSquilla home, never the repo, never the
decision log)::

    ~/.opensquilla/router/
        data/<agent_id>/samples-YYYYMMDD.jsonl   # captured turns
        data/<agent_id>/.train_cursor            # last consumed ts (offline)
        learned/<version>/                       # candidate model bundles
        active                                   # baseline | learned/<version>
        .receipts/                               # train/promote/rollback receipts

Writes are append-only JSON lines and best-effort: callers wrap them so a turn
never fails because capture failed.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from opensquilla.paths import default_opensquilla_home
from opensquilla.squilla_router.self_learning.schema import RouterTrainSample

# One env switch disables both capture and training (mirrors the Dream switch
# OPENSQUILLA_MEMORY_DREAM_DISABLED).
ENV_DISABLE = "OPENSQUILLA_ROUTER_SELFLEARN_DISABLED"

_SAFE_AGENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_TRUTHY = {"1", "true", "yes", "on"}


def self_learning_disabled_by_env() -> bool:
    """Return True when the global kill-switch env var is set truthy."""

    return os.environ.get(ENV_DISABLE, "").strip().lower() in _TRUTHY


def _safe_agent_id(agent_id: str) -> str:
    cleaned = _SAFE_AGENT_RE.sub("_", (agent_id or "default").strip()) or "default"
    # A pure-dot segment ("." / "..") would escape the data root; everything
    # else is a harmless single path segment (no separators survive the regex).
    if set(cleaned) <= {"."}:
        cleaned = "default"
    return cleaned[:128]


def router_data_root(home: Path | None = None) -> Path:
    """Resolve the single root holding all self-learning artifacts."""

    base = home or default_opensquilla_home()
    return base / "router"


def agent_data_dir(agent_id: str, home: Path | None = None) -> Path:
    """Resolve the per-agent captured-sample directory."""

    return router_data_root(home) / "data" / _safe_agent_id(agent_id)


def _samples_path(agent_id: str, day: str, home: Path | None = None) -> Path:
    return agent_data_dir(agent_id, home) / f"samples-{day}.jsonl"


def write_sample(
    sample: RouterTrainSample,
    agent_id: str,
    *,
    home: Path | None = None,
) -> Path:
    """Append one sample as a JSON line; return the file written to."""

    day = datetime.now(UTC).strftime("%Y%m%d")
    path = _samples_path(agent_id, day, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(sample.to_json_dict(), ensure_ascii=False) + "\n")
    return path


def iter_samples(
    agent_id: str,
    *,
    since_ts: str | None = None,
    home: Path | None = None,
) -> Iterator[RouterTrainSample]:
    """Yield captured samples for an agent in file/line order.

    ``since_ts`` filters to rows with ``ts > since_ts`` (the offline cursor).
    Malformed lines are skipped so a partial last write never aborts a read.
    """

    data_dir = agent_data_dir(agent_id, home)
    if not data_dir.is_dir():
        return
    for path in sorted(data_dir.glob("samples-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since_ts is not None and str(payload.get("ts", "")) <= since_ts:
                continue
            yield RouterTrainSample.from_json_dict(payload)


def cursor_path(agent_id: str, home: Path | None = None) -> Path:
    return agent_data_dir(agent_id, home) / ".train_cursor"


_SAMPLES_DAY_RE = re.compile(r"^samples-(\d{8})\.jsonl$")


def prune_expired_samples(
    agent_id: str,
    retention_days: int,
    *,
    home: Path | None = None,
    now: datetime | None = None,
) -> int:
    """Delete ``samples-YYYYMMDD.jsonl`` files older than ``retention_days``.

    Enforces the ``self_learning.retention_days`` knob so captured router
    samples (including redacted prompt summaries) do not accumulate forever.
    Returns the number of files removed. Best-effort per file: a locked/absent
    file cannot abort a turn or training pass. Never touches ``.train_cursor``
    or state files.
    """
    if retention_days <= 0:
        return 0
    data_dir = agent_data_dir(agent_id, home)
    if not data_dir.is_dir():
        return 0
    current = (now or datetime.now(UTC)).date()
    removed = 0
    for path in data_dir.glob("samples-*.jsonl"):
        match = _SAMPLES_DAY_RE.match(path.name)
        if match is None:
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if (current - day).days > retention_days:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def read_cursor(agent_id: str, home: Path | None = None) -> str | None:
    path = cursor_path(agent_id, home)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def write_cursor(agent_id: str, ts: str, home: Path | None = None) -> None:
    path = cursor_path(agent_id, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ts, encoding="utf-8")
