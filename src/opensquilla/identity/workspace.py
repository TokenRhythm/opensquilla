"""Workspace bootstrap file loading for agent identity context."""

import asyncio
import json
import re
from collections.abc import Iterable
from pathlib import Path

from opensquilla.bootstrap_types import BootstrapFileReport
from opensquilla.paths import native_io_path, state_dir
from opensquilla.safety.injection_guard import InjectionFinding, scan_for_injection
from opensquilla.session.keys import is_guest_webchat_key, is_subagent_key

# Matches YYYY-MM-DD.md or YYYY-MM-DD-<slug>.md (basename).
_DATED_BASENAME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:-[a-z0-9][a-z0-9-]*)?\.md")

_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MB
DEFAULT_BOOTSTRAP_MAX_CHARS = 20_000
DEFAULT_BOOTSTRAP_TOTAL_MAX_CHARS = 50_000
_MIN_BOOTSTRAP_FILE_BUDGET_CHARS = 32

# Ordered list of bootstrap filenames to load
# See THIRD_PARTY_NOTICES.md for attribution
BOOTSTRAP_FILENAMES = [
    "AGENTS.md",
    "SOUL.md",
    "IDENTITY.md",
    "TOOLS.md",
    "USER.md",
    "BOOTSTRAP.md",
    "HEARTBEAT.md",
]

_SUBAGENT_BOOTSTRAP_ALLOWLIST = frozenset({"AGENTS.md", "SOUL.md", "TOOLS.md"})
_CRON_BOOTSTRAP_ALLOWLIST = frozenset({"AGENTS.md", "TOOLS.md", "HEARTBEAT.md"})
_SHARED_BOOTSTRAP_ALLOWLIST = frozenset({"AGENTS.md", "SOUL.md", "TOOLS.md"})


def _is_within_root(root: Path, target: Path) -> bool:
    """Return True if target resolves within root (boundary guard)."""
    try:
        resolved_root = native_io_path(root).resolve()
        resolved_target = native_io_path(target).resolve()
        resolved_target.relative_to(resolved_root)
        return True
    except ValueError:
        return False


def _read_file_sync(path: Path) -> str | None:
    """Read a single bootstrap file, enforcing size limit."""
    native_path = native_io_path(path)
    if not native_path.is_file():
        return None
    size = native_path.stat().st_size
    if size > _MAX_FILE_BYTES:
        return None
    return native_path.read_text(encoding="utf-8", errors="replace")


def _bootstrap_allowlist_for_session(session_key: str | None) -> frozenset[str] | None:
    if not session_key:
        return None

    key = session_key.lower()
    if is_guest_webchat_key(key):
        return frozenset()
    if is_subagent_key(key):
        return _SUBAGENT_BOOTSTRAP_ALLOWLIST
    if key.startswith("cron:"):
        return _CRON_BOOTSTRAP_ALLOWLIST
    if ":group:" in key or ":channel:" in key:
        return _SHARED_BOOTSTRAP_ALLOWLIST
    return None


def filter_workspace_filenames_for_session(
    filenames: Iterable[str] | None,
    session_key: str | None,
) -> tuple[str, ...]:
    """Filter candidate bootstrap filenames before disk reads and budgeting."""
    ordered = tuple(filenames or BOOTSTRAP_FILENAMES)
    allowlist = _bootstrap_allowlist_for_session(session_key)
    if allowlist is None:
        return ordered
    return tuple(name for name in ordered if name in allowlist)


def load_workspace_files(
    workspace_dir: str | Path,
    *,
    filenames: Iterable[str] | None = None,
) -> dict[str, str]:
    """Synchronously load bootstrap files from workspace directory.

    Returns a dict mapping filename → content for files that exist
    and pass the boundary + size guards.
    """
    root = Path(workspace_dir).expanduser()
    result: dict[str, str] = {}

    for filename in filenames or BOOTSTRAP_FILENAMES:
        candidate = root / filename
        if not _is_within_root(root, candidate):
            continue

        content = _read_file_sync(candidate)
        if content is not None:
            result[filename] = content

    return result


def _normalize_budget(value: int, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, numeric)


def _truncate_with_marker(content: str, filename: str, max_chars: int) -> str:
    if len(content) <= max_chars:
        return content
    marker = f"[TRUNCATED: {filename} from {len(content)} chars]\n"
    if max_chars <= len(marker):
        return marker[:max_chars]
    keep_chars = max_chars - len(marker)
    head_chars = max(1, int(keep_chars * 0.7))
    tail_chars = max(0, keep_chars - head_chars)
    tail = content[-tail_chars:] if tail_chars else ""
    return content[:head_chars] + marker + tail


def load_workspace_files_budgeted(
    workspace_dir: str | Path,
    *,
    per_file_max_chars: int = DEFAULT_BOOTSTRAP_MAX_CHARS,
    total_max_chars: int = DEFAULT_BOOTSTRAP_TOTAL_MAX_CHARS,
    filenames: Iterable[str] | None = None,
    injection_scan_mode: str = "off",
    safety_log_path: str | Path | None = None,
) -> dict[str, str]:
    """Load bootstrap files with per-file and total prompt-size budgets."""
    files, _report = load_workspace_files_budgeted_with_report(
        workspace_dir,
        per_file_max_chars=per_file_max_chars,
        total_max_chars=total_max_chars,
        filenames=filenames,
        injection_scan_mode=injection_scan_mode,
        safety_log_path=safety_log_path,
    )
    return files


def _write_injection_findings(
    findings: list[InjectionFinding],
    *,
    safety_log_path: str | Path | None = None,
) -> None:
    if not findings:
        return
    path = Path(safety_log_path) if safety_log_path is not None else state_dir("safety_log.jsonl")
    native_path = native_io_path(path)
    native_path.parent.mkdir(parents=True, exist_ok=True)
    with native_path.open("a", encoding="utf-8") as fh:
        for finding in findings:
            fh.write(json.dumps(finding.asdict(), ensure_ascii=False, sort_keys=True) + "\n")


def load_workspace_files_budgeted_with_report(
    workspace_dir: str | Path,
    *,
    per_file_max_chars: int = DEFAULT_BOOTSTRAP_MAX_CHARS,
    total_max_chars: int = DEFAULT_BOOTSTRAP_TOTAL_MAX_CHARS,
    filenames: Iterable[str] | None = None,
    injection_scan_mode: str = "off",
    safety_log_path: str | Path | None = None,
) -> tuple[dict[str, str], list[BootstrapFileReport]]:
    """Load bootstrap files with budgets plus content-safe diagnostics."""
    per_file_limit = _normalize_budget(per_file_max_chars, DEFAULT_BOOTSTRAP_MAX_CHARS)
    remaining = _normalize_budget(total_max_chars, DEFAULT_BOOTSTRAP_TOTAL_MAX_CHARS)
    result: dict[str, str] = {}
    report: list[BootstrapFileReport] = []

    normalized_scan_mode = (
        injection_scan_mode if injection_scan_mode in {"off", "report", "enforce"} else "report"
    )
    for filename, content in load_workspace_files(workspace_dir, filenames=filenames).items():
        if remaining < _MIN_BOOTSTRAP_FILE_BUDGET_CHARS:
            break
        file_limit = min(per_file_limit, remaining)
        raw_chars = len(content)
        if filename != "BOOTSTRAP.md":
            content, findings = scan_for_injection(
                content,
                f"workspace:{filename}",
                mode=normalized_scan_mode,
            )
            _write_injection_findings(findings, safety_log_path=safety_log_path)
        injected = _truncate_with_marker(content, filename, file_limit)
        if not injected:
            continue
        result[filename] = injected
        truncated = raw_chars > len(injected) or raw_chars > file_limit
        cause: str | None = None
        if truncated:
            per_file_truncated = raw_chars > per_file_limit
            total_truncated = raw_chars > remaining
            if per_file_truncated and total_truncated:
                cause = "both"
            elif total_truncated:
                cause = "total"
            else:
                cause = "per-file"
        report.append(
            BootstrapFileReport(
                filename=filename,
                raw_chars=raw_chars,
                injected_chars=len(injected),
                truncated=truncated,
                truncation_cause=cause,
            )
        )
        remaining -= len(injected)

    return result, report


def filter_workspace_files_for_session(
    workspace_files: dict[str, str],
    session_key: str | None,
) -> dict[str, str]:
    """Filter bootstrap files for a session's privacy and execution context."""
    allowlist = _bootstrap_allowlist_for_session(session_key)
    if allowlist is None:
        return dict(workspace_files)

    return {name: content for name, content in workspace_files.items() if name in allowlist}


async def load_workspace_files_async(workspace_dir: str | Path) -> dict[str, str]:
    """Async wrapper for workspace file loading."""
    return await asyncio.get_event_loop().run_in_executor(None, load_workspace_files, workspace_dir)


def load_daily_notes(
    workspace_dir: str | Path,
    *,
    per_note_max_chars: int | None = None,
    total_max_chars: int | None = None,
) -> dict[str, str]:
    """Load today and yesterday's daily notes from memory/ directory.

    Returns dict mapping filename → content for files that exist.
    Matches both YYYY-MM-DD.md and YYYY-MM-DD-<slug>.md for today/yesterday.
    """
    from datetime import date, timedelta

    root = Path(workspace_dir).expanduser()
    memory_dir = root / "memory"
    native_memory_dir = native_io_path(memory_dir)
    if not native_memory_dir.is_dir():
        return {}
    per_note_limit = (
        int(per_note_max_chars)
        if isinstance(per_note_max_chars, int)
        and not isinstance(per_note_max_chars, bool)
        and per_note_max_chars > 0
        else None
    )
    remaining = (
        int(total_max_chars)
        if isinstance(total_max_chars, int)
        and not isinstance(total_max_chars, bool)
        and total_max_chars > 0
        else None
    )
    result: dict[str, str] = {}
    for offset in (0, 1):  # today, yesterday
        d = date.today() - timedelta(days=offset)
        prefix = d.isoformat()
        # Canonical file first, then slugged variants (alphabetical).
        canonical_name = f"{prefix}.md"
        matches = sorted(
            (
                memory_dir / candidate.name
                for candidate in native_memory_dir.glob(f"{prefix}*.md")
            ),
            key=lambda path: (path.name != canonical_name, path.name),
        )
        for candidate in matches:
            name = candidate.name
            if not _DATED_BASENAME_RE.fullmatch(name):
                continue
            if not name.startswith(prefix):
                continue
            if not _is_within_root(root, candidate):
                continue
            content = _read_file_sync(candidate)
            if content is not None:
                if remaining is not None:
                    if remaining < _MIN_BOOTSTRAP_FILE_BUDGET_CHARS:
                        return result
                    file_limit = remaining
                    if per_note_limit is not None:
                        file_limit = min(file_limit, per_note_limit)
                    content = _truncate_with_marker(content, name, file_limit)
                    remaining -= len(content)
                elif per_note_limit is not None:
                    content = _truncate_with_marker(content, name, per_note_limit)
                result[name] = content
    return result
