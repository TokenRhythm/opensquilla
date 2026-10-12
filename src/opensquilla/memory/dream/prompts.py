"""Dream provider prompts and constrained patch parsing."""

from __future__ import annotations

import json
import re
from typing import Any

from opensquilla.memory.dream.models import (
    PromotionCandidate,
    PromotionPatch,
    PromotionPatchOperation,
)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def promotion_patch_prompt(current_memory_md: str, candidates: list[PromotionCandidate]) -> str:
    candidate_lines = []
    for candidate in candidates:
        candidate_lines.append(
            "\n".join(
                [
                    f"- candidate_id: {candidate.candidate_id}",
                    f"  score: {candidate.score:.3f}",
                    f"  reasons: {', '.join(candidate.reasons)}",
                    f"  snippet: {candidate.snippet}",
                ]
            )
        )
    return (
        "You are updating OpenSquilla MEMORY.md as curated long-term memory.\n"
        "Return JSON only with an operations array. Do not write dated logs, scores, "
        "or source metadata into MEMORY.md.\n\n"
        "Allowed operations:\n"
        '- {"op":"upsert","candidate_ids":["..."],"section":"User Preferences",'
        '"memory_id":"mem_short_stable_id","text":"- durable memory"}\n'
        '- {"op":"merge","candidate_ids":["..."],"section":"Project Practices",'
        '"memory_id":"mem_short_stable_id","text":"- consolidated memory"}\n'
        '- {"op":"skip","candidate_ids":["..."],"reason":"not durable"}\n\n'
        f"Current MEMORY.md:\n<<<\n{current_memory_md}\n>>>\n\n"
        "Ranked candidates:\n"
        + "\n\n".join(candidate_lines)
        + "\n\nJSON:"
    )


def _json_payload(text: str) -> dict[str, Any]:
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        raise ValueError(f"Dream response did not contain JSON: {text[:300]}")
    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("Dream response JSON must be an object")
    return payload


def parse_promotion_patch(text: str, candidates: list[PromotionCandidate]) -> PromotionPatch:
    payload = _json_payload(text)
    candidate_ids = {candidate.candidate_id for candidate in candidates}
    operations_raw = payload.get("operations") or []
    if not isinstance(operations_raw, list):
        raise ValueError("Dream operations must be a list")
    operations: list[PromotionPatchOperation] = []
    for raw in operations_raw:
        if not isinstance(raw, dict):
            continue
        op = str(raw.get("op") or "")
        if op not in {"upsert", "merge", "skip"}:
            continue
        ids_raw = raw.get("candidate_ids") or []
        ids = [str(value) for value in ids_raw if isinstance(value, str)]
        if ids == ["auto"]:
            ids = sorted(candidate_ids)
        ids = [value for value in ids if value in candidate_ids]
        if not ids:
            continue
        operations.append(
            PromotionPatchOperation(
                op=op,
                candidate_ids=ids,
                section=str(raw.get("section") or "Long-Term Memory"),
                memory_id=str(raw.get("memory_id") or ""),
                text=str(raw.get("text") or ""),
                replaces_memory_id=(
                    str(raw["replaces_memory_id"])
                    if isinstance(raw.get("replaces_memory_id"), str)
                    else None
                ),
                replaces_memory_ids=[
                    str(value)
                    for value in raw.get("replaces_memory_ids", [])
                    if isinstance(value, str)
                ],
                expected_old_text_sha256=(
                    str(raw["expected_old_text_sha256"])
                    if isinstance(raw.get("expected_old_text_sha256"), str)
                    else None
                ),
                reason=str(raw.get("reason")) if raw.get("reason") is not None else None,
            )
        )
    return PromotionPatch(operations=operations)
