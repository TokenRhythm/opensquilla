"""Shared Dream data models."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawDreamCandidate:
    agent_id: str
    source_path: str
    source_kind: str
    source_mtime_ns: int
    source_size: int
    snippet: str
    snippet_sha256: str
    claim_sha256: str
    source_day: str | None = None
    signal_kind: str = "neutral"


@dataclass
class PromotionEvidenceEntry:
    candidate_id: str
    agent_id: str
    source_path: str
    source_kind: str
    source_mtime_ns: int
    source_size: int
    snippet: str
    snippet_sha256: str
    claim_sha256: str
    first_seen_at: str
    last_seen_at: str
    seen_count: int = 0
    positive_signal_count: int = 0
    correction_signal_count: int = 0
    failure_signal_count: int = 0
    manual_signal_count: int = 0
    source_days: list[str] = field(default_factory=list)
    status: str = "candidate"
    promoted_at: str | None = None
    rejected_at: str | None = None
    last_skip_reason: str | None = None


@dataclass
class PromotionEvidenceStore:
    version: int = 1
    updated_at: str = ""
    entries: dict[str, PromotionEvidenceEntry] = field(default_factory=dict)


@dataclass
class PromotionCandidate:
    candidate_id: str
    source_path: str
    snippet: str
    snippet_sha256: str
    claim_sha256: str
    score: float
    reasons: list[str]
    signal_counts: dict[str, int]


@dataclass
class PromotionPatchOperation:
    op: str
    candidate_ids: list[str] = field(default_factory=list)
    section: str = ""
    memory_id: str = ""
    text: str = ""
    replaces_memory_id: str | None = None
    replaces_memory_ids: list[str] = field(default_factory=list)
    expected_old_text_sha256: str | None = None
    reason: str | None = None


@dataclass
class PromotionPatch:
    operations: list[PromotionPatchOperation] = field(default_factory=list)


@dataclass
class ApplyPromotionResult:
    applied: int = 0
    skipped: int = 0
    changed: bool = False
    applied_operations: list[dict[str, object]] = field(default_factory=list)


@dataclass
class RehydrateResult:
    ok: bool
    reason: str | None = None
