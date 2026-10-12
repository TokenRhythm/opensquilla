"""Observe-only diagnostics for long-running coding-agent tool loops."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

_GENERATED_OR_DERIVED_PATH_PATTERNS = (
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
_DOCUMENTATION_PATH_PATTERNS = (
    "docs/**",
    "doc/**",
    "documentation/**",
    "manual/**",
    "**/manual/**",
)
_TEST_PATH_PATTERNS = (
    "test/**",
    "tests/**",
    "__tests__/**",
    "**/__tests__/**",
    "*.spec.*",
    "*.test.*",
    "**/*.spec.*",
    "**/*.test.*",
)
_DEBUG_PATH_MARKERS = (
    "debug",
    "scratch",
    "tmp",
    "repro",
)
_SCRATCH_ARTIFACT_NAMES = frozenset(
    {
        "analysis.py",
        "analyze.py",
        "bug.py",
        "bug_test.py",
        "check.py",
        "debug.py",
        "fix.py",
        "minimal.py",
        "minimal_bug.py",
        "repro.c",
        "repro.cc",
        "repro.cpp",
        "repro.cxx",
        "repro.py",
        "reproduce.py",
        "reproduce_issue.py",
        "reproduction.py",
        "scratch.py",
        "test_case.py",
        "test_issue.py",
        "tmp.py",
        "verify.py",
        "works.py",
    }
)
_SCRATCH_ARTIFACT_PREFIXES = (
    "analysis_",
    "analyze_",
    "debug_",
    "fix_",
    "minimal_",
    "repro_",
    "reproduce_",
    "scratch_",
    "tmp_",
    "verify_",
)
_SCRATCH_ARTIFACT_SUFFIXES = frozenset(
    {
        ".bak",
        ".orig",
        ".rej",
        ".scratch",
        ".test_fix",
        ".tmp",
    }
)
_SCRATCH_TEST_ARTIFACT_MARKERS = (
    "bug",
    "case",
    "issue",
    "repro",
    "scratch",
)
_EXECUTION_TOOL_NAMES = frozenset({"background_process", "exec_command", "execute_code"})
_SOURCE_CONTEXT_TOOL_NAMES = frozenset({"read_file", "grep_search", "glob_search", "list_dir"})
_FOCUSED_VERIFICATION_MARKERS = (
    "pytest",
    " unittest",
    "python -m unittest",
    "cargo test",
    "cargo check",
    "cargo build",
    "go test",
    "npm test",
    "npm run test",
    "npm run build",
    "pnpm test",
    "yarn test",
    "mvn test",
    "gradle test",
    "ctest",
    "rspec",
    "tox",
    " make test",
    "ant ",
    "javac ",
)


@dataclass(frozen=True)
class RuntimeDiagnosticsThresholds:
    repeated_failure_anchor: int = 3
    repeated_source_read_after_write: int = 6
    repeated_verification_without_diff_change: int = 3
    edit_churn_after_failure: int = 3

    def as_dict(self) -> dict[str, int]:
        return {
            "repeated_failure_anchor": self.repeated_failure_anchor,
            "repeated_source_read_after_write": self.repeated_source_read_after_write,
            "repeated_verification_without_diff_change": (
                self.repeated_verification_without_diff_change
            ),
            "edit_churn_after_failure": self.edit_churn_after_failure,
        }


@dataclass
class RuntimeDiagnosticsObserver:
    """Stateful observe-only detector scoped to one agent turn.

    The observer returns JSON-serializable event dictionaries. It never mutates
    conversation state and never decides whether the model should be warned.
    """

    session_key: str | None = None
    agent_id: str | None = None
    thresholds: RuntimeDiagnosticsThresholds = field(
        default_factory=RuntimeDiagnosticsThresholds
    )
    _seen_read_records: int = 0
    _seen_write_records: int = 0
    _seen_scratch_records: int = 0
    _read_after_write_counts: Counter[str] = field(default_factory=Counter)
    _failure_anchor_counts: Counter[str] = field(default_factory=Counter)
    _verification_diff_counts: Counter[tuple[str, str]] = field(default_factory=Counter)
    _source_edits_after_failure: int = 0
    _failure_seen: bool = False
    _previous_diff_fingerprint: str | None = None
    _emitted: set[tuple[str, str]] = field(default_factory=set)

    def observe_tool_results(
        self,
        *,
        iteration: int,
        provider_call_count: int,
        tool_calls: list[Any],
        results: list[Any],
        read_records: list[dict[str, Any]],
        write_records: list[dict[str, Any]],
        scratch_records: list[dict[str, Any]],
        diff_paths: list[str],
        diff_fingerprint: str | None,
        failure_anchor_summary: str,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        previous_diff_fingerprint = self._previous_diff_fingerprint
        self._previous_diff_fingerprint = diff_fingerprint

        new_reads = read_records[self._seen_read_records :]
        new_writes = write_records[self._seen_write_records :]
        new_scratch_writes = scratch_records[self._seen_scratch_records :]
        self._seen_read_records = len(read_records)
        self._seen_write_records = len(write_records)
        self._seen_scratch_records = len(scratch_records)

        changed_files = _unique_paths_from_records(write_records)
        diff_path_classes = {path: classify_path(path) for path in diff_paths}
        write_path_classes = {path: classify_path(path) for path in changed_files}
        source_writes = [
            _record_relative_path(record)
            for record in new_writes
            if classify_path(_record_relative_path(record)) == "source"
        ]

        anchor_hash = failure_anchor_hash(failure_anchor_summary)
        if anchor_hash is not None:
            self._failure_seen = True
            self._failure_anchor_counts[anchor_hash] += 1
            count = self._failure_anchor_counts[anchor_hash]
            if count >= self.thresholds.repeated_failure_anchor:
                events.append(
                    self._event(
                        "repeated_failure_anchor",
                        iteration=iteration,
                        provider_call_count=provider_call_count,
                        tool_calls=tool_calls,
                        results=results,
                        changed_files=changed_files,
                        diff_paths=diff_paths,
                        diff_path_classes=diff_path_classes,
                        write_path_classes=write_path_classes,
                        diff_fingerprint_before=previous_diff_fingerprint,
                        diff_fingerprint_after=diff_fingerprint,
                        trigger_count=count,
                        trigger_key=anchor_hash,
                        failure_anchor_hash=anchor_hash,
                        failure_anchor_excerpt=_excerpt(failure_anchor_summary, 420),
                        evidence={
                            "new_source_writes": source_writes,
                            "new_scratch_writes": _unique_paths_from_records(new_scratch_writes),
                        },
                    )
                )

        if self._failure_seen and source_writes:
            self._source_edits_after_failure += len(source_writes)
            if self._source_edits_after_failure >= self.thresholds.edit_churn_after_failure:
                events.append(
                    self._event(
                        "edit_churn_after_failure",
                        iteration=iteration,
                        provider_call_count=provider_call_count,
                        tool_calls=tool_calls,
                        results=results,
                        changed_files=changed_files,
                        diff_paths=diff_paths,
                        diff_path_classes=diff_path_classes,
                        write_path_classes=write_path_classes,
                        diff_fingerprint_before=previous_diff_fingerprint,
                        diff_fingerprint_after=diff_fingerprint,
                        trigger_count=self._source_edits_after_failure,
                        trigger_key="source_edits_after_failure",
                        failure_anchor_hash=anchor_hash,
                        failure_anchor_excerpt=_excerpt(failure_anchor_summary, 420),
                        evidence={"new_source_writes": source_writes},
                    )
                )

        if write_records:
            for record in new_reads:
                path = _record_relative_path(record)
                if not path or classify_path(path) != "source":
                    continue
                self._read_after_write_counts[path] += 1
                count = self._read_after_write_counts[path]
                if count >= self.thresholds.repeated_source_read_after_write:
                    events.append(
                        self._event(
                            "repeated_source_read_after_write",
                            iteration=iteration,
                            provider_call_count=provider_call_count,
                            tool_calls=tool_calls,
                            results=results,
                            changed_files=changed_files,
                            diff_paths=diff_paths,
                            diff_path_classes=diff_path_classes,
                            write_path_classes=write_path_classes,
                            diff_fingerprint_before=previous_diff_fingerprint,
                            diff_fingerprint_after=diff_fingerprint,
                            trigger_count=count,
                            trigger_key=path,
                            normalized_path=path,
                            evidence={
                                "read_record": _compact_record(record),
                                "path_class": classify_path(path),
                            },
                        )
                    )

        for tool_call in tool_calls:
            command = _tool_call_command(tool_call)
            if not command or not command_looks_like_focused_verification(command):
                continue
            command_family = normalize_command_family(command)
            if not write_records or not diff_fingerprint:
                continue
            key = (command_family, diff_fingerprint)
            self._verification_diff_counts[key] += 1
            count = self._verification_diff_counts[key]
            if count >= self.thresholds.repeated_verification_without_diff_change:
                events.append(
                    self._event(
                        "repeated_verification_without_diff_change",
                        iteration=iteration,
                        provider_call_count=provider_call_count,
                        tool_calls=tool_calls,
                        results=results,
                        changed_files=changed_files,
                        diff_paths=diff_paths,
                        diff_path_classes=diff_path_classes,
                        write_path_classes=write_path_classes,
                        diff_fingerprint_before=previous_diff_fingerprint,
                        diff_fingerprint_after=diff_fingerprint,
                        trigger_count=count,
                        trigger_key=f"{command_family}:{diff_fingerprint}",
                        command_family=command_family,
                        evidence={
                            "command_excerpt": _excerpt(command, 260),
                            "same_diff_fingerprint_count": count,
                        },
                    )
                )

        return self._dedupe(events)

    def observe_finish_error(
        self,
        *,
        iteration: int,
        provider_call_count: int,
        error_code: str,
        changed_files: list[str],
        diff_paths: list[str],
        diff_fingerprint: str | None,
    ) -> list[dict[str, Any]]:
        if not diff_paths:
            return []
        return self._dedupe(
            [
                self._event(
                    "finish_error_with_non_empty_diff",
                    iteration=iteration,
                    provider_call_count=provider_call_count,
                    tool_calls=[],
                    results=[],
                    changed_files=changed_files,
                    diff_paths=diff_paths,
                    diff_path_classes={path: classify_path(path) for path in diff_paths},
                    write_path_classes={path: classify_path(path) for path in changed_files},
                    diff_fingerprint_before=self._previous_diff_fingerprint,
                    diff_fingerprint_after=diff_fingerprint,
                    trigger_count=1,
                    trigger_key=error_code or "agent_error",
                    evidence={"error_code": error_code or "agent_error"},
                )
            ]
        )

    def _event(
        self,
        reason: str,
        *,
        iteration: int,
        provider_call_count: int,
        tool_calls: list[Any],
        results: list[Any],
        changed_files: list[str],
        diff_paths: list[str],
        diff_path_classes: dict[str, str],
        write_path_classes: dict[str, str],
        diff_fingerprint_before: str | None,
        diff_fingerprint_after: str | None,
        trigger_count: int,
        trigger_key: str,
        command_family: str | None = None,
        normalized_path: str | None = None,
        failure_anchor_hash: str | None = None,
        failure_anchor_excerpt: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        latest_tool = tool_calls[-1] if tool_calls else None
        latest_result = results[-1] if results else None
        path_hash = _hash(normalized_path) if normalized_path else None
        payload = {
            "feature": "runtime_observer",
            "mechanism": "trace_cache_diagnostics",
            "mode": "log",
            "reason": reason,
            "event_name": reason,
            "iteration": iteration,
            "provider_call_count": provider_call_count,
            "session_key": self.session_key,
            "agent_id": self.agent_id,
            "injected_to_model": False,
            "tool_name": _tool_name(latest_tool),
            "command_family": command_family or _command_family_for_tool(latest_tool),
            "normalized_path": normalized_path,
            "path_hash": path_hash,
            "failure_anchor_hash": failure_anchor_hash,
            "failure_anchor_excerpt": failure_anchor_excerpt,
            "changed_files": changed_files,
            "diff_paths": diff_paths,
            "path_classes": {
                "changed_files": write_path_classes,
                "diff_paths": diff_path_classes,
            },
            "diff_fingerprint_before": diff_fingerprint_before,
            "diff_fingerprint_after": diff_fingerprint_after,
            "trigger_thresholds": self.thresholds.as_dict(),
            "trigger_count": trigger_count,
            "trigger_key_hash": _hash(trigger_key),
            "evidence": {
                "latest_tool_result_error": bool(getattr(latest_result, "is_error", False)),
                **(evidence or {}),
            },
        }
        return {key: value for key, value in payload.items() if value is not None}

    def _dedupe(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for event in events:
            reason = str(event.get("reason") or "")
            key_hash = str(event.get("trigger_key_hash") or "")
            dedupe_key = (reason, key_hash)
            if dedupe_key in self._emitted:
                continue
            self._emitted.add(dedupe_key)
            kept.append(event)
        return kept


def classify_path(path: str | None) -> str:
    normalized = _normalize_path(path or "")
    if not normalized:
        return "unknown"
    if _matches_any(normalized, _GENERATED_OR_DERIVED_PATH_PATTERNS):
        return "generated"
    if _matches_any(normalized, _DOCUMENTATION_PATH_PATTERNS):
        return "docs"
    if _matches_any(normalized, _TEST_PATH_PATTERNS):
        return "test"
    if _path_looks_like_scratch_artifact(normalized):
        return "debug"
    parts = {part.lower() for part in normalized.split("/")}
    if parts.intersection(_DEBUG_PATH_MARKERS):
        return "debug"
    return "source"


def _path_looks_like_scratch_artifact(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    if not name:
        return False
    if name in _SCRATCH_ARTIFACT_NAMES:
        return True
    if any(name.endswith(suffix) for suffix in _SCRATCH_ARTIFACT_SUFFIXES):
        return True
    if any(name.startswith(prefix) for prefix in _SCRATCH_ARTIFACT_PREFIXES):
        return True
    if name.startswith("test_") or name.startswith("test-"):
        return any(marker in name for marker in _SCRATCH_TEST_ARTIFACT_MARKERS)
    return False


def command_looks_like_focused_verification(command: str) -> bool:
    normalized = " " + " ".join((command or "").lower().split())
    return any(marker in normalized for marker in _FOCUSED_VERIFICATION_MARKERS)


def normalize_command_family(command: str) -> str:
    normalized = " ".join((command or "").strip().split())
    normalized = re.sub(r"^cd\s+[^&;]+&&\s*", "", normalized)
    normalized = re.sub(r"\s+2>&1\b.*$", "", normalized)
    normalized = normalized.strip()
    lower = normalized.lower()
    if lower.startswith("cargo "):
        parts = lower.split()
        return "cargo:" + (parts[1] if len(parts) > 1 else "unknown")
    if lower.startswith("npm run "):
        parts = lower.split()
        return "npm run:" + (parts[2] if len(parts) > 2 else "unknown")
    if lower.startswith("npm test"):
        return "npm:test"
    if lower.startswith("npx "):
        parts = lower.split()
        return "npx:" + (parts[1] if len(parts) > 1 else "unknown")
    if lower.startswith("python -m "):
        parts = lower.split()
        return "python -m:" + (parts[2] if len(parts) > 2 else "unknown")
    if lower.startswith("pytest"):
        return "pytest"
    if lower.startswith("go test"):
        return "go:test"
    if lower.startswith("mvn "):
        return "mvn"
    if lower.startswith("gradle ") or lower.startswith("./gradlew "):
        return "gradle"
    if lower.startswith("ant "):
        return "ant"
    if lower.startswith("javac "):
        return "javac"
    if lower.startswith("ruby "):
        return "ruby"
    if lower.startswith("git "):
        parts = lower.split()
        return "git:" + (parts[1] if len(parts) > 1 else "unknown")
    return " ".join(lower.split()[:3]) or "unknown"


def failure_anchor_hash(summary: str | None) -> str | None:
    normalized = " ".join((summary or "").strip().lower().split())
    if not normalized:
        return None
    for marker in ("/tmp/", "/var/tmp/"):
        if marker in normalized:
            normalized = normalized.replace(marker, f"{marker}<path>/")
    return _hash(normalized)


def _tool_call_command(tool_call: Any) -> str | None:
    arguments = getattr(tool_call, "arguments", {}) or {}
    if not isinstance(arguments, dict):
        return None
    for key in ("command", "cmd", "code"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _command_family_for_tool(tool_call: Any) -> str | None:
    command = _tool_call_command(tool_call)
    if not command:
        return None
    return normalize_command_family(command)


def _tool_name(tool_call: Any) -> str | None:
    name = getattr(tool_call, "tool_name", None)
    return str(name) if name else None


def _unique_paths_from_records(records: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for record in records:
        path = _record_relative_path(record)
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _record_relative_path(record: dict[str, Any]) -> str:
    raw = record.get("relative_path") or record.get("path")
    if not isinstance(raw, str):
        return ""
    return _normalize_path(raw)


def _compact_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key in {"relative_path", "name", "suffix", "operation", "offset", "limit", "created"}
    }


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    for marker in ("/testbed/", "/workspace/", "/repo/"):
        if marker in normalized:
            normalized = normalized.split(marker, 1)[1]
            break
    normalized = normalized.removeprefix("/testbed/").removeprefix("/workspace/")
    return normalized.lstrip("./")


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def _hash(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _excerpt(value: str | None, limit: int) -> str:
    normalized = " ".join((value or "").split())
    if len(normalized) <= limit:
        return normalized
    suffix = _hash(normalized)
    return normalized[: max(0, limit - 20)].rstrip() + f"...#{suffix}"


def event_fingerprint_payload(event: dict[str, Any]) -> str:
    """Return a stable hash for tests and downstream duplicate audits."""

    relevant = {
        key: event.get(key)
        for key in (
            "reason",
            "iteration",
            "provider_call_count",
            "tool_name",
            "command_family",
            "normalized_path",
            "failure_anchor_hash",
            "diff_fingerprint_after",
        )
    }
    return _hash(json.dumps(relevant, sort_keys=True, ensure_ascii=False)) or ""
