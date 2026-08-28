"""Dream receipt writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opensquilla.memory.dream.models import ApplyPromotionResult, PromotionCandidate


def write_dream_receipt(
    *,
    workspace: Path,
    artifact_id: str,
    agent_id: str,
    dry_run: bool,
    candidate_paths: list[str],
    evidence_updated: int,
    ranked_candidates: list[PromotionCandidate],
    skipped_candidates: list[dict[str, Any]],
    applied: ApplyPromotionResult,
    memory_md_backup_path: str,
    cursor_before: float,
    cursor_after: float,
) -> str:
    receipt_dir = workspace / "memory" / ".dream_receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / f"{artifact_id}.json"
    payload = {
        "schema_version": 1,
        "agent_id": agent_id,
        "dry_run": dry_run,
        "candidate_paths": candidate_paths,
        "evidence_updated": evidence_updated,
        "ranked_candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "source_path": candidate.source_path,
                "score": candidate.score,
                "reasons": candidate.reasons,
            }
            for candidate in ranked_candidates
        ],
        "skipped_candidates": skipped_candidates,
        "applied_promotions": applied.applied_operations,
        "memory_md_backup_path": memory_md_backup_path,
        "cursor_before": cursor_before,
        "cursor_after": cursor_after,
        "rollback": {
            "restore_memory_from": memory_md_backup_path,
            "reset_cursor_to": cursor_before,
        },
    }
    receipt_path.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return receipt_path.relative_to(workspace).as_posix()
