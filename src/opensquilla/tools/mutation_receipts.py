"""Semantic workspace mutation receipt helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from opensquilla.tools.types import ToolContext, current_tool_context
from opensquilla.tools.write_tracking import classify_workspace_path


def fingerprint_file(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "exists": True,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def fingerprint_path(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "size": 0, "sha256": None}
    if not path.is_file():
        return {"exists": True, "size": 0, "sha256": None, "kind": "non_file"}
    return fingerprint_file(path)


def _workspace_relative_path(ctx: ToolContext, path: Path) -> str | None:
    if not ctx.workspace_dir:
        return None
    workspace = Path(ctx.workspace_dir).resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    try:
        return resolved_path.relative_to(workspace).as_posix()
    except ValueError:
        return None


def record_semantic_mutation_receipt(
    *,
    tool_name: str,
    path: Path,
    operation: str,
    before: dict[str, Any],
    after: dict[str, Any],
    partial: bool,
    metadata: dict[str, Any] | None = None,
    ctx: ToolContext | None = None,
) -> dict[str, Any] | None:
    from opensquilla.tools.run_mode import full_host_access_active

    if full_host_access_active():
        return None
    active = ctx if ctx is not None else current_tool_context.get()
    if active is None or not active.workspace_dir:
        return None

    relative_path = _workspace_relative_path(active, path)
    if relative_path is None:
        return None

    changed = before.get("sha256") != after.get("sha256") or before.get(
        "exists"
    ) != after.get("exists")
    if changed:
        active.workspace_epoch += 1

    receipt: dict[str, Any] = {
        "receipt_id": f"mut-{active.workspace_epoch}-{len(active.workspace_mutation_receipts) + 1}",
        "tool": tool_name,
        "tool_name": tool_name,
        "operation": operation,
        "path": str(path),
        "relative_path": relative_path,
        "classification": classify_workspace_path(relative_path),
        "changed": changed,
        "partial": partial,
        "workspace_epoch": active.workspace_epoch,
        "before": before,
        "after": after,
    }
    if metadata:
        receipt.update(metadata)

    active.workspace_mutation_receipts.append(receipt)
    callback = getattr(active, "on_runtime_event", None)
    if callback is not None:
        event = {
            "feature": "semantic_mutation",
            "name": "workspace.semantic_mutation_receipt",
            "agent_id": getattr(active, "agent_id", None),
            "session_key": getattr(active, "session_key", None),
            **receipt,
        }
        try:
            callback(event)
        except Exception:
            pass

    if changed and receipt["classification"] == "source":
        try:
            from opensquilla.tools.source_diff_candidates import (
                capture_source_diff_candidate,
            )

            capture_source_diff_candidate(
                ctx=active,
                relative_path=relative_path,
                workspace_epoch=active.workspace_epoch,
                receipt_id=receipt.get("receipt_id"),
                tool_name=tool_name,
            )
        except Exception:
            pass

    return receipt
