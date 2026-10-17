"""Write-time source rehydration for Dream."""

from __future__ import annotations

import hashlib
from pathlib import Path

from opensquilla.memory.dream.models import PromotionCandidate, RehydrateResult
from opensquilla.memory.dream.quarantine import is_quarantined_path
from opensquilla.memory.source_paths import resolve_memory_source_path


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    return " ".join(text.strip().split())


def rehydrate_candidate(
    workspace: Path, candidate: PromotionCandidate, *, notes_dir: Path | None = None,
) -> RehydrateResult:
    source_rel = candidate.source_path.replace("\\", "/").lstrip("./")
    if is_quarantined_path(source_rel):
        return RehydrateResult(ok=False, reason="source_quarantined")
    try:
        source_path = resolve_memory_source_path(
            workspace, notes_dir if notes_dir is not None else workspace / "memory", source_rel,
        )
    except ValueError:
        return RehydrateResult(ok=False, reason="source_outside_workspace")
    try:
        raw = source_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return RehydrateResult(ok=False, reason="source_missing")
    except OSError:
        return RehydrateResult(ok=False, reason="source_unreadable")
    if _normalize_text(candidate.snippet) not in _normalize_text(raw):
        return RehydrateResult(ok=False, reason="snippet_missing")
    if _sha256(candidate.snippet) != candidate.snippet_sha256:
        return RehydrateResult(ok=False, reason="hash_mismatch")
    return RehydrateResult(ok=True)
