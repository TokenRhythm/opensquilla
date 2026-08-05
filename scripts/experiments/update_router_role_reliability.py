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
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA_VERSION = "role-reliability-snapshot-v1"
OBSERVATION_POLICY = "aef-physical-model-calls-v4"
DEFAULT_WINDOW_SIZE = 50
ROLES = ("proposer", "aggregator")
LENGTH_CAPPED_STOP_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})


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


def _validate_completed_experiments(
    paths: Sequence[Path],
    *,
    allow_audit_issues: bool = False,
) -> None:
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
        audit = _load_json(audit_path)
        if not isinstance(audit, Mapping) or audit.get("complete") is not True:
            raise ValueError(f"experiment is not complete: {audit_path}")
        if allow_audit_issues:
            continue
        if not all(audit.get(field) is True for field in ("ok", "integrity_ok")):
            raise ValueError(f"experiment is not cleanly auditable: {audit_path}")
        if audit.get("issue_count") not in (None, 0) or audit.get("issues") not in (None, []):
            raise ValueError(f"experiment final audit reports issues: {audit_path}")


def _finished_at(output_path: Path) -> str:
    metadata_path = output_path.with_name(f"{output_path.stem}.meta.json")
    metadata = _load_json(metadata_path)
    if isinstance(metadata, Mapping):
        value = metadata.get("finished_at") or metadata.get("started_at")
        if isinstance(value, str) and value.strip():
            return _normalized_timestamp(value)
    return ""


def _normalized_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return value.strip()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _compact_timestamp(value: str) -> str:
    normalized = _normalized_timestamp(value)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid generated_at timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
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


def collect_observations(
    paths: Sequence[Path],
    *,
    allow_incomplete: bool = False,
    allow_audit_issues: bool = False,
) -> CollectionResult:
    if allow_incomplete and allow_audit_issues:
        raise ValueError("allow_incomplete and allow_audit_issues are mutually exclusive")
    if not allow_incomplete:
        _validate_completed_experiments(
            paths,
            allow_audit_issues=allow_audit_issues,
        )
    output_files = discover_output_files(paths)
    observations: list[Observation] = []
    seen_attempts: set[str] = set()
    framework_excluded = 0
    duplicate_attempts = 0
    physical_requests_seen = 0
    unclassified_requests = 0

    for output_path in output_files:
        payload = _load_json(output_path)
        if not isinstance(payload, Mapping):
            continue
        finished_at = _finished_at(output_path)
        fingerprint = _file_sha256(output_path)
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
            local_attempt_ids: set[str] = set()

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
                if attempt_id in local_attempt_ids:
                    duplicate_attempts += 1
                    return
                local_attempt_ids.add(attempt_id)
                if attempt_id in seen_attempts:
                    duplicate_attempts += 1
                    return
                seen_attempts.add(attempt_id)

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

            residual_index = 0
            for (provider, model_id, role), count in sorted(pool.items()):
                if count < 0:
                    raise ValueError(f"negative request count for {provider}:{model_id}:{role}")
                for ordinal in range(count):
                    attempt_id = (
                        f"{fingerprint}:{usage_index}:breakdown:"
                        f"{provider}:{model_id}:{role}:{ordinal}"
                    )
                    if attempt_id in seen_attempts:
                        duplicate_attempts += 1
                        continue
                    seen_attempts.add(attempt_id)
                    observations.append(
                        Observation(
                            order=(
                                finished_at,
                                str(output_path),
                                usage_index,
                                0,
                                residual_index,
                            ),
                            provider=provider,
                            model_id=model_id,
                            role=role,
                            success=True,
                            physical_attempt_id=attempt_id,
                            source_path=str(output_path),
                            reason="reported_usage_success",
                        )
                    )
                    residual_index += 1
            unclassified_requests += unknown_pool

    observations.sort(key=lambda item: item.order)
    if unclassified_requests:
        raise ValueError(
            f"{unclassified_requests} physical requests could not be assigned to a model role"
        )
    if len(observations) + framework_excluded != physical_requests_seen:
        raise ValueError(
            "physical request reconciliation failed after attribution: "
            f"seen={physical_requests_seen}, counted={len(observations)}, "
            f"framework_excluded={framework_excluded}"
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
                "complete_with_audit_override"
                if allow_audit_issues
                else "clean_final_audit"
            )
        ),
    )


def _profile_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    facts = row.get("registry_facts")
    if not isinstance(facts, Mapping):
        return "", ""
    provider = _clean_string(facts.get("provider")).lower()
    model_id = _clean_string(facts.get("model_id"))
    return provider, model_id


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

    base_snapshot = _clean_string(updated.get("snapshot_version")) or "unknown"
    window_calls = sum(len(items) for items in windows.values())
    attributable_calls = len(collection.observations)
    compact_time = _compact_timestamp(generated_at)
    updated["snapshot_version"] = snapshot_version or (
        f"{base_snapshot}-reliability-{compact_time}"
    )
    updated["role_reliability_snapshot"] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "base_snapshot_version": base_snapshot,
        "window_size": window_size,
        "source_artifacts": list(source_artifacts),
        "source_output_files": list(collection.output_files),
        "observation_policy": OBSERVATION_POLICY,
        "completion_gate": collection.completion_gate,
        "ordering_granularity": "task_aggregate",
        "raw_physical_calls": collection.physical_requests_seen,
        "framework_neutral_calls": collection.framework_excluded,
        "attributable_calls": attributable_calls,
        "window_calls": window_calls,
        "duplicate_attempts_excluded": collection.duplicate_attempts,
        "unclassified_requests": collection.unclassified_requests,
    }
    return updated, {
        "observed_models": observed_models,
        "raw_physical_calls": collection.physical_requests_seen,
        "framework_neutral_calls": collection.framework_excluded,
        "attributable_calls": attributable_calls,
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
        "--allow-audit-issues",
        action="store_true",
        help=(
            "Allow issues in final-audit.json, while still requiring the experiment "
            "to be complete"
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
        allow_audit_issues=args.allow_audit_issues,
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
