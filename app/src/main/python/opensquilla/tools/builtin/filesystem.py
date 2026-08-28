# Filesystem built-in tools: read_file, write_file, edit_file, list_dir, glob_search, grep_search.

from __future__ import annotations

import asyncio
import contextvars
import csv
import difflib
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import sys
import time
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import structlog

from opensquilla.sandbox.backend.unavailable import UnavailableBackend
from opensquilla.sandbox.backup_vault import BackupReceiptSummary, summarize_backup_receipts
from opensquilla.sandbox.destructive_backup import DestructiveBackupGate
from opensquilla.sandbox.directory_listing import format_directory_entry
from opensquilla.sandbox.elevation import (
    ApprovalDisplay,
    ElevationAction,
    gate_elevated_action,
)
from opensquilla.sandbox.escalation import (
    build_path_approval_params,
    current_tool_mounts,
    current_tool_run_context,
    grant_temporary_mount_for_current_tool,
    request_sandbox_approval,
)
from opensquilla.sandbox.file_policy import authority_roots_for_state, decide_file_access
from opensquilla.sandbox.integration import (
    active_file_system_profile,
    active_sandbox_policy,
    get_runtime,
)
from opensquilla.sandbox.operation_runtime import (
    FilesystemOperationRequest,
    SandboxOperation,
    SandboxOperationRuntime,
    SandboxToolDescriptor,
)
from opensquilla.sandbox.path_validation import (
    MountDecision,
    decide_path_access,
    logical_tool_path,
    trusted_write_auto_grant_allowed,
)
from opensquilla.sandbox.permissions import FileSystemAccess, FileSystemPermissionProfile
from opensquilla.tools.mutation_receipts import (
    fingerprint_path,
    record_semantic_mutation_receipt,
)
from opensquilla.tools.path_policy import reject_foreign_host_path
from opensquilla.tools.registry import tool
from opensquilla.tools.run_mode import (
    current_run_mode,
    full_host_access_active,
    trusted_sandbox_active,
)
from opensquilla.tools.source_edit_contract import (
    SourceEditContractError,
    apply_line_edits,
    build_diff_summary,
    build_line_receipt,
    source_revision_for_path,
)
from opensquilla.tools.types import (
    PlanAccess,
    RetryableToolInputError,
    SafeToolError,
    ToolError,
    WorkspaceAccessError,
    current_tool_context,
)
from opensquilla.tools.write_tracking import (
    record_scratch_file_write,
    record_workspace_file_read,
    record_workspace_file_write,
    refresh_workspace_file_read_state,
    require_fresh_workspace_file_read,
    scratch_only_progress_note,
    workspace_write_note,
    workspace_write_progress_note,
)

log = structlog.get_logger(__name__)


def _settlement_duration_bucket(duration_ms: int) -> str:
    if duration_ms <= 10:
        return "le_10ms"
    if duration_ms <= 50:
        return "le_50ms"
    if duration_ms <= 250:
        return "le_250ms"
    if duration_ms <= 1000:
        return "le_1s"
    return "gt_1s"

_SPREADSHEET_EXTENSIONS = {".csv", ".tsv", ".xlsx"}
_OFFICE_BINARY_EXTENSIONS = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
_BINARY_EXTENSIONS = {
    ".7z",
    ".bin",
    ".bz2",
    ".dmg",
    ".exe",
    ".gz",
    ".rar",
    ".tar",
    ".zip",
    *_OFFICE_BINARY_EXTENSIONS,
}
_SEARCH_EXCLUDED_DIR_NAMES = frozenset({".git", ".hg", ".svn"})
_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_XLSX_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_BOOTSTRAP_SOURCE_FILENAMES_FALLBACK = frozenset(
    {
        "AGENTS.md",
        "SOUL.md",
        "IDENTITY.md",
        "TOOLS.md",
        "USER.md",
        "BOOTSTRAP.md",
        "HEARTBEAT.md",
    }
)
_GREP_DEFAULT_MAX_RESULTS = 100
_GREP_MAX_RESULTS = 1000
_GREP_MAX_MATCH_LINE_CHARS = 2000
_SOURCE_SYMBOL_DEFAULT_MAX_RESULTS = 80
_SOURCE_SYMBOL_MAX_RESULTS = 200
_SOURCE_SYMBOL_MAX_FILE_BYTES = 1_000_000
_SOURCE_SYMBOL_EXTENSIONS = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".cs",
        ".go",
        ".h",
        ".hh",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".m",
        ".mm",
        ".php",
        ".py",
        ".pyi",
        ".rb",
        ".rs",
        ".scala",
        ".swift",
        ".ts",
        ".tsx",
    }
)


async def _run_executor_mutation[ExecutorResult](
    worker: Callable[[], ExecutorResult],
    *,
    settle: Callable[[BaseException | None], None],
) -> ExecutorResult:
    """Run an unkillable thread mutation and reconcile it before cancellation.

    Cancelling an asyncio future cannot stop a callable that is already running
    in a thread. Shield the future, absorb repeated cancellation until it ends,
    then run the caller's disk/receipt reconciliation before propagating the
    first cancellation.
    """
    # Keep the historical event-loop injection seam used by race tests and
    # embedders while normal async callers still receive the running loop.
    loop = asyncio.get_event_loop()
    started_at = time.monotonic()
    future = asyncio.ensure_future(loop.run_in_executor(None, worker))
    pending_cancel: asyncio.CancelledError | None = None
    worker_error: BaseException | None = None
    result: ExecutorResult | None = None

    while not future.done():
        try:
            result = await asyncio.shield(future)
        except asyncio.CancelledError as exc:
            if pending_cancel is None:
                pending_cancel = exc
        except BaseException as exc:  # noqa: BLE001 - reconciled below
            worker_error = exc
            break

    if future.done() and worker_error is None and result is None:
        try:
            result = future.result()
        except BaseException as exc:  # noqa: BLE001 - reconciled below
            worker_error = exc

    settle(worker_error)
    duration_ms = int((time.monotonic() - started_at) * 1000)
    log.info(
        "filesystem.executor_mutation_settled",
        duration_ms=duration_ms,
        duration_bucket=_settlement_duration_bucket(duration_ms),
        cancellation_delayed=pending_cancel is not None,
        worker_outcome="failed" if worker_error is not None else "completed",
    )
    if pending_cancel is not None:
        raise pending_cancel
    if worker_error is not None:
        raise worker_error
    return result  # type: ignore[return-value]


_SOURCE_SYMBOL_REGEXES: tuple[tuple[frozenset[str], str, re.Pattern[str]], ...] = (
    (
        frozenset({".py", ".pyi"}),
        "class",
        re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    ),
    (
        frozenset({".py", ".pyi"}),
        "function",
        re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    ),
    (
        frozenset({".js", ".jsx", ".ts", ".tsx"}),
        "class",
        re.compile(r"^\s*(?:export\s+default\s+|export\s+)?class\s+([A-Za-z_$][\w$]*)\b"),
    ),
    (
        frozenset({".js", ".jsx", ".ts", ".tsx"}),
        "function",
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
    ),
    (
        frozenset({".js", ".jsx", ".ts", ".tsx"}),
        "function",
        re.compile(
            r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="
            r"\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
        ),
    ),
    (
        frozenset({".java", ".cs", ".kt", ".kts", ".scala"}),
        "class",
        re.compile(
            r"^\s*(?:public|private|protected|internal|abstract|final|sealed|open|"
            r"static|\s)*(?:class|interface|enum|record|object)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
        ),
    ),
    (
        frozenset({".java", ".cs", ".kt", ".kts", ".scala"}),
        "function",
        re.compile(
            r"^\s*(?:public|private|protected|internal|static|final|abstract|open|"
            r"override|async|suspend|\s)+[A-Za-z_<>\[\].?,\s]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("
        ),
    ),
    (
        frozenset({".go"}),
        "function",
        re.compile(r"^\s*func\s+(?:\([^)]+\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    ),
    (
        frozenset({".rs"}),
        "function",
        re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    ),
    (
        frozenset({".rs"}),
        "class",
        re.compile(
            r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|impl)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
        ),
    ),
    (
        frozenset({".rb"}),
        "class",
        re.compile(r"^\s*(?:class|module)\s+([A-Za-z_][A-Za-z0-9_:]*)\b"),
    ),
    (
        frozenset({".rb"}),
        "function",
        re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z_][A-Za-z0-9_!?=]*)\b"),
    ),
    (
        frozenset({".php"}),
        "class",
        re.compile(
            r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
        ),
    ),
    (
        frozenset({".php"}),
        "function",
        re.compile(
            r"^\s*(?:public|private|protected|static|final|abstract|\s)*function\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)\s*\("
        ),
    ),
    (
        frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".m", ".mm"}),
        "class",
        re.compile(r"^\s*(?:class|struct|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    ),
    (
        frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".m", ".mm"}),
        "function",
        re.compile(
            r"^\s*(?:[A-Za-z_][\w:<>,~*&\s]+\s+)+([A-Za-z_][A-Za-z0-9_]*)\s*"
            r"\([^;{}]*\)\s*(?:const\s*)?(?:\{|$)"
        ),
    ),
)
_SOURCE_SYMBOL_IGNORED_NAMES = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "sizeof",
        "function",
    }
)


def _tool_path_request(args: Mapping[str, Any]) -> FilesystemOperationRequest:
    raw_path = args.get("path")
    path = Path(str(raw_path)) if raw_path else None
    return FilesystemOperationRequest(
        path=path,
        paths=(path,) if path is not None else (),
    )


def _tool_search_request(args: Mapping[str, Any]) -> FilesystemOperationRequest:
    raw_path = args.get("path")
    path = Path(str(raw_path)) if raw_path else None
    return FilesystemOperationRequest(
        path=path,
        paths=(path,) if path is not None else (),
        pattern=str(args.get("pattern", "") or ""),
        include=str(args["include"]) if args.get("include") is not None else None,
        max_results=int(args["max_results"]) if args.get("max_results") is not None else None,
    )


def _workspace_root() -> Path | None:
    ctx = current_tool_context.get()
    if ctx is not None and ctx.workspace_dir:
        return Path(ctx.workspace_dir).expanduser().resolve()
    runtime = get_runtime()
    if runtime is not None and runtime.effective.sandbox_enabled:
        return runtime.workspace.expanduser().resolve()
    return None


def _scratch_root() -> Path | None:
    ctx = current_tool_context.get()
    if ctx is None or not ctx.scratch_dir:
        return None
    return Path(ctx.scratch_dir).expanduser().resolve(strict=False)


def _is_inside_scratch(resolved: Path) -> bool:
    root = _scratch_root()
    if root is None:
        return False
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        return False


def _memory_source_root() -> Path | None:
    ctx = current_tool_context.get()
    if ctx is None or not ctx.memory_source_dir:
        return None
    return Path(ctx.memory_source_dir).expanduser().resolve()


def _memory_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for root in (_workspace_root(), _memory_source_root()):
        if root is None or root in roots:
            continue
        roots.append(root)
    return tuple(roots)


def _resolve_path(path: str) -> Path:
    """Resolve *path* against the active workspace when relative.

    Reads are always allowed; any workspace enforcement for writes happens in
    :func:`_gate_out_of_workspace_write`, not here.

    Sandbox-visible alias paths (``/workspace/...`` from ``execute_code``
    stdout, ``default_workspace_dir()/...`` from LLM training priors)
    are translated back to the active host workspace before any
    sensitive-path / workspace-strict enforcement runs. Without this,
    model-guessed default-workspace paths are hard-blocked by the
    sensitive_path check even though the same file written under the
    gateway-configured workspace would be valid.
    """
    from opensquilla.tools.path_aliases import resolve_workspace_alias

    raw = Path(path).expanduser()
    root = _workspace_root()
    reject_foreign_host_path(str(path), platform=os.name, workspace=root)
    alias = resolve_workspace_alias(raw, root)
    if alias is not None:
        assert isinstance(alias, Path)
        return alias.resolve(strict=False)
    if root is not None and not raw.is_absolute():
        return (root / raw).resolve(strict=False)
    return raw.resolve(strict=False) if raw.is_absolute() else raw


def _resolve_base(path: str | None) -> Path:
    if path:
        return _resolve_path(path)
    root = _workspace_root()
    return root if root is not None else Path.cwd()


def _workspace_display_path_for_root(path: Path, original_path: str, root: Path | None) -> str:
    resolved = path.resolve(strict=False)
    if root is None:
        return original_path
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return original_path


def _workspace_display_path(path: Path, original_path: str) -> str:
    return _workspace_display_path_for_root(path, original_path, _workspace_root())


def _memory_source_rel_path(path: Path) -> str | None:
    resolved = path.resolve(strict=False)
    for root in _memory_roots():
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue

        if rel.parts in {("MEMORY.md",), ("memory.md",)}:
            return rel.as_posix()
        if len(rel.parts) >= 2 and rel.parts[0] == "memory" and rel.suffix == ".md":
            return rel.as_posix()
    return None


def _bootstrap_source_rel_path(path: Path) -> str | None:
    root = _workspace_root()
    if root is None:
        return None
    resolved = path.resolve(strict=False)
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return None
    rel_path = rel.as_posix()
    try:
        from opensquilla.identity.workspace import BOOTSTRAP_FILENAMES

        bootstrap_source_filenames = frozenset(BOOTSTRAP_FILENAMES)
    except Exception:
        bootstrap_source_filenames = _BOOTSTRAP_SOURCE_FILENAMES_FALLBACK

    if len(rel.parts) == 1 and rel_path in bootstrap_source_filenames:
        return rel_path
    return None


def _notify_memory_source_write(path: Path) -> None:
    ctx = current_tool_context.get()
    if ctx is None or ctx.on_memory_source_write is None:
        return
    rel = _memory_source_rel_path(path)
    if rel is None:
        return
    ctx.on_memory_source_write(ctx.agent_id or "main", rel)


def _notify_bootstrap_source_write(path: Path) -> None:
    ctx = current_tool_context.get()
    if ctx is None or ctx.on_bootstrap_source_write is None:
        return
    rel = _bootstrap_source_rel_path(path)
    if rel is None:
        return
    ctx.on_bootstrap_source_write(ctx.agent_id or "main", rel)


def _binary_file_error(path: str, p: Path, *, reason: str | None = None) -> ToolError:
    hint = ""
    if p.suffix.lower() in _SPREADSHEET_EXTENSIONS:
        hint = " Use read_spreadsheet(path=...) for CSV/TSV/Excel workbook data."
    detail = f" ({reason})" if reason else ""
    return ToolError(f"Cannot read binary file as text: {path}{detail}.{hint}")


def _looks_binary(raw: bytes, p: Path) -> str | None:
    ext = p.suffix.lower()
    if ext in _OFFICE_BINARY_EXTENSIONS:
        return f"{ext} Office document"
    if ext in _BINARY_EXTENSIONS:
        return f"{ext} binary/container file"
    sample = raw[:8192]
    if b"\x00" in sample:
        return "contains NUL bytes"
    return None


def _read_binary_sample(p: Path, size: int = 8192) -> bytes:
    with p.open("rb") as fh:
        return fh.read(size)


def _is_search_excluded_path(path: Path) -> bool:
    return any(part in _SEARCH_EXCLUDED_DIR_NAMES for part in path.parts)


def _stream_numbered_lines_from_file(
    p: Path,
    original_path: str,
    *,
    offset: int | None = None,
    limit: int | None = None,
) -> str:
    """Read a numbered UTF-8 line window without loading the whole file.

    Counting offsets still requires decoding prior lines; invalid UTF-8 before
    the selected window therefore raises the same text/binary error style.
    """

    start_line = offset if offset and offset > 0 else 1
    selected: list[str] = []
    emitted = 0
    try:
        with p.open("rb") as fh:
            for lineno, raw_line in enumerate(fh, start=1):
                line = raw_line.decode("utf-8")
                if lineno < start_line:
                    continue
                if limit is not None and emitted >= limit:
                    break
                selected.append(f"{lineno}\t{line}")
                emitted += 1
    except UnicodeDecodeError as exc:
        raise _binary_file_error(original_path, p, reason="not valid UTF-8") from exc
    return "".join(selected)


def _bounded_positive_int(value: int | None, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _bounded_non_negative_int(
    value: int | None,
    *,
    default: int,
    maximum: int | None = None,
) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    parsed = max(0, parsed)
    return min(parsed, maximum) if maximum is not None else parsed


def _format_grep_match(
    fp: Path,
    lineno: int,
    line: str,
    *,
    include_line_numbers: bool = True,
    workspace_root: Path | None = None,
) -> str:
    text = line.rstrip()
    if len(text) > _GREP_MAX_MATCH_LINE_CHARS:
        omitted = len(text) - _GREP_MAX_MATCH_LINE_CHARS
        text = (
            text[:_GREP_MAX_MATCH_LINE_CHARS].rstrip()
            + f"... [line truncated: omitted_chars={omitted}]"
        )
    display_path = _workspace_display_path_for_root(fp, str(fp), workspace_root)
    if include_line_numbers:
        return f"{display_path}:{lineno}: {text}"
    return f"{display_path}: {text}"


def _is_outside_workspace(resolved: Path) -> bool:
    """True when *resolved* is not contained in the active workspace.

    No workspace configured → writes aren't gated at all (no root to compare).
    """
    root = _workspace_root()
    if root is None:
        return False
    try:
        resolved.relative_to(root)
        return False
    except ValueError:
        return True


def _outside_workspace_write_block(
    tool_name: str,
    resolved: Path,
    original_path: str,
) -> dict[str, object]:
    workspace = _workspace_root()
    message = (
        f"{tool_name} blocked: {resolved} is outside the active workspace "
        f"({workspace}) and no sandbox path grant is active."
        if workspace is not None
        else f"{tool_name} blocked: {resolved} is outside the active workspace."
    )
    return {
        "status": "blocked",
        "reason": "outside_workspace",
        "tool": tool_name,
        "path": original_path,
        "resolved_path": str(resolved),
        "workspace": str(workspace) if workspace is not None else None,
        "message": message,
        "retryable": False,
    }


def _sandbox_path_access_enabled() -> bool:
    runtime = get_runtime()
    if runtime is None or not runtime.effective.sandbox_enabled:
        return False
    if full_host_access_active():
        return False
    return True


def _active_sandbox_mounts() -> list[dict[str, object]]:
    mounts = current_tool_mounts()
    runtime = get_runtime()
    settings = getattr(runtime, "settings", None) if runtime is not None else None
    if (
        sys.platform.startswith("linux")
        and bool(getattr(settings, "host_root_readonly", False))
        and not any(str(item.get("path") or "") == "/" for item in mounts)
    ):
        mounts.insert(0, {"path": "/", "access": "ro"})
    return mounts


def _path_access_required_envelope(
    decision: MountDecision,
    *,
    approval_id: str | None = None,
) -> dict[str, object] | None:
    ctx = current_tool_context.get()
    workspace_root = _workspace_root()
    approval = build_path_approval_params(
        decision,
        session_key=getattr(ctx, "session_key", None) if ctx is not None else None,
        workspace=str(workspace_root) if workspace_root is not None else None,
    )
    if approval is None:
        return {
            "status": "path_access_required",
            "path": decision.normalized_path,
            "access": decision.access,
            "message": _path_access_message(workspace_root),
        }
    return request_sandbox_approval(
        approval,
        approval_id=approval_id,
        message=_path_access_message(workspace_root),
        denied_message=_path_access_denied_message(workspace_root),
    )


def _path_access_message(workspace_root: Path | None) -> str:
    workspace = str(workspace_root) if workspace_root is not None else "the configured workspace"
    return (
        f"The requested path is outside the current workspace ({workspace}). "
        "Ask the user whether to add this path as read-only or read/write access."
    )


def _path_access_denied_message(workspace_root: Path | None) -> str:
    workspace = str(workspace_root) if workspace_root is not None else "the configured workspace"
    return (
        "The user denied access outside the current workspace. "
        "Do not ask for the same access again in this turn. "
        "Explain that the requested path cannot be inspected from the current "
        f"workspace ({workspace}) unless the user approves access or changes run mode. "
        "Do not substitute details from other repositories or prior comparison context."
    )


def _path_access_blocked_envelope(decision: MountDecision) -> dict[str, object]:
    ctx = current_tool_context.get()
    if ctx is not None and bool(getattr(ctx, "guest_safe", False)):
        reason = (
            "GUEST_WRITE_OUTSIDE_DEFAULT_WORKSPACE"
            if decision.access == "rw"
            else "GUEST_SENSITIVE_PATH_DENIED"
        )
        return {
            "status": "blocked",
            "reason": reason,
            "path": decision.normalized_path,
            "message": (
                "Web guest mode can modify only the configured default workspace."
                if decision.access == "rw"
                else "Web guest mode cannot read credential or authority data."
            ),
        }
    return {
        "status": "blocked",
        "reason": decision.reason,
        "path": decision.normalized_path,
        "message": decision.reason,
    }


def _sandbox_path_access_envelope(
    resolved: Path,
    *,
    write: bool,
    approval_id: str | None = None,
) -> dict[str, object] | None:
    if not _sandbox_path_access_enabled():
        return None
    if _memory_source_rel_path(resolved) is not None:
        return None
    decision = decide_path_access(
        resolved,
        workspace=_workspace_root(),
        mounts=_active_sandbox_mounts(),
        write=write,
        profile=active_file_system_profile(_workspace_root()),
    )
    if decision.status == "allowed":
        return None
    if decision.status == "blocked":
        return _path_access_blocked_envelope(decision)
    ctx = current_tool_context.get()
    if ctx is not None and bool(getattr(ctx, "guest_safe", False)):
        return _path_access_blocked_envelope(
            replace(decision, status="blocked", reason="guest_boundary")
        )
    if not write and trusted_sandbox_active():
        # Safe mode treats local reads as host-readable. Sensitive data
        # exfiltration remains blocked at the action/network review boundary.
        return None
    if trusted_sandbox_active() and trusted_write_auto_grant_allowed(
        decision.normalized_path,
        workspace=_workspace_root(),
    ):
        if grant_temporary_mount_for_current_tool(decision):
            return None
    if write:
        return {
            "status": "elevation_required",
            "reason": decision.reason,
            "path": decision.normalized_path,
            "access": "rw",
            "message": (
                "This write is outside the sandbox's writable roots. Retry only if "
                "the user's request warrants it, using sandbox_permissions="
                "require_escalated and a precise justification."
            ),
        }
    return _path_access_required_envelope(decision, approval_id=approval_id)


def _sandbox_path_access_marker(candidate: Path, *, write: bool) -> str | None:
    envelope = _sandbox_path_access_envelope(
        candidate.resolve(strict=False),
        write=write,
    )
    if envelope is None:
        return None
    if envelope.get("status") == "blocked":
        return f"[blocked] {candidate}: {envelope.get('message') or 'sensitive path'}"
    return f"[blocked] {candidate}: outside current sandbox view"


def _active_sandbox_mount_allows(resolved: Path, *, write: bool) -> bool:
    if not _sandbox_path_access_enabled():
        return False
    decision = decide_path_access(
        resolved,
        workspace=_workspace_root(),
        mounts=_active_sandbox_mounts(),
        write=write,
        profile=active_file_system_profile(_workspace_root()),
    )
    return decision.status == "allowed"


def _active_filesystem_run_mode() -> str:
    context = current_tool_run_context()
    if context is not None:
        return context.run_mode.value
    return current_run_mode() or "safe"


async def _run_sandbox_operation_if_required(
    operation: SandboxOperation,
    *,
    host_execution_active: bool = False,
) -> object | None:
    if operation.domain == "filesystem" and operation.file_system_profile is None:
        profile = active_file_system_profile(_workspace_root())
        if profile is not None:
            operation = replace(operation, file_system_profile=profile)
    runtime = get_runtime()
    ctx = current_tool_context.get()
    if operation.domain == "filesystem" and ctx is not None and ctx.workspace_strict:
        filesystem_permissions = dict(operation.permissions.filesystem)
        filesystem_permissions["workspaceStrict"] = True
        workspace = operation.workspace
        if workspace is not None and ctx.artifact_session_id:
            from opensquilla.attachment_workspace import _safe_path_segment

            attachment_base = (
                workspace.expanduser().resolve(strict=False) / ".opensquilla" / "attachments"
            )
            attachment_segment = _safe_path_segment(
                ctx.artifact_session_id,
                fallback="session",
            )
            filesystem_permissions.update(
                {
                    "attachmentBase": str(attachment_base),
                    "attachmentSessionRoot": str(attachment_base / attachment_segment),
                }
            )
        if ctx.artifact_media_root and ctx.artifact_session_id:
            from opensquilla.attachment_refs import transcript_material_dir

            media_root = Path(ctx.artifact_media_root).expanduser().resolve(strict=False)
            filesystem_permissions.update(
                {
                    "transcriptBase": str(media_root / "transcripts"),
                    "transcriptSessionRoot": str(
                        transcript_material_dir(
                            media_root,
                            ctx.artifact_session_id,
                        ).resolve(strict=False)
                    ),
                }
            )
        operation = replace(
            operation,
            permissions=replace(
                operation.permissions,
                filesystem=filesystem_permissions,
            ),
        )
    if (
        trusted_sandbox_active()
        and ctx is not None
        and ctx.is_owner
        and runtime is not None
        and isinstance(runtime.backend, UnavailableBackend)
        and operation.kind not in {"create_source", "edit_source"}
    ):
        return None
    return await SandboxOperationRuntime(
        runtime,
        host_execution_active=full_host_access_active() or host_execution_active,
    ).run(operation)


def _filesystem_operation_workspace() -> Path | None:
    root = _workspace_root()
    if root is not None:
        return root
    runtime = get_runtime()
    if runtime is not None and runtime.effective.sandbox_enabled:
        return runtime.workspace.expanduser().resolve(strict=False)
    return None


def _is_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _write_scope_suffix(path: Path) -> str:
    ctx = current_tool_context.get()
    if ctx is not None and ctx.scratch_dir:
        scratch_root = Path(ctx.scratch_dir).expanduser()
        if _is_under_root(path, scratch_root):
            return (
                " (scratch file: temporary workspace for scripts, logs, debug "
                "output, and experiments; not a substitute for requested project changes)"
                f"{scratch_only_progress_note()}"
            )
    workspace_root = _workspace_root()
    if workspace_root is not None and _is_under_root(path, workspace_root):
        return (
            " (workspace file: part of the project working tree)"
            + workspace_write_note(path)
            + workspace_write_progress_note()
        )
    return ""


def _strict_read_workspace_root() -> Path | None:
    """Return the read-containment root when workspace-strict mode is active.

    Unlike :func:`_workspace_root`, strict read containment is intentionally
    opt-in through the entry-point ``ToolContext``. Runtime sandbox workspaces
    still provide relative-path resolution, but they do not by themselves turn
    every read into a strict containment check.
    """

    ctx = current_tool_context.get()
    if ctx is None or not ctx.workspace_strict or not ctx.workspace_dir:
        return None
    return Path(ctx.workspace_dir).expanduser().resolve(strict=False)


def _strict_read_material_root() -> Path | None:
    ctx = current_tool_context.get()
    if (
        ctx is None
        or not ctx.workspace_strict
        or not ctx.artifact_media_root
        or not ctx.artifact_session_id
    ):
        return None

    from opensquilla.attachment_refs import transcript_material_dir

    return transcript_material_dir(
        Path(ctx.artifact_media_root).expanduser(),
        ctx.artifact_session_id,
    ).resolve(strict=False)


def _strict_read_mount_roots() -> tuple[Path, ...]:
    ctx = current_tool_context.get()
    if ctx is None or not ctx.workspace_strict:
        return tuple()
    roots: list[Path] = []
    for mount in _active_sandbox_mounts():
        if not isinstance(mount, dict):
            continue
        raw_path = mount.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        try:
            root = Path(raw_path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _strict_read_profile_roots() -> tuple[Path, ...]:
    """Return readable roots from the same profile used by direct tools.

    Linux expresses Codex's host-readable policy as a read-only ``/`` mount,
    while Windows expresses it as a set of platform-specific profile entries.
    Strict workspace reads must understand both representations or filesystem
    tools disagree with shell and patch tools on Windows.
    """

    profile = active_file_system_profile(_workspace_root())
    if profile is None:
        return tuple()
    roots: list[Path] = []
    for entry in profile.entries:
        if entry.access is FileSystemAccess.DENY:
            continue
        try:
            root = Path(entry.path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _strict_read_roots() -> tuple[Path, ...]:
    if full_host_access_active():
        return tuple()
    roots: list[Path] = []
    workspace_root = _strict_read_workspace_root()
    if workspace_root is not None:
        roots.append(workspace_root)
    material_root = _strict_read_material_root()
    if material_root is not None:
        roots.append(material_root)
    for mount_root in _strict_read_mount_roots():
        if mount_root not in roots:
            roots.append(mount_root)
    for profile_root in _strict_read_profile_roots():
        if profile_root not in roots:
            roots.append(profile_root)
    return tuple(roots)


def _is_within_any_root(candidate: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _cross_session_attachment_block(
    tool_name: str,
    candidate: Path,
    original_path: str,
) -> dict[str, object] | None:
    """Block reads of another session's materialized attachments (#268).

    Materialized attachments live at ``<workspace>/.opensquilla/attachments/
    <session_segment>/`` inside the shared per-agent workspace, so the broad
    workspace read root would otherwise let one session read a sibling session's
    uploaded files. Under strict mode with a known session, deny that subtree for
    every segment except the current session's; authored files elsewhere in the
    workspace stay shared.
    """
    ctx = current_tool_context.get()
    if ctx is None or not ctx.workspace_strict or not ctx.artifact_session_id:
        return None
    workspace = _strict_read_workspace_root()
    if workspace is None:
        return None
    from opensquilla.attachment_workspace import _safe_path_segment

    base = (workspace / ".opensquilla" / "attachments").resolve(strict=False)
    try:
        rel = candidate.relative_to(base)
    except ValueError:
        return None  # not under the attachments subtree
    if not rel.parts:
        return None  # the attachments base directory itself
    candidate_segment = rel.parts[0]
    current_segment = _safe_path_segment(ctx.artifact_session_id, fallback="session")
    if candidate_segment == current_segment:
        return None  # the current session's own attachments
    return {
        "status": "blocked",
        "reason": "cross_session_attachment",
        "tool": tool_name,
        "path": original_path,
        "resolved_path": str(candidate),
        "message": (
            f"{tool_name} blocked: {candidate} belongs to another session's "
            "materialized attachments and is not readable from this session."
        ),
        "retryable": False,
    }


def _cross_session_transcript_material_block(
    tool_name: str,
    candidate: Path,
    original_path: str,
) -> dict[str, object] | None:
    """Block the shared transcript store except for the active session."""

    ctx = current_tool_context.get()
    if (
        ctx is None
        or not ctx.workspace_strict
        or not ctx.artifact_media_root
        or not ctx.artifact_session_id
    ):
        return None
    transcript_root = (Path(ctx.artifact_media_root).expanduser() / "transcripts").resolve(
        strict=False
    )
    try:
        candidate.relative_to(transcript_root)
    except ValueError:
        return None
    current_material_root = _strict_read_material_root()
    if current_material_root is not None:
        try:
            candidate.relative_to(current_material_root)
            return None
        except ValueError:
            pass
    return {
        "status": "blocked",
        "reason": "cross_session_transcript_material",
        "tool": tool_name,
        "path": original_path,
        "resolved_path": str(candidate),
        "message": (
            f"{tool_name} blocked: {candidate} is outside this session's "
            "session-scoped transcript materials."
        ),
        "retryable": False,
    }


def _cross_session_read_block(
    tool_name: str,
    candidate: Path,
    original_path: str,
) -> dict[str, object] | None:
    return _cross_session_attachment_block(
        tool_name,
        candidate,
        original_path,
    ) or _cross_session_transcript_material_block(
        tool_name,
        candidate,
        original_path,
    )


def _workspace_strict_read_block(
    tool_name: str,
    resolved: Path,
    original_path: str,
) -> dict[str, object] | None:
    """Return a block envelope when *resolved* escapes the strict workspace."""

    candidate = resolved.expanduser().resolve(strict=False)
    profile = active_file_system_profile(_workspace_root())
    ctx = current_tool_context.get()
    authenticated_safe_read = bool(
        trusted_sandbox_active()
        and ctx is not None
        and not ctx.guest_safe
    )
    if (
        authenticated_safe_read
        and profile is not None
        and not profile.is_explicitly_denied(candidate)
    ):
        return _cross_session_read_block(tool_name, candidate, original_path)
    if profile is not None and profile.resolve(candidate) is not FileSystemAccess.DENY:
        return _cross_session_read_block(tool_name, candidate, original_path)
    roots = _strict_read_roots()
    if not roots:
        return None
    if not _is_within_any_root(candidate, roots):
        root_labels = ", ".join(str(root) for root in roots)
        return {
            "status": "blocked",
            "reason": "workspace_strict",
            "tool": tool_name,
            "path": original_path,
            "resolved_path": str(candidate),
            "workspace": str(roots[0]),
            "allowed_roots": [str(root) for root in roots],
            "message": (
                f"{tool_name} blocked: {candidate} is outside active read roots ({root_labels})."
            ),
            "retryable": False,
        }
    return _cross_session_read_block(tool_name, candidate, original_path)


def _gate_workspace_strict_read(tool_name: str, resolved: Path, original_path: str) -> None:
    """Raise when a read target/base escapes the strict workspace.

    Call this after sensitive-path checks so sensitive hard-blocks keep higher
    priority, and before existence/metadata checks so strict mode does not
    become an existence oracle for outside paths.
    """

    blocked = _workspace_strict_read_block(tool_name, resolved, original_path)
    if blocked is not None:
        raise WorkspaceAccessError(str(blocked["message"]))


def _workspace_strict_candidate_marker(
    tool_name: str,
    candidate: Path,
    original_path: str | None = None,
    strict_root: Path | None = None,
    strict_roots: tuple[Path, ...] | None = None,
    resolved_candidate: Path | None = None,
) -> str | None:
    """Return a per-candidate blocked marker for directory/search tools."""

    roots = (strict_root,) if strict_root is not None else (strict_roots or _strict_read_roots())
    if not roots:
        return None
    resolved = (
        resolved_candidate
        if resolved_candidate is not None
        else candidate.expanduser().resolve(strict=False)
    )
    profile = active_file_system_profile(_workspace_root())
    ctx = current_tool_context.get()
    if (
        trusted_sandbox_active()
        and ctx is not None
        and not ctx.guest_safe
        and profile is not None
        and not profile.is_explicitly_denied(resolved)
    ):
        if _cross_session_read_block(
            tool_name,
            resolved,
            original_path or str(candidate),
        ):
            return f"[blocked] {candidate}: another session's private material"
        return None
    if profile is not None and profile.resolve(resolved) is not FileSystemAccess.DENY:
        if _cross_session_read_block(
            tool_name,
            resolved,
            original_path or str(candidate),
        ):
            return f"[blocked] {candidate}: another session's private material"
        return None
    if not _is_within_any_root(resolved, roots):
        root_labels = ", ".join(str(root) for root in roots)
        return f"[blocked] {candidate}: outside active read roots ({root_labels})"
    if _cross_session_read_block(tool_name, resolved, original_path or str(candidate)):
        return f"[blocked] {candidate}: another session's private material"
    return None


def _sensitive_access_block(tool_name: str, resolved: Path, original_path: str) -> dict | None:
    """Return a hard-block envelope for sensitive host paths, unless fully elevated."""
    from opensquilla.sandbox.sensitive_paths import build_block_envelope, sensitive_path_marker

    if full_host_access_active() or _sandbox_path_access_enabled():
        return None
    sensitive = sensitive_path_marker(str(resolved), workspace=_workspace_root())
    if sensitive is None:
        return None
    return build_block_envelope(f"{tool_name} {original_path}", sensitive, tool_name=tool_name)


def _is_sensitive_access_path(resolved: Path, workspace: Path | None = None) -> bool:
    from opensquilla.sandbox.sensitive_paths import sensitive_path_marker

    if _sandbox_path_access_enabled():
        return False
    root = workspace if workspace is not None else _workspace_root()
    return (
        not full_host_access_active()
        and sensitive_path_marker(str(resolved), workspace=root) is not None
    )


def _is_pathlib_symlink_loop_error(exc: RuntimeError) -> bool:
    """Return whether ``Path.resolve`` converted an OS symlink loop error."""

    return str(exc).startswith("Symlink loop from ") and isinstance(
        exc.__context__,
        OSError,
    )


def _workspace_lockdown_roots() -> list[Path]:
    ctx = current_tool_context.get()
    if ctx is None or not ctx.workspace_lockdown:
        return []
    roots: list[Path] = []
    if ctx.workspace_dir:
        roots.append(Path(ctx.workspace_dir).expanduser().resolve(strict=False))
    if ctx.scratch_dir:
        roots.append(Path(ctx.scratch_dir).expanduser().resolve(strict=False))
    return roots


def _inside_any_root(candidate: Path, roots: list[Path]) -> bool:
    resolved = candidate.expanduser().resolve(strict=False)
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _is_under_configured_scratch_dir(resolved: Path) -> bool:
    ctx = current_tool_context.get()
    if ctx is None or not ctx.scratch_dir:
        return False
    scratch = Path(ctx.scratch_dir).expanduser().resolve(strict=False)
    try:
        resolved.expanduser().resolve(strict=False).relative_to(scratch)
        return True
    except ValueError:
        return False


def _gate_workspace_lockdown_write(tool_name: str, resolved: Path, original_path: str) -> None:
    if full_host_access_active():
        return
    roots = _workspace_lockdown_roots()
    if not roots or _inside_any_root(resolved, roots):
        return
    allowed = ", ".join(str(root) for root in roots)
    raise SafeToolError(
        f"{tool_name} blocked by workspace lockdown: {original_path} resolves to "
        f"{resolved}, outside allowed roots: {allowed}."
    )


async def _gate_out_of_workspace_write(
    tool_name: str,
    resolved: Path,
    original_path: str,
    approval_id: str | None,
    *,
    sandbox_permissions: str = "use_default",
    justification: str = "",
    content_digest: str | None = None,
    prefix_rule: list[str] | None = None,
) -> tuple[dict[str, object] | None, bool, tuple[BackupReceiptSummary, ...]]:
    """Return ``(block, elevated, backups)`` after exact-action gating."""
    if full_host_access_active():
        return None, False, ()

    # Sensitive-path hard block — takes precedence over approval flow.
    from opensquilla.sandbox.sensitive_paths import build_block_envelope, sensitive_path_marker

    if not _sandbox_path_access_enabled():
        sensitive = sensitive_path_marker(str(resolved), workspace=_workspace_root())
        if sensitive is not None:
            return (
                build_block_envelope(
                    f"{tool_name} {original_path}", sensitive, tool_name=tool_name
                ),
                False,
                (),
            )

    _gate_workspace_lockdown_write(tool_name, resolved, original_path)
    from opensquilla.tools.write_policy import (
        gate_workspace_scratch_artifact,
        gate_workspace_write_deny,
    )

    gate_workspace_scratch_artifact(
        tool_name,
        resolved,
        original_path=original_path,
        workspace=_workspace_root(),
    )
    gate_workspace_write_deny(
        tool_name,
        resolved,
        original_path=original_path,
        workspace=_workspace_root(),
    )

    if sandbox_permissions not in {"use_default", "require_escalated"}:
        return (
            {
                "status": "invalid_request",
                "reason": "invalid_sandbox_permissions",
            },
            False,
            (),
        )

    if _sandbox_path_access_enabled():
        safe_policy_gate = await _gate_safe_policy_file_mutation(
            tool_name,
            resolved,
            original_path,
            approval_id,
            justification=justification,
            content_digest=content_digest,
            prefix_rule=prefix_rule,
        )
        if safe_policy_gate is not None:
            return safe_policy_gate
        decision = decide_path_access(
            resolved,
            workspace=_workspace_root(),
            mounts=_active_sandbox_mounts(),
            write=True,
            profile=active_file_system_profile(_workspace_root()),
            logical_path=original_path,
        )
        if decision.status == "blocked":
            return _path_access_blocked_envelope(decision), False, ()
        if decision.status == "request":
            ctx = current_tool_context.get()
            if ctx is not None and bool(getattr(ctx, "guest_safe", False)):
                return (
                    _path_access_blocked_envelope(
                        replace(decision, status="blocked", reason="guest_boundary")
                    ),
                    False,
                    (),
                )
            if sandbox_permissions == "use_default":
                return (
                    {
                        "status": "elevation_required",
                        "reason": decision.reason,
                        "path": decision.normalized_path,
                        "access": "rw",
                        "message": (
                            "This write is outside the sandbox's writable roots. Retry "
                            "only if the user's request warrants it, using "
                            "sandbox_permissions=require_escalated and a precise "
                            "justification."
                        ),
                    },
                    False,
                    (),
                )
            if not justification.strip():
                return (
                    {
                        "status": "elevation_required",
                        "reason": "justification_required",
                        "message": ("A precise justification is required for elevated execution."),
                    },
                    False,
                    (),
                )
            action_kinds = {
                "write_file": "fs.write",
                "edit_file": "fs.edit",
                "edit_source": "fs.edit_source",
            }
            action = ElevationAction(
                tool_name=tool_name,
                action_kind=action_kinds.get(tool_name, "fs.write"),
                argv=(tool_name, str(resolved)),
                cwd=str(_workspace_root() or Path.cwd()),
                sandbox_permissions="require_escalated",
                justification=justification,
                target_paths=((str(resolved), "write"),),
                content_digest=content_digest,
                prefix_rule=tuple(prefix_rule) if prefix_rule is not None else None,
                display=ApprovalDisplay(
                    kind="modify" if resolved.exists() or resolved.is_symlink() else "create",
                    target=str(resolved),
                ),
            )
            ctx = current_tool_context.get()
            if resolved.exists() or resolved.is_symlink():
                config = getattr(ctx, "sandbox_gateway_config", None) if ctx else None
                destructive = await DestructiveBackupGate().evaluate(
                    action,
                    approval_id=approval_id,
                    targets=(resolved,),
                    policy=active_sandbox_policy(),
                    state_dir=str(getattr(config, "state_dir", "") or ""),
                    session_key=ctx.session_key if ctx is not None else None,
                )
                if not destructive.allowed:
                    return destructive.envelope, False, ()
                return None, True, summarize_backup_receipts(destructive.receipts)
            gate = gate_elevated_action(
                action,
                approval_id=approval_id,
                session_key=ctx.session_key if ctx is not None else None,
            )
            if not gate.allowed:
                return gate.to_envelope(), False, ()
            return None, True, ()

    if not _is_outside_workspace(resolved):
        return None, False, ()
    if _is_inside_scratch(resolved):
        return None, False, ()
    if _memory_source_rel_path(resolved) is not None:
        return None, False, ()
    if _active_sandbox_mount_allows(resolved, write=True):
        return None, False, ()
    return _outside_workspace_write_block(tool_name, resolved, original_path), False, ()


async def _gate_safe_policy_file_mutation(
    tool_name: str,
    resolved: Path,
    original_path: str,
    approval_id: str | None,
    *,
    justification: str,
    content_digest: str | None,
    prefix_rule: list[str] | None,
) -> tuple[
    dict[str, object] | None,
    bool,
    tuple[BackupReceiptSummary, ...],
] | None:
    """Gate one exact structured mutation against the pinned Safe file policy.

    A protected user path can be approved because the structured filesystem
    tool performs only the fingerprinted operation.  This does not grant an
    arbitrary shell process unsandboxed access.  OpenSquilla authority paths
    remain non-overridable.
    """

    ctx = current_tool_context.get()
    config = getattr(ctx, "sandbox_gateway_config", None) if ctx is not None else None
    state_dir = str(getattr(config, "state_dir", "") or "").strip()
    authority_roots = authority_roots_for_state(state_dir) if state_dir else ()
    decision = decide_file_access(
        "write",
        resolved,
        active_sandbox_policy(),
        authority_roots=authority_roots,
    )
    if decision.allowed:
        return None
    if not decision.approval_required:
        return (
            {
                "status": "blocked",
                "reason": decision.code or "sandbox_file_mutation_denied",
                "path": str(resolved),
                "matched_path": (
                    str(decision.matched_path) if decision.matched_path is not None else None
                ),
            },
            False,
            (),
        )
    if ctx is not None and bool(getattr(ctx, "guest_safe", False)):
        return (
            {
                "status": "blocked",
                "reason": "guest_safe_protected_mutation_forbidden",
                "path": str(resolved),
                "message": (
                    "Authentication is required before a protected-path mutation "
                    "can be submitted for approval."
                ),
            },
            False,
            (),
        )

    action_kinds = {
        "write_file": "fs.write",
        "edit_file": "fs.edit",
        "edit_source": "fs.edit_source",
        "create_source": "fs.create_source",
    }
    action = ElevationAction(
        tool_name=tool_name,
        action_kind=action_kinds.get(tool_name, "fs.write"),
        argv=(tool_name, str(resolved)),
        cwd=str(_workspace_root() or Path.cwd()),
        sandbox_permissions="require_escalated",
        justification=(
            justification.strip()
            or f"Modify the exact Safe-mode protected path: {original_path}"
        ),
        target_paths=((str(resolved), "write"),),
        content_digest=content_digest,
        risk_markers=("safe_policy_protected_path",),
        prefix_rule=tuple(prefix_rule) if prefix_rule is not None else None,
        display=ApprovalDisplay(
            kind="modify" if resolved.exists() or resolved.is_symlink() else "create",
            target=str(resolved),
        ),
    )
    if resolved.exists() or resolved.is_symlink():
        destructive = await DestructiveBackupGate().evaluate(
            action,
            approval_id=approval_id,
            targets=(resolved,),
            policy=active_sandbox_policy(),
            state_dir=state_dir,
            session_key=ctx.session_key if ctx is not None else None,
        )
        if not destructive.allowed:
            envelope = destructive.envelope or {
                "status": "blocked",
                "reason": "destructive_backup_gate_failed",
            }
            envelope.update(
                {
                    "reason": decision.code
                    or "sensitive_file_mutation_requires_approval",
                    "path": str(resolved),
                    "matched_path": (
                        str(decision.matched_path)
                        if decision.matched_path is not None
                        else None
                    ),
                }
            )
            return envelope, False, ()
        return None, True, summarize_backup_receipts(destructive.receipts)
    gate = gate_elevated_action(
        action,
        approval_id=approval_id,
        session_key=ctx.session_key if ctx is not None else None,
        file_system_profile=FileSystemPermissionProfile.full_access(),
    )
    if not gate.allowed:
        envelope = gate.to_envelope()
        envelope.update(
            {
                "reason": decision.code or "sensitive_file_mutation_requires_approval",
                "path": str(resolved),
                "matched_path": (
                    str(decision.matched_path) if decision.matched_path is not None else None
                ),
            }
        )
        return envelope, False, ()
    return None, True, ()


def _backup_receipt_note(
    summaries: tuple[BackupReceiptSummary, ...],
) -> str:
    if not summaries:
        return ""
    return "\n" + json.dumps(
        {"recoverableBackups": list(summaries)},
        ensure_ascii=False,
        separators=(",", ":"),
    )


@tool(
    name="read_file",
    description=(
        "Read UTF-8 text file contents with line numbers. Supports offset and limit. "
        "Before modifying an existing workspace file with edit_file or write_file, "
        "read it once without offset or limit to establish fresh edit context. "
        "Use offset/limit for inspection windows only. For CSV/TSV/Excel workbook "
        "data, use read_spreadsheet."
    ),
    params={
        "path": {"type": "string", "description": "Absolute path to the file."},
        "offset": {
            "type": "integer",
            "description": "Line offset to start reading from (1-indexed).",
        },
        "limit": {"type": "integer", "description": "Maximum number of lines to read."},
    },
    required=["path"],
    plan_access=PlanAccess.READ_ONLY,
    sandbox=SandboxToolDescriptor.filesystem(
        kind="read_file",
        argv_factory=lambda a: ("read_file", str(a.get("path", ""))),
        request_factory=_tool_path_request,
        enforce=False,
        record_payload=False,
    ),
)
async def read_file(path: str, offset: int | None = None, limit: int | None = None) -> str:
    p = _resolve_path(path)
    blocked = _sensitive_access_block("read_file", p, path)
    if blocked is not None:
        return json.dumps(blocked)
    path_access = _sandbox_path_access_envelope(p, write=False)
    if path_access is not None:
        return json.dumps(path_access)
    _gate_workspace_strict_read("read_file", p, path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not p.is_file():
        raise IsADirectoryError(f"Path is a directory: {path}")
    workspace = _filesystem_operation_workspace()
    if workspace is not None:
        sandbox_result = await _run_sandbox_operation_if_required(
            SandboxOperation.filesystem(
                kind="read_file",
                workspace=workspace,
                run_mode=_active_filesystem_run_mode(),
                path=p,
                paths=(p,),
                display_path=path,
                offset=offset,
                limit=limit,
            )
        )
        if sandbox_result is not None:
            record_workspace_file_read(
                p,
                operation="read_file",
                offset=offset,
                limit=limit,
                complete=limit is None and (offset is None or offset <= 1),
            )
            return str(getattr(sandbox_result, "message"))

    loop = asyncio.get_event_loop()
    sample: bytes = await loop.run_in_executor(None, _read_binary_sample, p)
    if not sample:
        record_workspace_file_read(
            p,
            operation="read_file",
            offset=offset,
            limit=limit,
            complete=limit is None and (offset is None or offset <= 1),
        )
        return ""

    binary_reason = _looks_binary(sample, p)
    if binary_reason:
        raise _binary_file_error(path, p, reason=binary_reason)

    output = await loop.run_in_executor(
        None,
        lambda: _stream_numbered_lines_from_file(p, path, offset=offset, limit=limit),
    )
    record_workspace_file_read(
        p,
        operation="read_file",
        offset=offset,
        limit=limit,
        complete=limit is None and (offset is None or offset <= 1),
    )
    return output


@tool(
    name="read_source",
    description=(
        "Read a UTF-8 workspace source line range and return a JSON revision receipt. "
        "Use the returned revision as edit_source.expected_revision before editing "
        "that file. Returned lines are plain text without read_file line-number prefixes."
    ),
    params={
        "path": {
            "type": "string",
            "description": "Workspace-relative or absolute path to the source file.",
        },
        "start_line": {
            "type": "integer",
            "description": "Inclusive 1-based first source line to read (default 1).",
        },
        "end_line": {
            "type": "integer",
            "description": (
                "Inclusive 1-based last source line to read. If omitted, read up to "
                "200 lines from start_line or until end of file."
            ),
        },
    },
    required=["path"],
    exposed_by_default=False,
    plan_access=PlanAccess.READ_ONLY,
)
async def read_source(path: str, start_line: int = 1, end_line: int | None = None) -> str:
    p = _resolve_path(path)
    blocked = _sensitive_access_block("read_source", p, path)
    if blocked is not None:
        return json.dumps(blocked)
    path_access = _sandbox_path_access_envelope(p, write=False)
    if path_access is not None:
        return json.dumps(path_access)
    _gate_workspace_strict_read("read_source", p, path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not p.is_file():
        raise IsADirectoryError(f"Path is a directory: {path}")
    loop = asyncio.get_event_loop()
    sample: bytes = await loop.run_in_executor(None, _read_binary_sample, p)
    if sample:
        binary_reason = _looks_binary(sample, p)
        if binary_reason:
            raise _binary_file_error(path, p, reason=binary_reason)
    try:
        receipt = await loop.run_in_executor(
            None,
            lambda: build_line_receipt(
                p,
                start_line=start_line,
                end_line=end_line,
                display_path=_workspace_display_path(p, path),
            ),
        )
    except UnicodeDecodeError as exc:
        raise _binary_file_error(path, p, reason="not valid UTF-8") from exc

    record_workspace_file_read(
        p,
        operation="read_source",
        offset=receipt["range"][0],
        limit=receipt["range"][1] - receipt["range"][0] + 1,
        complete=(receipt["range"][0] == 1 and receipt["range"][1] == receipt["total_lines"]),
    )
    return json.dumps(receipt, ensure_ascii=False)


@tool(
    name="read_spreadsheet",
    description=(
        "Read CSV, TSV, or Excel .xlsx files as structured text tables. "
        "When reading .xlsx, all sheets are returned by default; pass sheet as "
        "a sheet name or 1-based index to read one sheet."
    ),
    params={
        "path": {"type": "string", "description": "Path to a .csv, .tsv, or .xlsx file."},
        "sheet": {
            "type": "string",
            "description": "Optional sheet name or 1-based sheet index for .xlsx files.",
        },
        "offset": {
            "type": "integer",
            "description": "Row offset to start reading from (1-indexed, default 1).",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum rows per sheet to return (default 200).",
        },
    },
    required=["path"],
    plan_access=PlanAccess.READ_ONLY,
    sandbox=SandboxToolDescriptor.filesystem(
        kind="read_spreadsheet",
        argv_factory=lambda a: ("read_spreadsheet", str(a.get("path", ""))),
        request_factory=_tool_path_request,
        enforce=False,
        record_payload=False,
    ),
)
async def read_spreadsheet(
    path: str,
    sheet: str | int | None = None,
    offset: int | None = None,
    limit: int | None = None,
) -> str:
    p = _resolve_path(path)
    blocked = _sensitive_access_block("read_spreadsheet", p, path)
    if blocked is not None:
        return json.dumps(blocked)
    path_access = _sandbox_path_access_envelope(p, write=False)
    if path_access is not None:
        return json.dumps(path_access)
    _gate_workspace_strict_read("read_spreadsheet", p, path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not p.is_file():
        raise IsADirectoryError(f"Path is a directory: {path}")

    record_workspace_file_read(p, operation="read_spreadsheet", offset=offset, limit=limit)
    ext = p.suffix.lower()
    row_offset = offset if offset and offset > 0 else 1
    row_limit = limit if limit and limit > 0 else 200
    loop = asyncio.get_event_loop()

    if ext in {".csv", ".tsv"}:
        delimiter = "\t" if ext == ".tsv" else ","
        sheets = await loop.run_in_executor(None, _read_delimited_rows, p, delimiter)
    elif ext == ".xlsx":
        sheets = await loop.run_in_executor(None, _read_xlsx_sheets, p)
    else:
        raise ToolError(
            f"Unsupported spreadsheet format: {ext or '(none)'}. Use .csv, .tsv, or .xlsx."
        )

    selected = _select_spreadsheet_sheets(sheets, sheet)
    return _format_spreadsheet(path=p, sheets=selected, offset=row_offset, limit=row_limit)


def _read_delimited_rows(path: Path, delimiter: str) -> list[tuple[str, list[list[str]]]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ToolError(f"Cannot read spreadsheet as UTF-8 text: {path}") from exc
    rows = [[cell for cell in row] for row in csv.reader(text.splitlines(), delimiter=delimiter)]
    return [(path.name, rows)]


def _read_xlsx_sheets(path: Path) -> list[tuple[str, list[list[str]]]]:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "xl/workbook.xml" not in names:
                raise ToolError(f"Invalid .xlsx workbook: missing xl/workbook.xml in {path}")
            shared_strings = _read_xlsx_shared_strings(zf, names)
            workbook = ET.fromstring(zf.read("xl/workbook.xml"))
            rels = _read_xlsx_workbook_relationships(zf, names)
            sheets: list[tuple[str, list[list[str]]]] = []
            for sheet_el in workbook.findall(f".//{{{_XLSX_MAIN_NS}}}sheet"):
                sheet_name = sheet_el.attrib.get("name") or f"Sheet{len(sheets) + 1}"
                rel_id = sheet_el.attrib.get(f"{{{_XLSX_OFFICE_REL_NS}}}id")
                target = rels.get(rel_id or "")
                if not target:
                    continue
                worksheet_path = _normalize_xlsx_target(target)
                if worksheet_path not in names:
                    continue
                rows = _read_xlsx_worksheet(zf.read(worksheet_path), shared_strings)
                sheets.append((sheet_name, rows))
            if not sheets:
                raise ToolError(f"No readable worksheets found in {path}")
            return sheets
    except zipfile.BadZipFile as exc:
        raise ToolError(f"Invalid .xlsx workbook: {path}") from exc
    except ET.ParseError as exc:
        raise ToolError(f"Invalid .xlsx XML content in {path}: {exc}") from exc


def _read_xlsx_shared_strings(zf: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    shared: list[str] = []
    for si in root.findall(f".//{{{_XLSX_MAIN_NS}}}si"):
        texts = [node.text or "" for node in si.findall(f".//{{{_XLSX_MAIN_NS}}}t")]
        shared.append("".join(texts))
    return shared


def _read_xlsx_workbook_relationships(
    zf: zipfile.ZipFile,
    names: set[str],
) -> dict[str, str]:
    rels_path = "xl/_rels/workbook.xml.rels"
    if rels_path not in names:
        return {}
    root = ET.fromstring(zf.read(rels_path))
    rels: dict[str, str] = {}
    for rel in root.findall(f".//{{{_XLSX_PACKAGE_REL_NS}}}Relationship"):
        rel_id = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if rel_id and target:
            rels[rel_id] = target
    return rels


def _normalize_xlsx_target(target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join("xl", target))


def _read_xlsx_worksheet(raw_xml: bytes, shared_strings: list[str]) -> list[list[str]]:
    root = ET.fromstring(raw_xml)
    rows: list[list[str]] = []
    for row_el in root.findall(f".//{{{_XLSX_MAIN_NS}}}row"):
        row: list[str] = []
        for cell_el in row_el.findall(f"{{{_XLSX_MAIN_NS}}}c"):
            column_index = _xlsx_column_index(cell_el.attrib.get("r", ""))
            while len(row) < column_index:
                row.append("")
            row.append(_xlsx_cell_value(cell_el, shared_strings))
        while row and row[-1] == "":
            row.pop()
        rows.append(row)
    return rows


def _xlsx_column_index(cell_ref: str) -> int:
    match = re.match(r"([A-Za-z]+)", cell_ref)
    if not match:
        return 0
    index = 0
    for char in match.group(1).upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return max(0, index - 1)


def _xlsx_cell_value(cell_el: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell_el.attrib.get("t")
    if cell_type == "inlineStr":
        texts = [node.text or "" for node in cell_el.findall(f".//{{{_XLSX_MAIN_NS}}}t")]
        return "".join(texts)

    value_el = cell_el.find(f"{{{_XLSX_MAIN_NS}}}v")
    raw = value_el.text if value_el is not None else ""
    if cell_type == "s" and raw:
        try:
            return shared_strings[int(raw)]
        except (IndexError, ValueError):
            return ""
    if cell_type == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return raw or ""


def _select_spreadsheet_sheets(
    sheets: list[tuple[str, list[list[str]]]],
    requested: str | int | None,
) -> list[tuple[str, list[list[str]]]]:
    if requested is None or requested == "":
        return sheets

    if isinstance(requested, int) or (isinstance(requested, str) and requested.isdigit()):
        index = int(requested) - 1
        if 0 <= index < len(sheets):
            return [sheets[index]]

    requested_name = str(requested)
    for name, rows in sheets:
        if name == requested_name:
            return [(name, rows)]
    for name, rows in sheets:
        if name.lower() == requested_name.lower():
            return [(name, rows)]

    available = ", ".join(name for name, _ in sheets)
    raise ToolError(f"Sheet not found: {requested_name}. Available sheets: {available}")


def _format_spreadsheet(
    *,
    path: Path,
    sheets: list[tuple[str, list[list[str]]]],
    offset: int,
    limit: int,
) -> str:
    parts = [f"Workbook: {path.name}"]
    start = max(0, offset - 1)
    for sheet_name, rows in sheets:
        width = max((len(row) for row in rows), default=0)
        parts.append("")
        parts.append(f"Sheet: {sheet_name} ({len(rows)} rows x {width} columns)")
        selected = rows[start : start + limit]
        for idx, row in enumerate(selected, start=start + 1):
            parts.append(f"{idx}\t" + "\t".join(row))
        if start + limit < len(rows):
            end = start + len(selected)
            parts.append(
                f"(Showing rows {offset}-{end} of {len(rows)}. Use offset={end + 1} to continue.)"
            )
    return "\n".join(parts)


@tool(
    name="write_file",
    description=(
        "Write full file content, creating directories as needed. Best for new "
        "files and scratch files. For existing workspace source files, first use "
        "read_file without offset or limit, then prefer edit_file for exact "
        "replacements or apply_patch for multi-line hunks."
    ),
    params={
        "path": {"type": "string", "description": "Absolute path to write to."},
        "content": {
            "type": "string",
            "description": "Complete file content to write; not a patch fragment.",
        },
        "sandbox_permissions": {
            "type": "string",
            "enum": ["use_default", "require_escalated"],
            "description": "Use require_escalated only for an exact out-of-root write.",
        },
        "justification": {
            "type": "string",
            "description": "Short user-facing reason for the exact elevated write.",
        },
        "prefix_rule": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional narrow prefix suggestion; never persisted by auto review.",
        },
        "approval_id": {
            "type": "string",
            "description": "Sandbox path approval record for writes outside the workspace.",
        },
    },
    required=["path", "content"],
    runtime_only_arguments=("approval_id",),
    sandbox=SandboxToolDescriptor.filesystem(
        kind="fs.write",
        argv_factory=lambda a: ("fs.write", str(a.get("path", ""))),
        request_factory=_tool_path_request,
        enforce=False,
        record_payload=False,
    ),
)
async def write_file(
    path: str,
    content: str,
    approval_id: str | None = None,
    sandbox_permissions: str = "use_default",
    justification: str = "",
    prefix_rule: list[str] | None = None,
) -> str:
    p = _resolve_path(path)
    if full_host_access_active():
        created = not p.exists()

        def _write_full_host() -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

        def _settle_full_host(error: BaseException | None) -> None:
            if error is None:
                record_workspace_file_write(p, operation="write_file", created=created)
                _notify_memory_source_write(p)
                _notify_bootstrap_source_write(p)

        await _run_executor_mutation(
            _write_full_host,
            settle=_settle_full_host,
        )
        return f"Written {len(content)} bytes to {p}"

    approval, elevated, backup_summaries = await _gate_out_of_workspace_write(
        "write_file",
        p,
        path,
        approval_id,
        sandbox_permissions=sandbox_permissions,
        justification=justification,
        content_digest=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        prefix_rule=prefix_rule,
    )
    if approval is not None:
        return json.dumps(approval)

    created = not p.exists()
    if not created:
        require_fresh_workspace_file_read(p, tool_name="write_file", original_path=path)
        _gate_write_file_destructive_overwrite(p, content, original_path=path)
    before_fingerprint = fingerprint_path(p)
    workspace = _filesystem_operation_workspace()
    if workspace is not None:
        sandbox_result = await _run_sandbox_operation_if_required(
            SandboxOperation.filesystem(
                kind="write_text",
                workspace=workspace,
                run_mode=_active_filesystem_run_mode(),
                path=p,
                paths=(p,),
                content=content,
            ),
            host_execution_active=elevated,
        )
        if sandbox_result is not None:
            created = bool(getattr(sandbox_result, "created", created))
            after_fingerprint = fingerprint_path(p)
            record_semantic_mutation_receipt(
                tool_name="write_file",
                path=p,
                operation="write_file",
                before=before_fingerprint,
                after=after_fingerprint,
                partial=False,
                metadata={"created": created},
            )
            record_workspace_file_write(p, operation="write_file", created=created)
            refresh_workspace_file_read_state(p, operation="write_file")
            record_scratch_file_write(p)
            _notify_memory_source_write(p)
            _notify_bootstrap_source_write(p)
            return str(getattr(sandbox_result, "message")) + _backup_receipt_note(
                backup_summaries
            )

    def _write() -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def _settle_write(error: BaseException | None) -> None:
        after_fingerprint = fingerprint_path(p)
        changed = before_fingerprint != after_fingerprint
        record_semantic_mutation_receipt(
            tool_name="write_file",
            path=p,
            operation="write_file",
            before=before_fingerprint,
            after=after_fingerprint,
            partial=error is not None,
            metadata={"created": created},
        )
        if error is None or changed:
            record_workspace_file_write(p, operation="write_file", created=created)
            refresh_workspace_file_read_state(p, operation="write_file")
            record_scratch_file_write(p)
            _notify_memory_source_write(p)
            _notify_bootstrap_source_write(p)

    await _run_executor_mutation(_write, settle=_settle_write)
    return (
        f"Written {len(content)} bytes to {p}{_write_scope_suffix(p)}"
        f"{_backup_receipt_note(backup_summaries)}"
    )


def _resolve_scratch_write_path(path: str) -> tuple[Path, str]:
    ctx = current_tool_context.get()
    if ctx is None or not ctx.scratch_dir:
        raise ToolError("write_scratch requires a configured scratch_dir.")
    scratch = Path(ctx.scratch_dir).expanduser().resolve(strict=False)
    raw = Path(path).expanduser()
    resolved = (
        raw.resolve(strict=False) if raw.is_absolute() else (scratch / raw).resolve(strict=False)
    )
    try:
        relative_path = resolved.relative_to(scratch).as_posix()
    except ValueError as exc:
        raise ToolError(
            f"write_scratch only writes inside the configured scratch directory: {path}"
        ) from exc
    if not relative_path or relative_path == ".":
        raise ToolError("write_scratch requires a file path inside the scratch directory.")
    if ctx.workspace_dir:
        workspace = Path(ctx.workspace_dir).expanduser().resolve(strict=False)
        try:
            resolved.relative_to(workspace)
        except ValueError:
            pass
        else:
            try:
                scratch_relative = scratch.relative_to(workspace)
            except ValueError as exc:
                raise ToolError(
                    "write_scratch refused a workspace target because scratch_dir "
                    "contains the active workspace."
                ) from exc
            if not scratch_relative.parts:
                raise ToolError(
                    "write_scratch refused a workspace target because scratch_dir "
                    "equals the active workspace."
                )
    return resolved, relative_path


@tool(
    name="write_scratch",
    description=(
        "Write a temporary scratch file for reproduction scripts, logs, debug output, "
        "or experiments. This tool only writes inside the configured scratch directory "
        "and is not a substitute for repository source changes."
    ),
    params={
        "path": {
            "type": "string",
            "description": (
                "Scratch-relative path, or an absolute path under the scratch directory."
            ),
        },
        "content": {
            "type": "string",
            "description": "Complete scratch file content to write.",
        },
    },
    required=["path", "content"],
    exposed_by_default=False,
    sandbox=SandboxToolDescriptor.filesystem(
        kind="fs.write",
        argv_factory=lambda a: ("fs.write_scratch", str(a.get("path", ""))),
        request_factory=_tool_path_request,
        record_payload=False,
    ),
)
async def write_scratch(path: str, content: str) -> str:
    p, relative_path = _resolve_scratch_write_path(path)
    blocked = _sensitive_access_block("write_scratch", p, path)
    if blocked is not None:
        return json.dumps(blocked)

    before_fingerprint = fingerprint_path(p)
    settled_after: dict[str, Any] = {}

    def _write() -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def _settle_write(error: BaseException | None) -> None:
        after_fingerprint = fingerprint_path(p)
        settled_after.update(after_fingerprint)
        if error is None or before_fingerprint != after_fingerprint:
            record_scratch_file_write(p)

    await _run_executor_mutation(_write, settle=_settle_write)
    after_fingerprint = settled_after
    result = {
        "status": "written",
        "path": relative_path,
        "scratch": True,
        "changed": before_fingerprint.get("sha256") != after_fingerprint.get("sha256")
        or before_fingerprint.get("exists") != after_fingerprint.get("exists"),
        "bytes": len(content.encode("utf-8")),
        "sha256": after_fingerprint.get("sha256"),
    }
    return json.dumps(result, ensure_ascii=False)


@tool(
    name="create_source",
    description=(
        "Create a new UTF-8 production workspace file. Use this for new source, "
        "configuration, or documentation files that should appear in the final git diff. "
        "It refuses to overwrite existing files; use read_source/edit_source for existing files."
    ),
    params={
        "path": {
            "type": "string",
            "description": "Workspace-relative or absolute path for the new repository file.",
        },
        "content": {
            "type": "string",
            "description": "Complete file content to create.",
        },
        "approval_id": {
            "type": "string",
            "description": (
                "Reserved for compatibility; create_source only writes inside the workspace."
            ),
        },
    },
    required=["path", "content"],
    runtime_only_arguments=("approval_id",),
    exposed_by_default=False,
    sandbox=SandboxToolDescriptor.filesystem(
        kind="fs.write",
        argv_factory=lambda a: ("fs.create_source", str(a.get("path", ""))),
        request_factory=_tool_path_request,
        record_payload=False,
    ),
)
async def create_source(path: str, content: str, approval_id: str | None = None) -> str:
    p = _resolve_path(path)
    blocked = _sensitive_access_block("create_source", p, path)
    if blocked is not None:
        return json.dumps(blocked)
    if not full_host_access_active():
        profile = active_file_system_profile(_workspace_root())
        decision = (
            decide_path_access(
                p,
                workspace=_workspace_root(),
                mounts=_active_sandbox_mounts(),
                write=True,
                profile=profile,
                logical_path=path,
            )
            if profile is not None
            else None
        )
        if decision is not None and decision.status != "allowed":
            blocked_envelope = _path_access_blocked_envelope(decision)
            blocked_envelope["message"] = (
                "create_source cannot write a non-writable workspace carveout. "
                "Use Full Host Access only when the user explicitly requests "
                "that exact protected change."
            )
            return json.dumps(blocked_envelope)
    _gate_workspace_lockdown_write("create_source", p, path)
    workspace = _workspace_root()
    if workspace is None:
        raise ToolError("create_source requires an active workspace_dir.")
    if _is_outside_workspace(p):
        raise WorkspaceAccessError(f"create_source only writes inside the active workspace: {path}")
    if _is_under_configured_scratch_dir(p):
        raise ToolError("create_source refused a scratch path; use write_scratch instead.")
    sandbox_result = await _run_sandbox_operation_if_required(
        SandboxOperation.filesystem(
            kind="create_source",
            workspace=workspace,
            run_mode=_active_filesystem_run_mode(),
            path=p,
            logical_path=Path(logical_tool_path(path, base=workspace)),
            paths=(p,),
            content=content,
        )
    )
    if sandbox_result is not None:
        metadata = getattr(sandbox_result, "metadata", {})
        after_revision = metadata.get("afterRevision")
        before_fingerprint = metadata.get("beforeFingerprint")
        after_fingerprint = metadata.get("afterFingerprint")
        if (
            not isinstance(after_revision, str)
            or not isinstance(before_fingerprint, dict)
            or not isinstance(after_fingerprint, dict)
        ):
            raise ToolError("create_source worker returned an invalid revision receipt.")
        display_path = _workspace_display_path(p, path)
        receipt = record_semantic_mutation_receipt(
            tool_name="create_source",
            path=p,
            operation="create_source",
            before=before_fingerprint,
            after=after_fingerprint,
            partial=False,
            metadata={
                "after_revision": after_revision,
                "created": True,
                "contract": "source_create_v1",
            },
        )
        workspace_epoch = receipt["workspace_epoch"] if receipt is not None else None
        record_workspace_file_write(p, operation="create_source", created=True)
        refresh_workspace_file_read_state(p, operation="create_source")
        _notify_memory_source_write(p)
        _notify_bootstrap_source_write(p)
        return json.dumps(
            {
                "status": "created",
                "path": display_path,
                "changed": True,
                "after_revision": after_revision,
                "workspace_epoch": workspace_epoch,
                "diff_summary": build_diff_summary("", content, path=display_path),
            },
            ensure_ascii=False,
        )

    if p.exists():
        raise RetryableToolInputError(
            f"create_source refused because the file already exists: {path}. "
            "Use read_source/edit_source for existing files."
        )
    before_fingerprint = fingerprint_path(p)
    settled_create: dict[str, Any] = {}

    def _write() -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("x", encoding="utf-8") as handle:
            handle.write(content)

    display_path = _workspace_display_path(p, path)

    def _settle_create(error: BaseException | None) -> None:
        if isinstance(error, FileExistsError):
            # Exclusive open proves this worker did not create or write the
            # file. A racing external writer must not be attributed to the
            # tool through a synthetic mutation receipt.
            return
        after_fingerprint = fingerprint_path(p)
        after_revision = (
            source_revision_for_path(p)
            if after_fingerprint.get("exists") and p.is_file()
            else None
        )
        metadata: dict[str, Any] = {
            "created": True,
            "contract": "source_create_v1",
        }
        if after_revision is not None:
            metadata["after_revision"] = after_revision
        receipt = record_semantic_mutation_receipt(
            tool_name="create_source",
            path=p,
            operation="create_source",
            before=before_fingerprint,
            after=after_fingerprint,
            partial=error is not None,
            metadata=metadata,
        )
        settled_create.update(
            {
                "after_fingerprint": after_fingerprint,
                "after_revision": after_revision,
                "workspace_epoch": (
                    receipt["workspace_epoch"] if receipt is not None else None
                ),
            }
        )
        if error is None or before_fingerprint != after_fingerprint:
            record_workspace_file_write(p, operation="create_source", created=True)
            refresh_workspace_file_read_state(p, operation="create_source")
            _notify_memory_source_write(p)
            _notify_bootstrap_source_write(p)

    await _run_executor_mutation(_write, settle=_settle_create)
    after_revision = settled_create.get("after_revision")
    if not isinstance(after_revision, str):
        raise ToolError("create_source write settled without a valid revision.")
    workspace_epoch = settled_create.get("workspace_epoch")
    result = {
        "status": "created",
        "path": display_path,
        "changed": True,
        "after_revision": after_revision,
        "workspace_epoch": workspace_epoch,
        "diff_summary": build_diff_summary("", content, path=display_path),
    }
    return json.dumps(result, ensure_ascii=False)


def _gate_write_file_destructive_overwrite(
    path: Path,
    content: str,
    *,
    original_path: str,
) -> None:
    """Block likely accidental whole-file truncation through write_file.

    `write_file` is useful for new files and scratch files, but LLMs sometimes
    use it with a replacement fragment for an existing source file. That turns
    a local edit into a full-file overwrite and often leaves the worktree
    unusable. Large existing workspace files should be edited with
    `edit_file` or `apply_patch` instead.
    """

    if full_host_access_active():
        return
    workspace = _workspace_root()
    if workspace is None or _is_outside_workspace(path):
        return
    ctx = current_tool_context.get()
    if ctx is not None and ctx.scratch_dir:
        scratch = Path(ctx.scratch_dir).expanduser().resolve(strict=False)
        try:
            path.relative_to(scratch)
            return
        except ValueError:
            pass

    try:
        old_bytes = path.read_bytes()
    except OSError:
        return
    old_size = len(old_bytes)
    new_size = len(content.encode("utf-8"))
    if old_size < 4096:
        return
    if new_size >= max(2048, old_size // 2):
        return

    alternatives = _existing_file_edit_alternatives()
    if alternatives:
        guidance = (
            f"If you are editing an existing file, use {' or '.join(alternatives)} "
            "with a precise edit instead of replacing the whole file."
        )
    else:
        guidance = (
            "If you are editing an existing file, only rewrite the whole file "
            "when the complete replacement content is intended."
        )
    raise SafeToolError(
        "write_file refused to overwrite an existing workspace file with much "
        f"smaller content: {original_path} would shrink from {old_size} bytes "
        f"to {new_size} bytes. {guidance}"
    )


def _tool_visible_in_current_context(name: str) -> bool:
    ctx = current_tool_context.get()
    if ctx is None:
        return True
    if name in ctx.denied_tools:
        return False
    if ctx.allowed_tools is not None and name not in ctx.allowed_tools:
        return False
    return True


def _existing_file_edit_alternatives() -> list[str]:
    return [name for name in ("edit_file", "apply_patch") if _tool_visible_in_current_context(name)]


def _edit_file_retry_guidance(*, duplicate_match: bool = False) -> str:
    if _tool_visible_in_current_context("apply_patch"):
        if duplicate_match:
            return "or use apply_patch with a line-specific hunk."
        return "or use apply_patch with a precise hunk."
    if duplicate_match:
        return "or retry with a longer unique exact old_text/new_text replacement."
    return "or retry with a smaller exact old_text/new_text replacement."


@tool(
    name="edit_file",
    description=(
        "Edit one existing workspace file using exact text replacement after reading "
        "the current file with read_file without offset or limit. For one change, "
        "pass old_text and new_text. For multiple non-overlapping replacements in "
        "the same file, pass edits[] with old_text/new_text or oldText/newText "
        "entries. old_text must match file contents exactly and must not include "
        "read_file line-number prefixes such as '12\\t'. For large or line-oriented "
        "changes, prefer apply_patch with a small hunk."
    ),
    params={
        "path": {"type": "string", "description": "Absolute path to the file to edit."},
        "old_text": {
            "type": "string",
            "description": (
                "Exact text to find and replace for a single edit. Do not include "
                "read_file line-number prefixes."
            ),
        },
        "new_text": {"type": "string", "description": "Replacement text."},
        "edits": {
            "type": "array",
            "description": (
                "Multiple exact replacements matched against the original file. "
                "Each edits[].old_text must be unique and non-overlapping. Merge "
                "nearby or overlapping changes into one edit."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "old_text": {
                        "type": "string",
                        "description": "Exact text to replace.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                    "oldText": {
                        "type": "string",
                        "description": "Compatibility alias for old_text.",
                    },
                    "newText": {
                        "type": "string",
                        "description": "Compatibility alias for new_text.",
                    },
                },
            },
        },
        "sandbox_permissions": {
            "type": "string",
            "enum": ["use_default", "require_escalated"],
            "description": "Use require_escalated only for an exact out-of-root edit.",
        },
        "justification": {
            "type": "string",
            "description": "Short user-facing reason for the exact elevated edit.",
        },
        "prefix_rule": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional narrow prefix suggestion; never persisted by auto review.",
        },
        "approval_id": {
            "type": "string",
            "description": "Sandbox path approval record for edits outside the workspace.",
        },
    },
    required=["path"],
    runtime_only_arguments=("approval_id",),
    sandbox=SandboxToolDescriptor.filesystem(
        kind="fs.edit",
        argv_factory=lambda a: ("fs.edit", str(a.get("path", ""))),
        request_factory=_tool_path_request,
        enforce=False,
        record_payload=False,
    ),
)
async def edit_file(
    path: str,
    old_text: str | None = None,
    new_text: str | None = None,
    approval_id: str | None = None,
    edits: list[dict[str, Any]] | str | None = None,
    sandbox_permissions: str = "use_default",
    justification: str = "",
    prefix_rule: list[str] | None = None,
) -> str:
    p = _resolve_path(path)
    replacements = _normalize_edit_replacements(
        path=path,
        old_text=old_text,
        new_text=new_text,
        edits=edits,
    )
    if full_host_access_active():
        if not p.exists():
            raise FileNotFoundError(f"File not found: {path}")
        loop = asyncio.get_event_loop()
        original = await loop.run_in_executor(None, p.read_text, "utf-8")
        updated = _apply_edit_replacements(original, replacements, path=path)
        before_fingerprint = fingerprint_path(p)

        def _write_full_host() -> int:
            return p.write_text(updated, encoding="utf-8")

        def _settle_full_host(error: BaseException | None) -> None:
            after_fingerprint = fingerprint_path(p)
            if error is None or before_fingerprint != after_fingerprint:
                record_workspace_file_write(p, operation="edit_file", created=False)
                _notify_memory_source_write(p)
                _notify_bootstrap_source_write(p)

        await _run_executor_mutation(
            _write_full_host,
            settle=_settle_full_host,
        )
        if len(replacements) == 1:
            replacement = replacements[0]
            return (
                f"Edited {p}: replaced {len(replacement.old_text)} chars with "
                f"{len(replacement.new_text)} chars"
            )
        return f"Edited {p}: applied {len(replacements)} replacements"

    edit_digest = hashlib.sha256(
        json.dumps(
            [{"old_text": item.old_text, "new_text": item.new_text} for item in replacements],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    approval, elevated, backup_summaries = await _gate_out_of_workspace_write(
        "edit_file",
        p,
        path,
        approval_id,
        sandbox_permissions=sandbox_permissions,
        justification=justification,
        content_digest=edit_digest,
        prefix_rule=prefix_rule,
    )
    if approval is not None:
        return json.dumps(approval)
    require_fresh_workspace_file_read(p, tool_name="edit_file", original_path=path)
    workspace = _filesystem_operation_workspace()
    if workspace is not None:
        if len(replacements) == 1:
            sandbox_replacement = replacements[0]
            before_fingerprint = fingerprint_path(p)
            sandbox_result = await _run_sandbox_operation_if_required(
                SandboxOperation.filesystem(
                    kind="edit_text",
                    workspace=workspace,
                    run_mode=_active_filesystem_run_mode(),
                    path=p,
                    paths=(p,),
                    old_text=sandbox_replacement.old_text,
                    new_text=sandbox_replacement.new_text,
                ),
                host_execution_active=elevated,
            )
            if sandbox_result is not None:
                after_fingerprint = fingerprint_path(p)
                record_semantic_mutation_receipt(
                    tool_name="edit_file",
                    path=p,
                    operation="edit_file",
                    before=before_fingerprint,
                    after=after_fingerprint,
                    partial=False,
                    metadata={"replacement_count": len(replacements)},
                )
                record_workspace_file_write(p, operation="edit_file", created=False)
                refresh_workspace_file_read_state(p, operation="edit_file")
                record_scratch_file_write(p)
                _notify_memory_source_write(p)
                _notify_bootstrap_source_write(p)
                return str(getattr(sandbox_result, "message")) + _backup_receipt_note(
                    backup_summaries
                )
        elif _sandbox_path_access_enabled() and not elevated:
            raise RetryableToolInputError(
                "edit_file accepts only one replacement per call when edits are "
                "dispatched through the sandbox runtime. Retry with a single "
                "old_text/new_text edit at a time."
            )
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    loop = asyncio.get_event_loop()
    original = await loop.run_in_executor(None, p.read_text, "utf-8")
    before_fingerprint = fingerprint_path(p)

    updated = _apply_edit_replacements(original, replacements, path=path)

    def _write() -> None:
        p.write_text(updated, encoding="utf-8")

    def _settle_edit(error: BaseException | None) -> None:
        after_fingerprint = fingerprint_path(p)
        changed = before_fingerprint != after_fingerprint
        record_semantic_mutation_receipt(
            tool_name="edit_file",
            path=p,
            operation="edit_file",
            before=before_fingerprint,
            after=after_fingerprint,
            partial=error is not None,
            metadata={"replacement_count": len(replacements)},
        )
        if error is None or changed:
            record_workspace_file_write(p, operation="edit_file", created=False)
            refresh_workspace_file_read_state(p, operation="edit_file")
            record_scratch_file_write(p)
            _notify_memory_source_write(p)
            _notify_bootstrap_source_write(p)

    await _run_executor_mutation(_write, settle=_settle_edit)
    if len(replacements) == 1:
        replacement = replacements[0]
        return (
            f"Edited {p}: replaced {len(replacement.old_text)} chars with "
            f"{len(replacement.new_text)} chars{_write_scope_suffix(p)}"
            f"{_backup_receipt_note(backup_summaries)}"
        )
    return (
        f"Edited {p}: applied {len(replacements)} replacements{_write_scope_suffix(p)}"
        f"{_backup_receipt_note(backup_summaries)}"
    )


@tool(
    name="edit_source",
    description=(
        "Atomically edit an existing UTF-8 source file using line ranges from a prior "
        "read_source receipt. Requires expected_revision from read_source; if the file "
        "changed since that read, the edit is rejected and must be retried after a new "
        "read_source call."
    ),
    params={
        "path": {
            "type": "string",
            "description": "Workspace-relative or absolute path to the source file.",
        },
        "expected_revision": {
            "type": "string",
            "description": "The revision returned by the latest read_source call for this file.",
        },
        "edits": {
            "type": "array",
            "description": (
                "Non-overlapping inclusive line-range edits against the revision. "
                "Each replacement is complete source text for that range."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "start_line": {
                        "type": "integer",
                        "description": "Inclusive 1-based first line to replace.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Inclusive 1-based last line to replace.",
                    },
                    "replacement": {
                        "type": "string",
                        "description": "Replacement source text for the full line range.",
                    },
                },
                "required": ["start_line", "end_line", "replacement"],
                "additionalProperties": False,
            },
        },
        "sandbox_permissions": {
            "type": "string",
            "enum": ["use_default", "require_escalated"],
            "description": "Use require_escalated only for an exact out-of-root edit.",
        },
        "justification": {
            "type": "string",
            "description": "Short user-facing reason for the exact elevated edit.",
        },
        "prefix_rule": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional narrow prefix suggestion; never persisted by auto review.",
        },
        "approval_id": {
            "type": "string",
            "description": "Approval record to consume for edits outside the workspace.",
        },
    },
    required=["path", "expected_revision", "edits"],
    runtime_only_arguments=("approval_id",),
    exposed_by_default=False,
    sandbox=SandboxToolDescriptor.filesystem(
        kind="fs.edit",
        argv_factory=lambda a: ("fs.edit", str(a.get("path", ""))),
        request_factory=_tool_path_request,
        enforce=False,
        record_payload=False,
    ),
)
async def edit_source(
    path: str,
    expected_revision: str,
    edits: list[dict[str, Any]],
    approval_id: str | None = None,
    sandbox_permissions: str = "use_default",
    justification: str = "",
    prefix_rule: list[str] | None = None,
) -> str:
    p = _resolve_path(path)
    edit_digest = hashlib.sha256(
        json.dumps(
            {"expected_revision": expected_revision, "edits": edits},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    approval, elevated, backup_summaries = await _gate_out_of_workspace_write(
        "edit_source",
        p,
        path,
        approval_id,
        sandbox_permissions=sandbox_permissions,
        justification=justification,
        content_digest=edit_digest,
        prefix_rule=prefix_rule,
    )
    if approval is not None:
        return json.dumps(approval)
    display_path = _workspace_display_path(p, path)
    workspace = _filesystem_operation_workspace()
    if workspace is not None:
        sandbox_result = await _run_sandbox_operation_if_required(
            SandboxOperation.filesystem(
                kind="edit_source",
                workspace=workspace,
                run_mode=_active_filesystem_run_mode(),
                path=p,
                logical_path=Path(logical_tool_path(path, base=workspace)),
                paths=(p,),
                expected_revision=expected_revision,
                edits=tuple(edits),
            ),
            host_execution_active=elevated,
        )
        if sandbox_result is not None:
            metadata = getattr(sandbox_result, "metadata", {})
            status = metadata.get("status")
            if status == "revision_conflict":
                current_revision = metadata.get("beforeRevision")
                raise RetryableToolInputError(
                    "revision_conflict: edit_source expected "
                    f"{expected_revision}, but current revision is {current_revision}. "
                    "Call read_source for the current file range and retry with the new revision."
                )
            if status == "contract_error":
                raise RetryableToolInputError(str(getattr(sandbox_result, "message", "")))
            original_result = metadata.get("original")
            updated_result = metadata.get("updated")
            before_revision_result = metadata.get("beforeRevision")
            after_revision_result = metadata.get("afterRevision")
            before_fingerprint = metadata.get("beforeFingerprint")
            after_fingerprint = metadata.get("afterFingerprint")
            if (
                status != "applied"
                or not isinstance(original_result, str)
                or not isinstance(updated_result, str)
                or not isinstance(before_revision_result, str)
                or not isinstance(after_revision_result, str)
                or not isinstance(before_fingerprint, dict)
                or not isinstance(after_fingerprint, dict)
            ):
                raise ToolError("edit_source worker returned an invalid revision receipt.")
            original = original_result
            updated = updated_result
            before_revision = before_revision_result
            after_revision = after_revision_result
            receipt = record_semantic_mutation_receipt(
                tool_name="edit_source",
                path=p,
                operation="edit_source",
                before=before_fingerprint,
                after=after_fingerprint,
                partial=False,
                metadata={
                    "before_revision": before_revision,
                    "after_revision": after_revision,
                    "edit_count": len(edits),
                    "contract": "source_revision_line_edit_v1",
                },
            )
            workspace_epoch = receipt["workspace_epoch"] if receipt is not None else None
            if updated != original:
                record_workspace_file_write(p, operation="edit_source", created=False)
                refresh_workspace_file_read_state(p, operation="edit_source")
                record_scratch_file_write(p)
                _notify_memory_source_write(p)
                _notify_bootstrap_source_write(p)
            return json.dumps(
                {
                    "status": "applied",
                    "path": display_path,
                    "changed": updated != original,
                    "before_revision": before_revision,
                    "after_revision": after_revision,
                    "workspace_epoch": workspace_epoch,
                    "diff_summary": build_diff_summary(original, updated, path=display_path),
                    "recoverableBackups": list(backup_summaries),
                },
                ensure_ascii=False,
            )

    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not p.is_file():
        raise IsADirectoryError(f"Path is a directory: {path}")
    if _is_under_configured_scratch_dir(p):
        raise ToolError("edit_source refused a scratch path; use edit_file instead.")

    loop = asyncio.get_event_loop()
    try:
        original = await loop.run_in_executor(None, p.read_text, "utf-8")
    except UnicodeDecodeError as exc:
        raise _binary_file_error(path, p, reason="not valid UTF-8") from exc

    before_revision = source_revision_for_path(p)
    if before_revision != expected_revision:
        raise RetryableToolInputError(
            "revision_conflict: edit_source expected "
            f"{expected_revision}, but current revision is {before_revision}. "
            "Call read_source for the current file range and retry with the new revision."
        )

    try:
        updated = apply_line_edits(original, edits)
    except SourceEditContractError as exc:
        raise RetryableToolInputError(str(exc)) from exc

    before_fingerprint = fingerprint_path(p)
    if updated != original:
        settled_edit: dict[str, Any] = {}

        def _write() -> None:
            p.write_text(updated, encoding="utf-8")

        def _settle_edit(error: BaseException | None) -> None:
            after_fingerprint = fingerprint_path(p)
            after_revision = source_revision_for_path(p)
            receipt = record_semantic_mutation_receipt(
                tool_name="edit_source",
                path=p,
                operation="edit_source",
                before=before_fingerprint,
                after=after_fingerprint,
                partial=error is not None,
                metadata={
                    "before_revision": before_revision,
                    "after_revision": after_revision,
                    "edit_count": len(edits),
                    "contract": "source_revision_line_edit_v1",
                },
            )
            settled_edit.update(
                {
                    "after_fingerprint": after_fingerprint,
                    "after_revision": after_revision,
                    "receipt": receipt,
                }
            )
            if error is None or before_fingerprint != after_fingerprint:
                record_workspace_file_write(p, operation="edit_source", created=False)
                refresh_workspace_file_read_state(p, operation="edit_source")
                record_scratch_file_write(p)
                _notify_memory_source_write(p)
                _notify_bootstrap_source_write(p)

        await _run_executor_mutation(_write, settle=_settle_edit)
        after_revision = settled_edit["after_revision"]
        receipt = settled_edit["receipt"]
    else:
        after_fingerprint = fingerprint_path(p)
        after_revision = source_revision_for_path(p)
        receipt = record_semantic_mutation_receipt(
            tool_name="edit_source",
            path=p,
            operation="edit_source",
            before=before_fingerprint,
            after=after_fingerprint,
            partial=False,
            metadata={
                "before_revision": before_revision,
                "after_revision": after_revision,
                "edit_count": len(edits),
                "contract": "source_revision_line_edit_v1",
            },
        )
    workspace_epoch = receipt["workspace_epoch"] if receipt is not None else None

    result = {
        "status": "applied",
        "path": display_path,
        "changed": updated != original,
        "before_revision": before_revision,
        "after_revision": after_revision,
        "workspace_epoch": workspace_epoch,
        "diff_summary": build_diff_summary(original, updated, path=display_path),
        "recoverableBackups": list(backup_summaries),
    }
    return json.dumps(result, ensure_ascii=False)


class _EditReplacement:
    def __init__(self, *, old_text: str, new_text: str, label: str) -> None:
        self.old_text = old_text
        self.new_text = new_text
        self.label = label


def _first_string_field(item: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = item.get(name)
        if isinstance(value, str):
            return value
    return None


def _normalize_edit_replacements(
    *,
    path: str,
    old_text: str | None,
    new_text: str | None,
    edits: list[dict[str, Any]] | str | None,
) -> list[_EditReplacement]:
    replacements: list[_EditReplacement] = []
    if edits is not None:
        parsed_edits: Any = edits
        if isinstance(edits, str):
            try:
                parsed_edits = json.loads(edits)
            except json.JSONDecodeError as exc:
                raise RetryableToolInputError(
                    "edit_file edits must be an array or JSON array string. "
                    "Retry with edits[] entries containing old_text/new_text."
                ) from exc
        if not isinstance(parsed_edits, list):
            raise RetryableToolInputError(
                "edit_file edits must be an array of replacements. "
                "Retry with edits[] entries containing old_text/new_text."
            )
        for index, item in enumerate(parsed_edits):
            if not isinstance(item, dict):
                raise RetryableToolInputError(
                    f"edit_file edits[{index}] must be an object with old_text/new_text."
                )
            edit_old_text = _first_string_field(
                item,
                "old_text",
                "oldText",
                "old_string",
                "oldString",
            )
            edit_new_text = _first_string_field(
                item,
                "new_text",
                "newText",
                "new_string",
                "newString",
            )
            if edit_old_text is None or edit_new_text is None:
                raise RetryableToolInputError(
                    f"edit_file edits[{index}] is missing old_text/new_text. "
                    "Retry with exact old_text and replacement new_text."
                )
            replacements.append(
                _EditReplacement(
                    old_text=edit_old_text,
                    new_text=edit_new_text,
                    label=f"edits[{index}].old_text",
                )
            )

    if old_text is not None or new_text is not None:
        if old_text is None or new_text is None:
            raise RetryableToolInputError(
                "edit_file requires both old_text and new_text for a single edit. "
                "Retry with both fields or use edits[]."
            )
        replacements.append(
            _EditReplacement(
                old_text=old_text,
                new_text=new_text,
                label="old_text",
            )
        )

    if not replacements:
        raise RetryableToolInputError(
            f"edit_file requires old_text/new_text or non-empty edits[] for {path}. "
            "Read the current file content, then retry with exact text from that file."
        )
    for replacement in replacements:
        if replacement.old_text == "":
            raise RetryableToolInputError(
                f"edit_file {replacement.label} must not be empty. "
                "Retry with exact non-empty text from the file."
            )
        if replacement.old_text == replacement.new_text:
            raise RetryableToolInputError(
                f"edit_file {replacement.label} is unchanged: old_text and new_text are "
                "identical, so the edit would do nothing. Retry with the intended new_text."
            )
    return replacements


def _apply_edit_replacements(
    original: str,
    replacements: list[_EditReplacement],
    *,
    path: str,
) -> str:
    spans: list[tuple[int, int, str, _EditReplacement]] = []
    for replacement in replacements:
        count = original.count(replacement.old_text)
        if count == 0:
            recovered = _recover_edit_replacement(
                original,
                replacement,
                path=path,
            )
            if recovered is None:
                hint = _edit_file_closest_lines_hint(replacement.old_text, original)
                if hint:
                    _record_edit_recovery_event(
                        name="edit_file.no_match_hint",
                        path=path,
                        replacement=replacement,
                        outcome="hint",
                        reason="closest_lines",
                        matches=hint.count("\n---\n") + 1,
                    )
                raise RetryableToolInputError(
                    f"edit_file could not find {replacement.label} in {path}. "
                    "Read the current file content, then retry with exact text from that file. "
                    "Do not include read_file line-number prefixes like '12\\t', "
                    f"{_edit_file_retry_guidance()}"
                    f"{hint}"
                )
            start, end, replacement_text = recovered
            spans.append((start, end, replacement_text, replacement))
            continue
        if count > 1:
            raise RetryableToolInputError(
                f"edit_file {replacement.label} matches {count} locations in {path}. "
                "Retry with a longer old_text that includes unique surrounding context, "
                f"{_edit_file_retry_guidance(duplicate_match=True)}"
            )
        start = original.index(replacement.old_text)
        spans.append((start, start + len(replacement.old_text), replacement.new_text, replacement))

    spans.sort(key=lambda item: item[0])
    previous_end = -1
    for start, end, _replacement_text, replacement in spans:
        if start < previous_end:
            raise RetryableToolInputError(
                f"edit_file {replacement.label} overlaps another edits[] replacement in {path}. "
                "Merge nearby or overlapping changes into one old_text/new_text entry."
            )
        previous_end = end

    chunks: list[str] = []
    cursor = 0
    for start, end, replacement_text, _replacement in spans:
        chunks.append(original[cursor:start])
        chunks.append(replacement_text)
        cursor = end
    chunks.append(original[cursor:])
    return "".join(chunks)


_READ_FILE_LINE_PREFIX_RE = re.compile(r"^\s*\d+\t")


def _strip_read_file_line_prefixes(value: str) -> str:
    """Strip ``{lineno}\\t`` prefixes left when read_file output is pasted as old_text.

    read_file renders lines as ``{lineno}\\t{line}``; models frequently copy that
    rendered block straight into edit_file.old_text, which then fails to match the
    real source. Only strip when *every* non-empty line carries the prefix, so
    ordinary source that merely starts a line with a number+tab is never mangled.
    """
    lines = value.split("\n")
    non_empty = 0
    prefixed = 0
    for line in lines:
        if line == "":
            continue
        non_empty += 1
        if _READ_FILE_LINE_PREFIX_RE.match(line):
            prefixed += 1
    if non_empty == 0 or prefixed != non_empty:
        return value
    return "\n".join(_READ_FILE_LINE_PREFIX_RE.sub("", line) for line in lines)


def _recover_edit_replacement(
    original: str,
    replacement: _EditReplacement,
    *,
    path: str,
) -> tuple[int, int, str] | None:
    ctx = current_tool_context.get()
    if ctx is not None and not ctx.file_edit_flexible_recovery:
        _record_edit_recovery_event(
            name="edit_file.flexible_match_rejected",
            path=path,
            replacement=replacement,
            outcome="rejected",
            reason="disabled",
            matches=0,
        )
        return None

    candidates: list[tuple[str, str]] = [("original", replacement.old_text)]
    unescaped = _unescape_edit_search_text(replacement.old_text)
    if unescaped != replacement.old_text:
        candidates.append(("unescape", unescaped))
    line_prefix_stripped = _strip_read_file_line_prefixes(replacement.old_text)
    if line_prefix_stripped != replacement.old_text:
        candidates.append(("line_prefix", line_prefix_stripped))

    for kind, candidate in candidates:
        transformed = kind != "original"
        repair_event = f"edit_file.{kind}_repair_used"
        if transformed:
            count = original.count(candidate)
            if count == 1:
                _record_edit_recovery_event(
                    name=repair_event,
                    path=path,
                    replacement=replacement,
                    outcome="used",
                    reason=f"exact_match_after_{kind}",
                    matches=count,
                )
                start = original.index(candidate)
                return start, start + len(candidate), replacement.new_text
            if count > 1:
                _record_edit_recovery_event(
                    name="edit_file.flexible_match_rejected",
                    path=path,
                    replacement=replacement,
                    outcome="rejected",
                    reason=f"multiple_matches_after_{kind}",
                    matches=count,
                )
                return None

        flexible = _find_unique_flexible_edit_match(
            original,
            candidate,
            replacement.new_text,
        )
        if flexible is None:
            continue
        if len(flexible) != 4 or flexible[0] != "ok":
            _record_edit_recovery_event(
                name="edit_file.flexible_match_rejected",
                path=path,
                replacement=replacement,
                outcome="rejected",
                reason=flexible[0],
                matches=flexible[1],
            )
            return None
        _tag, start, end, replacement_text = flexible
        if transformed:
            _record_edit_recovery_event(
                name=repair_event,
                path=path,
                replacement=replacement,
                outcome="used",
                reason=f"flexible_match_after_{kind}",
                matches=1,
            )
        _record_edit_recovery_event(
            name="edit_file.flexible_match_used",
            path=path,
            replacement=replacement,
            outcome="used",
            reason="unique_trim_window",
            matches=1,
        )
        return start, end, replacement_text

    _record_edit_recovery_event(
        name="edit_file.flexible_match_rejected",
        path=path,
        replacement=replacement,
        outcome="rejected",
        reason="no_match",
        matches=0,
    )
    return None


def _find_unique_flexible_edit_match(
    original: str,
    old_text: str,
    new_text: str,
) -> tuple[str, int, int, str] | tuple[str, int] | None:
    normalized_old = old_text.replace("\r\n", "\n")
    normalized_new = new_text.replace("\r\n", "\n")
    if not normalized_old:
        return None
    search_lines = normalized_old.splitlines(keepends=True)
    if not search_lines:
        return None
    stripped_search = [line.strip() for line in search_lines]
    if not any(stripped_search):
        return None

    source_lines = original.splitlines(keepends=True)
    if len(search_lines) > len(source_lines):
        return None

    line_starts: list[int] = []
    cursor = 0
    for line in source_lines:
        line_starts.append(cursor)
        cursor += len(line)

    matches: list[tuple[int, int, str]] = []
    width = len(search_lines)
    for index in range(0, len(source_lines) - width + 1):
        window = source_lines[index : index + width]
        if [line.strip() for line in window] != stripped_search:
            continue
        start = line_starts[index]
        end = line_starts[index + width - 1] + len(window[-1])
        indent = _leading_indent(window[0])
        replacement_text = _apply_replacement_indentation(
            normalized_new,
            indent,
            preserve_trailing_newline=window[-1].endswith("\n"),
        )
        matches.append((start, end, replacement_text))

    if len(matches) == 1:
        start, end, replacement_text = matches[0]
        return "ok", start, end, replacement_text
    if len(matches) > 1:
        return "multiple_matches", len(matches)
    return None


def _apply_replacement_indentation(
    replacement_text: str,
    indentation: str,
    *,
    preserve_trailing_newline: bool,
) -> str:
    lines = replacement_text.split("\n")
    if not lines:
        return replacement_text
    reference_indent = _leading_indent(lines[0])
    indented: list[str] = []
    for line in lines:
        if line.strip() == "":
            indented.append("")
        elif line.startswith(reference_indent):
            indented.append(indentation + line[len(reference_indent) :])
        else:
            indented.append(indentation + line.lstrip(" \t"))
    result = "\n".join(indented)
    if replacement_text and preserve_trailing_newline and not result.endswith("\n"):
        result += "\n"
    return result


def _leading_indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _unescape_edit_search_text(value: str) -> str:
    output: list[str] = []
    index = 0
    replacements = {
        "n": "\n",
        "t": "\t",
        "r": "\r",
        '"': '"',
        "'": "'",
        "`": "`",
        "\\": "\\",
    }
    while index < len(value):
        char = value[index]
        if char != "\\" or index + 1 >= len(value):
            output.append(char)
            index += 1
            continue
        next_char = value[index + 1]
        replacement = replacements.get(next_char)
        if replacement is None:
            output.append(char)
            index += 1
            continue
        output.append(replacement)
        index += 2
    return "".join(output)


_EDIT_HINT_MAX_CHARS = 1200
_EDIT_HINT_MAX_LINE_CHARS = 200


def _edit_file_closest_lines_hint(
    old_text: str,
    original: str,
    *,
    context_lines: int = 2,
    max_results: int = 3,
) -> str:
    """Build a "did you mean" hint for an edit_file exact-match failure.

    Scores every non-blank source line against the first non-blank line of
    ``old_text`` (difflib similarity) and returns up to ``max_results`` numbered,
    context-padded snippets of the closest regions, or "" when nothing is close
    enough. Only meant for the count==0 no-match path; the returned string is
    already formatted for appending to the retry error (leading blank line and
    header included). Output length is bounded so it never floods the turn.
    """
    if not old_text or not original:
        return ""
    old_lines = old_text.splitlines()
    content_lines = original.splitlines()
    if not old_lines or not content_lines:
        return ""

    anchor = ""
    for line in old_lines:
        if line.strip():
            anchor = line.strip()
            break
    if not anchor:
        return ""

    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(anchor)
    scored: list[tuple[float, int]] = []
    for i, line in enumerate(content_lines):
        stripped = line.strip()
        if not stripped:
            continue
        matcher.set_seq1(stripped)
        if matcher.real_quick_ratio() <= 0.3 or matcher.quick_ratio() <= 0.3:
            continue
        ratio = matcher.ratio()
        if ratio > 0.3:
            scored.append((ratio, i))
    if not scored:
        return ""
    scored.sort(key=lambda item: (-item[0], item[1]))

    parts: list[str] = []
    seen_ranges: set[tuple[int, int]] = set()
    for _ratio, line_idx in scored:
        if len(parts) >= max_results:
            break
        start = max(0, line_idx - context_lines)
        end = min(len(content_lines), line_idx + len(old_lines) + context_lines)
        key = (start, end)
        if key in seen_ranges:
            continue
        seen_ranges.add(key)
        snippet_lines = []
        for j in range(start, end):
            text = content_lines[j]
            if len(text) > _EDIT_HINT_MAX_LINE_CHARS:
                text = text[:_EDIT_HINT_MAX_LINE_CHARS] + "…"
            snippet_lines.append(f"{j + 1:4d}| {text}")
        parts.append("\n".join(snippet_lines))
    if not parts:
        return ""

    body = "\n---\n".join(parts)
    if len(body) > _EDIT_HINT_MAX_CHARS:
        body = body[:_EDIT_HINT_MAX_CHARS] + "\n… (truncated)"
    return "\n\nDid you mean one of these sections?\n" + body


def _record_edit_recovery_event(
    *,
    name: str,
    path: str,
    replacement: _EditReplacement,
    outcome: str,
    reason: str,
    matches: int,
) -> None:
    ctx = current_tool_context.get()
    callback = getattr(ctx, "on_runtime_event", None) if ctx is not None else None
    if callback is None:
        return
    event = {
        "feature": "edit_file_recovery",
        "name": name,
        "tool": "edit_file",
        "tool_name": "edit_file",
        "path": path,
        "label": replacement.label,
        "outcome": outcome,
        "reason": reason,
        "matches": matches,
        "agent_id": getattr(ctx, "agent_id", None),
        "session_key": getattr(ctx, "session_key", None),
    }
    try:
        callback(event)
    except Exception:
        return


@tool(
    name="list_dir",
    description="List directory contents with type and size.",
    params={
        "path": {"type": "string", "description": "Directory path to list."},
        "approval_id": {
            "type": "string",
            "description": "Approval record to consume after granting sandbox path access.",
        },
    },
    required=["path"],
    plan_access=PlanAccess.READ_ONLY,
    runtime_only_arguments=("approval_id",),
    sandbox=SandboxToolDescriptor.filesystem(
        kind="list_dir",
        argv_factory=lambda a: ("list_dir", str(a.get("path", ""))),
        request_factory=_tool_path_request,
        enforce=False,
        record_payload=False,
    ),
)
async def list_dir(path: str, approval_id: str | None = None) -> str:
    p = _resolve_path(path)
    blocked = _sensitive_access_block("list_dir", p, path)
    if blocked is not None:
        return json.dumps(blocked)
    path_access = _sandbox_path_access_envelope(p, write=False, approval_id=approval_id)
    if path_access is not None:
        return json.dumps(path_access)
    _gate_workspace_strict_read("list_dir", p, path)
    if not p.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    if not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")
    workspace = _filesystem_operation_workspace()
    if workspace is not None:
        sandbox_result = await _run_sandbox_operation_if_required(
            SandboxOperation.filesystem(
                kind="list_dir",
                workspace=workspace,
                run_mode=_active_filesystem_run_mode(),
                path=p,
                paths=(p,),
                display_path=path,
            )
        )
        if sandbox_result is not None:
            return str(getattr(sandbox_result, "message"))

    loop = asyncio.get_event_loop()
    strict_roots = _strict_read_roots()
    workspace_root = _workspace_root()

    def _list() -> list[str]:
        dirs: list[str] = []
        files: list[str] = []
        blocked_entries: list[str] = []
        for entry in sorted(p.iterdir(), key=lambda e: e.name):
            try:
                resolved_entry = entry.resolve(strict=False)
            except OSError:
                is_directory, line = format_directory_entry(entry, follow_target=False)
                (dirs if is_directory else files).append(line)
                continue
            except RuntimeError as exc:
                if not _is_pathlib_symlink_loop_error(exc):
                    raise
                is_directory, line = format_directory_entry(
                    entry,
                    follow_target=False,
                    symlink_target_is_broken=True,
                )
                (dirs if is_directory else files).append(line)
                continue
            marker = _workspace_strict_candidate_marker(
                "list_dir",
                entry,
                strict_roots=strict_roots,
                resolved_candidate=resolved_entry,
            )
            if marker is not None:
                blocked_entries.append(marker)
                continue
            if _is_sensitive_access_path(resolved_entry, workspace=workspace_root):
                continue
            is_directory, line = format_directory_entry(entry)
            (dirs if is_directory else files).append(line)
        return dirs + files + blocked_entries

    # _list reads the current tool context (run mode / full-host access) via
    # _is_sensitive_access_path; copy the context into the worker thread so it
    # is not lost (run_in_executor does not propagate contextvars).
    entries = await loop.run_in_executor(None, contextvars.copy_context().run, _list)
    if not entries:
        return f"{path}: (empty directory)"
    return "\n".join(entries)


@tool(
    name="glob_search",
    description="Find files matching a glob pattern.",
    params={
        "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py')."},
        "path": {"type": "string", "description": "Base directory to search from (default: cwd)."},
    },
    required=["pattern"],
    plan_access=PlanAccess.READ_ONLY,
    sandbox=SandboxToolDescriptor.filesystem(
        kind="glob_search",
        argv_factory=lambda a: (
            "glob_search",
            str(a.get("pattern", "")),
            str(a.get("path", "")),
        ),
        request_factory=_tool_search_request,
        enforce=False,
        record_payload=False,
    ),
)
async def glob_search(pattern: str, path: str | None = None) -> str:
    base = _resolve_base(path)
    blocked = _sensitive_access_block("glob_search", base, path or str(base))
    if blocked is not None:
        return json.dumps(blocked)
    path_access = _sandbox_path_access_envelope(base, write=False)
    if path_access is not None:
        return json.dumps(path_access)
    _gate_workspace_strict_read("glob_search", base, path or str(base))
    if not base.exists():
        return f"No files matched pattern '{pattern}' in {base}"
    workspace = _filesystem_operation_workspace()
    if workspace is not None:
        sandbox_result = await _run_sandbox_operation_if_required(
            SandboxOperation.filesystem(
                kind="glob_search",
                workspace=workspace,
                run_mode=_active_filesystem_run_mode(),
                path=base,
                paths=(base,),
                display_path=path or str(base),
                pattern=pattern,
            )
        )
        if sandbox_result is not None:
            return str(getattr(sandbox_result, "message"))

    loop = asyncio.get_event_loop()
    strict_roots = _strict_read_roots()
    workspace_root = _workspace_root()

    def _glob() -> list[str]:
        matches: list[str] = []
        for candidate in sorted(base.glob(pattern), key=lambda item: str(item)):
            try:
                relative_candidate = candidate.relative_to(base)
            except ValueError:
                relative_candidate = candidate
            if _is_search_excluded_path(relative_candidate):
                continue
            marker = _workspace_strict_candidate_marker(
                "glob_search",
                candidate,
                strict_roots=strict_roots,
            )
            if marker is not None:
                matches.append(marker)
                continue
            if _is_sensitive_access_path(candidate.resolve(strict=False), workspace=workspace_root):
                continue
            matches.append(
                _workspace_display_path_for_root(candidate, str(candidate), workspace_root)
            )
        return matches

    # Preserve the tool context (run mode / full-host access) inside the worker
    # thread; _glob classifies entries via _is_sensitive_access_path.
    matches = await loop.run_in_executor(None, contextvars.copy_context().run, _glob)
    if not matches:
        return f"No files matched pattern '{pattern}' in {base}"
    return "\n".join(matches)


def _source_symbol_files(
    base: Path,
    *,
    include: str | None,
    strict_roots: tuple[Path, ...],
    workspace_root: Path | None,
) -> list[Path]:
    def should_include(candidate: Path) -> bool:
        if _is_sensitive_access_path(candidate.resolve(strict=False), workspace=workspace_root):
            return False
        marker = _workspace_strict_candidate_marker(
            "source_symbols",
            candidate,
            strict_roots=strict_roots,
        )
        if marker is not None:
            return False
        if not candidate.is_file():
            return False
        if candidate.suffix.casefold() not in _SOURCE_SYMBOL_EXTENSIONS:
            return False
        if include:
            display_path = _workspace_display_path_for_root(
                candidate,
                str(candidate),
                workspace_root,
            )
            if not (
                fnmatch.fnmatch(candidate.name, include) or fnmatch.fnmatch(display_path, include)
            ):
                return False
        try:
            if candidate.stat().st_size > _SOURCE_SYMBOL_MAX_FILE_BYTES:
                return False
            if _looks_binary(_read_binary_sample(candidate), candidate):
                return False
        except OSError:
            return False
        return True

    if base.is_file():
        return [base] if should_include(base) else []

    files: list[Path] = []
    for candidate in sorted(base.rglob("*"), key=lambda item: str(item)):
        try:
            relative_candidate = candidate.relative_to(base)
        except ValueError:
            relative_candidate = candidate
        if _is_search_excluded_path(relative_candidate):
            continue
        if should_include(candidate):
            files.append(candidate)
    return files


def _source_symbol_matches_line(path: Path, line: str) -> list[tuple[str, str]]:
    extension = path.suffix.casefold()
    matches: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for extensions, kind, regex in _SOURCE_SYMBOL_REGEXES:
        if extension not in extensions:
            continue
        match = regex.search(line)
        if match is None:
            continue
        name = match.group(1)
        if name in _SOURCE_SYMBOL_IGNORED_NAMES:
            continue
        key = (kind, name)
        if key in seen:
            continue
        matches.append(key)
        seen.add(key)
    return matches


def _source_symbol_query_matches(
    *,
    query: str | None,
    path: str,
    name: str,
    preview: str,
) -> bool:
    if not query:
        return True
    needle = query.casefold()
    return needle in name.casefold() or needle in path.casefold() or needle in preview.casefold()


@tool(
    name="source_symbols",
    description=(
        "Find likely source symbols using read-only repository scanning. Returns JSON "
        "workspace-relative symbol receipts with path, line, kind, name, and preview. "
        "Use this to localize candidate files before read_source/edit_source."
    ),
    params={
        "query": {
            "type": "string",
            "description": "Optional case-insensitive symbol, path, or preview filter.",
        },
        "path": {
            "type": "string",
            "description": "Optional file or directory to scan (default: workspace/cwd).",
        },
        "include": {
            "type": "string",
            "description": "Optional glob filter for file names or workspace-relative paths.",
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum symbols to return (default 80, max 200).",
        },
    },
    required=[],
    exposed_by_default=False,
    plan_access=PlanAccess.READ_ONLY,
)
async def source_symbols(
    query: str | None = None,
    path: str | None = None,
    include: str | None = None,
    max_results: int | None = None,
) -> str:
    base = _resolve_base(path)
    blocked = _sensitive_access_block("source_symbols", base, path or str(base))
    if blocked is not None:
        return json.dumps(blocked)
    path_access = _sandbox_path_access_envelope(base, write=False)
    if path_access is not None:
        return json.dumps(path_access)
    _gate_workspace_strict_read("source_symbols", base, path or str(base))
    if not base.exists():
        raise FileNotFoundError(f"Path not found: {path or base}")

    limit = _bounded_positive_int(
        max_results,
        default=_SOURCE_SYMBOL_DEFAULT_MAX_RESULTS,
        maximum=_SOURCE_SYMBOL_MAX_RESULTS,
    )
    loop = asyncio.get_event_loop()
    strict_roots = _strict_read_roots()
    workspace_root = _workspace_root()

    def _scan() -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        searched_files = 0
        truncated = False
        for candidate in _source_symbol_files(
            base,
            include=include,
            strict_roots=strict_roots,
            workspace_root=workspace_root,
        ):
            searched_files += 1
            display_path = _workspace_display_path_for_root(
                candidate,
                str(candidate),
                workspace_root,
            )
            try:
                with candidate.open(encoding="utf-8") as fh:
                    for line_number, line in enumerate(fh, start=1):
                        preview = line.strip()
                        for kind, name in _source_symbol_matches_line(candidate, line):
                            if not _source_symbol_query_matches(
                                query=query,
                                path=display_path,
                                name=name,
                                preview=preview,
                            ):
                                continue
                            if len(results) >= limit:
                                truncated = True
                                return {
                                    "results": results,
                                    "searched_files": searched_files,
                                    "truncated": truncated,
                                }
                            results.append(
                                {
                                    "path": display_path,
                                    "line": line_number,
                                    "kind": kind,
                                    "name": name,
                                    "preview": preview,
                                }
                            )
            except (PermissionError, OSError, UnicodeDecodeError):
                continue
        return {
            "results": results,
            "searched_files": searched_files,
            "truncated": truncated,
        }

    scanned = await loop.run_in_executor(None, _scan)
    payload = {
        "status": "success",
        "query": query or "",
        "path": _workspace_display_path_for_root(base, path or str(base), workspace_root),
        "include": include or "",
        "max_results": limit,
        **scanned,
    }
    return json.dumps(payload, ensure_ascii=False)


@tool(
    name="grep_search",
    description="Search file contents for a regex pattern.",
    params={
        "pattern": {"type": "string", "description": "Regex pattern to search for."},
        "path": {"type": "string", "description": "File or directory to search (default: cwd)."},
        "include": {"type": "string", "description": "Glob pattern to filter files (e.g. '*.py')."},
        "max_results": {
            "type": "integer",
            "description": (
                "Maximum number of matches to return (default 100, max 1000). "
                "Use 0 for an explicit unlimited search."
            ),
        },
        "offset": {
            "type": "integer",
            "description": "Number of matches to skip before returning results.",
        },
        "include_line_numbers": {
            "type": "boolean",
            "description": "Whether returned matches include line numbers (default true).",
        },
    },
    required=["pattern"],
    plan_access=PlanAccess.READ_ONLY,
    sandbox=SandboxToolDescriptor.filesystem(
        kind="grep_search",
        argv_factory=lambda a: (
            "grep_search",
            str(a.get("pattern", "")),
            str(a.get("path", "")),
        ),
        request_factory=_tool_search_request,
        enforce=False,
        record_payload=False,
    ),
)
async def grep_search(
    pattern: str,
    path: str | None = None,
    include: str | None = None,
    max_results: int = 100,
    offset: int = 0,
    include_line_numbers: bool = True,
) -> str:
    base = _resolve_base(path)
    blocked = _sensitive_access_block("grep_search", base, path or str(base))
    if blocked is not None:
        return json.dumps(blocked)
    path_access = _sandbox_path_access_envelope(base, write=False)
    if path_access is not None:
        return json.dumps(path_access)
    _gate_workspace_strict_read("grep_search", base, path or str(base))
    if not base.exists():
        return f"No matches for '{pattern}'"
    workspace = _filesystem_operation_workspace()
    if workspace is not None:
        sandbox_result = await _run_sandbox_operation_if_required(
            SandboxOperation.filesystem(
                kind="grep_search",
                workspace=workspace,
                run_mode=_active_filesystem_run_mode(),
                path=base,
                paths=(base,),
                display_path=path or str(base),
                pattern=pattern,
                include=include,
                max_results=max_results,
            )
        )
        if sandbox_result is not None:
            return str(getattr(sandbox_result, "message"))

    loop = asyncio.get_event_loop()
    strict_roots = _strict_read_roots()
    workspace_root = _workspace_root()
    requested_max_results = _bounded_non_negative_int(
        max_results,
        default=_GREP_DEFAULT_MAX_RESULTS,
    )
    unlimited = requested_max_results == 0
    effective_max_results = (
        0
        if unlimited
        else _bounded_positive_int(
            requested_max_results,
            default=_GREP_DEFAULT_MAX_RESULTS,
            maximum=_GREP_MAX_RESULTS,
        )
    )
    effective_offset = _bounded_non_negative_int(offset, default=0)
    stop_after_match_count = None if unlimited else effective_offset + effective_max_results + 1

    def _search() -> tuple[list[str], int, bool]:
        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e

        results: list[str] = []
        match_count = 0
        has_more = False

        def add_match(fp: Path, lineno: int, line: str) -> None:
            nonlocal has_more, match_count
            if stop_after_match_count is not None and match_count >= stop_after_match_count:
                has_more = True
                return
            if match_count >= effective_offset and (
                unlimited or len(results) < effective_max_results
            ):
                results.append(
                    _format_grep_match(
                        fp,
                        lineno,
                        line,
                        include_line_numbers=include_line_numbers,
                        workspace_root=workspace_root,
                    )
                )
            elif not unlimited and match_count >= effective_offset + effective_max_results:
                has_more = True
            match_count += 1

        def search_file(fp: Path) -> None:
            nonlocal has_more
            if has_more and not unlimited:
                return
            marker = _sandbox_path_access_marker(fp, write=False)
            if marker is not None:
                results.append(marker)
                return
            if _is_sensitive_access_path(fp.resolve(strict=False), workspace=workspace_root):
                return
            try:
                if _looks_binary(_read_binary_sample(fp), fp):
                    return
                with fp.open(encoding="utf-8") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if has_more and not unlimited:
                            return
                        if regex.search(line):
                            add_match(fp, lineno, line)
            except (PermissionError, OSError, UnicodeDecodeError):
                pass

        if base.is_file():
            search_file(base)
        else:
            for fp in base.rglob("*"):
                if has_more and not unlimited:
                    break
                try:
                    relative_fp = fp.relative_to(base)
                except ValueError:
                    relative_fp = fp
                if _is_search_excluded_path(relative_fp):
                    continue
                marker = _workspace_strict_candidate_marker(
                    "grep_search",
                    fp,
                    strict_roots=strict_roots,
                )
                if marker is not None:
                    results.append(marker)
                    continue
                if not fp.is_file():
                    continue
                if include and not fnmatch.fnmatch(fp.name, include):
                    continue
                search_file(fp)

        return results, match_count, has_more

    # search_file classifies each file via _sandbox_path_access_marker and
    # _is_sensitive_access_path, both of which read the current run mode from
    # the tool-context contextvar. run_in_executor runs on a worker thread that
    # does not inherit contextvars, so copy the context in; otherwise every file
    # is judged as if full-host access were off and gets falsely marked blocked.
    matches, match_count, has_more = await loop.run_in_executor(
        None, contextvars.copy_context().run, _search
    )
    limit_text = "unlimited" if unlimited else str(effective_max_results)
    header = (
        "[grep_search]\n"
        f"returned: {len(matches)}\n"
        f"offset: {effective_offset}\n"
        f"limit: {limit_text}\n"
        f"has_more: {str(has_more).lower()}\n"
        f"matches_scanned: {match_count}\n"
        "---\n"
    )
    if not matches:
        return header + f"No matches for '{pattern}'"
    return header + "\n".join(matches)
