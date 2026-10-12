"""Promotion evidence store for Dream."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from opensquilla.memory.dream.models import (
    PromotionEvidenceEntry,
    PromotionEvidenceStore,
    RawDreamCandidate,
)


def promotion_evidence_path(workspace: Path) -> Path:
    return workspace / "memory" / ".dream_state" / "promotion_evidence.json"


def _normalize_snippet(text: str) -> str:
    return " ".join(text.strip().split())


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _candidate_id(candidate: RawDreamCandidate) -> str:
    claim_sha = candidate.claim_sha256 or _sha256(_normalize_snippet(candidate.snippet).lower())
    normalized = "\n".join(
        [
            candidate.agent_id,
            claim_sha,
        ]
    )
    return _sha256(normalized)


def _entry_from_dict(raw: dict[str, Any]) -> PromotionEvidenceEntry | None:
    try:
        return PromotionEvidenceEntry(
            candidate_id=str(raw["candidate_id"]),
            agent_id=str(raw.get("agent_id") or "main"),
            source_path=str(raw["source_path"]),
            source_kind=str(raw.get("source_kind") or "memory_file"),
            source_mtime_ns=int(raw.get("source_mtime_ns") or 0),
            source_size=int(raw.get("source_size") or 0),
            snippet=str(raw.get("snippet") or ""),
            snippet_sha256=str(raw.get("snippet_sha256") or ""),
            claim_sha256=str(raw.get("claim_sha256") or ""),
            first_seen_at=str(raw.get("first_seen_at") or ""),
            last_seen_at=str(raw.get("last_seen_at") or ""),
            seen_count=max(0, int(raw.get("seen_count") or 0)),
            positive_signal_count=max(0, int(raw.get("positive_signal_count") or 0)),
            correction_signal_count=max(0, int(raw.get("correction_signal_count") or 0)),
            failure_signal_count=max(0, int(raw.get("failure_signal_count") or 0)),
            manual_signal_count=max(0, int(raw.get("manual_signal_count") or 0)),
            source_days=[
                str(day)
                for day in raw.get("source_days", [])
                if isinstance(day, str) and day
            ],
            status=str(raw.get("status") or "candidate"),
            promoted_at=raw.get("promoted_at") if isinstance(raw.get("promoted_at"), str) else None,
            rejected_at=raw.get("rejected_at") if isinstance(raw.get("rejected_at"), str) else None,
            last_skip_reason=(
                raw.get("last_skip_reason")
                if isinstance(raw.get("last_skip_reason"), str)
                else None
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_evidence_store(workspace: Path) -> PromotionEvidenceStore:
    path = promotion_evidence_path(workspace)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return PromotionEvidenceStore(version=1, entries={})
    if not isinstance(raw, dict):
        return PromotionEvidenceStore(version=1, entries={})
    entries_raw = raw.get("entries")
    entries: dict[str, PromotionEvidenceEntry] = {}
    if isinstance(entries_raw, dict):
        for key, value in entries_raw.items():
            if not isinstance(value, dict):
                continue
            entry = _entry_from_dict(value)
            if entry is not None:
                entries[str(key)] = entry
    return PromotionEvidenceStore(
        version=1,
        updated_at=str(raw.get("updated_at") or ""),
        entries=entries,
    )


def write_evidence_store(workspace: Path, store: PromotionEvidenceStore) -> None:
    path = promotion_evidence_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": store.version,
        "updated_at": store.updated_at,
        "entries": {key: asdict(entry) for key, entry in store.entries.items()},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _increment_signal(entry: PromotionEvidenceEntry, signal_kind: str) -> None:
    if signal_kind == "positive":
        entry.positive_signal_count += 1
    elif signal_kind == "correction":
        entry.correction_signal_count += 1
    elif signal_kind == "failure":
        entry.failure_signal_count += 1
    elif signal_kind == "manual":
        entry.manual_signal_count += 1


def update_promotion_evidence(
    workspace: Path,
    candidates: list[RawDreamCandidate],
    *,
    now_iso: str,
    persist: bool = True,
) -> PromotionEvidenceStore:
    store = load_evidence_store(workspace)
    for candidate in candidates:
        snippet = candidate.snippet.strip()
        if not snippet:
            continue
        snippet_sha = candidate.snippet_sha256 or _sha256(snippet)
        claim_sha = candidate.claim_sha256 or _sha256(_normalize_snippet(snippet).lower())
        candidate_id = _candidate_id(
            RawDreamCandidate(
                **{
                    **candidate.__dict__,
                    "snippet": snippet,
                    "snippet_sha256": snippet_sha,
                    "claim_sha256": claim_sha,
                }
            )
        )
        entry = store.entries.get(candidate_id)
        if entry is None:
            entry = PromotionEvidenceEntry(
                candidate_id=candidate_id,
                agent_id=candidate.agent_id,
                source_path=candidate.source_path,
                source_kind=candidate.source_kind,
                source_mtime_ns=candidate.source_mtime_ns,
                source_size=candidate.source_size,
                snippet=snippet,
                snippet_sha256=snippet_sha,
                claim_sha256=claim_sha,
                first_seen_at=now_iso,
                last_seen_at=now_iso,
                seen_count=0,
                source_days=[],
            )
            store.entries[candidate_id] = entry
        entry.last_seen_at = now_iso
        entry.source_path = candidate.source_path
        entry.source_kind = candidate.source_kind
        entry.source_mtime_ns = candidate.source_mtime_ns
        entry.source_size = candidate.source_size
        entry.snippet = snippet
        entry.snippet_sha256 = snippet_sha
        entry.claim_sha256 = claim_sha
        entry.seen_count += 1
        if candidate.source_day and candidate.source_day not in entry.source_days:
            entry.source_days.append(candidate.source_day)
        _increment_signal(entry, candidate.signal_kind)
    store.updated_at = now_iso
    if persist:
        write_evidence_store(workspace, store)
    return store


def mark_evidence_promoted(
    store: PromotionEvidenceStore, candidate_ids: list[str], now_iso: str
) -> None:
    for candidate_id in candidate_ids:
        entry = store.entries.get(candidate_id)
        if entry is not None:
            entry.status = "promoted"
            entry.promoted_at = now_iso
            entry.last_skip_reason = None


def mark_evidence_skipped(
    store: PromotionEvidenceStore, candidate_id: str, reason: str
) -> None:
    entry = store.entries.get(candidate_id)
    if entry is not None:
        entry.last_skip_reason = reason


def mark_evidence_represented(
    store: PromotionEvidenceStore, candidate_ids: list[str], reason: str
) -> None:
    for candidate_id in candidate_ids:
        entry = store.entries.get(candidate_id)
        if entry is not None:
            entry.status = "represented"
            entry.last_skip_reason = reason
