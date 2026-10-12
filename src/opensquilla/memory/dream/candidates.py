"""Candidate scanning and lightweight signal classification for Dream."""

from __future__ import annotations

import hashlib
from pathlib import Path

from opensquilla.memory.dream.models import RawDreamCandidate
from opensquilla.memory.dream.quarantine import is_quarantined_path, is_quarantined_text

_SNIPPET_MAX_CHARS = 4000


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


def scan_dream_candidates(
    workspace: Path,
    *,
    cursor: float,
    max_batch_size: int,
    agent_id: str,
    quarantine_enabled: bool = True,
) -> list[RawDreamCandidate]:
    memory_dir = workspace / "memory"
    if not memory_dir.exists():
        return []
    candidates: list[tuple[float, RawDreamCandidate]] = []
    for path in memory_dir.iterdir():
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except FileNotFoundError:
            continue
        if path.name.startswith(".") or path.name == "MEMORY.md" or path.suffix.lower() != ".md":
            continue
        if stat.st_mtime <= cursor:
            continue
        rel_path = _workspace_relative(workspace, path)
        if quarantine_enabled and is_quarantined_path(rel_path):
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if quarantine_enabled and is_quarantined_text(raw):
            continue
        snippet = _normalize_snippet(raw)
        if len(snippet) > _SNIPPET_MAX_CHARS:
            snippet = snippet[:_SNIPPET_MAX_CHARS].rstrip()
        if not snippet:
            continue
        candidates.append(
            (
                stat.st_mtime,
                RawDreamCandidate(
                    agent_id=agent_id,
                    source_path=rel_path,
                    source_kind="memory_file",
                    source_mtime_ns=stat.st_mtime_ns,
                    source_size=stat.st_size,
                    snippet=snippet,
                    snippet_sha256=_sha256(snippet),
                    claim_sha256=_sha256(_normalize_snippet(snippet).lower()),
                    source_day=_source_day(path),
                    signal_kind=classify_signal(snippet),
                ),
            )
        )
    candidates.sort(key=lambda item: item[0])
    return [candidate for _mtime, candidate in candidates[: max(0, int(max_batch_size))]]
