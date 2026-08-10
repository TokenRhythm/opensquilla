#!/usr/bin/env python3
"""Build a frozen router role-reliability snapshot from AEF experiment artifacts.

This is deliberately an offline command. Runtime routing never imports it and a
completed experiment can only influence a later profile snapshot.
Pass the complete accumulated artifact set that should participate in the
recent-50 window: supplied artifacts rebuild the counts and do not merge with
counts already present in the input profile.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA_VERSION = "role-reliability-snapshot-v2"
LEGACY_SNAPSHOT_SCHEMA_VERSIONS = frozenset({"role-reliability-snapshot-v1"})
OBSERVATION_POLICY = "aef-physical-model-calls-v5"
DEFAULT_WINDOW_SIZE = 50
ROLES = ("proposer", "aggregator")
LENGTH_CAPPED_STOP_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})
AUDIT_ISSUE_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
SNAPSHOT_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
DEFAULT_MAX_UNKNOWN_OUTCOME_RATE = 0.0


@dataclass(frozen=True)
class Observation:
    order: tuple[str, str, int, int, int]
    provider: str
    model_id: str
    role: str
    success: bool
    physical_attempt_id: str
    source_path: str
    reason: str


@dataclass(frozen=True)
class CollectionResult:
    observations: tuple[Observation, ...]
    output_files: tuple[str, ...]
    physical_requests_seen: int
    framework_excluded: int
    duplicate_attempts: int
    unclassified_requests: int
    completion_gate: str
    unknown_outcomes: int = 0
    max_unknown_outcome_rate: float = DEFAULT_MAX_UNKNOWN_OUTCOME_RATE
    allowed_audit_issue_codes: tuple[str, ...] = ()
    output_file_hashes: tuple[tuple[str, str], ...] = ()
    evidence_file_hashes: tuple[tuple[str, str, str], ...] = ()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _output_record_identity(payload: Mapping[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    results = payload.get("results")
    task_ids = (
        tuple(
            sorted(
                _clean_string(result.get("task_id"))
                for result in results
                if isinstance(result, Mapping) and _clean_string(result.get("task_id"))
            )
        )
        if isinstance(results, list)
        else ()
    )
    return (
        _clean_string(payload.get("benchmark_id")),
        _clean_string(payload.get("run_id")),
        task_ids,
    )


def discover_output_files(paths: Sequence[Path]) -> list[Path]:
    """Find AEF result JSON files while ignoring metadata and summaries."""

    discovered: dict[str, Path] = {}
    identities: dict[tuple[str, str, tuple[str, ...]], tuple[str, Path]] = {}
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        if path.is_file():
            candidates: Iterable[Path] = (path,)
        elif path.is_dir():
            search_root = path / "outputs" if (path / "outputs").is_dir() else path
            candidates = search_root.rglob("*.json")
        else:
            raise FileNotFoundError(f"artifact path does not exist: {raw_path}")
        for candidate in candidates:
            if candidate.name.endswith(".meta.json"):
                continue
            payload = _load_json(candidate)
            if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
                continue
            identity = _output_record_identity(payload)
            if not all((identity[0], identity[1], identity[2])):
                raise ValueError(f"AEF output lacks stable record identity: {candidate}")
            fingerprint = _file_sha256(candidate)
            previous = identities.get(identity)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise ValueError(
                        "conflicting AEF outputs share stable record identity: "
                        f"{previous[1]} and {candidate}"
                    )
                continue
            identities[identity] = (fingerprint, candidate.resolve())
            discovered[str(candidate.resolve())] = candidate.resolve()
    return [discovered[key] for key in sorted(discovered)]


def _normalize_audit_issue_codes(values: Sequence[str]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for value in values:
        code = _clean_string(value).lower()
        if not code or AUDIT_ISSUE_CODE_PATTERN.fullmatch(code) is None:
            raise ValueError(f"invalid audit issue code: {value!r}")
        normalized.add(code)
    return tuple(sorted(normalized))


def _validate_completed_experiments(
    paths: Sequence[Path],
    *,
    allowed_audit_issue_codes: Sequence[str] = (),
) -> tuple[tuple[str, ...], tuple[tuple[str, str, str], ...]]:
    allowed_codes = frozenset(_normalize_audit_issue_codes(allowed_audit_issue_codes))
    encountered_allowed_codes: set[str] = set()
    evidence_file_hashes: list[tuple[str, str, str]] = []
    checked: set[Path] = set()
    for raw_path in paths:
        start = raw_path.expanduser().resolve()
        current = start if start.is_dir() else start.parent
        audit_path: Path | None = None
        for candidate_root in (current, *current.parents):
            candidate = candidate_root / "summary" / "final-audit.json"
            if candidate.is_file():
                audit_path = candidate
                break
        if audit_path is None:
            raise ValueError(f"no summary/final-audit.json found for artifact path: {raw_path}")
        if audit_path in checked:
            continue
        checked.add(audit_path)
        evidence_file_hashes.append(
            ("final_audit", str(audit_path), _file_sha256(audit_path))
        )
        audit = _load_json(audit_path)
        if not isinstance(audit, Mapping) or audit.get("complete") is not True:
            raise ValueError(f"experiment is not complete: {audit_path}")
        raw_issues = audit.get("issues")
        issues = [] if raw_issues is None else raw_issues
        if not isinstance(issues, list) or any(
            not isinstance(issue, Mapping) for issue in issues
        ):
            raise ValueError(f"experiment final audit has malformed issues: {audit_path}")
        issue_count = audit.get("issue_count")
        if issue_count is None:
            issue_count = len(issues)
        if (
            isinstance(issue_count, bool)
            or not isinstance(issue_count, int)
            or issue_count < 0
            or issue_count != len(issues)
        ):
            raise ValueError(f"experiment final audit issue_count disagrees: {audit_path}")
        issue_codes: set[str] = set()
        for issue in issues:
            raw_code = issue.get("code")
            code = _clean_string(raw_code).lower()
            if not code or AUDIT_ISSUE_CODE_PATTERN.fullmatch(code) is None:
                raise ValueError(f"experiment final audit has invalid issue code: {audit_path}")
            issue_codes.add(code)
        clean = (
            audit.get("ok") is True
            and audit.get("integrity_ok") is True
            and not issues
        )
        if clean:
            continue
        unallowed = sorted(issue_codes - allowed_codes)
        if not issues or unallowed:
            detail = ", ".join(unallowed) if unallowed else "no auditable issue evidence"
            raise ValueError(
                f"experiment is not cleanly auditable: {audit_path} ({detail})"
            )
        encountered_allowed_codes.update(issue_codes)
    return (
        tuple(sorted(encountered_allowed_codes)),
        tuple(evidence_file_hashes),
    )


def _finished_at(output_path: Path) -> str:
    metadata_path = output_path.with_name(f"{output_path.stem}.meta.json")
    metadata = _load_json(metadata_path)
    if not isinstance(metadata, Mapping):
        raise ValueError(f"AEF output lacks readable timing metadata: {metadata_path}")
    value = metadata.get("finished_at") or metadata.get("started_at")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"AEF output lacks finished_at/started_at: {metadata_path}")
    try:
        return _normalized_timestamp(value)
    except ValueError as exc:
        raise ValueError(f"AEF output has invalid timestamp: {metadata_path}") from exc


def _normalized_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp must include a UTC offset: {value}")
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _compact_timestamp(value: str) -> str:
    try:
        normalized = _normalized_timestamp(value)
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid generated_at timestamp: {value}") from exc
    return parsed.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _usage_records(payload: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    results = payload.get("results")
    if not isinstance(results, list):
        return
    for result in results:
        if not isinstance(result, Mapping):
            continue
        judge = result.get("judge")
        metadata = judge.get("metadata") if isinstance(judge, Mapping) else None
        usage = metadata.get("usage") if isinstance(metadata, Mapping) else None
        if isinstance(usage, Mapping):
            yield usage


def _ensemble_traces(usage: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    traces = usage.get("ensemble_traces")
    if isinstance(traces, list):
        return [trace for trace in traces if isinstance(trace, Mapping)]
    trace = usage.get("ensemble_trace")
    return [trace] if isinstance(trace, Mapping) else []


def _clean_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_identity(value: Any) -> tuple[str, str]:
    text = _clean_string(value)
    if ":" not in text:
        return "", ""
    provider, model_id = text.split(":", 1)
    return provider.strip().lower(), model_id.strip()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _walk_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_mappings(child)


def _record_identity(record: Mapping[str, Any], *, target: bool = False) -> tuple[str, str]:
    execution = record.get("execution")
    execution = execution if isinstance(execution, Mapping) else {}
    provider = _clean_string(
        record.get("actual_provider")
        or record.get("provider")
        or execution.get("provider")
        or record.get("requested_provider")
    ).lower()
    model_id = _clean_string(
        record.get("actual_model")
        or record.get("model")
        or execution.get("model")
        or record.get("requested_model")
    )
    if model_id:
        return provider, model_id
    parsed_provider, parsed_model = _parse_identity(record.get("identity"))
    if parsed_model:
        return provider or parsed_provider, parsed_model
    identity_field = "target_identity" if target else "source_identity"
    parsed_provider, parsed_model = _parse_identity(record.get(identity_field))
    return provider or parsed_provider, parsed_model


_PARTIAL_ISOLATION_V1 = "opensquilla.ensemble-partial-proposer-quorum/v1"
_NATIVE_CLEANUP_V1 = "opensquilla.proposer-cleanup-quorum-bypass/v1"
_ROUTER_CLEANUP_V1 = (
    "opensquilla.router-dynamic-proposer-cleanup-quorum-bypass/v1"
)


def _validated_tool_execution_state(
    attempt: Mapping[str, Any],
    *,
    context: str,
) -> str:
    """Return enabled/disabled/unknown and reject internally conflicting fields."""

    execution = attempt.get("execution")
    if not isinstance(execution, Mapping):
        return "unknown"

    tools_enabled = execution.get("tools_enabled")
    if tools_enabled is not None and not isinstance(tools_enabled, bool):
        raise ValueError(f"invalid tools_enabled in {context}")

    raw_count = execution.get("tool_count")
    if raw_count is not None and (
        isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0
    ):
        raise ValueError(f"invalid tool_count in {context}")

    raw_names = execution.get("tool_names")
    normalized_names: list[str] | None = None
    if raw_names is not None:
        if not isinstance(raw_names, list):
            raise ValueError(f"invalid tool_names in {context}")
        if not all(isinstance(name, str) and name.strip() for name in raw_names):
            raise ValueError(f"invalid tool_names in {context}")
        normalized_names = [name.strip() for name in raw_names]
        if len(set(normalized_names)) != len(normalized_names):
            raise ValueError(f"duplicate tool_names in {context}")
        if raw_count is not None and raw_count != len(normalized_names):
            raise ValueError(f"tool_count disagrees with tool_names in {context}")

    if tools_enabled is True:
        if raw_count == 0 or normalized_names == []:
            raise ValueError(f"tools_enabled conflicts with empty tools in {context}")
        if raw_count is None or raw_count <= 0 or not normalized_names:
            return "unknown"
        return "enabled"

    if tools_enabled is False:
        if (raw_count is not None and raw_count > 0) or normalized_names:
            raise ValueError(f"tools_disabled conflicts with configured tools in {context}")
        if (
            "effective_tool_choice" in execution
            and execution.get("effective_tool_choice") is not None
        ):
            raise ValueError(f"tools_disabled conflicts with tool_choice in {context}")
        if (
            raw_count == 0
            and normalized_names == []
            and "effective_tool_choice" in execution
            and execution.get("effective_tool_choice") is None
        ):
            return "disabled"
        return "unknown"

    if (raw_count is not None and raw_count > 0) or normalized_names:
        return "unknown"
    return "unknown"


def _validated_v1_isolation_marker(trace: Mapping[str, Any]) -> bool:
    """Validate exact historical v1 markers; newer schemas never prove neutrality."""

    markers: list[tuple[str, Any, str, bool]] = [
        (
            "proposer_partial_quorum",
            trace.get("proposer_partial_quorum"),
            _PARTIAL_ISOLATION_V1,
            True,
        ),
        (
            "proposer_cleanup_quorum_bypass",
            trace.get("proposer_cleanup_quorum_bypass"),
            _NATIVE_CLEANUP_V1,
            False,
        ),
    ]
    proposer_recovery = trace.get("proposer_recovery")
    markers.append(
        (
            "proposer_recovery.cleanup_quorum_bypass",
            proposer_recovery.get("cleanup_quorum_bypass")
            if isinstance(proposer_recovery, Mapping)
            else None,
            _ROUTER_CLEANUP_V1,
            False,
        )
    )

    validated = False
    for context, marker, expected_schema, requires_marker_isolation in markers:
        if not isinstance(marker, Mapping) or marker.get("schema") != expected_schema:
            continue
        applied = marker.get("applied")
        tools_disabled = marker.get("aggregator_tools_disabled")
        marker_isolated = marker.get("aggregator_isolated")
        for field_name, value in (
            ("applied", applied),
            ("aggregator_tools_disabled", tools_disabled),
            ("aggregator_isolated", marker_isolated),
        ):
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"invalid {field_name} in {context}")
        if applied is False and (
            tools_disabled is True
            or (requires_marker_isolation and marker_isolated is True)
        ):
            raise ValueError(f"contradictory v1 isolation marker in {context}")
        if applied is not True:
            continue
        if tools_disabled is False:
            raise ValueError(f"contradictory v1 isolation marker in {context}")
        if tools_disabled is not True:
            continue
        if requires_marker_isolation:
            if marker_isolated is False:
                raise ValueError(f"contradictory v1 isolation marker in {context}")
            if marker_isolated is not True:
                continue
        validated = True

    trace_isolated = trace.get("aggregator_isolated")
    if validated and trace_isolated is not None and not isinstance(trace_isolated, bool):
        raise ValueError("invalid trace.aggregator_isolated")
    if validated and trace_isolated is False:
        raise ValueError("v1 isolation marker conflicts with trace.aggregator_isolated")
    return validated and trace_isolated is True


def _validated_tool_removal_transition(
    trace: Mapping[str, Any],
    attempt: Mapping[str, Any],
    *,
    attempt_index: int,
) -> bool:
    aggregator_recovery = trace.get("aggregator_recovery")
    recovery_attempts = (
        aggregator_recovery.get("attempts")
        if isinstance(aggregator_recovery, Mapping)
        else None
    )
    if not isinstance(recovery_attempts, list):
        return False
    if attempt_index < 0 or attempt_index >= len(recovery_attempts):
        return False
    if recovery_attempts[attempt_index] is not attempt:
        return False

    current_state = _validated_tool_execution_state(
        attempt,
        context=f"aggregator_recovery.attempts[{attempt_index}]",
    )
    earlier_enabled = False
    for earlier_index, earlier_attempt in enumerate(recovery_attempts[:attempt_index]):
        if not isinstance(earlier_attempt, Mapping):
            continue
        if earlier_attempt.get("request_started") is not True:
            continue
        earlier_state = _validated_tool_execution_state(
            earlier_attempt,
            context=f"aggregator_recovery.attempts[{earlier_index}]",
        )
        earlier_enabled = earlier_enabled or earlier_state == "enabled"
    return current_state == "disabled" and earlier_enabled


def _framework_attributed_aggregator_attempt(
    trace: Mapping[str, Any],
    attempt: Mapping[str, Any],
    *,
    attempt_index: int,
) -> bool:
    if attempt.get("request_started") is not True:
        return False

    current_state = _validated_tool_execution_state(
        attempt,
        context=f"aggregator_recovery.attempts[{attempt_index}]",
    )
    marker_valid = _validated_v1_isolation_marker(trace)
    if marker_valid and current_state == "enabled":
        raise ValueError("v1 isolation marker conflicts with enabled aggregator tools")
    marker_evidence = marker_valid and current_state == "disabled"
    transition_evidence = _validated_tool_removal_transition(
        trace,
        attempt,
        attempt_index=attempt_index,
    )
    aggregator_tools = trace.get("aggregator_tools")
    if aggregator_tools is not None and not isinstance(aggregator_tools, bool):
        raise ValueError("invalid trace.aggregator_tools")
    if aggregator_tools is False and (marker_evidence or transition_evidence):
        raise ValueError("framework tool-removal evidence conflicts with aggregator_tools=false")
    if aggregator_tools is not True:
        return False
    return marker_evidence or transition_evidence


def _nonnegative_request_count(value: Any, *, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid request_count in {context}")
    return value


def _reported_usage_ids(
    traces: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[str, str, str]]:
    reported: dict[str, tuple[str, str, str]] = {}
    for trace in traces:
        for row in _walk_mappings(trace):
            attempt_id = _clean_string(row.get("physical_attempt_id"))
            role = _clean_string(row.get("role")).lower()
            if not attempt_id or role not in ROLES:
                continue
            provider, model_id = _record_identity(row)
            if not model_id:
                continue
            identity = (provider, model_id, role)
            previous = reported.get(attempt_id)
            if previous is not None and previous != identity:
                raise ValueError(f"physical attempt {attempt_id} has conflicting usage identities")
            reported[attempt_id] = identity
    return reported


def _breakdown_counts(
    usage: Mapping[str, Any],
    *,
    context: str,
) -> tuple[Counter[tuple[str, str, str]], int, int]:
    role_counts: Counter[tuple[str, str, str]] = Counter()
    total = 0
    unclassified = 0
    breakdown = usage.get("model_usage_breakdown")
    if not isinstance(breakdown, list):
        return role_counts, total, unclassified
    for index, row in enumerate(breakdown):
        if not isinstance(row, Mapping):
            raise ValueError(f"malformed model_usage_breakdown in {context}")
        count = _nonnegative_request_count(
            row.get("request_count"),
            context=f"{context} model_usage_breakdown[{index}]",
        )
        total += count
        role = _clean_string(row.get("role")).lower()
        provider, model_id = _record_identity(row)
        if role in ROLES and model_id:
            role_counts[(provider, model_id, role)] += count
        else:
            unclassified += count
    return role_counts, total, unclassified


def _trace_physical_request_count(traces: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for index, trace in enumerate(traces):
        raw = trace.get("physical_request_count")
        if raw is None:
            continue
        total += _nonnegative_request_count(raw, context=f"ensemble_traces[{index}]")
    return total


def _pool_key(
    counts: Counter[tuple[str, str, str]],
    *,
    provider: str,
    model_id: str,
    role: str,
) -> tuple[str, str, str] | None:
    exact = (provider.lower(), model_id, role)
    if counts.get(exact, 0) > 0:
        return exact
    matches = [
        key
        for key, count in counts.items()
        if count > 0 and key[1].lower() == model_id.lower() and key[2] == role
    ]
    return matches[0] if len(matches) == 1 else None


def _explicit_candidate_attempts(
    trace: Mapping[str, Any],
) -> Iterable[tuple[Mapping[str, Any], bool, tuple[str, str]]]:
    candidates = trace.get("candidates")
    if not isinstance(candidates, list):
        return
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        execution = candidate.get("execution")
        raw_attempts = (
            execution.get("physical_attempts") if isinstance(execution, Mapping) else None
        )
        attempts = (
            [attempt for attempt in raw_attempts if isinstance(attempt, Mapping)]
            if isinstance(raw_attempts, list)
            else []
        )
        candidate_failed = (
            candidate.get("ok") is not True
            or _clean_string(candidate.get("completion_outcome")).lower()
            in {"failed", "partial", "partial_usable"}
            or _clean_string(candidate.get("stop_reason")).lower()
            in LENGTH_CAPPED_STOP_REASONS
        )
        candidate_identity = _record_identity(candidate)
        if attempts:
            for attempt_index, attempt in enumerate(attempts):
                success = _clean_string(attempt.get("outcome")).lower() == "succeeded"
                if candidate_failed and attempt_index == len(attempts) - 1:
                    success = False
                yield attempt, success, candidate_identity
        elif candidate.get("request_started") is True:
            yield candidate, not candidate_failed, candidate_identity


def _validate_unknown_outcome_rate(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("max_unknown_outcome_rate must be a number between zero and one")
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError("max_unknown_outcome_rate must be a number between zero and one")
    return parsed


def collect_observations(
    paths: Sequence[Path],
    *,
    allow_incomplete: bool = False,
    allowed_audit_issue_codes: Sequence[str] = (),
    max_unknown_outcome_rate: float = DEFAULT_MAX_UNKNOWN_OUTCOME_RATE,
) -> CollectionResult:
    normalized_rate = _validate_unknown_outcome_rate(max_unknown_outcome_rate)
    normalized_allowed_codes = _normalize_audit_issue_codes(allowed_audit_issue_codes)
    if allow_incomplete and normalized_allowed_codes:
        raise ValueError(
            "allow_incomplete and allowed_audit_issue_codes are mutually exclusive"
        )
    encountered_allowed_codes: tuple[str, ...] = ()
    evidence_file_hashes: list[tuple[str, str, str]] = []
    if not allow_incomplete:
        encountered_allowed_codes, audit_file_hashes = _validate_completed_experiments(
            paths,
            allowed_audit_issue_codes=normalized_allowed_codes,
        )
        evidence_file_hashes.extend(audit_file_hashes)
    output_files = discover_output_files(paths)
    output_file_hashes: list[tuple[str, str]] = []
    observations: list[Observation] = []
    seen_attempts: dict[str, tuple[str, str, str, bool, str, int]] = {}
    framework_excluded = 0
    duplicate_attempts = 0
    physical_requests_seen = 0
    unclassified_requests = 0
    unknown_outcomes = 0

    for output_path in output_files:
        payload = _load_json(output_path)
        if not isinstance(payload, Mapping):
            continue
        finished_at = _finished_at(output_path)
        metadata_path = output_path.with_name(f"{output_path.stem}.meta.json")
        evidence_file_hashes.append(
            ("timing_metadata", str(metadata_path), _file_sha256(metadata_path))
        )
        fingerprint = _file_sha256(output_path)
        output_file_hashes.append((str(output_path), fingerprint))
        for usage_index, usage in enumerate(_usage_records(payload)):
            traces = _ensemble_traces(usage)
            reported_ids = _reported_usage_ids(traces)
            pool, breakdown_total, unknown_pool = _breakdown_counts(
                usage,
                context=str(output_path),
            )
            trace_total = _trace_physical_request_count(traces)
            if trace_total and breakdown_total and trace_total != breakdown_total:
                raise ValueError(
                    f"physical request reconciliation failed for {output_path}: "
                    f"trace={trace_total}, breakdown={breakdown_total}"
            )
            physical_requests_seen += max(trace_total, breakdown_total)
            local_attempts: dict[str, tuple[str, str, str, bool, str, int]] = {}

            def record_attempt(
                record: Mapping[str, Any],
                *,
                trace: Mapping[str, Any],
                trace_index: int,
                role: str,
                success: bool,
                phase_rank: int,
                item_index: int,
                target_identity: bool = False,
                fallback_identity: tuple[str, str] = ("", ""),
            ) -> None:
                nonlocal duplicate_attempts
                nonlocal framework_excluded
                nonlocal unknown_pool
                if record.get("request_started") is not True:
                    return
                reported_identity = reported_ids.get(
                    _clean_string(record.get("physical_attempt_id"))
                )
                provider, model_id = (
                    reported_identity[:2]
                    if reported_identity is not None
                    else _record_identity(record, target=target_identity)
                )
                if not model_id:
                    provider, model_id = fallback_identity
                if not model_id:
                    raise ValueError(
                        f"started physical attempt lacks model identity in {output_path}"
                    )
                attempt_id = _clean_string(record.get("physical_attempt_id")) or (
                    f"{fingerprint}:{usage_index}:{trace_index}:{role}:{phase_rank}:{item_index}"
                )
                signature = (
                    provider.lower(),
                    model_id.lower(),
                    role,
                    success,
                    str(output_path),
                    usage_index,
                )
                previous_local = local_attempts.get(attempt_id)
                if previous_local is not None:
                    if previous_local[:4] != signature[:4]:
                        raise ValueError(
                            f"physical attempt {attempt_id} has conflicting duplicate evidence"
                        )
                    duplicate_attempts += 1
                    return
                local_attempts[attempt_id] = signature
                previous_global = seen_attempts.get(attempt_id)
                if previous_global is not None:
                    raise ValueError(
                        "physical attempt id is reused across usage records: "
                        f"{attempt_id} ({previous_global[4]} and {output_path})"
                    )
                seen_attempts[attempt_id] = signature

                reported_identity = reported_ids.get(attempt_id)
                key = _pool_key(
                    pool,
                    provider=provider,
                    model_id=model_id,
                    role=role,
                )
                represented = (
                    reported_identity is not None
                    or record.get("usage_reported") is True
                    or (success and key is not None)
                )
                if reported_identity is not None:
                    provider, model_id, reported_role = reported_identity
                    if reported_role != role:
                        raise ValueError(f"physical attempt {attempt_id} has conflicting roles")
                if represented:
                    key = _pool_key(pool, provider=provider, model_id=model_id, role=role)
                    if key is None:
                        raise ValueError(
                            f"reported physical attempt {attempt_id} is absent from "
                            f"model_usage_breakdown in {output_path}"
                        )
                    pool[key] -= 1
                elif unknown_pool > 0:
                    unknown_pool -= 1
                else:
                    raise ValueError(
                        f"physical attempt {attempt_id} has no usage ledger entry in {output_path}"
                    )

                if role == "aggregator" and _framework_attributed_aggregator_attempt(
                    trace,
                    record,
                    attempt_index=item_index,
                ):
                    framework_excluded += 1
                    return
                reason = _clean_string(
                    record.get("error")
                    or record.get("trigger")
                    or record.get("outcome")
                    or record.get("stop_reason")
                )
                observations.append(
                    Observation(
                        order=(
                            finished_at,
                            str(output_path),
                            usage_index,
                            phase_rank,
                            item_index,
                        ),
                        provider=provider,
                        model_id=model_id,
                        role=role,
                        success=success,
                        physical_attempt_id=attempt_id,
                        source_path=str(output_path),
                        reason=reason,
                    )
                )

            for trace_index, trace in enumerate(traces):
                for item_index, (attempt, success, fallback_identity) in enumerate(
                    _explicit_candidate_attempts(trace)
                ):
                    record_attempt(
                        attempt,
                        trace=trace,
                        trace_index=trace_index,
                        role="proposer",
                        success=success,
                        phase_rank=1,
                        item_index=item_index,
                        fallback_identity=fallback_identity,
                    )

                proposer_recovery = trace.get("proposer_recovery")
                recovery_attempts = (
                    proposer_recovery.get("attempts")
                    if isinstance(proposer_recovery, Mapping)
                    else None
                )
                if isinstance(recovery_attempts, list):
                    for item_index, attempt in enumerate(recovery_attempts):
                        if isinstance(attempt, Mapping):
                            record_attempt(
                                attempt,
                                trace=trace,
                                trace_index=trace_index,
                                role="proposer",
                                success=_clean_string(attempt.get("outcome")).lower()
                                == "succeeded",
                                phase_rank=2,
                                item_index=item_index,
                                target_identity=True,
                            )

                aggregator_recovery = trace.get("aggregator_recovery")
                aggregator_attempts = (
                    aggregator_recovery.get("attempts")
                    if isinstance(aggregator_recovery, Mapping)
                    else None
                )
                if isinstance(aggregator_attempts, list):
                    for item_index, attempt in enumerate(aggregator_attempts):
                        if isinstance(attempt, Mapping):
                            record_attempt(
                                attempt,
                                trace=trace,
                                trace_index=trace_index,
                                role="aggregator",
                                success=_clean_string(attempt.get("outcome")).lower()
                                == "succeeded",
                                phase_rank=3,
                                item_index=item_index,
                            )

            for (provider, model_id, role), count in sorted(pool.items()):
                if count < 0:
                    raise ValueError(f"negative request count for {provider}:{model_id}:{role}")
                unknown_outcomes += count
            unclassified_requests += unknown_pool

    observations.sort(key=lambda item: item.order)
    if unclassified_requests:
        raise ValueError(
            f"{unclassified_requests} physical requests could not be assigned to a model role"
        )
    if (
        len(observations) + framework_excluded + unknown_outcomes
        != physical_requests_seen
    ):
        raise ValueError(
            "physical request reconciliation failed after attribution: "
            f"seen={physical_requests_seen}, counted={len(observations)}, "
            f"framework_excluded={framework_excluded}, "
            f"unknown_outcomes={unknown_outcomes}"
        )
    outcome_population = len(observations) + unknown_outcomes
    unknown_outcome_rate = (
        unknown_outcomes / outcome_population if outcome_population else 0.0
    )
    if unknown_outcome_rate > normalized_rate:
        raise ValueError(
            "physical requests with unknown outcomes exceed the configured threshold: "
            f"{unknown_outcomes}/{outcome_population} "
            f"({unknown_outcome_rate:.6f} > {normalized_rate:.6f})"
        )
    return CollectionResult(
        observations=tuple(observations),
        output_files=tuple(str(path) for path in output_files),
        physical_requests_seen=physical_requests_seen,
        framework_excluded=framework_excluded,
        duplicate_attempts=duplicate_attempts,
        unclassified_requests=unclassified_requests,
        completion_gate=(
            "fixture_without_final_audit"
            if allow_incomplete
            else (
                "complete_with_audit_issue_allowlist"
                if encountered_allowed_codes
                else "clean_final_audit"
            )
        ),
        unknown_outcomes=unknown_outcomes,
        max_unknown_outcome_rate=normalized_rate,
        allowed_audit_issue_codes=encountered_allowed_codes,
        output_file_hashes=tuple(output_file_hashes),
        evidence_file_hashes=tuple(evidence_file_hashes),
    )


def _profile_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    facts = row.get("registry_facts")
    if not isinstance(facts, Mapping):
        return "", ""
    provider = _clean_string(facts.get("provider")).lower()
    model_id = _clean_string(facts.get("model_id"))
    return provider, model_id


def _base_snapshot_version(profiles: Mapping[str, Any]) -> str:
    """Return the immutable registry base across repeated reliability refreshes."""

    current = _validated_snapshot_version(
        profiles.get("snapshot_version"),
        field_name="snapshot_version",
    )
    provenance = profiles.get("role_reliability_snapshot")
    if provenance is None:
        return current
    if not isinstance(provenance, Mapping):
        raise ValueError("role_reliability_snapshot must be an object")
    if provenance.get("schema_version") not in {
        SNAPSHOT_SCHEMA_VERSION,
        *LEGACY_SNAPSHOT_SCHEMA_VERSIONS,
    }:
        raise ValueError("role_reliability_snapshot schema_version differs")
    if provenance.get("schema_version") == SNAPSHOT_SCHEMA_VERSION:
        recorded_content_sha256 = _clean_string(provenance.get("content_sha256")).lower()
        if len(recorded_content_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in recorded_content_sha256
        ):
            raise ValueError("role_reliability_snapshot lacks a valid content_sha256")
        models = profiles.get("models")
        if not isinstance(models, list) or (
            _reliability_snapshot_content_sha256(models, provenance)
            != recorded_content_sha256
        ):
            raise ValueError("role_reliability_snapshot content_sha256 differs")
    recorded_base = _validated_snapshot_version(
        provenance.get("base_snapshot_version"),
        field_name="role_reliability_snapshot.base_snapshot_version",
    )
    if not current.startswith(f"{recorded_base}-reliability-"):
        raise ValueError(
            "snapshot_version is inconsistent with role_reliability_snapshot base"
        )
    return recorded_base


def _validated_snapshot_version(value: Any, *, field_name: str) -> str:
    normalized = _clean_string(value)
    if not normalized or SNAPSHOT_VERSION_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a portable non-empty version string")
    return normalized


def _portable_artifact_reference(
    value: str,
    *,
    fallback_uri: str | None = None,
) -> str:
    """Use a machine-independent URI for paths inside an AEF reports tree."""

    raw = _clean_string(value)
    if not raw or raw.startswith("aef-report://"):
        return raw
    parts = [part for part in raw.replace("\\", "/").split("/") if part]
    report_indexes = [index for index, part in enumerate(parts) if part == "reports"]
    if not report_indexes:
        if raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
            if fallback_uri is None:
                raise ValueError(f"absolute artifact path is not portable: {raw}")
            return fallback_uri
        return raw
    suffix = parts[report_indexes[-1] + 1 :]
    if len(suffix) < 2:
        if raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
            if fallback_uri is None:
                raise ValueError(f"absolute artifact path is not portable: {raw}")
            return fallback_uri
        return raw
    return "aef-report://" + "/".join(suffix)


def _next_snapshot_version(
    *,
    base_snapshot: str,
    current_snapshot: str,
    generated_at: str,
    content_sha256: str,
    requested: str | None,
) -> str:
    prefix = f"{base_snapshot}-reliability-"
    if requested is None:
        normalized = (
            f"{prefix}{_compact_timestamp(generated_at)}-{content_sha256[:12]}"
        )
    else:
        normalized = _validated_snapshot_version(
            requested,
            field_name="snapshot_version",
        )
    if not normalized.startswith(prefix) or normalized == prefix:
        raise ValueError(
            "snapshot_version must preserve the original base and use the "
            f"{prefix!r} prefix"
        )
    if normalized == current_snapshot:
        raise ValueError("new snapshot_version must differ from the input snapshot_version")
    return normalized


def _portable_output_file_manifest(
    collection: CollectionResult,
) -> list[dict[str, str]]:
    expected_paths = tuple(collection.output_files)
    recorded_paths = tuple(path for path, _digest in collection.output_file_hashes)
    if recorded_paths != expected_paths:
        raise ValueError("output file hashes do not match the collected output files")
    manifest: list[dict[str, str]] = []
    seen_references: set[str] = set()
    for index, (path, digest) in enumerate(collection.output_file_hashes):
        normalized_digest = _clean_string(digest).lower()
        if len(normalized_digest) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_digest
        ):
            raise ValueError(f"invalid output file sha256 for {path}")
        source_path = Path(path)
        if not source_path.is_file() or _file_sha256(source_path) != normalized_digest:
            raise ValueError(f"output file changed after collection: {path}")
        reference = _portable_artifact_reference(
            path,
            fallback_uri=f"artifact-sha256://{normalized_digest}/{index}",
        )
        if reference in seen_references:
            raise ValueError(f"duplicate portable output reference: {reference}")
        seen_references.add(reference)
        manifest.append({"uri": reference, "sha256": normalized_digest})
    return manifest


def _portable_evidence_file_manifest(
    collection: CollectionResult,
) -> list[dict[str, str]]:
    manifest: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, (kind, path, digest) in enumerate(collection.evidence_file_hashes):
        if kind not in {"final_audit", "timing_metadata"}:
            raise ValueError(f"invalid evidence file kind: {kind}")
        normalized_digest = _clean_string(digest).lower()
        if len(normalized_digest) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_digest
        ):
            raise ValueError(f"invalid evidence file sha256 for {path}")
        source_path = Path(path)
        if not source_path.is_file() or _file_sha256(source_path) != normalized_digest:
            raise ValueError(f"evidence file changed after collection: {path}")
        reference = _portable_artifact_reference(
            path,
            fallback_uri=f"artifact-sha256://{normalized_digest}/{kind}/{index}",
        )
        identity = (kind, reference)
        if identity in seen:
            raise ValueError(f"duplicate portable evidence reference: {kind}:{reference}")
        seen.add(identity)
        manifest.append(
            {"kind": kind, "uri": reference, "sha256": normalized_digest}
        )
    return manifest


def _reliability_snapshot_content_sha256(
    models: Sequence[Any],
    provenance: Mapping[str, Any],
) -> str:
    provenance_without_hash = dict(provenance)
    provenance_without_hash.pop("content_sha256", None)
    return _canonical_json_sha256(
        {
            "models": list(models),
            "role_reliability_snapshot": provenance_without_hash,
        }
    )


def update_profiles(
    profiles: Mapping[str, Any],
    collection: CollectionResult,
    *,
    window_size: int = DEFAULT_WINDOW_SIZE,
    generated_at: str,
    source_artifacts: Sequence[str],
    snapshot_version: str | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    if window_size != DEFAULT_WINDOW_SIZE:
        raise ValueError(f"window_size must be {DEFAULT_WINDOW_SIZE}")
    updated = copy.deepcopy(dict(profiles))
    models = updated.get("models")
    if not isinstance(models, list):
        raise ValueError("profiles JSON must contain a models array")
    base_snapshot = _base_snapshot_version(updated)

    exact: dict[tuple[str, str], int] = {}
    by_model: defaultdict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(models):
        if not isinstance(row, Mapping):
            raise ValueError(f"profiles model row {index} must be an object")
        provider, model_id = _profile_identity(row)
        if not provider or not model_id:
            raise ValueError(f"profiles model row {index} lacks provider/model_id")
        key = (provider, model_id.lower())
        if key in exact:
            raise ValueError(f"duplicate profile identity: {provider}:{model_id}")
        exact[key] = index
        by_model[model_id.lower()].append(index)

    windows: defaultdict[tuple[int, str], deque[Observation]] = defaultdict(
        lambda: deque(maxlen=window_size)
    )
    unknown_identity_calls = 0
    for observation in collection.observations:
        key = (observation.provider.lower(), observation.model_id.lower())
        model_index = exact.get(key)
        if model_index is None:
            matches = by_model.get(observation.model_id.lower(), [])
            model_index = matches[0] if len(matches) == 1 else None
        if model_index is None:
            unknown_identity_calls += 1
            continue
        windows[(model_index, observation.role)].append(observation)
    if unknown_identity_calls:
        raise ValueError(
            f"{unknown_identity_calls} attributed calls do not match the model registry"
        )

    observed_models = 0
    for index, row in enumerate(models):
        row_dict = dict(row)
        online = row_dict.get("online_profile")
        online_dict = dict(online) if isinstance(online, Mapping) else {}
        role_counts: dict[str, dict[str, int]] = {}
        has_observation = False
        for role in ROLES:
            items = windows[(index, role)]
            success_count = sum(item.success for item in items)
            failure_count = len(items) - success_count
            role_counts[role] = {
                "success": success_count,
                "failure": failure_count,
            }
            has_observation = has_observation or bool(items)
        observed_models += int(has_observation)
        online_dict["role_reliability"] = {
            "window_size": window_size,
            "proposer": role_counts["proposer"],
            "aggregator": role_counts["aggregator"],
            "source": "aef_experiment_artifacts",
        }
        row_dict["online_profile"] = online_dict
        models[index] = row_dict

    window_calls = sum(len(items) for items in windows.values())
    attributable_calls = len(collection.observations)
    normalized_generated_at = _normalized_timestamp(generated_at)
    output_manifest = _portable_output_file_manifest(collection)
    evidence_manifest = _portable_evidence_file_manifest(collection)
    reliability_snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": normalized_generated_at,
        "base_snapshot_version": base_snapshot,
        "window_size": window_size,
        "source_artifacts": [
            _portable_artifact_reference(
                value,
                fallback_uri=f"artifact-root://{index}",
            )
            for index, value in enumerate(source_artifacts)
        ],
        "source_output_files": [row["uri"] for row in output_manifest],
        "source_output_file_hashes": output_manifest,
        "source_evidence_file_hashes": evidence_manifest,
        "observation_policy": OBSERVATION_POLICY,
        "completion_gate": collection.completion_gate,
        "allowed_audit_issue_codes": list(collection.allowed_audit_issue_codes),
        "ordering_granularity": "task_aggregate",
        "raw_physical_calls": collection.physical_requests_seen,
        "framework_neutral_calls": collection.framework_excluded,
        "attributable_calls": attributable_calls,
        "unknown_outcome_calls": collection.unknown_outcomes,
        "max_unknown_outcome_rate": collection.max_unknown_outcome_rate,
        "window_calls": window_calls,
        "duplicate_attempts_excluded": collection.duplicate_attempts,
        "unclassified_requests": collection.unclassified_requests,
    }
    content_sha256 = _reliability_snapshot_content_sha256(
        models,
        reliability_snapshot,
    )
    reliability_snapshot["content_sha256"] = content_sha256
    current_snapshot = _validated_snapshot_version(
        updated.get("snapshot_version"),
        field_name="snapshot_version",
    )
    updated["snapshot_version"] = _next_snapshot_version(
        base_snapshot=base_snapshot,
        current_snapshot=current_snapshot,
        generated_at=normalized_generated_at,
        content_sha256=content_sha256,
        requested=snapshot_version,
    )
    updated["role_reliability_snapshot"] = reliability_snapshot
    return updated, {
        "observed_models": observed_models,
        "raw_physical_calls": collection.physical_requests_seen,
        "framework_neutral_calls": collection.framework_excluded,
        "attributable_calls": attributable_calls,
        "unknown_outcome_calls": collection.unknown_outcomes,
        "window_calls": window_calls,
        "unclassified_requests": collection.unclassified_requests,
    }


def default_profiles_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "src"
        / "opensquilla"
        / "provider"
        / "router_dynamic_model_profiles.json"
    )


def default_output_path(profiles_path: Path) -> Path:
    return profiles_path.with_name(f"{profiles_path.stem}.updated{profiles_path.suffix}")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artifacts",
        nargs="+",
        type=Path,
        help=("complete accumulated set of AEF experiment roots, outputs "
              "directories, or result JSON files used to rebuild recent-50"),
    )
    parser.add_argument("--profiles", type=Path, default=default_profiles_path())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--in-place", action="store_true")
    parser.add_argument(
        "--allow-audit-issue-code",
        action="append",
        default=[],
        help=(
            "Explicit final-audit issue code that is safe for reliability attribution; "
            "repeat for multiple codes"
        ),
    )
    parser.add_argument(
        "--max-unknown-outcome-rate",
        type=float,
        default=DEFAULT_MAX_UNKNOWN_OUTCOME_RATE,
        help=(
            "Maximum fraction of role-attributed physical requests lacking an "
            "authenticated per-request outcome (default: 0)"
        ),
    )
    parser.add_argument("--snapshot-version")
    parser.add_argument(
        "--generated-at",
        help="Override UTC snapshot time for reproducible tests/audits",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.in_place and args.output is not None:
        raise SystemExit("--in-place and --output are mutually exclusive")
    profiles_path = args.profiles.expanduser().resolve()
    profiles = _load_json(profiles_path)
    if not isinstance(profiles, Mapping):
        raise SystemExit(f"invalid profiles JSON: {profiles_path}")
    generated_at = args.generated_at or datetime.now(UTC).isoformat(timespec="seconds")
    collection = collect_observations(
        args.artifacts,
        allowed_audit_issue_codes=args.allow_audit_issue_code,
        max_unknown_outcome_rate=args.max_unknown_outcome_rate,
    )
    if not collection.output_files:
        raise SystemExit("no AEF output JSON files with results were found")
    updated, summary = update_profiles(
        profiles,
        collection,
        window_size=DEFAULT_WINDOW_SIZE,
        generated_at=generated_at,
        source_artifacts=[str(path.expanduser().resolve()) for path in args.artifacts],
        snapshot_version=args.snapshot_version,
    )
    if args.in_place:
        output_path = profiles_path
    elif args.output:
        output_path = args.output.expanduser().resolve()
    else:
        output_path = default_output_path(profiles_path)
    _write_json_atomic(output_path, updated)
    print(
        json.dumps(
            {
                "output": str(output_path),
                "output_files": len(collection.output_files),
                **summary,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
