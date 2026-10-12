"""Decision-log aggregation primitives.

Pure functions over ``~/.opensquilla/logs/decisions-*.jsonl``: skill
co-occurrence counts, meta-skill invocation counts. Lifted out of
``skills/bundled/history-explorer/scripts/explore.py`` so that both
the bundled history-explorer script (a subprocess entrypoint) and
in-tree callers (e.g. ``skills.creator.auto_propose``) can share the
exact same logic — duplicating the aggregation in two places would
inevitably drift.

These functions read directly from disk and never mutate; they are
safe to call concurrently from a cron handler, a dream hook, or a
plain CLI invocation.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path


def parse_log_line(line: str) -> dict | None:
    """Parse one JSONL line into a dict; return None for blank/malformed."""
    line = line.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def within_window(ts_str: str, cutoff: datetime) -> bool:
    """True iff the ISO timestamp string is at or after ``cutoff``."""
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return False
    return ts >= cutoff


def aggregate_co_occurrences(
    log_dir: Path, window_days: int, top_k: int,
) -> list[dict]:
    """Return top-K most-frequent skill co-occurrence chains in the window.

    A "chain" is the exact tuple of ``skills_invoked`` from a single
    DecisionEntry — order preserved, only chains of length ≥ 2 are
    counted. Each returned dict has shape::

        {"skills": [str, ...], "freq": int}

    Returns ``[]`` when the log dir does not exist (fresh install path).
    """
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    counter: Counter[tuple[str, ...]] = Counter()
    intents: dict[tuple[str, ...], Counter[str]] = {}
    if not log_dir.is_dir():
        return []
    for log_path in sorted(log_dir.glob("decisions-*.jsonl")):
        for raw in log_path.read_text(encoding="utf-8").splitlines():
            payload = parse_log_line(raw)
            if not payload:
                continue
            if not within_window(payload.get("ts", ""), cutoff):
                continue
            skills = payload.get("skills_invoked") or []
            if not isinstance(skills, list) or len(skills) < 2:
                continue
            key = tuple(skills)
            counter[key] += 1
            intent = str(
                payload.get("intent_summary")
                or payload.get("session_intent")
                or payload.get("user_intent")
                or payload.get("user_message")
                or payload.get("prompt")
                or payload.get("message")
                or "",
            ).strip()
            if intent:
                intents.setdefault(key, Counter())[intent[:500]] += 1
    return [
        {
            "skills": list(combo),
            "freq": freq,
            "sample_intents": [
                intent for intent, _count in intents.get(combo, Counter()).most_common(3)
            ],
        }
        for combo, freq in counter.most_common(top_k)
    ]


def aggregate_meta_usage(
    log_dir: Path,
    window_days: int,
    meta_names: set[str] | None = None,
) -> list[dict]:
    """Count how often each kind=meta skill was invoked.

    Args:
        log_dir: decision-log directory containing decisions-*.jsonl files.
        window_days: time window for inclusion.
        meta_names: set of skill names where kind == "meta". When None,
            falls back to the name-prefix heuristic (skill.startswith("meta-")).
            The heuristic is less accurate because helper bundles like
            skill-creator-linter / skill-creator-proposals / skill-creator-smoke-test
            are kind=skill but share the prefix (N12 fix).
    """
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    counter: Counter[str] = Counter()
    if not log_dir.is_dir():
        return []
    for log_path in sorted(log_dir.glob("decisions-*.jsonl")):
        for raw in log_path.read_text(encoding="utf-8").splitlines():
            payload = parse_log_line(raw)
            if not payload or not within_window(payload.get("ts", ""), cutoff):
                continue
            for skill in payload.get("skills_invoked") or []:
                if not isinstance(skill, str):
                    continue
                if meta_names is not None:
                    if skill in meta_names:
                        counter[skill] += 1
                elif skill.startswith("meta-"):
                    counter[skill] += 1
    return [
        {"meta_skill_id": name, "invocation_count": ct}
        for name, ct in counter.most_common()
    ]


__all__ = [
    "parse_log_line",
    "within_window",
    "aggregate_co_occurrences",
    "aggregate_meta_usage",
]
