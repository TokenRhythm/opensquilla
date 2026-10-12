"""Observe-only patch evidence ledger for coding-agent diagnostics."""

from __future__ import annotations

import json
import subprocess
import time
from collections import Counter
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from opensquilla.safety.secret_redaction import redact_secret_text

_MAX_TOOL_EVENTS = 2000
_MAX_TEXT_CHARS = 600
_MAX_FAILURE_ANCHORS = 300
_MAX_SIBLING_CANDIDATES = 120
_DOC_PATTERNS = (
    "docs/**",
    "doc/**",
    "documentation/**",
    "manual/**",
    "**/manual/**",
)
_TEST_PATTERNS = (
    "test/**",
    "tests/**",
    "__tests__/**",
    "**/__tests__/**",
    "*.spec.*",
    "*.test.*",
    "**/*.spec.*",
    "**/*.test.*",
)
_GENERATED_PATTERNS = (
    "*.prebuilt",
    "*.generated.*",
    "*.min.*",
    "*.bundle.*",
    "dist/**",
    "build/**",
    "target/**",
    "generated/**",
    "gen/**",
)
_DEBUG_PATH_MARKERS = (
    "debug",
    "scratch",
    "tmp",
    "repro",
    "experiment",
)


class PatchEvidenceLedger:
    """Collects coding evidence without changing model inputs or tool results."""

    def __init__(
        self,
        *,
        path: str,
        workspace_dir: str | None,
        session_key: str | None,
        agent_id: str | None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.workspace_dir = (
            Path(workspace_dir).expanduser().resolve(strict=False) if workspace_dir else None
        )
        self.session_key = session_key
        self.agent_id = agent_id
        self.started_at = time.time()
        self.tool_events: list[dict[str, Any]] = []
        self.failure_anchors: list[dict[str, Any]] = []
        self.verification_commands: list[dict[str, Any]] = []

    def record_tool_result(
        self,
        *,
        iteration: int,
        tool_name: str,
        arguments: dict[str, Any],
        result_text: str,
        is_error: bool,
        duration_ms: int,
        failure_anchors: list[str],
        focused_verification: bool,
    ) -> None:
        if len(self.tool_events) < _MAX_TOOL_EVENTS:
            event = {
                "iteration": iteration,
                "tool_name": tool_name,
                "arguments": _summarize_arguments(arguments),
                "is_error": bool(is_error),
                "duration_ms": int(duration_ms),
                "result_chars": len(result_text),
                "failure_anchor_count": len(failure_anchors),
            }
            self.tool_events.append(event)

        if failure_anchors and len(self.failure_anchors) < _MAX_FAILURE_ANCHORS:
            for anchor in failure_anchors:
                if len(self.failure_anchors) >= _MAX_FAILURE_ANCHORS:
                    break
                self.failure_anchors.append(
                    {
                        "iteration": iteration,
                        "tool_name": tool_name,
                        "anchor": _truncate(_redact(anchor), _MAX_TEXT_CHARS),
                    }
                )

        command = _command_from_arguments(arguments)
        if focused_verification and command:
            self.verification_commands.append(
                {
                    "iteration": iteration,
                    "tool_name": tool_name,
                    "command": _truncate(_redact(command), _MAX_TEXT_CHARS),
                    "duration_ms": int(duration_ms),
                    "is_error": bool(is_error),
                }
            )

    def write_final(
        self,
        *,
        read_records: list[dict[str, Any]],
        write_records: list[dict[str, Any]],
        scratch_records: list[dict[str, Any]],
        final_status: str,
        iterations: int,
        provider_call_count: int,
    ) -> None:
        git_snapshot = self._git_snapshot()
        read_files = _unique_file_records(read_records)
        changed_files = _unique_file_records(write_records)
        diff_paths = git_snapshot.get("diff_paths", [])
        path_signal_counts: Counter[str] = Counter()
        for rel_path in set(read_files) | set(changed_files) | set(diff_paths):
            for signal in _path_signals(rel_path):
                path_signal_counts[signal] += 1

        sibling_candidates = self._sibling_candidates(
            changed_paths=changed_files or diff_paths,
            read_paths=read_files,
        )
        payload = {
            "version": 1,
            "generated_at_unix": time.time(),
            "duration_s": round(time.time() - self.started_at, 3),
            "session_key": self.session_key,
            "agent_id": self.agent_id,
            "workspace_dir": str(self.workspace_dir) if self.workspace_dir else None,
            "final_status": final_status,
            "iterations": iterations,
            "provider_call_count": provider_call_count,
            "summary": {
                "tool_event_count": len(self.tool_events),
                "read_file_count": len(read_files),
                "workspace_write_record_count": len(write_records),
                "changed_file_count": len(changed_files),
                "diff_path_count": len(diff_paths),
                "failure_anchor_count": len(self.failure_anchors),
                "verification_command_count": len(self.verification_commands),
                "scratch_write_count": len(scratch_records),
                "sibling_trigger_candidate_count": len(sibling_candidates),
                "path_signal_counts": dict(sorted(path_signal_counts.items())),
            },
            "read_files": read_files,
            "changed_files": changed_files,
            "diff_paths": diff_paths,
            "git_status_porcelain": git_snapshot.get("status_porcelain"),
            "verification_commands": self.verification_commands,
            "failure_anchors": self.failure_anchors,
            "sibling_trigger_candidates": sibling_candidates,
            "workspace_write_records": write_records,
            "workspace_read_records": read_records,
            "scratch_write_records": scratch_records,
            "tool_events": self.tool_events,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)

    def _git_snapshot(self) -> dict[str, Any]:
        if self.workspace_dir is None:
            return {"diff_paths": [], "status_porcelain": None}
        status = _run_git(self.workspace_dir, "status", "--porcelain=v1", "--untracked-files=all")
        diff = _run_git(self.workspace_dir, "diff", "--name-only")
        staged = _run_git(self.workspace_dir, "diff", "--cached", "--name-only")
        paths = (
            _paths_from_name_only(diff)
            | _paths_from_name_only(staged)
            | _paths_from_status(status)
        )
        return {
            "diff_paths": sorted(paths),
            "status_porcelain": status,
        }

    def _sibling_candidates(
        self,
        *,
        changed_paths: list[str],
        read_paths: list[str],
    ) -> list[dict[str, str]]:
        if self.workspace_dir is None:
            return []
        read_or_changed = set(changed_paths) | set(read_paths)
        candidates: list[dict[str, str]] = []
        for rel_path in changed_paths:
            path = self.workspace_dir / rel_path
            parent = path.parent
            if not parent.is_dir():
                continue
            try:
                entries = sorted(parent.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for entry in entries:
                if len(candidates) >= _MAX_SIBLING_CANDIDATES:
                    return candidates
                if not entry.is_file() or entry.name.startswith("."):
                    continue
                try:
                    sibling_rel = entry.resolve(strict=False).relative_to(self.workspace_dir)
                except ValueError:
                    continue
                sibling = sibling_rel.as_posix()
                if sibling in read_or_changed or sibling == rel_path:
                    continue
                if entry.suffix != path.suffix:
                    continue
                candidates.append(
                    {
                        "changed_path": rel_path,
                        "candidate_path": sibling,
                        "reason": "same_directory_same_suffix_unread_unmodified",
                    }
                )
        return candidates


def _run_git(workspace: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _summarize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("path", "paths", "workdir", "cwd", "timeout", "staged", "offset", "limit"):
        if key in arguments:
            summary[key] = _redact_value(arguments[key])
    command = _command_from_arguments(arguments)
    if command:
        summary["command"] = _truncate(_redact(command), _MAX_TEXT_CHARS)
    return summary


def _command_from_arguments(arguments: dict[str, Any]) -> str | None:
    for key in ("command", "cmd", "code"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _unique_file_records(records: list[dict[str, Any]]) -> list[str]:
    paths: set[str] = set()
    for record in records:
        rel = record.get("relative_path")
        if isinstance(rel, str) and rel:
            paths.add(rel)
    return sorted(paths)


def _paths_from_name_only(output: str | None) -> set[str]:
    if not output:
        return set()
    return {line.strip() for line in output.splitlines() if line.strip()}


def _paths_from_status(output: str | None) -> set[str]:
    paths: set[str] = set()
    if not output:
        return paths
    for line in output.splitlines():
        if len(line) < 4:
            continue
        raw = line[3:].strip()
        if " -> " in raw:
            raw = raw.rsplit(" -> ", 1)[1]
        if raw:
            paths.add(raw)
    return paths


def _path_signals(rel_path: str) -> list[str]:
    normalized = rel_path.replace("\\", "/").lstrip("./")
    signals: list[str] = []
    if _matches(normalized, _TEST_PATTERNS):
        signals.append("test_path")
    if _matches(normalized, _DOC_PATTERNS):
        signals.append("doc_path")
    if _matches(normalized, _GENERATED_PATTERNS):
        signals.append("generated_or_derived_path")
    lowered = normalized.lower()
    if any(marker in lowered for marker in _DEBUG_PATH_MARKERS):
        signals.append("debug_or_scratch_path")
    return signals


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _truncate(_redact(value), _MAX_TEXT_CHARS)
    if isinstance(value, list):
        return [_redact_value(item) for item in value[:20]]
    if isinstance(value, dict):
        return {str(k): _redact_value(v) for k, v in list(value.items())[:20]}
    return value


def _redact(text: str) -> str:
    return redact_secret_text(text)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...[truncated]"
