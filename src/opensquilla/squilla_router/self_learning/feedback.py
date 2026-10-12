"""Explicit user feedback (thumbs up/down) sidecar for the self-learning loop.

Feedback lives in its own append-only JSONL next to (not inside) the captured
samples: a sample row is written once when the turn ends, while a rating can
arrive minutes or days later and be revised (last-write-wins) or revoked
(``neutral``). The offline trainer joins the two streams by ``decision_id``
at dataset-build time (samples carry the same V017 id).

Every row also carries the ``executed_kind`` of the decision it rates
(``single`` | ``ensemble``). The distinction is load-bearing downstream: an
ensemble turn's rating judges the whole candidates-plus-aggregator chain, so
alignment must not convert it into a tier-upgrade label, and the rollback
monitor must not attribute it to the routing classifier.

Same conventions as ``store.py``: pure functions, injectable ``home``, no
raw prompt text, best-effort retention pruning at write time.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from opensquilla.squilla_router.self_learning.store import agent_data_dir

FEEDBACK_SCHEMA_VERSION = 1
FEEDBACK_FILENAME = "feedback.jsonl"

RATINGS = frozenset({"up", "down", "neutral"})
EXECUTED_KINDS = frozenset({"single", "ensemble"})


@dataclass(frozen=True)
class FeedbackEntry:
    """The effective (post-merge) rating for one routing decision."""

    rating: str  # "up" | "down"
    executed_kind: str  # "single" | "ensemble"


@dataclass(frozen=True)
class FeedbackStats:
    """Aggregate counts for gates, rollback monitoring, and the status RPC."""

    total: int = 0
    up: int = 0
    down: int = 0
    # Single-model slice. The rollback monitor uses only this: an ensemble
    # rating co-varies with candidate/aggregator changes, not the promoted
    # classifier, and must not trip a classifier revert.
    total_single: int = 0
    down_single: int = 0

    @property
    def downvote_rate(self) -> float:
        """Single-model down-vote rate — numerator AND denominator sliced."""
        return (self.down_single / self.total_single) if self.total_single else 0.0


def feedback_path(agent_id: str, home: Path | None = None) -> Path:
    return agent_data_dir(agent_id, home) / FEEDBACK_FILENAME


# Serializes append + prune within the gateway process (RPC submissions run on
# worker threads). Without it a prune's read-rewrite-replace can clobber a
# rating another thread appended in between — and a lost rating is a defect.
_write_lock = threading.Lock()


def write_feedback(
    agent_id: str,
    *,
    decision_id: str,
    session_key: str,
    turn_index: int,
    rating: str,
    executed_kind: str = "single",
    decision_ts: str | None = None,
    home: Path | None = None,
    now: datetime | None = None,
    retention_days: int = 30,
) -> Path:
    """Append one rating row. Revisions append; readers merge last-write-wins.

    ``neutral`` is a revocation: it is stored (audit trail) and drops the
    decision from the effective map on read. ``decision_ts`` is when the rated
    decision itself was made — the rollback monitor windows on it so a rating
    of an old model's turn never counts against a newly promoted classifier.
    """

    if rating not in RATINGS:
        raise ValueError(f"rating must be one of {sorted(RATINGS)}")
    kind = executed_kind if executed_kind in EXECUTED_KINDS else "single"
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {
        "decision_id": decision_id,
        "session_key": session_key,
        "turn_index": int(turn_index),
        "rating": rating,
        "executed_kind": kind,
        "ts": stamp,
        "decision_ts": decision_ts or stamp,
        "schema_version": FEEDBACK_SCHEMA_VERSION,
    }
    path = feedback_path(agent_id, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        _prune_expired(path, now=now, retention_days=retention_days)
    return path


def _iter_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _merged_by_decision(path: Path) -> dict[str, dict]:
    """File-order merge: the last row per decision_id is the effective rating."""

    merged: dict[str, dict] = {}
    for row in _iter_rows(path):
        decision_id = row.get("decision_id")
        if isinstance(decision_id, str) and decision_id:
            merged[decision_id] = row
    return merged


def load_feedback_map(
    agent_id: str, home: Path | None = None
) -> dict[str, FeedbackEntry]:
    """Effective feedback keyed by ``decision_id`` for the exact sample join.

    Samples carry the same V017 decision id (``RouterTrainSample.decision_id``),
    so the join is one-to-one by construction. ``(session_key, turn_index)``
    is deliberately NOT the key: the history-derived turn index saturates at
    the routing-history cap and resets after idle windows, so it collides
    within long sessions and would fan one rating out to unrelated samples.
    Last write per decision wins; ``neutral`` (revoked) entries are dropped.
    """

    out: dict[str, FeedbackEntry] = {}
    for decision_id, row in _merged_by_decision(feedback_path(agent_id, home)).items():
        rating = row.get("rating")
        if rating not in ("up", "down"):
            continue
        kind = row.get("executed_kind")
        out[decision_id] = FeedbackEntry(
            rating=str(rating),
            executed_kind=kind if kind in EXECUTED_KINDS else "single",
        )
    return out


def scan_feedback_stats(
    agent_id: str,
    *,
    since_ts: str | None = None,
    home: Path | None = None,
) -> FeedbackStats:
    """Aggregate effective ratings, optionally restricted to a decision window.

    ``since_ts`` compares the **decision's own timestamp** (``decision_ts``,
    falling back to the rating ts for legacy rows): the rollback monitor asks
    "how are decisions made *after* the promotion being judged", and a rating
    of an old model's turn — however recent — must not count against the newly
    promoted classifier.
    """

    total = up = down = total_single = down_single = 0
    for row in _merged_by_decision(feedback_path(agent_id, home)).values():
        rating = row.get("rating")
        if rating not in ("up", "down"):
            continue
        window_ts = str(row.get("decision_ts") or row.get("ts", ""))
        if since_ts is not None and window_ts <= since_ts:
            continue
        total += 1
        is_single = row.get("executed_kind") != "ensemble"
        if is_single:
            total_single += 1
        if rating == "up":
            up += 1
        else:
            down += 1
            if is_single:
                down_single += 1
    return FeedbackStats(
        total=total, up=up, down=down, total_single=total_single, down_single=down_single
    )


def _prune_expired(
    path: Path, *, now: datetime | None = None, retention_days: int = 30
) -> None:
    """Opportunistic write-time pruning, mirroring the decision writer.

    Rewrites the file without rows older than the retention window. Best
    effort: any failure leaves the file as-is (a long file is a nuisance,
    a lost rating is a defect).
    """

    if retention_days <= 0:
        return
    try:
        cutoff = ((now or datetime.now(UTC)) - timedelta(days=retention_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rows = _iter_rows(path)
        # Retention keys on the RATING's own timestamp: the user acted
        # recently, so the row is alive — even when the decision it rates is
        # older than the window (the monitor separately windows on
        # decision_ts and will simply not count it).
        kept = [r for r in rows if str(r.get("ts", "")) > cutoff]
        if len(kept) == len(rows):
            return
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for row in kept:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(path)
    except OSError:
        pass


def prune_expired_feedback(
    agent_id: str,
    retention_days: int,
    *,
    home: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Apply configured retention even when no new rating is submitted."""

    with _write_lock:
        _prune_expired(
            feedback_path(agent_id, home),
            now=now,
            retention_days=retention_days,
        )


__all__ = [
    "EXECUTED_KINDS",
    "FEEDBACK_FILENAME",
    "FEEDBACK_SCHEMA_VERSION",
    "RATINGS",
    "FeedbackEntry",
    "FeedbackStats",
    "feedback_path",
    "load_feedback_map",
    "prune_expired_feedback",
    "scan_feedback_stats",
    "write_feedback",
]
