"""Structured runtime facts for coding-agent turns."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from opensquilla.engine.final_diff_contract import classify_final_diff_path
from opensquilla.tools.types import ToolContext


def build_runtime_state_capsule(
    *,
    workspace: str | Path | None,
    tool_context: ToolContext | None,
) -> dict[str, Any]:
    """Build a compact, factual capsule of current workspace state."""

    diff_paths = _git_dirty_paths(Path(workspace).expanduser().resolve()) if workspace else []
    source_paths = _paths_by_kind(diff_paths, "source")
    scratch_paths = _paths_by_kind(diff_paths, "scratch")
    test_like_paths = _paths_by_kind(diff_paths, "test-like")
    receipts = _mutation_receipts(tool_context)
    changed_receipts = [receipt for receipt in receipts if receipt.get("changed") is True]
    workspace_epoch = _workspace_epoch(tool_context, changed_receipts)
    last_mutation = _last_mutation_summary(receipts)

    return {
        "schema": "runtime_state_capsule_v1",
        "workspace": {
            "epoch": workspace_epoch,
            "diff": bool(diff_paths),
            "diff_paths": diff_paths,
            "source_diff": bool(source_paths),
            "source_paths": source_paths,
            "test_like_paths": test_like_paths,
            "scratch_paths": scratch_paths,
            "scratch_only": bool(scratch_paths and not source_paths and diff_paths),
        },
        "mutations": {
            "receipt_count": len(receipts),
            "changed_receipt_count": len(changed_receipts),
            "changed_source_paths": _changed_source_paths(changed_receipts),
        },
        "last_mutation": last_mutation,
        "finalization": {
            "source_diff_present": bool(source_paths),
            "blocking_facts": _blocking_facts(source_paths, scratch_paths, changed_receipts),
        },
    }


def runtime_state_capsule_message(capsule: Mapping[str, Any]) -> str:
    """Return provider-visible capsule text with stable JSON ordering."""

    return "Runtime state capsule:\n" + json.dumps(
        capsule,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _mutation_receipts(tool_context: ToolContext | None) -> list[dict[str, Any]]:
    records = getattr(tool_context, "workspace_mutation_receipts", []) if tool_context else []
    return [record for record in records if isinstance(record, dict)]


def _workspace_epoch(
    tool_context: ToolContext | None,
    changed_receipts: Sequence[Mapping[str, Any]],
) -> int:
    raw_epoch = getattr(tool_context, "workspace_epoch", 0) if tool_context else 0
    try:
        epoch = int(raw_epoch or 0)
    except (TypeError, ValueError):
        epoch = 0
    for receipt in changed_receipts:
        try:
            epoch = max(epoch, int(receipt.get("workspace_epoch") or 0))
        except (TypeError, ValueError):
            continue
    return epoch


def _last_mutation_summary(receipts: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not receipts:
        return None
    receipt = receipts[-1]
    relative_path = str(receipt.get("relative_path") or receipt.get("path") or "")
    if relative_path.startswith("/"):
        relative_path = Path(relative_path).name
    summary = {
        "tool": receipt.get("tool_name") or receipt.get("tool"),
        "operation": receipt.get("operation"),
        "path": relative_path,
        "classification": receipt.get("classification"),
        "changed": bool(receipt.get("changed")),
        "partial": bool(receipt.get("partial")),
        "workspace_epoch": receipt.get("workspace_epoch"),
    }
    return {key: value for key, value in summary.items() if value is not None}


def _changed_source_paths(receipts: Sequence[Mapping[str, Any]]) -> list[str]:
    paths: list[str] = []
    for receipt in receipts:
        if receipt.get("classification") != "source":
            continue
        path = str(receipt.get("relative_path") or "")
        if path:
            paths.append(path.replace("\\", "/"))
    return sorted(set(paths))


def _blocking_facts(
    source_paths: Sequence[str],
    scratch_paths: Sequence[str],
    changed_receipts: Sequence[Mapping[str, Any]],
) -> list[str]:
    facts: list[str] = []
    if changed_receipts and not source_paths:
        facts.append("changed_mutation_without_current_source_diff")
    if scratch_paths and not source_paths:
        facts.append("scratch_diff_without_source_diff")
    return facts


def _paths_by_kind(paths: Sequence[str], kind: str) -> list[str]:
    return [path for path in paths if classify_final_diff_path(path) == kind]


def _git_dirty_paths(root: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    return _parse_git_status_z(completed.stdout.decode("utf-8", errors="replace"))


def _parse_git_status_z(output: str) -> list[str]:
    paths: list[str] = []
    entries = output.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        relative_path = entry[3:] if len(entry) > 3 else ""
        if status[:1] in {"R", "C"} and index < len(entries):
            relative_path = entries[index] or relative_path
            index += 1
        if relative_path:
            paths.append(relative_path.replace("\\", "/"))
    return sorted(set(paths))
