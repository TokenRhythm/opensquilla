"""Generated-artifact quarantine rules for Dream."""

from __future__ import annotations

from pathlib import PurePosixPath


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def is_quarantined_path(path: str) -> bool:
    normalized = _normalize_path(path)
    if normalized == "memory/.dream_cursor":
        return True
    if normalized.startswith("memory/.dream"):
        return True
    if normalized == "logs" or normalized.startswith("logs/"):
        return True
    name = PurePosixPath(normalized).name
    return name.startswith("dream-") and name.endswith(".jsonl")


def is_quarantined_text(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "opensquilla-dream-promotion:",
        "dream receipt",
    )
    return any(marker in lowered for marker in markers)
