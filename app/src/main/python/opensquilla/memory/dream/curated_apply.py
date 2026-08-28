"""Curated MEMORY.md writes for Dream."""

from __future__ import annotations

from pathlib import Path

from opensquilla.memory.dream.models import (
    ApplyPromotionResult,
    PromotionPatch,
    PromotionPatchOperation,
)


def _section_heading(section: str) -> str:
    cleaned = section.strip() or "Long-Term Memory"
    return f"## {cleaned}"


def _normalize_bullet(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped if stripped.startswith("- ") else f"- {stripped}"


def _upsert_under_section(content: str, section: str, bullet: str) -> tuple[str, bool]:
    heading = _section_heading(section)
    if not content.strip():
        return f"{heading}\n\n{bullet}\n", True
    lines = content.splitlines()
    try:
        start = lines.index(heading)
    except ValueError:
        base = content.rstrip()
        return f"{base}\n\n{heading}\n\n{bullet}\n", True
    next_heading = len(lines)
    for idx in range(start + 1, len(lines)):
        if lines[idx].startswith("## "):
            next_heading = idx
            break
    section_lines = lines[start + 1 : next_heading]
    if bullet in section_lines:
        return content if content.endswith("\n") else f"{content}\n", False
    insert_at = next_heading
    next_lines = lines[:insert_at] + [bullet] + lines[insert_at:]
    return "\n".join(next_lines).rstrip() + "\n", True


def _apply_operation(content: str, operation: PromotionPatchOperation) -> tuple[str, bool]:
    if operation.op not in {"upsert", "merge"}:
        return content, False
    bullet = _normalize_bullet(operation.text)
    if not bullet:
        return content, False
    return _upsert_under_section(content, operation.section, bullet)


def apply_promotion_patch(
    workspace: Path,
    patch: PromotionPatch,
    *,
    dry_run: bool,
) -> ApplyPromotionResult:
    memory_path = workspace / "MEMORY.md"
    try:
        content = memory_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    applied = 0
    skipped = 0
    applied_operations: list[dict[str, object]] = []
    next_content = content
    for operation in patch.operations:
        if operation.op == "skip":
            skipped += 1
            applied_operations.append(
                {
                    "op": operation.op,
                    "candidate_ids": operation.candidate_ids,
                    "changed": False,
                    "reason": operation.reason or "skip",
                }
            )
            continue
        next_content, changed = _apply_operation(next_content, operation)
        if changed:
            applied += 1
        applied_operations.append(
            {
                "op": operation.op,
                "candidate_ids": operation.candidate_ids,
                "memory_id": operation.memory_id,
                "section": operation.section,
                "changed": changed,
            }
        )
    changed = next_content != content
    if changed and not dry_run:
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text(next_content, encoding="utf-8")
    return ApplyPromotionResult(
        applied=0 if dry_run else applied,
        skipped=skipped,
        changed=changed and not dry_run,
        applied_operations=applied_operations,
    )
