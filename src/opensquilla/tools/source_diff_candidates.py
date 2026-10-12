"""In-memory source diff candidate ledger for coding-agent recovery."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opensquilla.tools.types import ToolContext

MAX_CANDIDATES = 8
MAX_PATCH_CHARS = 64_000

_CANDIDATE_MODES = frozenset({"off", "log", "warn_model"})


def capture_source_diff_candidate(
    *,
    ctx: ToolContext,
    relative_path: str,
    workspace_epoch: int,
    receipt_id: str | None,
    tool_name: str,
) -> dict[str, Any] | None:
    """Capture the current git diff for a changed source path.

    Capture is intentionally best-effort: failures are reported as runtime
    events when possible, but never raised into the source-edit tool path.
    """

    if _candidate_mode(ctx) == "off":
        return None
    workspace = _workspace_path(ctx)
    if workspace is None:
        _emit_event(ctx, "source_diff_candidate.capture_skipped", reason="missing_workspace")
        return None
    path = _normalize_relative_path(relative_path)
    if not path:
        _emit_event(ctx, "source_diff_candidate.capture_skipped", reason="invalid_path")
        return None
    patch = _git_diff_for_path(workspace, path)
    if patch is None:
        _emit_event(
            ctx,
            "source_diff_candidate.capture_skipped",
            reason="git_diff_failed",
            paths=[path],
        )
        return None
    if not patch.strip():
        _emit_event(
            ctx,
            "source_diff_candidate.capture_skipped",
            reason="empty_diff",
            paths=[path],
        )
        return None
    if len(patch) > MAX_PATCH_CHARS:
        _emit_event(
            ctx,
            "source_diff_candidate.capture_skipped",
            reason="patch_too_large",
            paths=[path],
            patch_chars=len(patch),
            max_patch_chars=MAX_PATCH_CHARS,
        )
        return None

    ctx.source_diff_candidate_counter += 1
    candidate = {
        "candidate_id": f"srcdiff-{ctx.source_diff_candidate_counter}",
        "paths": [path],
        "patch": patch,
        "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "workspace_epoch": workspace_epoch,
        "receipt_id": receipt_id,
        "tool_name": tool_name,
        "lost": False,
        "lost_reason": None,
        "lost_command": None,
        "restored": False,
        "created_at": datetime.now(UTC).isoformat(),
    }
    ctx.source_diff_candidates.append(candidate)
    if len(ctx.source_diff_candidates) > MAX_CANDIDATES:
        del ctx.source_diff_candidates[: len(ctx.source_diff_candidates) - MAX_CANDIDATES]
    _emit_event(
        ctx,
        "source_diff_candidate.captured",
        candidate_id=candidate["candidate_id"],
        paths=candidate["paths"],
        patch_sha256=candidate["patch_sha256"],
        patch_chars=len(patch),
        workspace_epoch=workspace_epoch,
        receipt_id=receipt_id,
        tool_name=tool_name,
    )
    return candidate


def mark_source_diff_candidates_lost(
    *,
    ctx: ToolContext,
    paths: list[str],
    reason: str,
    command: str | None = None,
) -> list[dict[str, Any]]:
    """Mark recoverable candidates whose paths were targeted by a destructive action."""

    targets = {_normalize_relative_path(path) for path in paths}
    targets.discard("")
    if not targets:
        return []
    marked: list[dict[str, Any]] = []
    for candidate in ctx.source_diff_candidates:
        if candidate.get("lost") is True or candidate.get("restored") is True:
            continue
        candidate_paths = {
            _normalize_relative_path(path)
            for path in candidate.get("paths", [])
            if isinstance(path, str)
        }
        if not candidate_paths.intersection(targets):
            continue
        candidate["lost"] = True
        candidate["lost_reason"] = reason
        candidate["lost_command"] = command
        candidate["lost_at"] = datetime.now(UTC).isoformat()
        marked.append(candidate)
        _emit_event(
            ctx,
            "source_diff_candidate.marked_lost",
            candidate_id=candidate.get("candidate_id"),
            paths=sorted(candidate_paths),
            reason=reason,
            command=command,
            lost_path_count=len(candidate_paths.intersection(targets)),
        )
    return marked


def latest_recoverable_source_candidate(ctx: ToolContext) -> dict[str, Any] | None:
    """Return the newest candidate that has not been restored."""

    for candidate in reversed(ctx.source_diff_candidates):
        if candidate.get("restored") is True:
            continue
        if not candidate.get("patch"):
            continue
        return candidate
    return None


def recoverable_lost_source_candidate_ids(
    *,
    candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    lost_source_paths: list[str],
) -> list[str]:
    """Return lost candidate ids that overlap current lost source paths."""

    lost_paths = {_normalize_relative_path(path) for path in lost_source_paths}
    lost_paths.discard("")
    if not lost_paths:
        return []
    result: list[str] = []
    for candidate in candidates:
        if candidate.get("lost") is not True or candidate.get("restored") is True:
            continue
        candidate_paths = {
            _normalize_relative_path(path)
            for path in candidate.get("paths", [])
            if isinstance(path, str)
        }
        if not candidate_paths.intersection(lost_paths):
            continue
        candidate_id = candidate.get("candidate_id")
        if isinstance(candidate_id, str) and candidate_id not in result:
            result.append(candidate_id)
    return result


def _candidate_mode(ctx: ToolContext) -> str:
    value = str(getattr(ctx, "source_diff_candidate_mode", "log") or "log").strip().lower()
    if value in _CANDIDATE_MODES:
        return value
    return "log"


def _workspace_path(ctx: ToolContext) -> Path | None:
    raw = getattr(ctx, "workspace_dir", None)
    if not raw:
        return None
    return Path(raw).expanduser().resolve(strict=False)


def _git_diff_for_path(workspace: Path, relative_path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "diff", "--", relative_path],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _normalize_relative_path(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return Path(text).as_posix().lstrip("/")


def _emit_event(ctx: ToolContext, name: str, **details: Any) -> None:
    callback = getattr(ctx, "on_runtime_event", None)
    if callback is None:
        return
    try:
        callback(
            {
                "feature": "source_diff_candidate",
                "name": name,
                "session_key": getattr(ctx, "session_key", None),
                "agent_id": getattr(ctx, "agent_id", None),
                **details,
            }
        )
    except Exception:
        return
