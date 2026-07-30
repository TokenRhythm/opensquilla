"""Candidate scanning and lightweight signal classification for Dream."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

from opensquilla.memory.dream.models import RawDreamCandidate
from opensquilla.memory.dream.quarantine import is_quarantined_path, is_quarantined_text

_SNIPPET_MAX_CHARS = 4000


@dataclass(frozen=True)
class DreamCandidateScan:
    """Candidates plus enough scan state to advance the Dream cursor safely."""

    candidates: list[RawDreamCandidate]
    files_considered: int
    files_skipped_unchanged: int
    cursor_high_watermark: float
    cursor_high_watermark_ns: int = 0
    cursor_high_watermark_path: str = ""
    transient_failures: int = 0


def _workspace_relative(workspace: Path, path: Path) -> str:
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return path.as_posix()


def _normalize_snippet(text: str) -> str:
    return " ".join(text.strip().split())


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_day(path: Path) -> str | None:
    stem = path.stem
    if len(stem) >= 10 and stem[4:5] == "-" and stem[7:8] == "-":
        candidate = stem[:10]
        if all(part.isdigit() for part in candidate.split("-")):
            return candidate
    return None


def classify_signal(text: str) -> str:
    lowered = text.lower()
    if "memory:" in lowered or "remember that" in lowered:
        return "manual"
    if any(marker in lowered for marker in ("do not", "don't", "rejected", "wrong", "instead")):
        return "correction"
    if any(
        marker in lowered
        for marker in ("failed", "error", "exception", "traceback", "rollback")
    ):
        return "failure"
    if any(
        marker in lowered
        for marker in ("prefers", "accepted", "successful", "works", "use ")
    ):
        return "positive"
    return "neutral"


def scan_dream_candidate_batch(
    workspace: Path,
    *,
    cursor: float,
    max_batch_size: int,
    agent_id: str,
    quarantine_enabled: bool = True,
    known_observations: set[tuple[str, str]] | None = None,
    cursor_position: tuple[int, str] | None = None,
) -> DreamCandidateScan:
    memory_dir = workspace / "memory"
    if not memory_dir.exists():
        return DreamCandidateScan([], 0, 0, cursor)
    candidates: list[tuple[int, str, RawDreamCandidate]] = []
    unchanged_positions: list[tuple[int, str]] = []
    transient_failures = 0
    cursor_blocking_failures = 0
    resolved_memory_dir = memory_dir.resolve()
    for path in memory_dir.rglob("*.md"):
        try:
            if not path.is_file():
                continue
            path.resolve().relative_to(resolved_memory_dir)
            stat = path.stat()
        except (OSError, ValueError):
            transient_failures += 1
            continue
        rel_path = _workspace_relative(workspace, path)
        if (
            any(part.startswith(".") for part in path.relative_to(memory_dir).parts)
            or path.name == "MEMORY.md"
            or path.suffix.lower() != ".md"
        ):
            continue
        position = (stat.st_mtime_ns, rel_path)
        if cursor_position is not None:
            if position <= cursor_position:
                continue
        elif stat.st_mtime <= cursor:
            continue
        if quarantine_enabled and is_quarantined_path(rel_path):
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            transient_failures += 1
            cursor_blocking_failures += 1
            continue
        if quarantine_enabled and is_quarantined_text(raw):
            continue
        normalized_content = _normalize_snippet(raw)
        content_sha = _sha256(raw)
        snippet = normalized_content
        if len(snippet) > _SNIPPET_MAX_CHARS:
            head_size = _SNIPPET_MAX_CHARS * 3 // 4
            tail_size = _SNIPPET_MAX_CHARS - head_size
            snippet = (
                snippet[:head_size].rstrip()
                + "\n[…]\n"
                + snippet[-tail_size:].lstrip()
            )
        if not snippet:
            continue
        # D10: a touch-only rewrite of the same file is not a new observation.
        # Identical content in another file remains independent recurrence
        # evidence for source diversity and source-day frequency.
        snippet_sha = _sha256(snippet)
        if known_observations and (
            (rel_path, content_sha) in known_observations
            or (rel_path, snippet_sha) in known_observations
        ):
            unchanged_positions.append(position)
            continue
        candidates.append(
            (
                stat.st_mtime_ns,
                rel_path,
                RawDreamCandidate(
                    agent_id=agent_id,
                    source_path=rel_path,
                    source_kind="memory_file",
                    source_mtime_ns=stat.st_mtime_ns,
                    source_size=stat.st_size,
                    snippet=snippet,
                    snippet_sha256=snippet_sha,
                    claim_sha256=_sha256(_normalize_snippet(snippet).lower()),
                    source_day=_source_day(path),
                    signal_kind=classify_signal(snippet),
                    content_sha256=content_sha,
                ),
            )
        )
    candidates.sort(key=lambda item: (item[0], item[1]))
    selected = candidates[: max(0, int(max_batch_size))]

    starting_position = cursor_position or (int(cursor * 1_000_000_000), "")
    eligible_positions = [
        *(position[:2] for position in selected),
        *(unchanged_positions if len(selected) == len(candidates) else ()),
    ]
    high_position = max(eligible_positions, default=starting_position)
    # A temporarily unreadable source is epistemically different from an
    # unchanged source. Keep the cursor in place so a later source cannot make
    # the failed one permanently unreachable. Already-seen later sources are
    # cheap on the next scan because content-hash dedup suppresses them.
    if cursor_blocking_failures:
        high_position = starting_position
    high_watermark = high_position[0] / 1_000_000_000
    if (
        not cursor_blocking_failures
        and cursor_position is None
        and len(selected) < len(candidates)
    ):
        first_deferred_seconds = candidates[len(selected)][0] / 1_000_000_000
        high_watermark = max(cursor, math.nextafter(first_deferred_seconds, -math.inf))

    return DreamCandidateScan(
        candidates=[candidate for _mtime_ns, _path, candidate in selected],
        files_considered=len(candidates) + len(unchanged_positions),
        files_skipped_unchanged=len(unchanged_positions),
        cursor_high_watermark=high_watermark,
        cursor_high_watermark_ns=high_position[0],
        cursor_high_watermark_path=high_position[1],
        transient_failures=transient_failures,
    )


def scan_dream_candidates(
    workspace: Path,
    *,
    cursor: float,
    max_batch_size: int,
    agent_id: str,
    quarantine_enabled: bool = True,
    known_observations: set[tuple[str, str]] | None = None,
    cursor_position: tuple[int, str] | None = None,
) -> list[RawDreamCandidate]:
    """Backward-compatible candidate-only view of a Dream scan."""
    return scan_dream_candidate_batch(
        workspace,
        cursor=cursor,
        max_batch_size=max_batch_size,
        agent_id=agent_id,
        quarantine_enabled=quarantine_enabled,
        known_observations=known_observations,
        cursor_position=cursor_position,
    ).candidates
