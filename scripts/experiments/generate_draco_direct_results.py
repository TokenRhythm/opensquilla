#!/usr/bin/env python3
"""Build a strict offline EXPERIMENT_RESULTS.md from direct DRACO waves.

The command imports accounting and resume-selection helpers from the selected
OpenSquilla checkout, but never invokes either runner main, a provider, a
Judge, or the network.  Result JSONLs must be supplied in causal order:
initial run first, followed by every resume/repair wave.

The primary analysis accepts exactly three sealed, policy-matched 3/3 generation
exhaustions (B2/B4/S4 on the same frozen task) as observed protocol failures.
They remain unscored Judge cells, contribute actual spend, and receive utility
zero only in the explicitly failure-aware operational analysis.  A legacy B2
no-history replacement CLI remains parsed as a fail-closed compatibility path;
it never replaces the 57/60 primary scoring matrix and is unused here.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import random
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ARMS = ("B0", "B1", "B2", "B4", "G1", "S4")
ARM_SET = frozenset(ARMS)
EXPECTED_TASKS = 10
EXPECTED_PAIRS = EXPECTED_TASKS * len(ARMS)
BOOTSTRAP_SAMPLES = 20_000
RESULT_NAME = re.compile(r"^draco_ensemble_(?P<stamp>[0-9]{8}-[0-9]{6})\.jsonl$")
MANIFEST_SCHEMA = "opensquilla.draco-run-manifest/v2"
INCIDENT_SCHEMA = "opensquilla.draco-report-incident-replacement/v1"
INCIDENT_GROUP = "B2"
INCIDENT_TASK_ID = "f004b46b-c0e7-4e86-a072-c7491328d538"
INCIDENT_KEY = (INCIDENT_GROUP, INCIDENT_TASK_ID)
INCIDENT_ERROR_FRAGMENT = "tool-enabled aggregation requires 3 fully completed proposer"
INCIDENT_COMPLETED_PROPOSERS = (1, 2, 2)
INCIDENT_ATTEMPT_ERRORS = tuple(
    "tool-enabled aggregation requires 3 fully completed proposer draft(s), "
    f"but only {completed} completed; aggregation was not started"
    for completed in INCIDENT_COMPLETED_PROPOSERS
)
INCIDENT_PRIMARY_BUDGET = 3
INCIDENT_REPLACEMENT_PROTOCOL = "fresh_targeted_budget_extension_via_resume_runner"
INCIDENT_REPLACEMENT_GROUPS = ("B2", "G1")
B4_INCIDENT_SCHEMA = "opensquilla.draco-report-b4-empty-response-replacement/v1"
B4_INCIDENT_GROUP = "B4"
B4_INCIDENT_TASK_ID = INCIDENT_TASK_ID
B4_INCIDENT_KEY = (B4_INCIDENT_GROUP, B4_INCIDENT_TASK_ID)
B4_INCIDENT_ERROR = (
    "Provider returned no visible response for a large input. Send the material as an "
    "attachment, summarize or shorten the prompt, or use a stronger model."
)
B4_INCIDENT_REPLACEMENT_GROUPS = ("B4", "G1")
S4_INCIDENT_SCHEMA = "opensquilla.draco-report-s4-empty-response-replacement/v1"
S4_INCIDENT_GROUP = "S4"
S4_INCIDENT_TASK_ID = INCIDENT_TASK_ID
S4_INCIDENT_KEY = (S4_INCIDENT_GROUP, S4_INCIDENT_TASK_ID)
S4_INCIDENT_SHORT_ERROR = "Provider returned an empty response"
S4_INCIDENT_REPLACEMENT_GROUPS = ("S4", "G1")
REPORTABLE_METADATA_ONLY_COST_REASONS = ("cost_metadata_incomplete",)
B2_VALIDATOR_FALSE_REASON = "missing_expected_b2_ensemble_contract"
B2_INCIDENT_CONTRACT_REASON = "insufficient_b2_configured_quorum"
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_STATUSES = frozenset(
    {
        "aborted",
        "complete",
        "judge_incomplete",
        "metadata_incomplete",
        "result_incomplete",
        "resume_repair_incomplete",
    }
)


class ReportError(ValueError):
    """An input cannot support a strict final report."""


@dataclass(frozen=True)
class IncidentPolicy:
    name: str
    schema: str
    incident_id: str
    group: str
    task_id: str
    failure_kind: str
    replacement_groups: tuple[str, str]
    expected_actual_llm_request_count: int
    expected_actual_llm_unknown_request_count: int
    expected_recorded_actual_llm_cost_usd: Decimal | None = None
    expected_attempt_errors: tuple[str, ...] = ()
    expected_generation_reasons: tuple[str, ...] = ()
    expected_judge_reasons: tuple[str, ...] = ()
    expected_cost_metadata_reasons: tuple[str, ...] = ()
    expected_audit_reasons: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str]:
        return (self.group, self.task_id)


B2_INCIDENT_POLICY = IncidentPolicy(
    name="b2_strict_quorum",
    schema=INCIDENT_SCHEMA,
    incident_id="b2-f004-strict-quorum-exhaustion-posthoc-replacement",
    group=INCIDENT_GROUP,
    task_id=INCIDENT_TASK_ID,
    failure_kind="strict_quorum",
    replacement_groups=INCIDENT_REPLACEMENT_GROUPS,
    expected_actual_llm_request_count=12,
    expected_actual_llm_unknown_request_count=1,
    expected_recorded_actual_llm_cost_usd=Decimal("0.534564759"),
    expected_attempt_errors=INCIDENT_ATTEMPT_ERRORS,
    expected_generation_reasons=(
        "generation_error",
        "empty_final_text",
        "aggregator_call_error",
        "final_request_not_aggregator",
        "insufficient_proposer_quorum",
        "insufficient_configured_proposer_quorum",
        "insufficient_actual_proposer_quorum",
        "aggregator_request_incomplete",
        "final_request_trace_not_aggregator",
        "aggregator_request_not_started",
        "aggregator_done_output_missing",
        B2_INCIDENT_CONTRACT_REASON,
    ),
    expected_judge_reasons=(
        "judge_incomplete",
        "judge_errors",
        "missing_quality_total",
    ),
    expected_cost_metadata_reasons=(
        "missing_actual_aggregator_model_backfill_required",
        "missing_actual_aggregator_provider_backfill_required",
        "cost_metadata_incomplete",
    ),
)
B4_INCIDENT_POLICY = IncidentPolicy(
    name="b4_empty_response",
    schema=B4_INCIDENT_SCHEMA,
    incident_id="b4-f004-repeated-empty-response-posthoc-replacement",
    group=B4_INCIDENT_GROUP,
    task_id=B4_INCIDENT_TASK_ID,
    failure_kind="repeated_empty_response",
    replacement_groups=B4_INCIDENT_REPLACEMENT_GROUPS,
    expected_actual_llm_request_count=35,
    expected_actual_llm_unknown_request_count=0,
    expected_recorded_actual_llm_cost_usd=Decimal("6.865969500000003"),
    expected_attempt_errors=(B4_INCIDENT_ERROR,) * 3,
    expected_generation_reasons=("generation_error", "empty_final_text"),
    expected_judge_reasons=(
        "judge_incomplete",
        "judge_errors",
        "missing_quality_total",
    ),
)
S4_INCIDENT_POLICY = IncidentPolicy(
    name="s4_empty_response",
    schema=S4_INCIDENT_SCHEMA,
    incident_id="s4-f004-repeated-empty-response-posthoc-replacement",
    group=S4_INCIDENT_GROUP,
    task_id=S4_INCIDENT_TASK_ID,
    failure_kind="repeated_empty_response",
    replacement_groups=S4_INCIDENT_REPLACEMENT_GROUPS,
    expected_actual_llm_request_count=25,
    expected_actual_llm_unknown_request_count=0,
    expected_recorded_actual_llm_cost_usd=Decimal("0.20786312800000004"),
    expected_attempt_errors=(
        B4_INCIDENT_ERROR,
        S4_INCIDENT_SHORT_ERROR,
        S4_INCIDENT_SHORT_ERROR,
    ),
    expected_generation_reasons=("generation_error", "empty_final_text"),
    expected_judge_reasons=(
        "judge_incomplete",
        "judge_errors",
        "missing_quality_total",
    ),
)
INCIDENT_POLICIES = (B2_INCIDENT_POLICY, B4_INCIDENT_POLICY, S4_INCIDENT_POLICY)
INCIDENT_POLICY_BY_KEY = {policy.key: policy for policy in INCIDENT_POLICIES}


@dataclass(frozen=True)
class RepoHelpers:
    runner: Any
    verify_durable_draco_artifacts: Any


@dataclass(frozen=True)
class WaveEvidence:
    results_path: Path
    trace_path: Path
    checkpoint_path: Path
    manifest_path: Path
    stamp: str
    status: str
    rows_written: int
    groups: tuple[str, ...]
    started_at: Any
    finished_at: Any
    results_sha256: str
    manifest_sha256: str


@dataclass(frozen=True)
class IncidentEvidence:
    policy_name: str
    failure_kind: str
    spec_path: Path
    spec_sha256: str
    incident_id: str
    group: str
    task_id: str
    primary_source_path: str
    primary_source_line: int
    primary_results_sha256: str
    primary_manifest_sha256: str
    primary_row_sha256: str
    primary_attempt_ids: tuple[str, ...]
    primary_physical_attempt_ids: tuple[str, ...]
    primary_actual_account: Mapping[str, Any]
    replacement_wave: WaveEvidence
    replacement_row_sha256: str
    replacement_attempt_ids: tuple[str, ...]
    replacement_physical_attempt_ids: tuple[str, ...]
    replacement_actual_account: Mapping[str, Any]
    replacement_groups: tuple[str, str]
    only_group_task_keys_sha256: str
    expected_compatibility_manifest_sha256: str
    resume_runner_sha256: str
    resume_selection: Mapping[str, Any]


@dataclass(frozen=True)
class NativeFailureEvidence:
    policy_name: str
    failure_kind: str
    group: str
    task_id: str
    source_path: str
    source_line: int
    row_sha256: str
    run_compatibility_fingerprint: str
    attempt_ids: tuple[str, ...]
    attempt_errors: tuple[str, ...]
    actual_llm_account: Mapping[str, Any]


def load_repo_helpers(repo_root: Path) -> RepoHelpers:
    repo_root = repo_root.resolve()
    src = repo_root / "src"
    scripts = repo_root / "scripts"
    runner_path = scripts / "run_draco_routing_experiment_resume.py"
    if not src.is_dir() or not runner_path.is_file():
        raise ReportError(f"not an OpenSquilla DRACO checkout: {repo_root}")
    for entry in (str(scripts), str(src), str(repo_root)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    try:
        runner = importlib.import_module("run_draco_routing_experiment_resume")
        artifact_io = importlib.import_module("opensquilla.eval.draco_artifact_io")
    except Exception as exc:
        raise ReportError(f"cannot import offline DRACO helpers: {exc}") from exc
    return RepoHelpers(
        runner=runner,
        verify_durable_draco_artifacts=artifact_io.verify_durable_draco_artifacts,
    )


def read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReportError(f"{label} is not a JSON object: {path}")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ReportError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def canonical_object_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_sha256(value: Any, *, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if SHA256_HEX.fullmatch(normalized) is None:
        raise ReportError(f"{label} must be a lowercase 64-hex SHA-256")
    return normalized


def incident_primary_field_names(policy: IncidentPolicy) -> set[str]:
    fields = {
        "results_sha256",
        "manifest_sha256",
        "row_canonical_sha256",
        "run_compatibility_fingerprint",
        "generation_attempt_budget_used",
        "generation_attempt_budget_limit",
        "actual_llm_request_count",
        "actual_llm_unknown_request_count",
        "recorded_actual_llm_cost_usd",
    }
    fields.add(
        "completed_proposer_counts"
        if policy.failure_kind == "strict_quorum"
        else "generation_attempt_errors"
    )
    return fields


def load_incident_spec(
    path: Path,
    *,
    policy: IncidentPolicy = B2_INCIDENT_POLICY,
) -> dict[str, Any]:
    spec = read_json_object(path, label="incident replacement receipt")
    if set(spec) != {
        "schema",
        "incident_id",
        "group",
        "task_id",
        "primary",
        "replacement",
        "analysis_policy",
    }:
        raise ReportError("incident receipt has an unexpected top-level field set")
    if spec.get("schema") != policy.schema:
        raise ReportError(f"incident receipt schema must be {policy.schema!r}")
    incident_id = str(spec.get("incident_id") or "").strip()
    if not incident_id or len(incident_id) > 160:
        raise ReportError("incident receipt incident_id is missing or too long")
    if (spec.get("group"), spec.get("task_id")) != policy.key:
        raise ReportError(f"receipt does not authorize incident {policy.group}/{policy.task_id}")
    primary = spec.get("primary")
    replacement = spec.get("replacement")
    analysis_policy = spec.get("analysis_policy")
    if not isinstance(primary, Mapping) or set(primary) != incident_primary_field_names(policy):
        raise ReportError("incident receipt primary binding has an unexpected field set")
    if not isinstance(replacement, Mapping) or set(replacement) != {
        "protocol",
        "results_sha256",
        "manifest_sha256",
        "row_canonical_sha256",
        "run_compatibility_fingerprint",
        "fresh_generation_attempt_budget_limit",
        "groups",
        "task_count",
        "rows_written",
        "only_group_task_keys_recorded_path",
        "only_group_task_keys_raw_sha256",
        "expected_compatibility_manifest_recorded_path",
        "expected_compatibility_manifest_raw_sha256",
        "resume_runner_raw_sha256",
        "resume_selection",
    }:
        raise ReportError("incident receipt replacement binding has an unexpected field set")
    if not isinstance(analysis_policy, Mapping) or dict(analysis_policy) != {
        "confirmatory": "exclude_incident_cell",
        "sensitivity": "include_replacement_post_hoc",
    }:
        raise ReportError("incident receipt analysis policy is not the fail-closed policy")
    for section_name, section in (("primary", primary), ("replacement", replacement)):
        for key in ("results_sha256", "manifest_sha256", "row_canonical_sha256"):
            require_sha256(section.get(key), label=f"incident {section_name}.{key}")
        fingerprint = str(section.get("run_compatibility_fingerprint") or "")
        if (
            not fingerprint.startswith("sha256:")
            or SHA256_HEX.fullmatch(fingerprint.removeprefix("sha256:")) is None
        ):
            raise ReportError(f"incident {section_name}.run_compatibility_fingerprint is invalid")
    if primary.get("generation_attempt_budget_used") != INCIDENT_PRIMARY_BUDGET:
        raise ReportError("incident primary budget-used binding must be 3")
    if primary.get("generation_attempt_budget_limit") != INCIDENT_PRIMARY_BUDGET:
        raise ReportError("incident primary budget-limit binding must be 3")
    if policy.failure_kind == "strict_quorum":
        if tuple(primary.get("completed_proposer_counts") or ()) != (INCIDENT_COMPLETED_PROPOSERS):
            raise ReportError("incident primary proposer counts must be [1, 2, 2]")
    elif tuple(primary.get("generation_attempt_errors") or ()) != policy.expected_attempt_errors:
        raise ReportError(
            f"{policy.group} incident attempt errors differ from the frozen failure contract"
        )
    if primary.get("actual_llm_request_count") != policy.expected_actual_llm_request_count:
        raise ReportError(
            f"incident primary actual LLM request binding must be "
            f"{policy.expected_actual_llm_request_count}"
        )
    if (
        primary.get("actual_llm_unknown_request_count")
        != policy.expected_actual_llm_unknown_request_count
    ):
        raise ReportError(
            f"incident primary unknown request binding must be "
            f"{policy.expected_actual_llm_unknown_request_count}"
        )
    try:
        parent_cost = Decimal(str(primary.get("recorded_actual_llm_cost_usd")))
    except Exception as exc:
        raise ReportError("incident primary recorded cost binding is invalid") from exc
    if not parent_cost.is_finite() or parent_cost < 0:
        raise ReportError("incident primary recorded cost binding is invalid")
    if (
        policy.expected_recorded_actual_llm_cost_usd is not None
        and parent_cost != policy.expected_recorded_actual_llm_cost_usd
    ):
        raise ReportError("incident primary recorded cost differs from frozen evidence")
    if replacement.get("protocol") != INCIDENT_REPLACEMENT_PROTOCOL:
        raise ReportError(f"incident replacement protocol must be {INCIDENT_REPLACEMENT_PROTOCOL}")
    if replacement.get("fresh_generation_attempt_budget_limit") != 3:
        raise ReportError("fresh replacement must retain the frozen three-attempt budget")
    if tuple(replacement.get("groups") or ()) != policy.replacement_groups:
        raise ReportError(
            f"incident replacement groups must be exactly {list(policy.replacement_groups)!r}"
        )
    if replacement.get("task_count") != EXPECTED_TASKS:
        raise ReportError("incident replacement manifest task_count binding must be 10")
    if replacement.get("rows_written") != 1:
        raise ReportError("incident replacement row-count binding must be 1")
    for key in (
        "only_group_task_keys_raw_sha256",
        "expected_compatibility_manifest_raw_sha256",
        "resume_runner_raw_sha256",
    ):
        require_sha256(replacement.get(key), label=f"incident replacement.{key}")
    for key in (
        "only_group_task_keys_recorded_path",
        "expected_compatibility_manifest_recorded_path",
    ):
        if not str(replacement.get(key) or "").strip():
            raise ReportError(f"incident replacement.{key} must be non-empty")
    if replacement.get("resume_selection") != expected_incident_resume_selection(policy):
        raise ReportError("incident replacement resume_selection binding is not exact")
    return spec


def expected_incident_resume_selection(
    policy: IncidentPolicy = B2_INCIDENT_POLICY,
) -> dict[str, Any]:
    return {
        "selected_pair_count": 1,
        "scheduled_pair_count": 1,
        "regenerate_pair_count": 1,
        "model_regenerate_pair_count": 1,
        "generation_budget_exhausted_pair_count": 0,
        "generation_auto_retry_blocked_pair_count": 0,
        "judge_only_pair_count": 0,
        "metadata_only_pair_count": 0,
        "audit_only_pair_count": 0,
        "policy_violation_pair_count": 0,
        "scheduled_pairs": [
            {"group": policy.group, "task_id": policy.task_id, "action": "regenerate"}
        ],
    }


def validate_only_group_task_keys(
    path: Path,
    *,
    policy: IncidentPolicy = B2_INCIDENT_POLICY,
) -> None:
    try:
        raw_lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise ReportError(f"cannot read incident only-keys file {path}: {exc}") from exc
    lines = [line for line in raw_lines if line.strip()]
    if len(lines) != 1:
        raise ReportError("incident only-keys file must contain exactly one nonblank JSONL row")
    try:
        value = json.loads(lines[0].decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportError("incident only-keys file is not strict UTF-8 JSONL") from exc
    if value != {"group": policy.group, "task_id": policy.task_id}:
        raise ReportError(
            f"incident only-keys file must select exactly {policy.group}/{policy.task_id}"
        )


def read_jsonl_objects(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        raw_lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise ReportError(f"cannot read {label} {path}: {exc}") from exc
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw_lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReportError(f"invalid {label} JSONL at line {line_number}") from exc
        if not isinstance(value, dict):
            raise ReportError(f"non-object {label} JSONL row at line {line_number}")
        values.append(value)
    return values


def exclusive_write(path: Path, text: str) -> None:
    """Create an authorization receipt once; never replace an existing path."""

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        raise


def canonical_receipt_text(spec: Mapping[str, Any]) -> str:
    return json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def derive_incident_receipt(
    *,
    primary_row: Mapping[str, Any],
    primary_wave: WaveEvidence,
    replacement_row: Mapping[str, Any],
    replacement_wave: WaveEvidence,
    expected_manifest_path: Path,
    only_keys_path: Path,
    resume_runner_path: Path,
    replacement_manifest: Mapping[str, Any],
    runner: Any,
    policy: IncidentPolicy = B2_INCIDENT_POLICY,
) -> dict[str, Any]:
    validate_only_group_task_keys(only_keys_path, policy=policy)
    if not resume_runner_path.is_file():
        raise ReportError(f"resume runner does not exist: {resume_runner_path}")
    manifest_args_value = replacement_manifest.get("args")
    if not isinstance(manifest_args_value, Mapping):
        raise ReportError("replacement manifest lacks args evidence")
    manifest_args = manifest_args_value
    resume_sources = manifest_args.get("resume_from_jsonl")
    if resume_sources not in ([], None):
        raise ReportError("incident replacement must have no --resume-from-jsonl history")
    recorded_only_keys = str(manifest_args.get("only_group_task_keys") or "")
    recorded_expected = str(manifest_args.get("expected_compatibility_manifest") or "")
    if not recorded_only_keys or not recorded_expected:
        raise ReportError("replacement manifest lacks targeted key / expected manifest args")
    if Path(recorded_only_keys).name != only_keys_path.name:
        raise ReportError("replacement manifest only-keys basename differs from supplied evidence")
    if Path(recorded_expected).name != expected_manifest_path.name:
        raise ReportError("replacement expected-manifest basename differs from supplied evidence")
    resume_selection = replacement_manifest.get("resume_selection")
    if resume_selection != expected_incident_resume_selection(policy):
        raise ReportError("replacement manifest resume_selection is not exact 1/1 regenerate")
    primary_account = runner.row_cost_accounting(dict(primary_row))["actual_llm_total"]
    if (
        int(primary_account.get("request_count") or 0) != policy.expected_actual_llm_request_count
        or int(primary_account.get("unknown_request_count") or 0)
        != policy.expected_actual_llm_unknown_request_count
    ):
        raise ReportError(
            f"known {policy.name} accounting differs from its frozen request/unknown counts"
        )
    primary_cost = Decimal(str(primary_account.get("recorded_cost_usd")))
    if (
        policy.expected_recorded_actual_llm_cost_usd is not None
        and primary_cost != policy.expected_recorded_actual_llm_cost_usd
    ):
        raise ReportError(f"known {policy.name} accounting differs from its frozen cost")
    failure_binding = (
        {"completed_proposer_counts": list(_strict_quorum_completed_counts(primary_row))}
        if policy.failure_kind == "strict_quorum"
        else {"generation_attempt_errors": list(_generation_attempt_errors(primary_row))}
    )
    return {
        "schema": policy.schema,
        "incident_id": policy.incident_id,
        "group": policy.group,
        "task_id": policy.task_id,
        "primary": {
            "results_sha256": primary_wave.results_sha256,
            "manifest_sha256": primary_wave.manifest_sha256,
            "row_canonical_sha256": canonical_object_sha256(primary_row),
            "run_compatibility_fingerprint": primary_row.get("run_compatibility_fingerprint"),
            "generation_attempt_budget_used": primary_row.get("generation_attempt_budget_used"),
            "generation_attempt_budget_limit": primary_row.get("generation_attempt_budget_limit"),
            **failure_binding,
            "actual_llm_request_count": int(primary_account.get("request_count") or 0),
            "actual_llm_unknown_request_count": int(
                primary_account.get("unknown_request_count") or 0
            ),
            "recorded_actual_llm_cost_usd": str(primary_cost),
        },
        "replacement": {
            "protocol": INCIDENT_REPLACEMENT_PROTOCOL,
            "results_sha256": replacement_wave.results_sha256,
            "manifest_sha256": replacement_wave.manifest_sha256,
            "row_canonical_sha256": canonical_object_sha256(replacement_row),
            "run_compatibility_fingerprint": replacement_row.get("run_compatibility_fingerprint"),
            "fresh_generation_attempt_budget_limit": replacement_row.get(
                "generation_attempt_budget_limit"
            ),
            "groups": list(replacement_wave.groups),
            "task_count": replacement_manifest.get("task_count"),
            "rows_written": replacement_wave.rows_written,
            "only_group_task_keys_recorded_path": recorded_only_keys,
            "only_group_task_keys_raw_sha256": file_sha256(only_keys_path),
            "expected_compatibility_manifest_recorded_path": recorded_expected,
            "expected_compatibility_manifest_raw_sha256": file_sha256(expected_manifest_path),
            "resume_runner_raw_sha256": file_sha256(resume_runner_path),
            "resume_selection": resume_selection,
        },
        "analysis_policy": {
            "confirmatory": "exclude_incident_cell",
            "sensitivity": "include_replacement_post_hoc",
        },
    }


def manifest_is_dry_run(manifest: Mapping[str, Any]) -> bool:
    args = manifest.get("args")
    if isinstance(args, Mapping) and args.get("dry_run") is True:
        return True
    compatibility = manifest.get("run_compatibility")
    contracts = compatibility.get("contracts") if isinstance(compatibility, Mapping) else None
    return bool(
        isinstance(contracts, Mapping)
        and any(
            isinstance(contract, Mapping) and contract.get("dry_run") is True
            for contract in contracts.values()
        )
    )


def expected_compatibility(
    manifest_path: Path,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Mapping[str, Any]]]:
    manifest = read_json_object(manifest_path, label="compatibility manifest")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ReportError(f"unexpected manifest schema in {manifest_path}")
    if manifest_is_dry_run(manifest):
        raise ReportError("the expected compatibility manifest is a dry run")
    groups = manifest.get("groups")
    if not isinstance(groups, list) or tuple(groups) != ARMS:
        raise ReportError(f"expected manifest groups must be exactly {ARMS!r}")
    compatibility = manifest.get("run_compatibility")
    fingerprints = compatibility.get("fingerprints") if isinstance(compatibility, Mapping) else None
    contracts = compatibility.get("contracts") if isinstance(compatibility, Mapping) else None
    if not isinstance(fingerprints, Mapping) or set(fingerprints) != ARM_SET:
        raise ReportError("expected manifest lacks exact six-arm compatibility fingerprints")
    if not isinstance(contracts, Mapping) or set(contracts) != ARM_SET:
        raise ReportError("expected manifest lacks exact six-arm compatibility contracts")
    normalized_fingerprints: dict[str, str] = {}
    normalized_contracts: dict[str, Mapping[str, Any]] = {}
    for group in ARMS:
        fingerprint = fingerprints.get(group)
        contract = contracts.get(group)
        if not isinstance(fingerprint, str) or not fingerprint.startswith("sha256:"):
            raise ReportError(f"invalid compatibility fingerprint for {group}")
        if not isinstance(contract, Mapping) or contract.get("group") != group:
            raise ReportError(f"invalid compatibility contract for {group}")
        if contract.get("dry_run") is True:
            raise ReportError(f"compatibility contract is dry-run for {group}")
        normalized_fingerprints[group] = fingerprint
        normalized_contracts[group] = contract
    return manifest, normalized_fingerprints, normalized_contracts


def compatibility_experiment_config_projection(
    effective_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Reproduce the runner's exact experiment-config fingerprint projection."""

    projected = deepcopy(dict(effective_config))
    if not projected.get("router_dynamic_ranking_override"):
        projected.pop("router_dynamic_ranking_override", None)
    ensemble = projected.get("ensemble")
    if isinstance(ensemble, dict):
        ensemble.pop("proposer_backup_count", None)
    runner_config = projected.get("runner")
    if isinstance(runner_config, dict):
        runner_config.pop("concurrency", None)
    judge_config = projected.get("judge")
    if isinstance(judge_config, dict):
        judge_config.pop("concurrency", None)
    return projected


def load_b2_validator_experiment_config(
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    contracts: Mapping[str, Mapping[str, Any]],
    runner: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Authenticate the effective config before hydrating B2 validation in memory."""

    b2_contract = contracts.get("B2")
    compact_config = (
        b2_contract.get("experiment_config") if isinstance(b2_contract, Mapping) else None
    )
    if (
        not isinstance(compact_config, Mapping)
        or set(compact_config) != {"sha256"}
        or not isinstance(compact_config.get("sha256"), str)
        or not str(compact_config["sha256"]).startswith("sha256:")
        or SHA256_HEX.fullmatch(str(compact_config["sha256"])[7:]) is None
    ):
        raise ReportError("B2 compatibility contract lacks the exact compact experiment-config pin")
    contract_pin = str(compact_config["sha256"])

    stamp = str(manifest.get("stamp") or "")
    expected_effective_name = f"draco_run_{stamp}.experiment-config.effective.json"
    expected_resolution_name = f"draco_run_{stamp}.experiment-config.resolution.json"
    artifacts = manifest.get("artifacts")
    args = manifest.get("args")
    if not isinstance(artifacts, Mapping) or not isinstance(args, Mapping):
        raise ReportError("expected manifest lacks experiment-config artifact bindings")
    declared_effective = artifacts.get("experiment_config_effective_json")
    declared_resolution = artifacts.get("experiment_config_resolution_json")
    recorded_arg = args.get("experiment_config")
    if (
        not isinstance(declared_effective, str)
        or Path(declared_effective).name != expected_effective_name
        or not isinstance(declared_resolution, str)
        or Path(declared_resolution).name != expected_resolution_name
        or not isinstance(recorded_arg, str)
        or Path(recorded_arg).name != expected_effective_name
    ):
        raise ReportError(
            "expected manifest experiment-config paths do not bind the stamped artifacts"
        )

    parent = manifest_path.resolve().parent
    effective_path = parent / expected_effective_name
    resolution_path = parent / expected_resolution_name
    for label, path in (
        ("effective experiment config", effective_path),
        ("experiment config resolution", resolution_path),
    ):
        if path.is_symlink() or not path.is_file():
            raise ReportError(f"missing or symlinked {label} artifact: {path}")
    effective = read_json_object(
        effective_path,
        label="effective experiment config",
    )
    resolution = read_json_object(
        resolution_path,
        label="experiment config resolution",
    )
    raw_canonical_sha256 = runner.canonical_json_sha256(effective)
    resolution_effective = resolution.get("effective_config")
    if (
        not isinstance(resolution_effective, Mapping)
        or resolution_effective.get("sha256") != raw_canonical_sha256
        or not isinstance(resolution_effective.get("path"), str)
        or Path(str(resolution_effective["path"])).name != expected_effective_name
        or resolution.get("profile_id") != effective.get("profile_id")
    ):
        raise ReportError(
            "experiment-config resolution does not authenticate the effective artifact"
        )
    alignments = manifest.get("benchmark_alignments")
    global_alignment = (
        alignments.get("global_experiment_profile") if isinstance(alignments, Mapping) else None
    )
    b2_alignment = alignments.get("B2") if isinstance(alignments, Mapping) else None
    if (
        not isinstance(global_alignment, Mapping)
        or not isinstance(b2_alignment, Mapping)
        or global_alignment.get("effective_config_sha256") != raw_canonical_sha256
        or b2_alignment.get("effective_config_sha256") != raw_canonical_sha256
        or dict(global_alignment) != dict(b2_alignment)
    ):
        raise ReportError("manifest benchmark alignment does not authenticate the effective config")
    projected = compatibility_experiment_config_projection(effective)
    if not isinstance(projected.get("ensemble"), Mapping) or not isinstance(
        projected.get("routing"), Mapping
    ):
        raise ReportError("effective config lacks the B2 ensemble/routing domains")
    projected_sha256 = runner.canonical_json_sha256(projected)
    if projected_sha256 != contract_pin:
        raise ReportError(
            "effective experiment-config projection differs from the B2 contract pin: "
            f"observed={projected_sha256}, expected={contract_pin}"
        )
    return projected, {
        "schema": "opensquilla.draco.report-b2-config-hydration-proof/v1",
        "effective_artifact_path": str(effective_path),
        "effective_artifact_raw_sha256": file_sha256(effective_path),
        "effective_config_canonical_sha256": raw_canonical_sha256,
        "resolution_artifact_path": str(resolution_path),
        "resolution_artifact_raw_sha256": file_sha256(resolution_path),
        "compatibility_projection_sha256": projected_sha256,
        "contract_pin": contract_pin,
    }


def sibling_artifact_paths(results_path: Path) -> tuple[str, Path, Path, Path]:
    match = RESULT_NAME.fullmatch(results_path.name)
    if match is None:
        raise ReportError(f"non-standard result filename: {results_path}")
    stamp = match.group("stamp")
    return (
        stamp,
        results_path.parent / f"draco_run_{stamp}.trace.jsonl",
        results_path.parent / f"draco_run_{stamp}.checkpoint.json",
        results_path.parent / f"draco_run_{stamp}.manifest.json",
    )


def _artifact_basename_matches(
    manifest: Mapping[str, Any],
    key: str,
    actual: Path,
) -> bool:
    artifacts = manifest.get("artifacts")
    declared = artifacts.get(key) if isinstance(artifacts, Mapping) else None
    return isinstance(declared, str) and Path(declared).name == actual.name


def validate_wave(
    results_path: Path,
    *,
    helpers: RepoHelpers,
    expected_fingerprints: Mapping[str, str],
) -> WaveEvidence:
    results_path = results_path.resolve()
    if not results_path.is_file():
        raise ReportError(f"result JSONL does not exist: {results_path}")
    stamp, trace_path, checkpoint_path, manifest_path = sibling_artifact_paths(results_path)
    for label, path in (
        ("trace", trace_path),
        ("checkpoint", checkpoint_path),
        ("manifest", manifest_path),
    ):
        if not path.is_file():
            raise ReportError(f"missing {label} sibling for {results_path}: {path}")
    manifest = read_json_object(manifest_path, label="wave manifest")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ReportError(f"unexpected wave manifest schema: {manifest_path}")
    status = str(manifest.get("status") or "")
    if status not in TERMINAL_STATUSES:
        raise ReportError(f"wave manifest is not terminal ({status!r}): {manifest_path}")
    if manifest_is_dry_run(manifest):
        raise ReportError(f"dry-run wave cannot enter a final report: {results_path}")
    groups_value = manifest.get("groups")
    if (
        not isinstance(groups_value, list)
        or not groups_value
        or not set(groups_value) <= ARM_SET
        or len(groups_value) != len(set(groups_value))
    ):
        raise ReportError(f"invalid wave group set: {manifest_path}")
    for key, actual in (
        ("results_jsonl", results_path),
        ("trace_jsonl", trace_path),
        ("checkpoint_json", checkpoint_path),
        ("manifest_json", manifest_path),
    ):
        if not _artifact_basename_matches(manifest, key, actual):
            raise ReportError(f"wave manifest artifact binding mismatch for {key}: {manifest_path}")
    compatibility = manifest.get("run_compatibility")
    wave_fingerprints = (
        compatibility.get("fingerprints") if isinstance(compatibility, Mapping) else None
    )
    if not isinstance(wave_fingerprints, Mapping):
        raise ReportError(f"wave manifest lacks compatibility fingerprints: {manifest_path}")
    for group in groups_value:
        if wave_fingerprints.get(group) != expected_fingerprints[group]:
            raise ReportError(f"wave compatibility differs for {group}: {manifest_path}")
    try:
        verification = helpers.verify_durable_draco_artifacts(
            results_path=results_path,
            trace_path=trace_path,
            checkpoint_path=checkpoint_path,
        )
    except Exception as exc:
        raise ReportError(
            f"durable artifact verification failed for {results_path}: {exc}"
        ) from exc
    rows_written = int(verification.get("rows_written") or 0)
    if manifest.get("rows_written") != rows_written:
        raise ReportError(f"manifest/checkpoint row count mismatch: {manifest_path}")
    return WaveEvidence(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
        manifest_path=manifest_path,
        stamp=stamp,
        status=status,
        rows_written=rows_written,
        groups=tuple(str(group) for group in groups_value),
        started_at=manifest.get("started_at"),
        finished_at=manifest.get("finished_at"),
        results_sha256=str(verification.get("results_sha256") or file_sha256(results_path)),
        manifest_sha256=file_sha256(manifest_path),
    )


def load_tasks_and_hashes(
    input_path: Path,
    *,
    runner: Any,
    expected_manifest: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str], dict[str, str], dict[str, str]]:
    tasks = runner.load_tasks(input_path, max_tasks=0)
    task_ids = [str(task.get("id") or "") for task in tasks]
    if len(tasks) != EXPECTED_TASKS or len(set(task_ids)) != EXPECTED_TASKS or not all(task_ids):
        raise ReportError("DRACO input must contain exactly 10 uniquely identified tasks")
    manifest_ids = expected_manifest.get("task_ids")
    if not isinstance(manifest_ids, list) or [str(value) for value in manifest_ids] != task_ids:
        raise ReportError("expected manifest task IDs/order differ from the DRACO input")
    if expected_manifest.get("task_count") != EXPECTED_TASKS:
        raise ReportError("expected manifest task_count is not 10")
    prompt_hashes = {
        str(task["id"]): runner.text_sha256(str(task.get("prompt") or "")) for task in tasks
    }
    task_input_hashes = {str(task["id"]): runner.canonical_json_sha256(task) for task in tasks}
    return tasks, task_ids, prompt_hashes, task_input_hashes


def _selection_plan_analyzer_contracts(value: Any) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        selection_plan = value.get("selection_plan")
        if isinstance(selection_plan, Mapping):
            declared = selection_plan.get("task_analyzer_execution_contract")
            if isinstance(declared, Mapping):
                found.append(declared)
        for item in value.values():
            found.extend(_selection_plan_analyzer_contracts(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_selection_plan_analyzer_contracts(item))
    return found


def _load_primary_snapshot(
    *,
    result_paths: list[Path],
    expected_keys: set[tuple[str, str]],
    task_ids: Sequence[str],
    prompt_hashes: Mapping[str, str],
    task_input_hashes: Mapping[str, str],
    fingerprints: Mapping[str, str],
    contracts: Mapping[str, Mapping[str, Any]],
    require_non_byok: bool,
    runner: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any], Counter[str], list[dict[str, Any]]]:
    try:
        states, audit = runner.load_resume_group_task_states(
            resume_paths=result_paths,
            selected_keys=expected_keys,
            prompt_hashes=dict(prompt_hashes),
            task_input_hashes=dict(task_input_hashes),
            run_compatibility_fingerprints=dict(fingerprints),
            run_compatibility_contracts=contracts,
            require_openrouter_non_byok=require_non_byok,
            judge_required=True,
        )
    except Exception as exc:
        raise ReportError(f"cross-wave selection failed: {exc}") from exc
    rows: list[dict[str, Any]] = []
    actions: Counter[str] = Counter()
    snapshots: list[dict[str, Any]] = []
    missing: list[tuple[str, str]] = []
    try:
        for task_id in task_ids:
            for group in ARMS:
                key = (group, task_id)
                state = states.get(key)
                if not isinstance(state, Mapping):
                    missing.append(key)
                    continue
                action = str(state.get("action") or "missing")
                actions[action] += 1
                row = states.consume_row(key)
                rows.append(row)
                snapshots.append(
                    {
                        "group": group,
                        "task_id": task_id,
                        "action": action,
                        "generation_valid": state.get("generation_valid"),
                        "judge_complete": state.get("judge_complete"),
                        "cost_metadata_complete": state.get("cost_metadata_complete"),
                        "generation_reasons": list(state.get("generation_reasons") or []),
                        "judge_reasons": list(state.get("judge_reasons") or []),
                        "cost_metadata_reasons": list(state.get("cost_metadata_reasons") or []),
                        "audit_reasons": list(state.get("audit_reasons") or []),
                        "fatal_policy_reasons": list(state.get("fatal_policy_reasons") or []),
                        "source_path": state.get("source_path"),
                        "source_line": state.get("source_line"),
                        "row_sha256": canonical_object_sha256(row),
                    }
                )
    finally:
        states.close(verify=True)
    if missing:
        preview = ", ".join(f"{group}/{task}" for group, task in missing[:8])
        raise ReportError(f"missing {len(missing)} expected group/task pairs: {preview}")
    return rows, audit, actions, snapshots


def validate_reportable_resume_state(
    *,
    state: Mapping[str, Any],
    row: Mapping[str, Any],
    key: tuple[str, str],
    expected_fingerprint: str,
    runner: Any,
) -> dict[str, Any] | None:
    """Accept complete rows or the one closed cost-only metadata exception."""

    group, task_id = key
    label = f"{group}/{task_id}"
    action = str(state.get("action") or "missing")
    if action not in {"complete", "metadata_only"}:
        raise ReportError(f"non-reportable resume action at {label}: {action!r}")
    common_failures: list[str] = []
    if state.get("generation_valid") is not True:
        common_failures.append("generation_valid_not_true")
    if state.get("judge_complete") is not True:
        common_failures.append("judge_complete_not_true")
    for field in (
        "generation_reasons",
        "judge_reasons",
        "audit_reasons",
        "fatal_policy_reasons",
    ):
        reasons = list(state.get(field) or [])
        if reasons:
            common_failures.append(f"{field}={reasons!r}")
    judge = row.get("judge")
    if (str(row.get("group") or ""), str(row.get("task_id") or "")) != key:
        common_failures.append("row_key_mismatch")
    if row.get("error"):
        common_failures.append("row_error_present")
    if row.get("selected_generation_succeeded") is not True:
        common_failures.append("selected_generation_not_succeeded")
    if not str(row.get("final_text") or "").strip():
        common_failures.append("final_text_missing")
    execution = row.get("execution")
    attempts = execution.get("generation_attempts") if isinstance(execution, Mapping) else None
    if (
        not isinstance(attempts, list)
        or not attempts
        or row.get("generation_attempt_count") != len(attempts)
    ):
        common_failures.append("generation_attempt_evidence_missing")
    if not isinstance(judge, Mapping) or judge.get("score_status") != "complete":
        common_failures.append("judge_surface_incomplete")
    elif int(judge.get("judge_error_count") or 0) != 0:
        common_failures.append("judge_error_present")
    if not finite_number(row.get("quality_total")):
        common_failures.append("quality_missing")
    if row.get("run_compatibility_fingerprint") != expected_fingerprint:
        common_failures.append("fingerprint_mismatch")
    if common_failures:
        raise ReportError(
            f"reportable-row completion gate failed at {label}: " + ", ".join(common_failures)
        )

    cost_reasons = tuple(str(value) for value in (state.get("cost_metadata_reasons") or []))
    if action == "complete":
        if state.get("cost_metadata_complete") is not True or cost_reasons:
            raise ReportError(
                f"complete action has incomplete cost metadata at {label}: {cost_reasons!r}"
            )
        return None

    if state.get("cost_metadata_complete") is not False:
        raise ReportError(f"metadata_only cost-complete flag is not false at {label}")
    if cost_reasons != REPORTABLE_METADATA_ONLY_COST_REASONS:
        raise ReportError(
            f"metadata_only has non-whitelisted cost reasons at {label}: {cost_reasons!r}"
        )
    account = runner.row_cost_accounting(dict(row))
    actual = account.get("actual_llm_total")
    if (
        account.get("actual_llm_cost_complete") is not False
        or not isinstance(actual, Mapping)
        or actual.get("cost_complete") is not False
        or int(actual.get("unknown_request_count") or 0) <= 0
    ):
        raise ReportError(f"metadata_only accounting is not an explicit lower bound at {label}")
    stored_account = row.get("cost_accounting")
    if (
        isinstance(stored_account, Mapping)
        and stored_account.get("actual_llm_cost_complete") is True
    ):
        raise ReportError(f"metadata_only row falsely declares complete actual LLM cost at {label}")
    return {
        "group": group,
        "task_id": task_id,
        "action": action,
        "cost_metadata_reasons": list(cost_reasons),
        "source_path": state.get("source_path"),
        "source_line": state.get("source_line"),
        "row_sha256": canonical_object_sha256(row),
        "actual_llm_request_count": int(actual.get("request_count") or 0),
        "actual_llm_unknown_request_count": int(actual.get("unknown_request_count") or 0),
        "actual_llm_known_request_coverage_pct": actual.get("known_request_coverage_pct"),
        "actual_llm_recorded_cost_usd": actual.get("recorded_cost_usd"),
        "actual_llm_cost_complete": False,
        "actual_llm_recorded_cost_is_lower_bound": True,
    }


def validate_native_failure_resume_state(
    state: Mapping[str, Any],
    *,
    key: tuple[str, str],
    policy: IncidentPolicy,
) -> None:
    if state.get("action") != "regenerate":
        raise ReportError(f"authorized native failure is not regenerate: {key}")
    if state.get("generation_valid") is not False:
        raise ReportError(f"authorized native failure is not generation-invalid: {key}")
    if state.get("judge_complete") is not False:
        raise ReportError(f"authorized native failure unexpectedly has a complete Judge: {key}")
    expected_state_reasons = {
        "generation_reasons": policy.expected_generation_reasons,
        "judge_reasons": policy.expected_judge_reasons,
        "cost_metadata_reasons": policy.expected_cost_metadata_reasons,
        "audit_reasons": policy.expected_audit_reasons,
        "fatal_policy_reasons": (),
    }
    for field, expected_reasons in expected_state_reasons.items():
        observed_reasons = tuple(str(value) for value in (state.get(field) or []))
        if observed_reasons != expected_reasons:
            raise ReportError(
                f"authorized native failure {field} differ from frozen tuple: "
                f"{key}; observed={observed_reasons!r}; expected={expected_reasons!r}"
            )


def _snapshot_index(
    states: Sequence[Mapping[str, Any]],
    *,
    label: str,
) -> dict[tuple[str, str], Mapping[str, Any]]:
    indexed: dict[tuple[str, str], Mapping[str, Any]] = {}
    for state in states:
        key = (str(state.get("group") or ""), str(state.get("task_id") or ""))
        if not all(key) or key in indexed:
            raise ReportError(f"{label} contains a missing or duplicate state key: {key}")
        indexed[key] = state
    return indexed


def _validate_b2_surface_complete_correction_candidate(
    row: Mapping[str, Any],
    *,
    key: tuple[str, str],
    expected_fingerprint: str,
    expected_prompt_sha256: str,
    expected_task_input_sha256: str,
) -> None:
    failures: list[str] = []
    if (str(row.get("group") or ""), str(row.get("task_id") or "")) != key:
        failures.append("row_key_mismatch")
    if row.get("error"):
        failures.append("row_error_present")
    if row.get("selected_generation_succeeded") is not True:
        failures.append("selected_generation_not_succeeded")
    if not str(row.get("final_text") or "").strip():
        failures.append("final_text_missing")
    execution = row.get("execution")
    attempts = execution.get("generation_attempts") if isinstance(execution, Mapping) else None
    if (
        not isinstance(attempts, list)
        or not attempts
        or row.get("generation_attempt_count") != len(attempts)
    ):
        failures.append("generation_attempt_evidence_missing")
    execution_status = row.get("execution_status")
    if (
        not isinstance(execution_status, Mapping)
        or execution_status.get("success") is not True
        or execution_status.get("status") not in {"success", "degraded_success"}
    ):
        failures.append("execution_status_not_success")
    completion = row.get("completion_status")
    if (
        not isinstance(completion, Mapping)
        or completion.get("status") != "complete"
        or completion.get("generation_accepted") is not True
        or completion.get("judge_complete") is not True
    ):
        failures.append("completion_status_not_complete")
    judge = row.get("judge")
    if not isinstance(judge, Mapping) or judge.get("score_status") != "complete":
        failures.append("judge_surface_incomplete")
    elif int(judge.get("judge_error_count") or 0) != 0:
        failures.append("judge_error_present")
    if not finite_number(row.get("quality_total")):
        failures.append("quality_missing")
    if row.get("run_compatibility_fingerprint") != expected_fingerprint:
        failures.append("fingerprint_mismatch")
    if row.get("prompt_sha256") != expected_prompt_sha256:
        failures.append("prompt_hash_mismatch")
    if row.get("task_input_sha256") != expected_task_input_sha256:
        failures.append("task_input_hash_mismatch")
    if failures:
        raise ReportError(
            f"B2 validator correction candidate is not independently complete at {key}: "
            + ", ".join(failures)
        )


def validate_b2_validator_domain_delta(
    *,
    original_rows: Sequence[Mapping[str, Any]],
    original_states: Sequence[Mapping[str, Any]],
    hydrated_rows: Sequence[Mapping[str, Any]],
    hydrated_states: Sequence[Mapping[str, Any]],
    task_ids: Sequence[str],
    fingerprints: Mapping[str, str],
    prompt_hashes: Mapping[str, str],
    task_input_hashes: Mapping[str, str],
) -> list[tuple[str, str]]:
    """Allow only the proven B2 compact-contract classifier-domain delta."""

    original_index = _snapshot_index(original_states, label="unhydrated snapshot")
    hydrated_index = _snapshot_index(hydrated_states, label="hydrated snapshot")
    if set(original_index) != set(hydrated_index):
        raise ReportError("B2 validator hydration changed the selected key set")
    original_rows_by_key = {
        (str(row.get("group") or ""), str(row.get("task_id") or "")): row for row in original_rows
    }
    hydrated_rows_by_key = {
        (str(row.get("group") or ""), str(row.get("task_id") or "")): row for row in hydrated_rows
    }
    if set(original_rows_by_key) != set(hydrated_rows_by_key):
        raise ReportError("B2 validator hydration changed the selected row key set")

    b2_keys = {("B2", task_id) for task_id in task_ids}
    if {key for key in original_index if key[0] == "B2"} != b2_keys:
        raise ReportError("B2 validator hydration did not observe the exact 10-task arm")
    corrected: list[tuple[str, str]] = []
    source_fields = ("source_path", "source_line", "row_sha256")
    reason_fields = (
        "judge_reasons",
        "cost_metadata_reasons",
        "audit_reasons",
        "fatal_policy_reasons",
    )
    for key, original in original_index.items():
        hydrated = hydrated_index[key]
        if any(original.get(field) != hydrated.get(field) for field in source_fields):
            raise ReportError(
                f"B2 validator hydration changed selected source/line/row SHA at {key}"
            )
        if canonical_object_sha256(original_rows_by_key[key]) != canonical_object_sha256(
            hydrated_rows_by_key[key]
        ):
            raise ReportError(f"B2 validator hydration changed selected row bytes at {key}")
        if key[0] != "B2":
            if dict(original) != dict(hydrated):
                raise ReportError(f"B2 validator hydration changed non-B2 state at {key}")
            continue
        if key == INCIDENT_KEY:
            expected_original_generation = tuple(
                B2_VALIDATOR_FALSE_REASON if reason == B2_INCIDENT_CONTRACT_REASON else reason
                for reason in B2_INCIDENT_POLICY.expected_generation_reasons
            )
            if (
                original.get("action") != "regenerate"
                or original.get("generation_valid") is not False
                or original.get("judge_complete") is not False
                or original.get("cost_metadata_complete") is not False
                or tuple(original.get("generation_reasons") or []) != expected_original_generation
                or any(
                    tuple(original.get(field) or [])
                    != tuple(getattr(B2_INCIDENT_POLICY, f"expected_{field}"))
                    for field in reason_fields[:-1]
                )
                or tuple(original.get("fatal_policy_reasons") or [])
            ):
                raise ReportError("B2 native failure does not match the exact pre-hydration state")
            validate_native_failure_resume_state(
                hydrated,
                key=key,
                policy=B2_INCIDENT_POLICY,
            )
            if hydrated.get("cost_metadata_complete") is not False:
                raise ReportError("B2 native failure became cost-metadata complete after hydration")
        else:
            if (
                original.get("action") != "regenerate"
                or original.get("generation_valid") is not False
                or original.get("judge_complete") is not False
                or original.get("cost_metadata_complete") is not False
                or tuple(original.get("generation_reasons") or []) != (B2_VALIDATOR_FALSE_REASON,)
                or any(tuple(original.get(field) or []) for field in reason_fields)
            ):
                raise ReportError(
                    f"B2 unhydrated classifier has non-whitelisted state at {key}: "
                    f"{dict(original)!r}"
                )
            _validate_b2_surface_complete_correction_candidate(
                original_rows_by_key[key],
                key=key,
                expected_fingerprint=fingerprints["B2"],
                expected_prompt_sha256=prompt_hashes[key[1]],
                expected_task_input_sha256=task_input_hashes[key[1]],
            )
            if (
                hydrated.get("action") != "complete"
                or hydrated.get("generation_valid") is not True
                or hydrated.get("judge_complete") is not True
                or hydrated.get("cost_metadata_complete") is not True
                or tuple(hydrated.get("generation_reasons") or [])
                or any(tuple(hydrated.get(field) or []) for field in reason_fields)
            ):
                raise ReportError(
                    f"B2 hydrated classifier did not become exactly complete at {key}: "
                    f"{dict(hydrated)!r}"
                )
        original_generation = tuple(original.get("generation_reasons") or [])
        hydrated_generation = tuple(hydrated.get("generation_reasons") or [])
        if B2_VALIDATOR_FALSE_REASON not in original_generation:
            raise ReportError(f"B2 hydration candidate lacks the false reason at {key}")
        if B2_VALIDATOR_FALSE_REASON in hydrated_generation:
            raise ReportError(f"B2 hydration failed to remove the false reason at {key}")
        corrected.append(key)
    return sorted(corrected)


def select_primary_rows(
    *,
    result_paths: list[Path],
    task_ids: list[str],
    prompt_hashes: Mapping[str, str],
    task_input_hashes: Mapping[str, str],
    fingerprints: Mapping[str, str],
    contracts: Mapping[str, Mapping[str, Any]],
    b2_experiment_config: Mapping[str, Any],
    b2_config_audit: Mapping[str, Any],
    runner: Any,
    incident_keys: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected_keys = {(group, task_id) for task_id in task_ids for group in ARMS}
    require_non_byok_values = {
        bool((contracts[group].get("cost_policy") or {}).get("require_openrouter_non_byok"))
        for group in ARMS
    }
    if len(require_non_byok_values) != 1:
        raise ReportError("six-arm non-BYOK policy is inconsistent")
    require_non_byok = require_non_byok_values.pop()
    unpatched_rows, _unpatched_audit, unpatched_actions, unpatched_states = _load_primary_snapshot(
        result_paths=result_paths,
        expected_keys=expected_keys,
        task_ids=task_ids,
        prompt_hashes=prompt_hashes,
        task_input_hashes=task_input_hashes,
        fingerprints=fingerprints,
        contracts=contracts,
        require_non_byok=require_non_byok,
        runner=runner,
    )

    validator_contracts = {group: deepcopy(dict(contracts[group])) for group in ARMS}
    compact_b2_config = validator_contracts["B2"].get("experiment_config")
    if (
        not isinstance(compact_b2_config, Mapping)
        or set(compact_b2_config) != {"sha256"}
        or compact_b2_config.get("sha256") != b2_config_audit.get("contract_pin")
        or b2_config_audit.get("compatibility_projection_sha256") != compact_b2_config.get("sha256")
    ):
        raise ReportError("B2 validator hydration proof/contract binding changed")
    validator_contracts["B2"]["experiment_config"] = deepcopy(dict(b2_experiment_config))
    b2_rows, b2_resume_audit, b2_actions, b2_states = _load_primary_snapshot(
        result_paths=result_paths,
        expected_keys=expected_keys,
        task_ids=task_ids,
        prompt_hashes=prompt_hashes,
        task_input_hashes=task_input_hashes,
        fingerprints=fingerprints,
        contracts=validator_contracts,
        require_non_byok=require_non_byok,
        runner=runner,
    )
    b2_correction_keys = validate_b2_validator_domain_delta(
        original_rows=unpatched_rows,
        original_states=unpatched_states,
        hydrated_rows=b2_rows,
        hydrated_states=b2_states,
        task_ids=task_ids,
        fingerprints=fingerprints,
        prompt_hashes=prompt_hashes,
        task_input_hashes=task_input_hashes,
    )

    expected_analyzer_contract = contracts["G1"].get("task_analyzer_execution_contract")
    expected_registry_contract = contracts["G1"].get("g1_registry_contract")
    if not isinstance(expected_analyzer_contract, Mapping) or not isinstance(
        expected_registry_contract, Mapping
    ):
        raise ReportError("frozen G1 contract lacks registry/analyzer execution domains")
    g1_unpatched_by_key = {(str(row["group"]), str(row["task_id"])): row for row in b2_rows}
    correction_keys: list[tuple[str, str]] = []
    for state in b2_states:
        key = (str(state["group"]), str(state["task_id"]))
        if key[0] != "G1":
            continue
        row = g1_unpatched_by_key[key]
        declared_contracts = _selection_plan_analyzer_contracts(row)
        if not declared_contracts or any(
            dict(contract) != dict(expected_analyzer_contract) for contract in declared_contracts
        ):
            raise ReportError(
                f"G1 validator-domain correction proof failed: declared contract drift at {key}"
            )
        reasons = state["generation_reasons"]
        if state["action"] == "regenerate":
            if reasons != ["invalid_g1_task_analyzer_execution_contract"]:
                raise ReportError(
                    f"G1 unpatched classifier has non-whitelisted reasons at {key}: {reasons}"
                )
            judge = row.get("judge")
            if (
                row.get("error")
                or row.get("selected_generation_succeeded") is not True
                or not str(row.get("final_text") or "").strip()
                or not isinstance(judge, Mapping)
                or judge.get("score_status") != "complete"
                or int(judge.get("judge_error_count") or 0) != 0
                or not finite_number(row.get("quality_total"))
            ):
                raise ReportError(f"G1 correction candidate is not surface-complete at {key}")
            correction_keys.append(key)
        elif reasons:
            raise ReportError(f"G1 unpatched classifier has unexpected reasons at {key}: {reasons}")

    correction_audit: dict[str, Any] = {
        "schema": "opensquilla.draco.report-validator-domain-correction/v2",
        "reason": (
            "resume_b2_classifier_expected_inline_config_but_manifest_contract_stored_pin;"
            "resume_g1_calls_omitted_task_analyzer_execution_contract"
        ),
        "b2_applied": True,
        "b2_config_hydration_proof": dict(b2_config_audit),
        "b2_unpatched_action_counts": dict(sorted(unpatched_actions.items())),
        "b2_hydrated_action_counts": dict(sorted(b2_actions.items())),
        "b2_corrected_keys": [f"{group}/{task_id}" for group, task_id in b2_correction_keys],
        "expected_g1_registry_contract_sha256": canonical_object_sha256(expected_registry_contract),
        "expected_task_analyzer_execution_contract_sha256": canonical_object_sha256(
            expected_analyzer_contract
        ),
        "unpatched_action_counts": dict(sorted(unpatched_actions.items())),
        "unpatched_correctable_keys": [f"{group}/{task_id}" for group, task_id in correction_keys],
        "g1_applied": bool(correction_keys),
        "applied": True,
    }
    if correction_keys:
        original_core = runner.ensemble_call_core_reasons
        original_registry_reasons = runner.g1_registry_contract_reasons
        core_injections = 0
        registry_injections = 0

        def corrected_core(*args: Any, **kwargs: Any):
            nonlocal core_injections
            observed_registry = kwargs.get("expected_g1_registry_contract")
            if (
                isinstance(observed_registry, Mapping)
                and dict(observed_registry) == dict(expected_registry_contract)
                and canonical_object_sha256(observed_registry)
                == canonical_object_sha256(expected_registry_contract)
            ):
                existing = kwargs.get("expected_task_analyzer_execution_contract")
                if existing is not None and (
                    not isinstance(existing, Mapping)
                    or dict(existing) != dict(expected_analyzer_contract)
                ):
                    raise ReportError(
                        "G1 validator wrapper observed a conflicting analyzer contract"
                    )
                if existing is None:
                    kwargs["expected_task_analyzer_execution_contract"] = expected_analyzer_contract
                    core_injections += 1
            return original_core(*args, **kwargs)

        def corrected_registry_reasons(
            trace: Mapping[str, Any],
            contract: Mapping[str, Any] | None,
            task_analyzer_execution_contract: Mapping[str, Any] | None = None,
        ) -> list[str]:
            nonlocal registry_injections
            if (
                isinstance(contract, Mapping)
                and dict(contract) == dict(expected_registry_contract)
                and canonical_object_sha256(contract)
                == canonical_object_sha256(expected_registry_contract)
            ):
                if task_analyzer_execution_contract is not None and (
                    not isinstance(task_analyzer_execution_contract, Mapping)
                    or dict(task_analyzer_execution_contract) != dict(expected_analyzer_contract)
                ):
                    raise ReportError(
                        "G1 registry validator wrapper observed a conflicting analyzer contract"
                    )
                if task_analyzer_execution_contract is None:
                    task_analyzer_execution_contract = expected_analyzer_contract
                    registry_injections += 1
            return original_registry_reasons(
                trace,
                contract,
                task_analyzer_execution_contract,
            )

        runner.ensemble_call_core_reasons = corrected_core
        runner.g1_registry_contract_reasons = corrected_registry_reasons
        try:
            rows, audit, actions, selected_sources = _load_primary_snapshot(
                result_paths=result_paths,
                expected_keys=expected_keys,
                task_ids=task_ids,
                prompt_hashes=prompt_hashes,
                task_input_hashes=task_input_hashes,
                fingerprints=fingerprints,
                contracts=validator_contracts,
                require_non_byok=require_non_byok,
                runner=runner,
            )
        finally:
            runner.ensemble_call_core_reasons = original_core
            runner.g1_registry_contract_reasons = original_registry_reasons
        if core_injections <= 0 or registry_injections <= 0:
            raise ReportError(
                "G1 validator-domain correction required both core and lifecycle injections"
            )
        correction_audit["ensemble_call_core_injection_count"] = core_injections
        correction_audit["g1_registry_reason_injection_count"] = registry_injections
        correction_audit["injection_count"] = core_injections + registry_injections
        correction_audit["patched_action_counts"] = dict(sorted(actions.items()))
        unpatched_sources_by_key = {(item["group"], item["task_id"]): item for item in b2_states}
        patched_sources_by_key = {
            (item["group"], item["task_id"]): item for item in selected_sources
        }
        for key in correction_keys:
            patched = patched_sources_by_key[key]
            if (
                patched["action"] not in {"complete", "metadata_only"}
                or patched["generation_reasons"]
            ):
                raise ReportError(
                    f"G1 validator-domain correction did not clear generation reasons at "
                    f"{key}: {patched}"
                )
        for item in selected_sources:
            key = (item["group"], item["task_id"])
            prior = unpatched_sources_by_key[key]
            if (
                item["row_sha256"] != prior["row_sha256"]
                or item["source_path"] != prior["source_path"]
                or item["source_line"] != prior["source_line"]
            ):
                raise ReportError("validator correction changed cross-wave row selection")
    else:
        rows, audit, actions, selected_sources = (
            b2_rows,
            b2_resume_audit,
            b2_actions,
            b2_states,
        )
        correction_audit["patched_action_counts"] = dict(sorted(actions.items()))
    rows_by_key = {(str(row["group"]), str(row["task_id"])): row for row in rows}
    metadata_only_pairs: list[dict[str, Any]] = []
    incident_sources: list[dict[str, Any]] = []
    for state in selected_sources:
        key = (str(state["group"]), str(state["task_id"]))
        if key in incident_keys:
            incident_sources.append(state)
            validate_native_failure_resume_state(
                state,
                key=key,
                policy=INCIDENT_POLICY_BY_KEY[key],
            )
            continue
        evidence = validate_reportable_resume_state(
            state=state,
            row=rows_by_key[key],
            key=key,
            expected_fingerprint=fingerprints[key[0]],
            runner=runner,
        )
        if evidence is not None:
            metadata_only_pairs.append(evidence)

    observed_incident_keys = {
        (str(item["group"]), str(item["task_id"])) for item in incident_sources
    }
    if observed_incident_keys != set(incident_keys):
        raise ReportError(
            f"native failure key gate failed: observed={sorted(observed_incident_keys)!r}, "
            f"expected={sorted(incident_keys)!r}"
        )
    if actions.get("regenerate", 0) != len(incident_keys):
        raise ReportError("primary selection contains an unauthorized regenerate state")
    allowed_count = actions.get("complete", 0) + actions.get("metadata_only", 0)
    expected_allowed_count = EXPECTED_PAIRS - len(incident_keys)
    if len(rows) != EXPECTED_PAIRS or allowed_count != expected_allowed_count:
        raise ReportError(
            f"primary selection action gate failed; rows={len(rows)}, "
            f"actions={dict(sorted(actions.items()))}, "
            f"expected reportable={expected_allowed_count} plus "
            f"native_failures={len(incident_keys)}"
        )
    return rows, {
        "resume_audit": audit,
        "action_counts": dict(sorted(actions.items())),
        "metadata_only_pairs": metadata_only_pairs,
        "native_failure_sources": incident_sources,
        "selected_sources": selected_sources,
        "validator_domain_correction": correction_audit,
    }


def select_replacement_row(
    *,
    result_path: Path,
    incident_key: tuple[str, str],
    prompt_hashes: Mapping[str, str],
    task_input_hashes: Mapping[str, str],
    fingerprints: Mapping[str, str],
    contracts: Mapping[str, Mapping[str, Any]],
    runner: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    group, _task_id = incident_key
    require_non_byok = bool(
        (contracts[group].get("cost_policy") or {}).get("require_openrouter_non_byok")
    )
    try:
        states, audit = runner.load_resume_group_task_states(
            resume_paths=[result_path],
            selected_keys={incident_key},
            prompt_hashes=dict(prompt_hashes),
            task_input_hashes=dict(task_input_hashes),
            run_compatibility_fingerprints=dict(fingerprints),
            run_compatibility_contracts=contracts,
            require_openrouter_non_byok=require_non_byok,
            judge_required=True,
        )
    except Exception as exc:
        raise ReportError(f"targeted replacement selection failed: {exc}") from exc
    try:
        state = states.get(incident_key)
        if not isinstance(state, Mapping):
            action = state.get("action") if isinstance(state, Mapping) else "missing"
            raise ReportError(f"targeted replacement is missing (action={action!r})")
        row = states.consume_row(incident_key)
        metadata_evidence = validate_reportable_resume_state(
            state=state,
            row=row,
            key=incident_key,
            expected_fingerprint=fingerprints[group],
            runner=runner,
        )
    finally:
        states.close(verify=True)
    return row, {
        "resume_audit": audit,
        "action": str(state["action"]),
        "metadata_only_pair": metadata_evidence,
    }


def validate_native_failure_row(
    row: Mapping[str, Any],
    *,
    policy: IncidentPolicy,
    expected_fingerprint: str,
    expected_prompt_sha256: str,
    expected_task_input_sha256: str,
    runner: Any,
) -> None:
    label = f"{policy.group}/{policy.task_id}"
    reasons: list[str] = []
    if (str(row.get("group") or ""), str(row.get("task_id") or "")) != policy.key:
        reasons.append("row_key_mismatch")
    if row.get("run_compatibility_fingerprint") != expected_fingerprint:
        reasons.append("fingerprint_mismatch")
    if row.get("prompt_sha256") != expected_prompt_sha256:
        reasons.append("prompt_hash_mismatch")
    if row.get("task_input_sha256") != expected_task_input_sha256:
        reasons.append("task_input_hash_mismatch")
    top_error = str(row.get("error") or "")
    if not top_error:
        reasons.append("error_missing")
    if row.get("selected_generation_succeeded") is not False:
        reasons.append("generation_not_failed")
    if str(row.get("final_text") or ""):
        reasons.append("final_text_unexpected")
    if row.get("quality_total") is not None:
        reasons.append("quality_unexpected")
    judge = row.get("judge")
    if judge not in (None, {}):
        reasons.append("judge_unexpected")
    if row.get("candidate_judges") not in (None, []):
        reasons.append("candidate_judge_unexpected")
    if row.get("generation_attempt_budget_used") != INCIDENT_PRIMARY_BUDGET:
        reasons.append("budget_used_not_3")
    if row.get("generation_attempt_budget_limit") != INCIDENT_PRIMARY_BUDGET:
        reasons.append("budget_limit_not_3")
    if row.get("generation_attempt_count") != INCIDENT_PRIMARY_BUDGET:
        reasons.append("attempt_count_not_3")
    execution = row.get("execution")
    if not isinstance(execution, Mapping) or execution.get("prior_generation_attempts_used") != 0:
        reasons.append("prior_attempts_not_0")
    if (
        not isinstance(execution, Mapping)
        or execution.get("generation_attempt_budget_remaining") != 0
    ):
        reasons.append("budget_remaining_not_0")
    if (
        not isinstance(execution, Mapping)
        or execution.get("generation_attempt_count") != INCIDENT_PRIMARY_BUDGET
    ):
        reasons.append("execution_attempt_count_not_3")
    try:
        attempts = _attempts(row, label=f"native failure {label}")
        attempt_ids = _attempt_ids(row, label=f"native failure {label}")
    except ReportError as exc:
        reasons.append(str(exc))
        attempts = []
        attempt_ids = ()
    if len(attempts) != INCIDENT_PRIMARY_BUDGET or len(attempt_ids) != INCIDENT_PRIMARY_BUDGET:
        reasons.append("attempt_evidence_not_3")
    if attempts and tuple(int(item.get("attempt") or 0) for item in attempts) != (
        1,
        2,
        3,
    ):
        reasons.append("attempt_ordinals_not_1_2_3")
    try:
        if policy.failure_kind == "strict_quorum":
            observed_counts = _strict_quorum_completed_counts(row)
            if observed_counts != INCIDENT_COMPLETED_PROPOSERS:
                reasons.append("strict_quorum_counts_mismatch")
            observed_errors = _generation_attempt_errors(row)
            if observed_errors != policy.expected_attempt_errors:
                reasons.append("strict_quorum_attempt_errors_mismatch")
            if top_error != policy.expected_attempt_errors[-1]:
                reasons.append("top_level_strict_quorum_error_mismatch")
        elif _generation_attempt_errors(row) != policy.expected_attempt_errors:
            reasons.append("attempt_errors_mismatch")
        elif top_error != policy.expected_attempt_errors[-1]:
            reasons.append("top_level_attempt_error_mismatch")
        if top_error != _generation_attempt_errors(row)[-1]:
            reasons.append("top_level_error_not_final_attempt_error")
    except ReportError as exc:
        reasons.append(str(exc))
    row_accounting = runner.row_cost_accounting(dict(row))
    selected_accounting = row_accounting["llm_total"]
    accounting = row_accounting["actual_llm_total"]
    if int(selected_accounting.get("request_count") or 0) != 0:
        reasons.append("failed_cell_selected_request_count_nonzero")
    try:
        selected_cost = Decimal(str(selected_accounting.get("recorded_cost_usd")))
    except (ArithmeticError, ValueError):
        selected_cost = Decimal("NaN")
    if selected_cost != 0:
        reasons.append("failed_cell_selected_cost_nonzero")
    if selected_accounting.get("cost_complete") is not True:
        reasons.append("failed_cell_selected_cost_coverage_mismatch")
    if int(accounting.get("request_count") or 0) != policy.expected_actual_llm_request_count:
        reasons.append("actual_request_count_mismatch")
    if int(accounting.get("unknown_request_count") or 0) != (
        policy.expected_actual_llm_unknown_request_count
    ):
        reasons.append("actual_unknown_request_count_mismatch")
    try:
        recorded_cost = Decimal(str(accounting.get("recorded_cost_usd")))
    except (ArithmeticError, ValueError):
        recorded_cost = Decimal("NaN")
    if not recorded_cost.is_finite() or recorded_cost < 0:
        reasons.append("recorded_actual_cost_invalid")
    elif (
        policy.expected_recorded_actual_llm_cost_usd is not None
        and recorded_cost != policy.expected_recorded_actual_llm_cost_usd
    ):
        reasons.append("recorded_actual_cost_mismatch")
    expected_complete = policy.expected_actual_llm_unknown_request_count == 0
    if accounting.get("cost_complete") is not expected_complete:
        reasons.append("actual_cost_complete_mismatch")
    if reasons:
        raise ReportError(f"native failure contract failed at {label}: " + ", ".join(reasons))


def validate_primary_rows_with_incidents(
    rows: Sequence[Mapping[str, Any]],
    *,
    task_ids: Sequence[str],
    fingerprints: Mapping[str, str],
    prompt_hashes: Mapping[str, str],
    task_input_hashes: Mapping[str, str],
    policies: Sequence[IncidentPolicy],
    runner: Any,
) -> None:
    expected = {(group, task_id) for task_id in task_ids for group in ARMS}
    policies_by_key = {policy.key: policy for policy in policies}
    seen: set[tuple[str, str]] = set()
    seen_attempt_ids: set[str] = set()
    reasons: list[str] = []
    for row in rows:
        key = (str(row.get("group") or ""), str(row.get("task_id") or ""))
        if key in seen:
            reasons.append(f"duplicate:{key[0]}/{key[1]}")
        seen.add(key)
        if row.get("run_compatibility_fingerprint") != fingerprints.get(key[0]):
            reasons.append(f"fingerprint_mismatch:{key[0]}/{key[1]}")
        judge = row.get("judge")
        if key in policies_by_key:
            try:
                validate_native_failure_row(
                    row,
                    policy=policies_by_key[key],
                    expected_fingerprint=fingerprints[key[0]],
                    expected_prompt_sha256=prompt_hashes[key[1]],
                    expected_task_input_sha256=task_input_hashes[key[1]],
                    runner=runner,
                )
            except ReportError as exc:
                reasons.append(str(exc))
            try:
                attempt_ids = set(_attempt_ids(row, label=f"native failure {key}"))
                if seen_attempt_ids & attempt_ids:
                    reasons.append(f"cross_failure_attempt_id_reuse:{key[0]}/{key[1]}")
                seen_attempt_ids.update(attempt_ids)
            except ReportError as exc:
                reasons.append(str(exc))
            continue
        if row.get("error"):
            reasons.append(f"error:{key[0]}/{key[1]}")
        if row.get("selected_generation_succeeded") is not True:
            reasons.append(f"generation_failed:{key[0]}/{key[1]}")
        if not str(row.get("final_text") or "").strip():
            reasons.append(f"final_text_missing:{key[0]}/{key[1]}")
        if not isinstance(judge, Mapping) or judge.get("score_status") != "complete":
            reasons.append(f"judge_incomplete:{key[0]}/{key[1]}")
        elif int(judge.get("judge_error_count") or 0) != 0:
            reasons.append(f"judge_error:{key[0]}/{key[1]}")
        if not finite_number(row.get("quality_total")):
            reasons.append(f"quality_missing:{key[0]}/{key[1]}")
    if seen != expected:
        reasons.append(f"coverage:{len(seen & expected)}/{len(expected)}")
    if reasons:
        raise ReportError(
            "primary native-failure row validation failed: " + ", ".join(reasons[:20])
        )


def build_native_failure_evidence(
    rows: Sequence[Mapping[str, Any]],
    *,
    selected_sources: Sequence[Mapping[str, Any]],
    policies: Sequence[IncidentPolicy],
    runner: Any,
) -> list[NativeFailureEvidence]:
    rows_by_key = {
        (str(row.get("group") or ""), str(row.get("task_id") or "")): row for row in rows
    }
    sources_by_key = {
        (str(item.get("group") or ""), str(item.get("task_id") or "")): item
        for item in selected_sources
    }
    result: list[NativeFailureEvidence] = []
    for policy in policies:
        row = rows_by_key[policy.key]
        source = sources_by_key[policy.key]
        row_sha256 = canonical_object_sha256(row)
        if (
            not str(source.get("source_path") or "")
            or int(source.get("source_line") or 0) <= 0
            or source.get("row_sha256") != row_sha256
        ):
            raise ReportError(
                f"native failure source binding is incomplete at {policy.group}/{policy.task_id}"
            )
        result.append(
            NativeFailureEvidence(
                policy_name=policy.name,
                failure_kind=policy.failure_kind,
                group=policy.group,
                task_id=policy.task_id,
                source_path=str(source.get("source_path") or ""),
                source_line=int(source.get("source_line") or 0),
                row_sha256=row_sha256,
                run_compatibility_fingerprint=str(row.get("run_compatibility_fingerprint") or ""),
                attempt_ids=_attempt_ids(row, label=f"native failure {policy.name}"),
                attempt_errors=_generation_attempt_errors(row),
                actual_llm_account=runner.row_cost_accounting(dict(row))["actual_llm_total"],
            )
        )
    return result


def finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def validate_selected_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    task_ids: Sequence[str],
    fingerprints: Mapping[str, str],
) -> None:
    expected = {(group, task_id) for task_id in task_ids for group in ARMS}
    seen: set[tuple[str, str]] = set()
    reasons: list[str] = []
    per_group: Counter[str] = Counter()
    for row in rows:
        group = str(row.get("group") or "")
        task_id = str(row.get("task_id") or "")
        key = (group, task_id)
        if key in seen:
            reasons.append(f"duplicate:{group}/{task_id}")
        seen.add(key)
        per_group[group] += 1
        judge = row.get("judge")
        if row.get("error"):
            reasons.append(f"error:{group}/{task_id}")
        if row.get("selected_generation_succeeded") is not True:
            reasons.append(f"generation_failed:{group}/{task_id}")
        if not str(row.get("final_text") or "").strip():
            reasons.append(f"final_text_missing:{group}/{task_id}")
        execution = row.get("execution")
        attempts = execution.get("generation_attempts") if isinstance(execution, Mapping) else None
        if (
            not isinstance(attempts, list)
            or not attempts
            or row.get("generation_attempt_count") != len(attempts)
        ):
            reasons.append(f"attempt_evidence_missing:{group}/{task_id}")
        if not isinstance(judge, Mapping) or judge.get("score_status") != "complete":
            reasons.append(f"judge_incomplete:{group}/{task_id}")
        elif int(judge.get("judge_error_count") or 0) != 0:
            reasons.append(f"judge_error:{group}/{task_id}")
        if not finite_number(row.get("quality_total")):
            reasons.append(f"quality_missing:{group}/{task_id}")
        if row.get("run_compatibility_fingerprint") != fingerprints.get(group):
            reasons.append(f"fingerprint_mismatch:{group}/{task_id}")
    if seen != expected:
        reasons.append(f"coverage:{len(seen & expected)}/{len(expected)}")
    if any(per_group[group] != EXPECTED_TASKS for group in ARMS):
        reasons.append(f"per_group:{dict(sorted(per_group.items()))}")
    if reasons:
        raise ReportError("selected-row validation failed: " + ", ".join(reasons[:20]))


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _attempts(row: Mapping[str, Any], *, label: str) -> list[Mapping[str, Any]]:
    execution = row.get("execution")
    raw = execution.get("generation_attempts") if isinstance(execution, Mapping) else None
    if not isinstance(raw, list) or not raw or not all(isinstance(item, Mapping) for item in raw):
        raise ReportError(f"{label} lacks generation attempt evidence")
    return list(raw)


def _attempt_ids(row: Mapping[str, Any], *, label: str) -> tuple[str, ...]:
    values = tuple(str(item.get("attempt_id") or "") for item in _attempts(row, label=label))
    if any(re.fullmatch(r"[0-9a-f]{32}", value) is None for value in values):
        raise ReportError(f"{label} contains an invalid generation attempt id")
    if len(values) != len(set(values)):
        raise ReportError(f"{label} contains duplicate generation attempt ids")
    return values


def _physical_attempt_ids(row: Mapping[str, Any], *, label: str) -> tuple[str, ...]:
    values: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            physical = value.get("physical_attempt_id")
            if physical is not None and str(physical).strip():
                normalized = str(physical).strip().lower()
                if re.fullmatch(r"[0-9a-f]{32}", normalized) is None:
                    raise ReportError(f"{label} contains an invalid physical_attempt_id")
                values.add(normalized)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for attempt in _attempts(row, label=label):
        visit(attempt.get("run"))
    if not values:
        raise ReportError(f"{label} contains no physical request identities")
    return tuple(sorted(values))


def _strict_quorum_completed_counts(row: Mapping[str, Any]) -> tuple[int, ...]:
    counts: list[int] = []
    for attempt in _attempts(row, label="primary incident row"):
        strings = list(_iter_strings(attempt))
        if not any(INCIDENT_ERROR_FRAGMENT in value for value in strings):
            raise ReportError("a primary incident attempt lacks the strict-quorum error")
        if not any("aggregation was not started" in value for value in strings):
            raise ReportError("a primary incident attempt does not prove aggregation was skipped")
        matches = [
            re.search(r"but only ([0-9]+) completed", value)
            for value in strings
            if "but only " in value and " completed" in value
        ]
        observed = [int(match.group(1)) for match in matches if match is not None]
        if not observed:
            raise ReportError("cannot extract completed-proposer count from incident attempt")
        counts.append(observed[0])
    return tuple(counts)


def _generation_attempt_errors(row: Mapping[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    for attempt in _attempts(row, label="primary incident row"):
        run = attempt.get("run")
        if not isinstance(run, Mapping) or not isinstance(run.get("error"), str):
            raise ReportError("a primary incident attempt lacks a string run.error")
        errors.append(str(run["error"]))
    return tuple(errors)


def validate_incident_replacement(
    *,
    spec_path: Path,
    spec_sha256: str,
    spec: Mapping[str, Any],
    primary_row: dict[str, Any],
    primary_selection_source: Mapping[str, Any],
    primary_waves: Sequence[WaveEvidence],
    replacement_row: dict[str, Any],
    replacement_wave: WaveEvidence,
    task_ids: Sequence[str],
    expected_manifest_path: Path,
    only_keys_path: Path,
    resume_runner_path: Path,
    fingerprints: Mapping[str, str],
    runner: Any,
) -> IncidentEvidence:
    primary = spec["primary"]
    replacement = spec["replacement"]
    expected_fingerprint = fingerprints[INCIDENT_GROUP]
    if primary.get("run_compatibility_fingerprint") != expected_fingerprint:
        raise ReportError("incident primary receipt fingerprint differs from frozen B2")
    if replacement.get("run_compatibility_fingerprint") != expected_fingerprint:
        raise ReportError("incident replacement receipt fingerprint differs from frozen B2")
    if primary_row.get("run_compatibility_fingerprint") != expected_fingerprint:
        raise ReportError("primary incident row fingerprint differs from frozen B2")
    if replacement_row.get("run_compatibility_fingerprint") != expected_fingerprint:
        raise ReportError("replacement row fingerprint differs from frozen B2")

    source_path = Path(str(primary_selection_source.get("source_path") or "")).resolve()
    source_line = int(primary_selection_source.get("source_line") or 0)
    source_wave = next(
        (wave for wave in primary_waves if wave.results_path.resolve() == source_path),
        None,
    )
    if source_wave is None or source_line <= 0:
        raise ReportError("cannot bind the incident row to a validated primary wave")
    if require_sha256(primary.get("results_sha256"), label="primary results SHA") != (
        source_wave.results_sha256
    ):
        raise ReportError("incident receipt primary results SHA does not match selected source")
    if require_sha256(primary.get("manifest_sha256"), label="primary manifest SHA") != (
        source_wave.manifest_sha256
    ):
        raise ReportError("incident receipt primary manifest SHA does not match selected source")
    primary_row_sha = canonical_object_sha256(primary_row)
    if require_sha256(primary.get("row_canonical_sha256"), label="primary row SHA") != (
        primary_row_sha
    ):
        raise ReportError("incident receipt primary row SHA does not match selected row")

    if replacement_wave.status not in {"complete", "metadata_incomplete"}:
        raise ReportError(
            "fresh targeted replacement wave must have status=complete or metadata_incomplete"
        )
    if replacement_wave.groups != INCIDENT_REPLACEMENT_GROUPS or replacement_wave.rows_written != 1:
        raise ReportError(
            "targeted resume-runner replacement must declare groups [B2,G1] and one row"
        )
    replacement_manifest = read_json_object(
        replacement_wave.manifest_path, label="replacement manifest"
    )
    if replacement_manifest.get("task_count") != EXPECTED_TASKS or replacement_manifest.get(
        "task_ids"
    ) != list(task_ids):
        raise ReportError("replacement manifest must retain the frozen ordered 10-task universe")
    replacement_rows = read_jsonl_objects(replacement_wave.results_path, label="replacement result")
    if (
        len(replacement_rows) != 1
        or (replacement_rows[0].get("group"), replacement_rows[0].get("task_id")) != INCIDENT_KEY
    ):
        raise ReportError("replacement result must contain B2/f004 exactly once and zero G1 rows")
    derived_spec = derive_incident_receipt(
        primary_row=primary_row,
        primary_wave=source_wave,
        replacement_row=replacement_row,
        replacement_wave=replacement_wave,
        expected_manifest_path=expected_manifest_path,
        only_keys_path=only_keys_path,
        resume_runner_path=resume_runner_path,
        replacement_manifest=replacement_manifest,
        runner=runner,
    )
    if dict(spec) != derived_spec:
        raise ReportError(
            "incident receipt differs from the exact receipt derived from sealed artifacts"
        )
    if require_sha256(replacement.get("results_sha256"), label="replacement results SHA") != (
        replacement_wave.results_sha256
    ):
        raise ReportError("incident receipt replacement results SHA does not match")
    if require_sha256(replacement.get("manifest_sha256"), label="replacement manifest SHA") != (
        replacement_wave.manifest_sha256
    ):
        raise ReportError("incident receipt replacement manifest SHA does not match")
    replacement_row_sha = canonical_object_sha256(replacement_row)
    if require_sha256(replacement.get("row_canonical_sha256"), label="replacement row SHA") != (
        replacement_row_sha
    ):
        raise ReportError("incident receipt replacement row SHA does not match")
    if source_wave.results_path.resolve() == replacement_wave.results_path.resolve():
        raise ReportError("replacement result cannot also be a causal primary wave")
    if source_wave.results_sha256 == replacement_wave.results_sha256:
        raise ReportError("replacement result bytes are identical to the primary source")

    if primary_row.get("generation_attempt_budget_used") != INCIDENT_PRIMARY_BUDGET or (
        primary_row.get("generation_attempt_budget_limit") != INCIDENT_PRIMARY_BUDGET
    ):
        raise ReportError("primary incident row does not prove frozen budget exhaustion at 3/3")
    primary_attempt_ids = _attempt_ids(primary_row, label="primary incident row")
    if len(primary_attempt_ids) != INCIDENT_PRIMARY_BUDGET:
        raise ReportError("primary incident row must contain exactly three generation attempts")
    counts = _strict_quorum_completed_counts(primary_row)
    if counts != INCIDENT_COMPLETED_PROPOSERS:
        raise ReportError(f"primary incident proposer counts are {counts!r}, expected (1, 2, 2)")
    if tuple(primary.get("completed_proposer_counts") or ()) != counts:
        raise ReportError("incident receipt proposer counts differ from primary evidence")

    replacement_attempts = _attempts(replacement_row, label="replacement row")
    replacement_attempt_ids = _attempt_ids(replacement_row, label="replacement row")
    replacement_execution = replacement_row.get("execution") or {}
    replacement_used = replacement_row.get("generation_attempt_budget_used")
    if replacement_row.get("generation_attempt_budget_limit") != 3:
        raise ReportError("replacement did not retain the frozen fresh 3-attempt budget")
    if replacement_execution.get("prior_generation_attempts_used") != 0:
        raise ReportError(
            "replacement is not a no-history targeted resume-runner wave "
            "(prior attempts is not zero)"
        )
    if replacement_used != len(replacement_attempts) or not 1 <= int(replacement_used or 0) <= 3:
        raise ReportError("replacement generation attempt declarations are inconsistent")
    ordinals = tuple(int(item.get("attempt") or 0) for item in replacement_attempts)
    if ordinals != tuple(range(1, len(replacement_attempts) + 1)):
        raise ReportError("replacement attempt ordinals do not start fresh at one")

    primary_physical = _physical_attempt_ids(primary_row, label="primary incident row")
    replacement_physical = _physical_attempt_ids(replacement_row, label="replacement row")
    if set(primary_attempt_ids) & set(replacement_attempt_ids):
        raise ReportError("replacement reuses a primary logical generation attempt id")
    if set(primary_physical) & set(replacement_physical):
        raise ReportError("replacement reuses a primary physical request id")

    primary_account = runner.row_cost_accounting(primary_row)["actual_llm_total"]
    replacement_account = runner.row_cost_accounting(replacement_row)["actual_llm_total"]
    if int(primary_account.get("request_count") or 0) != primary.get("actual_llm_request_count"):
        raise ReportError("primary incident actual request count differs from receipt")
    if int(primary_account.get("unknown_request_count") or 0) != primary.get(
        "actual_llm_unknown_request_count"
    ):
        raise ReportError("primary incident unknown request count differs from receipt")
    observed_parent_cost = Decimal(str(primary_account.get("recorded_cost_usd")))
    if observed_parent_cost != Decimal(str(primary.get("recorded_actual_llm_cost_usd"))):
        raise ReportError("primary incident recorded actual cost differs from receipt")

    return IncidentEvidence(
        policy_name=B2_INCIDENT_POLICY.name,
        failure_kind=B2_INCIDENT_POLICY.failure_kind,
        spec_path=spec_path.resolve(),
        spec_sha256=spec_sha256,
        incident_id=str(spec["incident_id"]),
        group=INCIDENT_GROUP,
        task_id=INCIDENT_TASK_ID,
        primary_source_path=str(source_path),
        primary_source_line=source_line,
        primary_results_sha256=source_wave.results_sha256,
        primary_manifest_sha256=source_wave.manifest_sha256,
        primary_row_sha256=primary_row_sha,
        primary_attempt_ids=primary_attempt_ids,
        primary_physical_attempt_ids=primary_physical,
        primary_actual_account=primary_account,
        replacement_wave=replacement_wave,
        replacement_row_sha256=replacement_row_sha,
        replacement_attempt_ids=replacement_attempt_ids,
        replacement_physical_attempt_ids=replacement_physical,
        replacement_actual_account=replacement_account,
        replacement_groups=B2_INCIDENT_POLICY.replacement_groups,
        only_group_task_keys_sha256=str(replacement["only_group_task_keys_raw_sha256"]),
        expected_compatibility_manifest_sha256=str(
            replacement["expected_compatibility_manifest_raw_sha256"]
        ),
        resume_runner_sha256=str(replacement["resume_runner_raw_sha256"]),
        resume_selection=dict(replacement["resume_selection"]),
    )


def aggregate_cost_coverage(
    scoring_rows: Sequence[dict[str, Any]],
    *,
    actual_ledger_rows: Sequence[dict[str, Any]] | None = None,
    runner: Any,
) -> dict[str, dict[str, Any]]:
    actual_ledger_rows = actual_ledger_rows or scoring_rows
    result: dict[str, dict[str, Any]] = {}
    for group in ARMS:
        selected_accounts = [
            runner.row_cost_accounting(row) for row in scoring_rows if row["group"] == group
        ]
        actual_accounts = [
            runner.row_cost_accounting(row) for row in actual_ledger_rows if row["group"] == group
        ]
        if len(selected_accounts) != EXPECTED_TASKS:
            raise ReportError(
                f"cannot aggregate selected cost coverage for {group}: "
                f"{len(selected_accounts)} rows"
            )
        selected = runner.merge_cost_accounting(
            f"report_{group}_selected_llm",
            [account["llm_total"] for account in selected_accounts],
        )
        actual = runner.merge_cost_accounting(
            f"report_{group}_actual_llm",
            [account["actual_llm_total"] for account in actual_accounts],
        )
        selected_generation = runner.merge_cost_accounting(
            f"report_{group}_selected_generation",
            [account["generation"] for account in selected_accounts],
        )
        actual_generation = runner.merge_cost_accounting(
            f"report_{group}_actual_generation",
            [account["actual_generation_spend"] for account in actual_accounts],
        )
        actual_judge = runner.merge_cost_accounting(
            f"report_{group}_actual_judge",
            [
                account[scope]
                for account in actual_accounts
                for scope in ("judge", "candidate_judge")
            ],
        )
        result[group] = {
            "selected": selected,
            "actual": actual,
            "selected_generation": selected_generation,
            "actual_generation": actual_generation,
            "actual_judge": actual_judge,
            "selected_unit_count": len(selected_accounts),
            "actual_unit_count": len(actual_accounts),
            "result_llm_complete_rows": sum(
                account["result_llm_cost_complete"] is True for account in selected_accounts
            ),
            "actual_llm_complete_rows": sum(
                account["actual_llm_cost_complete"] is True for account in actual_accounts
            ),
            "result_complete_rows": sum(
                account["result_cost_complete"] is True for account in selected_accounts
            ),
            "actual_complete_rows": sum(
                account["actual_spend_cost_complete"] is True for account in actual_accounts
            ),
        }
    return result


def validate_metadata_only_cost_coverage(
    cost_coverage: Mapping[str, Mapping[str, Any]],
    metadata_only_pairs: Sequence[Mapping[str, Any]],
) -> None:
    """Prove relaxed metadata rows remain visible as aggregate lower bounds."""

    for group in sorted({str(item.get("group") or "") for item in metadata_only_pairs}):
        coverage = cost_coverage.get(group)
        actual = coverage.get("actual") if isinstance(coverage, Mapping) else None
        if (
            not isinstance(actual, Mapping)
            or actual.get("cost_complete") is not False
            or int(actual.get("unknown_request_count") or 0) <= 0
        ):
            raise ReportError(
                f"metadata_only aggregate cost for {group} is not retained as a lower bound"
            )


def paired_quality_comparisons(
    rows: Sequence[Mapping[str, Any]],
    *,
    bootstrap_samples: int = BOOTSTRAP_SAMPLES,
    excluded_keys: frozenset[tuple[str, str]] = frozenset(),
    failure_keys: frozenset[tuple[str, str]] = frozenset(),
    analysis_label: str = "confirmatory",
) -> list[dict[str, Any]]:
    if bootstrap_samples <= 0:
        raise ReportError("bootstrap sample count must be positive")
    task_ids = sorted({str(row["task_id"]) for row in rows})
    if len(task_ids) != EXPECTED_TASKS:
        raise ReportError(
            f"paired comparison requires {EXPECTED_TASKS} task ids, got {len(task_ids)}"
        )
    by_key: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        key = (str(row["group"]), str(row["task_id"]))
        if finite_number(row.get("quality_total")):
            by_key[key] = Decimal(str(row["quality_total"]))
        elif key in failure_keys:
            by_key[key] = Decimal(0)
        elif key not in excluded_keys:
            raise ReportError(f"paired comparison has an unclassified missing quality at {key}")
    comparisons: list[dict[str, Any]] = []
    pairs = [
        *((group, "B0") for group in ("B1", "B2", "B4", "G1", "S4")),
        *((group, "B1") for group in ("B2", "B4", "G1", "S4")),
    ]
    for group, baseline in pairs:
        common = [
            task_id
            for task_id in task_ids
            if (group, task_id) in by_key
            and (baseline, task_id) in by_key
            and (group, task_id) not in excluded_keys
            and (baseline, task_id) not in excluded_keys
        ]
        expected_count = sum(
            (group, task_id) not in excluded_keys and (baseline, task_id) not in excluded_keys
            for task_id in task_ids
        )
        if len(common) != expected_count or not common:
            raise ReportError(
                f"paired comparison {group}-{baseline} has {len(common)}/{expected_count} pairs"
            )
        differences = [by_key[(group, task_id)] - by_key[(baseline, task_id)] for task_id in common]
        mean = sum(differences, Decimal(0)) / Decimal(len(differences))
        seed = f"draco:{analysis_label}:{group}:{baseline}"
        rng = random.Random(seed)
        bootstrap = sorted(
            sum(
                (differences[rng.randrange(len(differences))] for _ in differences),
                Decimal(0),
            )
            / Decimal(len(differences))
            for _ in range(bootstrap_samples)
        )
        low = bootstrap[int(Decimal("0.025") * Decimal(bootstrap_samples - 1))]
        high = bootstrap[int(Decimal("0.975") * Decimal(bootstrap_samples - 1))]
        comparisons.append(
            {
                "group": group,
                "baseline": baseline,
                "pair_count": len(common),
                "mean_delta_quality": str(mean),
                "ci95_low": str(low),
                "ci95_high": str(high),
                "wins": sum(value > 0 for value in differences),
                "ties": sum(value == 0 for value in differences),
                "losses": sum(value < 0 for value in differences),
                "bootstrap_samples": bootstrap_samples,
                "seed": seed,
                "analysis": analysis_label,
            }
        )
    return comparisons


def failure_aware_group_metrics(
    rows: Sequence[Mapping[str, Any]],
    *,
    failure_keys: frozenset[tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for group in ARMS:
        group_rows = [row for row in rows if str(row.get("group") or "") == group]
        if len(group_rows) != EXPECTED_TASKS:
            raise ReportError(f"failure-aware metrics require 10 observed rows for {group}")
        scored = [
            Decimal(str(row["quality_total"]))
            for row in group_rows
            if finite_number(row.get("quality_total"))
        ]
        failures = [
            row for row in group_rows if (group, str(row.get("task_id") or "")) in failure_keys
        ]
        if len(scored) + len(failures) != EXPECTED_TASKS:
            raise ReportError(f"failure-aware metrics found an unclassified row for {group}")
        scored_total = sum(scored, Decimal(0))
        result[group] = {
            "observed_rows": EXPECTED_TASKS,
            "scored_rows": len(scored),
            "protocol_failure_rows": len(failures),
            "completion_rate_pct": str(
                Decimal(len(scored)) * Decimal(100) / Decimal(EXPECTED_TASKS)
            ),
            "scored_only_avg_quality": (
                str(scored_total / Decimal(len(scored))) if scored else None
            ),
            "failure_adjusted_avg_quality": str(scored_total / Decimal(EXPECTED_TASKS)),
        }
    return result


def common_task_ranking(
    rows: Sequence[Mapping[str, Any]],
    *,
    excluded_task_ids: frozenset[str],
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    expected_count = EXPECTED_TASKS - len(excluded_task_ids)
    for group in ARMS:
        values = [
            Decimal(str(row["quality_total"]))
            for row in rows
            if str(row.get("group") or "") == group
            and str(row.get("task_id") or "") not in excluded_task_ids
            and finite_number(row.get("quality_total"))
        ]
        if len(values) != expected_count:
            raise ReportError(
                f"common-task ranking has {len(values)}/{expected_count} values for {group}"
            )
        ranked.append(
            {
                "group": group,
                "task_count": len(values),
                "avg_quality": str(sum(values, Decimal(0)) / Decimal(len(values))),
            }
        )
    ranked.sort(key=lambda item: (-Decimal(str(item["avg_quality"])), ARMS.index(item["group"])))
    for index, item in enumerate(ranked, 1):
        item["rank"] = index
    return ranked


def build_arm_definitions(
    *,
    manifest: Mapping[str, Any],
    contracts: Mapping[str, Mapping[str, Any]],
    b2_experiment_config: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Bind concise arm descriptions to frozen specs and provider surfaces."""

    manifest_specs = manifest.get("group_specs")
    if not isinstance(manifest_specs, Mapping) or set(manifest_specs) != ARM_SET:
        raise ReportError("expected manifest lacks the exact six-arm group specs")
    specs: dict[str, dict[str, Any]] = {}
    for group in ARMS:
        contract_spec = contracts[group].get("group_spec")
        manifest_spec = manifest_specs.get(group)
        if (
            not isinstance(contract_spec, Mapping)
            or not isinstance(manifest_spec, Mapping)
            or dict(contract_spec) != dict(manifest_spec)
        ):
            raise ReportError(f"manifest/contract group spec differs for {group}")
        specs[group] = dict(contract_spec)
    required_specs = {
        "B0": {
            "kind": "single",
            "model": "anthropic/claude-fable-5",
            "label": "fixed_claude_fable5",
        },
        "B1": {
            "kind": "router_single",
            "label": "single_model_routing",
        },
        "B2": {
            "kind": "selection_mode",
            "selection_mode": "static_openrouter",
            "label": "b2_quality_first_static_openrouter_b5",
        },
        "B4": {
            "kind": "single",
            "model": "openai/gpt-5.6-sol",
            "label": "fixed_gpt56_sol",
        },
        "G1": {
            "kind": "selection_mode",
            "selection_mode": "router_dynamic",
            "label": "ranking_router_dynamic",
        },
    }
    for group, required in required_specs.items():
        if any(specs[group].get(key) != value for key, value in required.items()):
            raise ReportError(f"frozen {group} group definition differs: {specs[group]!r}")
    s4_spec = {
        "kind": "router_single",
        "label": "single_model_routing_restricted_4",
        "tier_models": {
            "c0": "qwen/qwen3-8b",
            "c1": "deepseek/deepseek-v4-flash",
            "c2": "qwen/qwen3.7-plus",
            "c3": "deepseek/deepseek-v4-pro",
        },
    }
    if specs["S4"] != s4_spec:
        raise ReportError(f"frozen S4 restricted-router definition differs: {specs['S4']!r}")
    for row in rows:
        group = str(row.get("group") or "")
        provider_spec = row.get("provider_spec")
        if group not in ARM_SET or not isinstance(provider_spec, Mapping):
            raise ReportError("selected row lacks a frozen provider spec")
        if dict(provider_spec) != specs[group]:
            raise ReportError(
                f"selected row provider spec differs from manifest at {group}/{row.get('task_id')}"
            )

    ensemble = b2_experiment_config.get("ensemble")
    proposers = ensemble.get("proposers") if isinstance(ensemble, Mapping) else None
    aggregator = ensemble.get("aggregator") if isinstance(ensemble, Mapping) else None
    if not isinstance(proposers, list) or len(proposers) != 4:
        raise ReportError("frozen B2 config is not a four-member proposer ensemble")
    expanded_proposers: list[str] = []
    for proposer in proposers:
        if not isinstance(proposer, Mapping):
            raise ReportError("frozen B2 proposer is not an object")
        provider = str(proposer.get("provider") or "")
        model = str(proposer.get("model") or "")
        count = proposer.get("k", 1)
        if (
            not provider
            or not model
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
        ):
            raise ReportError("frozen B2 proposer identity/sample count is invalid")
        expanded_proposers.extend([f"{provider}:{model}"] * count)
    if len(expanded_proposers) != 4:
        raise ReportError("frozen B2 config does not expand to exactly four proposers")
    if (
        not isinstance(aggregator, Mapping)
        or aggregator.get("provider") != "openrouter"
        or aggregator.get("model") != "z-ai/glm-5.2"
    ):
        raise ReportError("frozen B2 aggregator is not OpenRouter GLM 5.2")

    definitions = {
        "B0": "fixed Claude Fable 5 (`anthropic/claude-fable-5`)",
        "B1": "current SquillaRouter single-model tiers (`router_single`)",
        "B2": "fixed 4-proposer ensemble + GLM 5.2 aggregator",
        "B4": "fixed GPT-5.6-sol (`openai/gpt-5.6-sol`)",
        "G1": "current dynamic routing + fusion (`router_dynamic`)",
        "S4": (
            "restricted four-model single router: c0=`qwen/qwen3-8b`; "
            "c1=`deepseek/deepseek-v4-flash`; c2=`qwen/qwen3.7-plus`; "
            "c3=`deepseek/deepseek-v4-pro`"
        ),
    }
    return [
        {
            "arm": group,
            "definition": definitions[group],
            "label": str(specs[group].get("label") or ""),
            "group_spec_sha256": canonical_object_sha256(specs[group]),
        }
        for group in ARMS
    ]


def md_escape(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def fmt(value: Any, digits: int = 2) -> str:
    if value is None or isinstance(value, bool):
        return "N/A"
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return "N/A"
    if not math.isfinite(number):
        return "N/A"
    return f"{number:.{digits}f}"


def render_markdown(report: Mapping[str, Any]) -> str:
    summaries = report["summary"]["groups"]
    failure_metrics = report["failure_metrics"]
    native_failures = list(report["native_failures"])
    native_failure_keys = {(failure.group, failure.task_id) for failure in native_failures}
    costs = report["cost_coverage"]
    rows = report["rows"]
    task_ids = report["task_ids"]
    arm_definitions = list(report["arm_definitions"])
    waves = report["waves"]
    incident = report.get("incident")
    selection = report.get("selection") or {}
    correction = selection.get("validator_domain_correction") or {}
    action_counts = selection.get("action_counts") or {}
    primary_metadata_pairs = list(selection.get("metadata_only_pairs") or [])
    replacement_selection = selection.get("incident_replacement") or {}
    replacement_metadata_pair = replacement_selection.get("metadata_only_pair")
    all_metadata_pairs = [
        *primary_metadata_pairs,
        *([replacement_metadata_pair] if isinstance(replacement_metadata_pair, Mapping) else []),
    ]
    action_text = ", ".join(
        f"{action}={int(action_counts.get(action, 0))}"
        for action in (
            "complete",
            "metadata_only",
            "regenerate",
            "judge_only",
            "audit_only",
        )
        if action in action_counts or action in {"complete", "metadata_only"}
    )
    lines = ["# DRACO Mini B0/B1/B2/B4/G1/S4 实验结果", "", "## 完整性", ""]
    completed_by_arm = ", ".join(
        f"{group} {failure_metrics[group]['scored_rows']}/{EXPECTED_TASKS}" for group in ARMS
    )
    lines.extend(
        [
            (
                f"- 冻结任务矩阵已观测 `{len(rows)}/{EXPECTED_PAIRS}` 行；其中 Judge/quality "
                f"可评分 `57/{EXPECTED_PAIRS}`。"
            ),
            (
                f"- 各臂 completed/scored：`{completed_by_arm}`；三项原生 protocol failure "
                "均保留为未评分观测。"
            ),
            (
                "- Primary operational utility 将执行失败显式计为 `U=0`（每臂分母 10）；"
                "质量矩阵仍写 `EXEC_FAIL`，没有伪造 Judge 0 分。"
            ),
            (
                "- 共同 complete-case 诊断统一剔除 f004，六臂都使用相同 9 题；"
                "不是仅对失败臂选择性删题。"
            ),
            f"- Primary resume classifier action counts：`{action_text}`。",
            (
                f"- Primary 由 `{len(waves)}` 个 initial/resume wave 按因果顺序离线合并；"
                "报告生成未调用模型或网络。"
            ),
            f"- DRACO input SHA-256：`{report['input_sha256']}`。",
            "",
        ]
    )
    lines.extend(
        [
            "## 实验臂定义",
            "",
            "| Arm | Frozen definition | Manifest evidence |",
            "|---|---|---|",
        ]
    )
    for arm in arm_definitions:
        lines.append(
            f"| {arm['arm']} | {arm['definition']} | label=`{arm['label']}`; "
            f"group_spec SHA-256=`{arm['group_spec_sha256']}` |"
        )
    lines.append("")
    if correction.get("applied") is True:
        lines.extend(["## Offline validator-domain correction", ""])
        if correction.get("b2_applied") is True:
            b2_proof = correction.get("b2_config_hydration_proof") or {}
            b2_keys = correction.get("b2_corrected_keys") or []
            lines.extend(
                [
                    (
                        f"- Current resume classifier 将全部 `{len(b2_keys)}` 个 B2 rows "
                        "先判为 regenerate，因为 manifest compatibility contract 按设计只保存 "
                        "`experiment_config.sha256`，classifier 却尝试从该字段内读取 "
                        "`ensemble`/`routing`，从而产生 "
                        "`missing_expected_b2_ensemble_contract`。"
                    ),
                    (
                        "- 报告器从 expected-manifest 绑定的 effective/resolution artifacts "
                        "读取配置，逐字复现 runner compatibility 投影；投影 SHA-256 "
                        f"`{b2_proof.get('compatibility_projection_sha256')}` 必须精确等于 "
                        f"B2 contract pin `{b2_proof.get('contract_pin')}`，随后只在内存中的 "
                        "deep-copy B2 validator contract 注入该投影。"
                    ),
                    (
                        "- Hydrate 前后 selected source/line/row SHA 与非 B2 state 必须全等；"
                        "9 个 surface-complete/Judge-complete B2 rows 的唯一 delta 是删除上述伪"
                        " reason 并变为 complete。B2/f004 仍为 regenerate，伪 reason 被真实 "
                        f"`{B2_INCIDENT_CONTRACT_REASON}` 替换。"
                    ),
                    (
                        "- B2 hydrate 前/后 action counts："
                        f"`{correction.get('b2_unpatched_action_counts')}` → "
                        f"`{correction.get('b2_hydrated_action_counts')}`。"
                    ),
                ]
            )
        if correction.get("g1_applied") is True:
            corrected_keys = correction.get("unpatched_correctable_keys") or []
            lines.extend(
                [
                    (
                        f"- 另有 `{len(corrected_keys)}` 个 surface-complete G1 rows 被未修正"
                        " classifier 判为 regenerate；每行唯一 generation reason 均为 "
                        "`invalid_g1_task_analyzer_execution_contract`。"
                    ),
                    (
                        "- 根因是 resume validator 在 `ensemble_call_core_reasons` 及 G1 "
                        "lifecycle 的多处 `g1_registry_contract_reasons` 调用中遗漏 manifest-"
                        "authenticated `task_analyzer_execution_contract`；并非 row plan 漂移。"
                    ),
                    (
                        "- 每个受影响 row 的 declared plan contract 必须与 manifest expected "
                        "contract whole-equal（SHA-256 "
                        f"`{correction.get('expected_task_analyzer_execution_contract_sha256')}`），"
                        "且 registry SHA-256 精确为 "
                        f"`{correction.get('expected_g1_registry_contract_sha256')}`。"
                    ),
                    (
                        "- 仅在 registry canonical hash 精确匹配时临时包装两个函数并注入 "
                        "expected analyzer contract：core "
                        f"`{correction.get('ensemble_call_core_injection_count')}` 次、"
                        "lifecycle/registry "
                        f"`{correction.get('g1_registry_reason_injection_count')}` 次；"
                        "`finally` 双恢复，selected source/line/row SHA 必须不变。"
                    ),
                ]
            )
        lines.extend(
            [
                (
                    "- 最终 patched classifier action counts："
                    f"`{correction.get('patched_action_counts')}`；cost-only "
                    "`metadata_only` 不会被静默改写为 `complete`。"
                ),
                (
                    "- 两项均为离线 validator 参数域修正，不修改远端 worktree、result rows、"
                    "generation、Judge、成本或分数；独立 execution/Judge/quality/fingerprint "
                    "门仍全部执行。"
                ),
                "",
            ]
        )
    if all_metadata_pairs:
        pair_labels = ", ".join(
            f"`{item['group']}/{item['task_id']}`" for item in all_metadata_pairs
        )
        primary_labels = ", ".join(
            f"`{item['group']}/{item['task_id']}`" for item in primary_metadata_pairs
        )
        accounting_details = "; ".join(
            f"`{item['group']}/{item['task_id']}` requests="
            f"{int(item['actual_llm_request_count'])}, unknown="
            f"{int(item['actual_llm_unknown_request_count'])}, known="
            f"{fmt(item['actual_llm_known_request_coverage_pct'], 2)}%, recorded=$"
            f"{fmt(item['actual_llm_recorded_cost_usd'], 9)} (lower bound)"
            for item in all_metadata_pairs
        )
        lines.extend(
            [
                "## Relaxed cost-metadata audit",
                "",
                f"- 允许报告的 `metadata_only` pairs 共 `{len(all_metadata_pairs)}`："
                f"{pair_labels}。Primary 中为 `{len(primary_metadata_pairs)}` 项"
                + (f"（{primary_labels}）" if primary_metadata_pairs else "")
                + "。",
                (
                    "- 这是 fail-closed 的 cost-only 例外：每项必须 "
                    "`generation_valid=true`、`judge_complete=true`，"
                    "generation/Judge/audit/fatal reasons 全空，且唯一 cost reason 精确为 "
                    "`cost_metadata_incomplete`；final text、attempt evidence、Judge、"
                    "quality、fingerprint 与 row error 门保持严格。"
                ),
                (
                    "- 这些行的 actual LLM account 明确保持 `cost_complete=false` 且 "
                    "unknown requests > 0；报告器据此将 recorded cost 标为 lower bound。"
                    "Unknown requests 和 coverage 原样计入下表，不补零、不估成完整成本，"
                    "也不触发 G1 重跑。"
                ),
                f"- 行级 lower-bound accounting：{accounting_details}。",
                "",
            ]
        )
    lines.extend(
        [
            "## 原生 protocol failures",
            "",
            (
                "仅下列三个冻结 key 可进入失败路径；每项都已通过 group/task/fingerprint、"
                "3/3 budget、prior=0、attempt ordinal/ID、精确错误序列、Judge 缺失、"
                "selected spend=0 与 actual accounting 的 closed gate。其他失败仍会终止报告。"
            ),
            "",
            (
                "| Arm / task | Kind | Budget | Actual req | Unknown | Actual LLM$ | "
                "Known% | Cost complete | Source | Row SHA-256 |"
            ),
            "|---|---|---:|---:|---:|---:|---:|---|---|---|",
        ]
    )
    for failure in native_failures:
        account = failure.actual_llm_account
        lines.append(
            f"| `{failure.group}/{failure.task_id}` | `{failure.failure_kind}` | 3/3 | "
            f"{int(account['request_count'])} | {int(account['unknown_request_count'])} | "
            f"{fmt(account['recorded_cost_usd'], 9)} | "
            f"{fmt(account['known_request_coverage_pct'], 4)}% | "
            f"`{str(bool(account['cost_complete'])).lower()}` | "
            f"`{Path(failure.source_path).name}:{failure.source_line}` | "
            f"`{failure.row_sha256}` |"
        )
    lines.extend(["", "Attempt terminal errors：", ""])
    for failure in native_failures:
        error_sequence = " → ".join(f"`{md_escape(error)}`" for error in failure.attempt_errors)
        lines.append(
            f"- `{failure.group}/{failure.task_id}`：{error_sequence}；"
            f"logical attempt IDs `{','.join(failure.attempt_ids)}`。"
        )
    lines.extend(
        [
            "",
            (
                "三项失败的全部 generation attempts 均计入 Actual；Selected 只含成功选中的 "
                "generation/Judge，失败 cell 的 selected requests/cost 为 0。Primary 未使用 "
                "fresh replacement。"
            ),
            "",
        ]
    )
    if isinstance(incident, IncidentEvidence):
        parent = incident.primary_actual_account
        repair = incident.replacement_actual_account
        lines.extend(
            [
                "## Post-hoc replacement sensitivity（不属于 primary）",
                "",
                (
                    f"- Incident：`{incident.incident_id}`；pair："
                    f"`{incident.group}/{incident.task_id}`。"
                ),
                (
                    "- Primary 始终保持 57/60 scored；replacement 不填补质量矩阵、"
                    "不改变 primary 排名，也不计入下方 primary Actual 成本表。"
                ),
                (
                    "- Primary 的 3 次 generation attempts 均未达到 tool-enabled aggregation "
                    "的 3-proposer legal quorum；完成数为 `1/3, 2/3, 2/3`，Judge 缺失。"
                ),
                (
                    f"- Primary 证据：row `{incident.primary_row_sha256}`；result "
                    f"`{incident.primary_results_sha256}`；manifest "
                    f"`{incident.primary_manifest_sha256}`。"
                ),
                (
                    f"- Primary actual：{int(parent['request_count'])} requests，recorded "
                    f"`${fmt(parent['recorded_cost_usd'], 9)}`，known coverage "
                    f"{fmt(parent['known_request_coverage_pct'], 4)}%。"
                ),
                (
                    f"- Replacement protocol `{INCIDENT_REPLACEMENT_PROTOCOL}`：manifest "
                    "groups 固定 `B2,G1`、task_count=10，仅调度 B2/f004、G1 产出 0 行；"
                    f"fresh attempts `{len(incident.replacement_attempt_ids)}/3`，row "
                    f"`{incident.replacement_row_sha256}`。"
                ),
                (
                    f"- Replacement actual：{int(repair['request_count'])} requests，recorded "
                    f"`${fmt(repair['recorded_cost_usd'], 9)}`，known coverage "
                    f"{fmt(repair['known_request_coverage_pct'], 4)}%。"
                ),
                (
                    f"- Replacement terminal action：`{replacement_selection.get('action')}`；"
                    "`metadata_only` 仍只能使用上文 closed cost-only exception。"
                ),
                (
                    f"- Receipt SHA-256 `{incident.spec_sha256}`；primary/replacement logical "
                    "与 physical request IDs 已验证互不重用。"
                ),
                (
                    f"- 控制面 SHA：only-keys `{incident.only_group_task_keys_sha256}`；"
                    f"expected manifest `{incident.expected_compatibility_manifest_sha256}`；"
                    f"resume runner `{incident.resume_runner_sha256}`；selection 为 1/1。"
                ),
                (
                    "- 该替换在观察失败后决定，未作多重比较修正；只能解释额外预算下"
                    "的 post-hoc sensitivity，不是 frozen 10-task primary 结果。"
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## 分组指标",
            "",
            (
                "| Arm | Observed | Scored | Completion | AvgQ scored-only | "
                "AvgQ failure-adjusted | AvgPass scored-only | JudgeErr | Avg Tokens | "
                "Avg Tools | Avg LLMReq | p50 ms | p95 ms | Note |"
            ),
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for group in ARMS:
        item = summaries[group]
        metrics = failure_metrics[group]
        note = (
            f"native EXEC_FAIL on {INCIDENT_TASK_ID[:12]}"
            if metrics["protocol_failure_rows"]
            else "all 10 scored"
        )
        lines.append(
            f"| {group} | {metrics['observed_rows']} | {metrics['scored_rows']} | "
            f"{fmt(metrics['completion_rate_pct'], 1)}% | "
            f"{fmt(metrics['scored_only_avg_quality'], 4)} | "
            f"{fmt(metrics['failure_adjusted_avg_quality'], 4)} | "
            f"{fmt(item['avg_pass_rate'], 2)} | {item['judge_errors']} | "
            f"{fmt(item['avg_total_tokens'], 1)} | {fmt(item['avg_tool_calls'], 2)} | "
            f"{fmt(item['avg_llm_requests'], 2)} | "
            f"{fmt(item['latency_p50_ms'], 0)} | {fmt(item['latency_p95_ms'], 0)} | {note} |"
        )
    lines.extend(
        [
            "",
            (
                "`AvgQ scored-only` 只平均有完整 Judge 的行；`AvgQ failure-adjusted` "
                "将 protocol failure 的 operational utility 计 0、固定分母 10。"
                "这不是为失败行生成 Judge 分数。"
            ),
        ]
    )
    lines.extend(
        [
            "",
            "## 成本与 coverage",
            "",
            (
                "| Arm | Selected Gen$ | Actual Gen$ | Judge$ | Selected LLM$ | "
                "Selected req X/E/M/U | Known% | Exact% | Actual LLM$ (LB if incomplete) | "
                "Actual req X/E/M/U | Known% | LLM complete | Full complete |"
            ),
            "|---|---:|---:|---:|---:|---|---:|---:|---:|---|---:|---|---|",
        ]
    )
    for group in ARMS:
        coverage = costs[group]
        selected = coverage["selected"]
        actual = coverage["actual"]
        selected_counts = "/".join(
            str(int(selected[key]))
            for key in (
                "exact_request_count",
                "estimated_request_count",
                "mixed_request_count",
                "unknown_request_count",
            )
        )
        actual_counts = "/".join(
            str(int(actual[key]))
            for key in (
                "exact_request_count",
                "estimated_request_count",
                "mixed_request_count",
                "unknown_request_count",
            )
        )
        lines.append(
            f"| {group} | {fmt(coverage['selected_generation']['recorded_cost_usd'], 6)} | "
            f"{fmt(coverage['actual_generation']['recorded_cost_usd'], 6)} | "
            f"{fmt(coverage['actual_judge']['recorded_cost_usd'], 6)} | "
            f"{fmt(selected['recorded_cost_usd'], 6)} | {selected_counts} | "
            f"{fmt(selected['known_request_coverage_pct'], 1)}% | "
            f"{fmt(selected['exact_request_coverage_pct'], 1)}% | "
            f"{fmt(actual['recorded_cost_usd'], 6)} | {actual_counts} | "
            f"{fmt(actual['known_request_coverage_pct'], 1)}% | "
            f"{coverage['result_llm_complete_rows']}/{coverage['selected_unit_count']} → "
            f"{coverage['actual_llm_complete_rows']}/{coverage['actual_unit_count']} | "
            f"{coverage['result_complete_rows']}/{coverage['selected_unit_count']} → "
            f"{coverage['actual_complete_rows']}/{coverage['actual_unit_count']} |"
        )
    cost_note = (
        "`X/E/M/U` 分别为 exact / estimated / mixed / unknown request。Selected LLM "
        "只含成功选中的 generation 与 Judge；三项失败 cell 经 closed gate 证明 selected "
        "request/cost 为 0。Actual LLM 包含 primary 每个 observed cell 的全部 generation "
        "attempts 与 Judge，因此保留三项失败成本。Unknown 不按 `$0` 处理。Full complete "
        "还受本地 Web 等外部工具成本证据约束；行级 actual 不含无结果 aborted shard 或 "
        "preflight，故不是 whole-account/window total。若提供 post-hoc replacement，其成本"
        "只在 sensitivity incident 节单列，不进入此 primary 表。"
    )
    lines.extend(
        [
            "",
            cost_note,
            (
                "存在 `metadata_only` 时，Actual LLM$ 必为 lower bound，且 "
                "LLM/Full complete 分母中的不完整行不会被强制改写为 complete。"
            )
            if all_metadata_pairs
            else "所有 cost-completeness 值均来自 runner accounting 的离线重算。",
            "",
            "## Primary failure-aware 同题配对比较（U，n=10）",
            "",
            "| Arm - baseline | Pairs | Mean ΔU | 95% CI | W/T/L | Seed |",
            "|---|---:|---:|---|---|---|",
        ]
    )
    for comparison in report["paired"]:
        lines.append(
            f"| {comparison['group']} - {comparison['baseline']} | "
            f"{comparison['pair_count']} | {fmt(comparison['mean_delta_quality'], 4)} | "
            f"[{fmt(comparison['ci95_low'], 4)}, {fmt(comparison['ci95_high'], 4)}] | "
            f"{comparison['wins']}/{comparison['ties']}/{comparison['losses']} | "
            f"`{comparison['seed']}` |"
        )
    lines.extend(
        [
            "",
            (
                f"每项比较包含相同 10 题。三项 EXEC_FAIL 的 operational utility `U=0`；"
                f"其余 U=Judge quality。CI 使用 task-level paired percentile bootstrap，"
                f"每项固定 seed、`{report['bootstrap_samples']}` 次重采样；未作多重比较修正。"
            ),
            "",
            "## Complete-case 共同 9 题诊断",
            "",
            "所有六臂统一剔除 f004；该表是共同 complete-case 诊断，不是 primary。",
            "",
            "| Arm - baseline | Pairs | Mean ΔQ | 95% CI | W/T/L | Seed |",
            "|---|---:|---:|---|---|---|",
        ]
    )
    for comparison in report["paired_complete_case"]:
        lines.append(
            f"| {comparison['group']} - {comparison['baseline']} | "
            f"{comparison['pair_count']} | {fmt(comparison['mean_delta_quality'], 4)} | "
            f"[{fmt(comparison['ci95_low'], 4)}, "
            f"{fmt(comparison['ci95_high'], 4)}] | "
            f"{comparison['wins']}/{comparison['ties']}/{comparison['losses']} | "
            f"`{comparison['seed']}` |"
        )
    lines.extend(
        [
            "",
            "### 共同 9 题 AvgQ 排名",
            "",
            "| Rank | Arm | Tasks | AvgQ |",
            "|---:|---|---:|---:|",
        ]
    )
    for item in report["common_task_ranking"]:
        lines.append(
            f"| {item['rank']} | {item['group']} | {item['task_count']} | "
            f"{fmt(item['avg_quality'], 4)} |"
        )
    lines.append("")
    if isinstance(incident, IncidentEvidence):
        lines.extend(
            [
                "## Post-hoc sensitivity（含 replacement）",
                "",
                "| Arm - baseline | Pairs | Mean ΔU | 95% CI | W/T/L | Seed |",
                "|---|---:|---:|---|---|---|",
            ]
        )
        for comparison in report["paired_sensitivity"]:
            if INCIDENT_GROUP not in {comparison["group"], comparison["baseline"]}:
                continue
            lines.append(
                f"| {comparison['group']} - {comparison['baseline']} | "
                f"{comparison['pair_count']} | {fmt(comparison['mean_delta_quality'], 4)} | "
                f"[{fmt(comparison['ci95_low'], 4)}, {fmt(comparison['ci95_high'], 4)}] | "
                f"{comparison['wins']}/{comparison['ties']}/{comparison['losses']} | "
                f"`{comparison['seed']}` |"
            )
        lines.extend(
            [
                "",
                (
                    "该表在观察到 primary failure 后才纳入 B2 targeted replacement；"
                    "仅用于 sensitivity，不得替代 57/60 primary、failure-aware n=10 或共同 "
                    "9 题诊断。B4/S4 原生失败仍按 U=0。"
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## 同题质量矩阵",
            "",
            "| Domain | Task | " + " | ".join(ARMS) + " |",
            "|---|---|" + "---:|" * len(ARMS),
        ]
    )
    by_key = {(str(row["group"]), str(row["task_id"])): row for row in rows}
    for task_id in task_ids:
        exemplar = by_key[(ARMS[0], task_id)]
        qualities = []
        for group in ARMS:
            key = (group, task_id)
            value = (
                "EXEC_FAIL‡" if key in native_failure_keys else fmt(by_key[key]["quality_total"], 2)
            )
            qualities.append(value)
        lines.append(
            f"| {md_escape(exemplar.get('domain') or 'Unknown')} | `{task_id[:12]}` | "
            + " | ".join(qualities)
            + " |"
        )
    lines.extend(
        [
            "",
            (
                "‡ `EXEC_FAIL` 表示 generation 预算 3/3 耗尽且 Judge 未运行；"
                "它不是 Judge 0 分。只有 failure-aware U 分析将其操作性效用计为 0。"
            ),
        ]
    )
    lines.extend(
        [
            "",
            "## Wave 取证",
            "",
            "| Ordinal | Stamp | Status | Rows | Groups | Result SHA-256 | Manifest SHA-256 |",
            "|---:|---|---|---:|---|---|---|",
        ]
    )
    for index, wave in enumerate(waves, start=1):
        lines.append(
            f"| {index} | `{wave.stamp}` | `{wave.status}` | {wave.rows_written} | "
            f"{','.join(wave.groups)} | `{wave.results_sha256}` | `{wave.manifest_sha256}` |"
        )
    if isinstance(incident, IncidentEvidence):
        replacement_wave = incident.replacement_wave
        lines.extend(
            [
                "",
                "No-history targeted resume-runner replacement（不属于 causal resume wave）：",
                "",
                "| Stamp | Status | Rows | Groups | Result SHA-256 | Manifest SHA-256 |",
                "|---|---|---:|---|---|---|",
                (
                    f"| `{replacement_wave.stamp}` | `{replacement_wave.status}` | "
                    f"{replacement_wave.rows_written} | "
                    f"{','.join(replacement_wave.groups)} | "
                    f"`{replacement_wave.results_sha256}` | "
                    f"`{replacement_wave.manifest_sha256}` |"
                ),
            ]
        )
    source = report["source_provenance"]
    lines.extend(
        [
            "",
            "## 来源与限制",
            "",
            (
                f"- Git HEAD：`{source.get('git_head')}`；source tree SHA-256："
                f"`{source.get('source_tree_sha256')}`；manifest dirty："
                f"`{source.get('git_dirty')}`。"
            ),
            (
                "- Primary 多 wave 选择使用 runner resume classifier；没有拼接 JSONL、"
                "累加 wave summary 或 simple last-row-wins。Relaxed gate 仅限上文逐项披露"
                "的 closed cost-only `metadata_only`；三项失败使用独立 exact allowlist gate。"
            ),
            (
                "- Primary 是 60 observed / 57 scored；失败成本保留、Judge 缺失不被改写。"
                "可选 fresh B2 replacement 若出现，只在单独 sensitivity 与 receipt 证据中披露。"
            ),
            (
                "- 若各臂未逐题交错执行，paired ΔQ 仍可能混入 provider/time drift；"
                "DRACO Mini 仅用于 10 题诊断。"
            ),
            "",
            f"生成时间：`{report['generated_at']}`。",
            "",
        ]
    )
    return "\n".join(lines)


def atomic_write(path: Path, text: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def self_test() -> None:
    task_ids = [
        *(f"task-{index:02d}" for index in range(EXPECTED_TASKS - 1)),
        INCIDENT_TASK_ID,
    ]
    fingerprints = {group: f"sha256:{group.lower()}" for group in ARMS}
    prompt_hashes = {task_id: f"prompt:{task_id}" for task_id in task_ids}
    task_input_hashes = {task_id: f"input:{task_id}" for task_id in task_ids}

    class SyntheticConfigRunner:
        @staticmethod
        def canonical_json_sha256(value: Any) -> str:
            return "sha256:" + canonical_object_sha256(value)

    with tempfile.TemporaryDirectory() as directory:
        config_dir = Path(directory)
        stamp = "20260819-010203"
        manifest_path = config_dir / f"draco_run_{stamp}.manifest.json"
        effective_path = config_dir / f"draco_run_{stamp}.experiment-config.effective.json"
        resolution_path = config_dir / f"draco_run_{stamp}.experiment-config.resolution.json"
        raw_config = {
            "schema_version": 1,
            "profile_id": "synthetic-profile",
            "routing": {"selection_mode": "static_openrouter"},
            "ensemble": {
                "profile_name": "synthetic-b2",
                "proposers": [{"provider": "openrouter", "model": "p1"}],
                "aggregator": {"provider": "openrouter", "model": "agg"},
                "proposer_backup_count": 2,
            },
            "runner": {"mode": "agent_loop", "concurrency": 7},
            "judge": {"model": "judge", "concurrency": 3},
            "router_dynamic_ranking_override": None,
        }
        projected_config = compatibility_experiment_config_projection(raw_config)
        raw_config_sha256 = SyntheticConfigRunner.canonical_json_sha256(raw_config)
        projected_config_sha256 = SyntheticConfigRunner.canonical_json_sha256(projected_config)
        alignment = {
            "id": "synthetic-profile",
            "effective_config_sha256": raw_config_sha256,
        }
        manifest = {
            "stamp": stamp,
            "artifacts": {
                "experiment_config_effective_json": str(effective_path),
                "experiment_config_resolution_json": str(resolution_path),
            },
            "args": {"experiment_config": str(effective_path)},
            "benchmark_alignments": {
                "global_experiment_profile": alignment,
                "B2": deepcopy(alignment),
            },
        }
        resolution = {
            "profile_id": "synthetic-profile",
            "effective_config": {
                "path": str(effective_path),
                "sha256": raw_config_sha256,
            },
        }
        effective_path.write_text(
            json.dumps(raw_config, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        resolution_path.write_text(
            json.dumps(resolution, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_contracts = {"B2": {"experiment_config": {"sha256": projected_config_sha256}}}
        loaded_config, loaded_audit = load_b2_validator_experiment_config(
            manifest_path=manifest_path,
            manifest=manifest,
            contracts=config_contracts,
            runner=SyntheticConfigRunner,
        )
        assert loaded_config == projected_config
        assert loaded_audit["contract_pin"] == projected_config_sha256
        assert "concurrency" not in loaded_config["runner"]
        assert "concurrency" not in loaded_config["judge"]
        assert "proposer_backup_count" not in loaded_config["ensemble"]
        for bad_contract in (
            {"B2": {"experiment_config": {"sha256": "sha256:" + "0" * 64}}},
            {
                "B2": {
                    "experiment_config": {
                        "sha256": projected_config_sha256,
                        "ensemble": {},
                    }
                }
            },
        ):
            try:
                load_b2_validator_experiment_config(
                    manifest_path=manifest_path,
                    manifest=manifest,
                    contracts=bad_contract,
                    runner=SyntheticConfigRunner,
                )
            except ReportError:
                pass
            else:  # pragma: no cover - hydration requires one exact config pin.
                raise AssertionError("B2 config hydration accepted a bad contract pin")
        drifted_config = deepcopy(raw_config)
        drifted_config["ensemble"]["profile_name"] = "drifted"
        drifted_raw_sha256 = SyntheticConfigRunner.canonical_json_sha256(drifted_config)
        drifted_resolution = deepcopy(resolution)
        drifted_resolution["effective_config"]["sha256"] = drifted_raw_sha256
        drifted_alignment = {
            "id": "synthetic-profile",
            "effective_config_sha256": drifted_raw_sha256,
        }
        drifted_manifest = deepcopy(manifest)
        drifted_manifest["benchmark_alignments"] = {
            "global_experiment_profile": drifted_alignment,
            "B2": deepcopy(drifted_alignment),
        }
        effective_path.write_text(
            json.dumps(drifted_config, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        resolution_path.write_text(
            json.dumps(drifted_resolution, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            load_b2_validator_experiment_config(
                manifest_path=manifest_path,
                manifest=drifted_manifest,
                contracts=config_contracts,
                runner=SyntheticConfigRunner,
            )
        except ReportError as exc:
            assert "projection differs" in str(exc)
        else:  # pragma: no cover - an authenticated-but-different config must fail.
            raise AssertionError("B2 config projection drift was admitted")

    synthetic_group_specs = {
        "B0": {
            "kind": "single",
            "model": "anthropic/claude-fable-5",
            "label": "fixed_claude_fable5",
        },
        "B1": {"kind": "router_single", "label": "single_model_routing"},
        "B2": {
            "kind": "selection_mode",
            "selection_mode": "static_openrouter",
            "label": "b2_quality_first_static_openrouter_b5",
            "experiment_config": "draco_b2_quality_first_v1",
        },
        "B4": {
            "kind": "single",
            "model": "openai/gpt-5.6-sol",
            "label": "fixed_gpt56_sol",
        },
        "G1": {
            "kind": "selection_mode",
            "selection_mode": "router_dynamic",
            "label": "ranking_router_dynamic",
        },
        "S4": {
            "kind": "router_single",
            "label": "single_model_routing_restricted_4",
            "tier_models": {
                "c0": "qwen/qwen3-8b",
                "c1": "deepseek/deepseek-v4-flash",
                "c2": "qwen/qwen3.7-plus",
                "c3": "deepseek/deepseek-v4-pro",
            },
        },
    }
    rows: list[dict[str, Any]] = []
    for task_index, task_id in enumerate(task_ids):
        for group_index, group in enumerate(ARMS):
            rows.append(
                {
                    "group": group,
                    "task_id": task_id,
                    "domain": f"D{task_index}",
                    "provider_spec": deepcopy(synthetic_group_specs[group]),
                    "prompt_sha256": prompt_hashes[task_id],
                    "task_input_sha256": task_input_hashes[task_id],
                    "error": None,
                    "selected_generation_succeeded": True,
                    "final_text": "synthetic answer",
                    "generation_attempt_count": 1,
                    "execution": {"generation_attempts": [{}]},
                    "execution_status": {
                        "status": "success",
                        "success": True,
                        "degraded_reasons": [],
                    },
                    "completion_status": {
                        "status": "complete",
                        "generation_accepted": True,
                        "judge_complete": True,
                    },
                    "quality_total": float(50 + task_index + group_index),
                    "judge": {"score_status": "complete", "judge_error_count": 0},
                    "run_compatibility_fingerprint": fingerprints[group],
                }
            )
    validate_selected_rows(rows, task_ids=task_ids, fingerprints=fingerprints)
    metadata_row = next(
        row for row in rows if (row["group"], row["task_id"]) == ("G1", task_ids[0])
    )
    metadata_state = {
        "group": "G1",
        "task_id": task_ids[0],
        "action": "metadata_only",
        "generation_valid": True,
        "judge_complete": True,
        "cost_metadata_complete": False,
        "generation_reasons": [],
        "judge_reasons": [],
        "cost_metadata_reasons": ["cost_metadata_incomplete"],
        "audit_reasons": [],
        "fatal_policy_reasons": [],
        "source_path": "synthetic.jsonl",
        "source_line": 5,
    }

    class SyntheticMetadataRunner:
        @staticmethod
        def row_cost_accounting(_row: dict[str, Any]) -> dict[str, Any]:
            actual = {
                "request_count": 3,
                "unknown_request_count": 1,
                "known_request_coverage_pct": 66.6667,
                "recorded_cost_usd": 0.125,
                "cost_complete": False,
            }
            return {"actual_llm_total": actual, "actual_llm_cost_complete": False}

    metadata_evidence = validate_reportable_resume_state(
        state=metadata_state,
        row=metadata_row,
        key=("G1", task_ids[0]),
        expected_fingerprint=fingerprints["G1"],
        runner=SyntheticMetadataRunner,
    )
    assert metadata_evidence is not None
    assert metadata_evidence["actual_llm_cost_complete"] is False
    assert metadata_evidence["actual_llm_recorded_cost_is_lower_bound"] is True
    assert (
        "recorded_cost_is_lower_bound"
        not in SyntheticMetadataRunner.row_cost_accounting(metadata_row)["actual_llm_total"]
    )

    class FalselyCompleteMetadataRunner:
        @staticmethod
        def row_cost_accounting(_row: dict[str, Any]) -> dict[str, Any]:
            actual = {
                "request_count": 3,
                "unknown_request_count": 0,
                "known_request_coverage_pct": 100.0,
                "recorded_cost_usd": 0.125,
                "cost_complete": True,
            }
            return {"actual_llm_total": actual, "actual_llm_cost_complete": True}

    try:
        validate_reportable_resume_state(
            state=metadata_state,
            row=metadata_row,
            key=("G1", task_ids[0]),
            expected_fingerprint=fingerprints["G1"],
            runner=FalselyCompleteMetadataRunner,
        )
    except ReportError as exc:
        assert "not an explicit lower bound" in str(exc)
    else:  # pragma: no cover - metadata-only cannot be relabelled complete.
        raise AssertionError("metadata-only accounting was falsely accepted as complete")
    bad_metadata_state = {
        **metadata_state,
        "cost_metadata_reasons": ["non_cost_validation_reason"],
    }
    try:
        validate_reportable_resume_state(
            state=bad_metadata_state,
            row=metadata_row,
            key=("G1", task_ids[0]),
            expected_fingerprint=fingerprints["G1"],
            runner=SyntheticMetadataRunner,
        )
    except ReportError as exc:
        assert "non-whitelisted cost reasons" in str(exc)
    else:  # pragma: no cover - the relaxed gate must remain closed.
        raise AssertionError("non-cost metadata reason was admitted")
    bad_audit_state = {**metadata_state, "audit_reasons": ["policy_warning"]}
    try:
        validate_reportable_resume_state(
            state=bad_audit_state,
            row=metadata_row,
            key=("G1", task_ids[0]),
            expected_fingerprint=fingerprints["G1"],
            runner=SyntheticMetadataRunner,
        )
    except ReportError as exc:
        assert "audit_reasons" in str(exc)
    else:  # pragma: no cover - audit reasons cannot enter a cost-only exception.
        raise AssertionError("audit reason was admitted as cost-only metadata")
    for rejected_action in ("regenerate", "judge_only", "audit_only"):
        try:
            validate_reportable_resume_state(
                state={**metadata_state, "action": rejected_action},
                row=metadata_row,
                key=("G1", task_ids[0]),
                expected_fingerprint=fingerprints["G1"],
                runner=SyntheticMetadataRunner,
            )
        except ReportError as exc:
            assert "non-reportable resume action" in str(exc)
        else:  # pragma: no cover - only complete/closed metadata are reportable.
            raise AssertionError(f"{rejected_action} action was admitted")

    synthetic_replacement_row = {
        **metadata_row,
        "group": INCIDENT_GROUP,
        "task_id": INCIDENT_TASK_ID,
        "run_compatibility_fingerprint": fingerprints[INCIDENT_GROUP],
    }
    synthetic_replacement_state = {
        **metadata_state,
        "group": INCIDENT_GROUP,
        "task_id": INCIDENT_TASK_ID,
    }

    class SyntheticStates(dict):
        def consume_row(self, key: tuple[str, str]) -> dict[str, Any]:
            assert key == INCIDENT_KEY
            return synthetic_replacement_row

        def close(self, *, verify: bool = False) -> None:
            assert verify is True

    class SyntheticReplacementRunner(SyntheticMetadataRunner):
        @staticmethod
        def load_resume_group_task_states(**_kwargs: Any):
            return SyntheticStates({INCIDENT_KEY: synthetic_replacement_state}), {"synthetic": True}

    selected_replacement, selected_replacement_state = select_replacement_row(
        result_path=Path("synthetic-replacement.jsonl"),
        incident_key=INCIDENT_KEY,
        prompt_hashes={},
        task_input_hashes={},
        fingerprints=fingerprints,
        contracts={INCIDENT_GROUP: {"cost_policy": {}}},
        runner=SyntheticReplacementRunner,
    )
    assert selected_replacement is synthetic_replacement_row
    assert selected_replacement_state["action"] == "metadata_only"
    assert selected_replacement_state["metadata_only_pair"] is not None
    comparisons = paired_quality_comparisons(rows, bootstrap_samples=200)
    assert len(comparisons) == 9
    assert all(comparison["pair_count"] == EXPECTED_TASKS for comparison in comparisons)
    assert comparisons[0]["mean_delta_quality"] == "1.0"
    summary_groups = {
        group: {
            "rows": 10,
            "completed": 10,
            "scored_rows": 10,
            "avg_quality": 50.0,
            "avg_pass_rate": 75.0,
            "judge_errors": 0,
            "avg_visible_tokens": 100.0,
            "avg_reasoning_tokens": 20.0,
            "avg_total_tokens": 200.0,
            "avg_tool_calls": 1.0,
            "tool_call_rate_pct": 50.0,
            "avg_trajectory_steps": 3.0,
            "avg_llm_requests": 2.0,
            "latency_p50_ms": 1000.0,
            "latency_p95_ms": 2000.0,
            "recorded_generation_cost_usd": 1.0,
            "actual_spend_generation_cost_usd": 1.2,
            "recorded_judge_cost_usd": 0.5,
            "recorded_candidate_judge_cost_usd": 0.0,
        }
        for group in ARMS
    }
    account = {
        "recorded_cost_usd": 1.5,
        "exact_request_count": 20,
        "estimated_request_count": 0,
        "mixed_request_count": 0,
        "unknown_request_count": 0,
        "known_request_coverage_pct": 100.0,
        "exact_request_coverage_pct": 100.0,
        "cost_complete": True,
        "recorded_cost_is_lower_bound": False,
    }
    cost_coverage = {
        group: {
            "selected": dict(account),
            "actual": dict(account),
            "selected_generation": dict(account),
            "actual_generation": dict(account),
            "actual_judge": dict(account),
            "selected_unit_count": 10,
            "actual_unit_count": 10,
            "result_llm_complete_rows": 10,
            "actual_llm_complete_rows": 10,
            "result_complete_rows": 10,
            "actual_complete_rows": 10,
        }
        for group in ARMS
    }
    cost_coverage["G1"]["actual"] = {
        **account,
        "unknown_request_count": 1,
        "known_request_coverage_pct": 95.0,
        "cost_complete": False,
        "recorded_cost_is_lower_bound": True,
    }
    cost_coverage["G1"]["actual_llm_complete_rows"] = 9
    cost_coverage["G1"]["actual_complete_rows"] = 9
    validate_metadata_only_cost_coverage(cost_coverage, [metadata_evidence])
    wave = WaveEvidence(
        results_path=Path("initial.jsonl"),
        trace_path=Path("trace.jsonl"),
        checkpoint_path=Path("checkpoint.json"),
        manifest_path=Path("manifest.json"),
        stamp="20260819-000000",
        status="complete",
        rows_written=60,
        groups=ARMS,
        started_at=0,
        finished_at=1,
        results_sha256="result-sha",
        manifest_sha256="manifest-sha",
    )
    broken = [dict(row) for row in rows]
    broken[0] = {**broken[0], "quality_total": None}
    try:
        validate_selected_rows(broken, task_ids=task_ids, fingerprints=fingerprints)
    except ReportError as exc:
        assert "quality_missing" in str(exc)
    else:  # pragma: no cover - explicit regression assertion.
        raise AssertionError("missing quality did not fail")

    primary_rows = deepcopy(rows)
    synthetic_failure_accounts: dict[tuple[str, str], dict[str, Any]] = {}
    for group_index, policy in enumerate(INCIDENT_POLICIES, 1):
        target = next(row for row in primary_rows if (row["group"], row["task_id"]) == policy.key)
        errors = policy.expected_attempt_errors
        attempts = [
            {
                "attempt_id": f"{group_index * 10 + index:032x}",
                "attempt": index,
                "run": {
                    "error": error,
                    "usage": {"physical_attempt_id": f"{group_index * 100 + index:032x}"},
                },
            }
            for index, error in enumerate(errors, 1)
        ]
        target.update(
            {
                "error": errors[-1],
                "selected_generation_succeeded": False,
                "final_text": "",
                "quality_total": None,
                "judge": None,
                "generation_attempt_count": 3,
                "generation_attempt_budget_used": 3,
                "generation_attempt_budget_limit": 3,
                "execution": {
                    "prior_generation_attempts_used": 0,
                    "generation_attempt_budget_remaining": 0,
                    "generation_attempt_count": 3,
                    "generation_attempts": attempts,
                },
                "execution_status": {
                    "status": "execution_failed",
                    "success": False,
                    "degraded_reasons": [],
                },
                "completion_status": {
                    "status": "incomplete",
                    "generation_accepted": False,
                    "judge_complete": False,
                },
            }
        )
        unknown_count = policy.expected_actual_llm_unknown_request_count
        synthetic_failure_accounts[policy.key] = {
            "request_count": policy.expected_actual_llm_request_count,
            "unknown_request_count": unknown_count,
            "known_request_coverage_pct": (
                100
                * (policy.expected_actual_llm_request_count - unknown_count)
                / policy.expected_actual_llm_request_count
            ),
            "recorded_cost_usd": float(policy.expected_recorded_actual_llm_cost_usd),
            "cost_complete": unknown_count == 0,
        }

    class SyntheticFailureRunner:
        @staticmethod
        def row_cost_accounting(row: dict[str, Any]) -> dict[str, Any]:
            key = (str(row["group"]), str(row["task_id"]))
            actual = dict(synthetic_failure_accounts[key])
            selected = {
                "request_count": 0,
                "unknown_request_count": 0,
                "known_request_coverage_pct": 100.0,
                "recorded_cost_usd": 0.0,
                "cost_complete": True,
            }
            return {"llm_total": selected, "actual_llm_total": actual}

    validate_primary_rows_with_incidents(
        primary_rows,
        task_ids=task_ids,
        fingerprints=fingerprints,
        prompt_hashes=prompt_hashes,
        task_input_hashes=task_input_hashes,
        policies=INCIDENT_POLICIES,
        runner=SyntheticFailureRunner,
    )
    failure_keys = frozenset(policy.key for policy in INCIDENT_POLICIES)
    failure_metrics = failure_aware_group_metrics(
        primary_rows,
        failure_keys=failure_keys,
    )
    assert sum(item["scored_rows"] for item in failure_metrics.values()) == 57
    for group in ARMS:
        expected_scored = 9 if group in {"B2", "B4", "S4"} else 10
        assert failure_metrics[group]["scored_rows"] == expected_scored
        expected_rate = "90" if expected_scored == 9 else "100"
        assert Decimal(failure_metrics[group]["completion_rate_pct"]) == Decimal(expected_rate)
    assert Decimal(failure_metrics["B2"]["scored_only_avg_quality"]) == Decimal(56)
    assert Decimal(failure_metrics["B2"]["failure_adjusted_avg_quality"]) == Decimal("50.4")
    assert (
        failure_metrics["B0"]["scored_only_avg_quality"]
        == failure_metrics["B0"]["failure_adjusted_avg_quality"]
    )

    failure_aware = paired_quality_comparisons(
        primary_rows,
        bootstrap_samples=200,
        failure_keys=failure_keys,
        analysis_label="failure-aware-primary",
    )
    assert all(item["pair_count"] == 10 for item in failure_aware)
    b2_vs_b0 = next(
        item for item in failure_aware if (item["group"], item["baseline"]) == ("B2", "B0")
    )
    assert Decimal(b2_vs_b0["mean_delta_quality"]) == Decimal("-4.1")
    common_excluded = frozenset((group, INCIDENT_TASK_ID) for group in ARMS)
    complete_case = paired_quality_comparisons(
        primary_rows,
        bootstrap_samples=200,
        excluded_keys=common_excluded,
        analysis_label="complete-case-common-9-task",
    )
    assert all(item["pair_count"] == 9 for item in complete_case)
    b2_vs_b0_complete = next(
        item for item in complete_case if (item["group"], item["baseline"]) == ("B2", "B0")
    )
    assert Decimal(b2_vs_b0_complete["mean_delta_quality"]) == Decimal(2)
    common_ranking = common_task_ranking(
        primary_rows,
        excluded_task_ids=frozenset({INCIDENT_TASK_ID}),
    )
    assert len(common_ranking) == len(ARMS)
    assert all(item["task_count"] == 9 for item in common_ranking)

    sources = [
        {
            "group": policy.group,
            "task_id": policy.task_id,
            "source_path": "initial.jsonl",
            "source_line": index,
            "row_sha256": canonical_object_sha256(
                next(row for row in primary_rows if (row["group"], row["task_id"]) == policy.key)
            ),
        }
        for index, policy in enumerate(INCIDENT_POLICIES, 1)
    ]
    native_failures = build_native_failure_evidence(
        primary_rows,
        selected_sources=sources,
        policies=INCIDENT_POLICIES,
        runner=SyntheticFailureRunner,
    )
    assert len(native_failures) == 3
    for policy in INCIDENT_POLICIES:
        exact_state = {
            "action": "regenerate",
            "generation_valid": False,
            "judge_complete": False,
            "generation_reasons": list(policy.expected_generation_reasons),
            "judge_reasons": list(policy.expected_judge_reasons),
            "cost_metadata_reasons": list(policy.expected_cost_metadata_reasons),
            "audit_reasons": list(policy.expected_audit_reasons),
            "fatal_policy_reasons": [],
        }
        validate_native_failure_resume_state(
            exact_state,
            key=policy.key,
            policy=policy,
        )
        try:
            validate_native_failure_resume_state(
                {
                    **exact_state,
                    "generation_reasons": [
                        *policy.expected_generation_reasons,
                        "unexpected_reason",
                    ],
                },
                key=policy.key,
                policy=policy,
            )
        except ReportError as exc:
            assert "differ from frozen tuple" in str(exc)
        else:  # pragma: no cover - unknown classifier reasons are fatal.
            raise AssertionError("native failure state reason allowlist was open")

    original_classifier_states: list[dict[str, Any]] = []
    hydrated_classifier_states: list[dict[str, Any]] = []
    for source_line, row in enumerate(primary_rows, 1):
        key = (str(row["group"]), str(row["task_id"]))
        source = {
            "group": key[0],
            "task_id": key[1],
            "source_path": "synthetic.jsonl",
            "source_line": source_line,
            "row_sha256": canonical_object_sha256(row),
        }
        if key in failure_keys:
            policy = INCIDENT_POLICY_BY_KEY[key]
            hydrated_state = {
                **source,
                "action": "regenerate",
                "generation_valid": False,
                "judge_complete": False,
                "cost_metadata_complete": False,
                "generation_reasons": list(policy.expected_generation_reasons),
                "judge_reasons": list(policy.expected_judge_reasons),
                "cost_metadata_reasons": list(policy.expected_cost_metadata_reasons),
                "audit_reasons": list(policy.expected_audit_reasons),
                "fatal_policy_reasons": [],
            }
            original_state = deepcopy(hydrated_state)
            if key == INCIDENT_KEY:
                original_state["generation_reasons"] = [
                    B2_VALIDATOR_FALSE_REASON if reason == B2_INCIDENT_CONTRACT_REASON else reason
                    for reason in policy.expected_generation_reasons
                ]
        else:
            hydrated_state = {
                **source,
                "action": "complete",
                "generation_valid": True,
                "judge_complete": True,
                "cost_metadata_complete": True,
                "generation_reasons": [],
                "judge_reasons": [],
                "cost_metadata_reasons": [],
                "audit_reasons": [],
                "fatal_policy_reasons": [],
            }
            original_state = deepcopy(hydrated_state)
            if key[0] == "B2":
                original_state.update(
                    {
                        "action": "regenerate",
                        "generation_valid": False,
                        "judge_complete": False,
                        "cost_metadata_complete": False,
                        "generation_reasons": [B2_VALIDATOR_FALSE_REASON],
                    }
                )
        original_classifier_states.append(original_state)
        hydrated_classifier_states.append(hydrated_state)
    corrected_b2 = validate_b2_validator_domain_delta(
        original_rows=primary_rows,
        original_states=original_classifier_states,
        hydrated_rows=primary_rows,
        hydrated_states=hydrated_classifier_states,
        task_ids=task_ids,
        fingerprints=fingerprints,
        prompt_hashes=prompt_hashes,
        task_input_hashes=task_input_hashes,
    )
    assert corrected_b2 == sorted(("B2", task_id) for task_id in task_ids)

    bad_delta_states = deepcopy(hydrated_classifier_states)
    bad_delta = next(
        state
        for state in bad_delta_states
        if (state["group"], state["task_id"]) == ("B2", task_ids[0])
    )
    bad_delta["generation_reasons"] = ["unexpected_reason"]
    try:
        validate_b2_validator_domain_delta(
            original_rows=primary_rows,
            original_states=original_classifier_states,
            hydrated_rows=primary_rows,
            hydrated_states=bad_delta_states,
            task_ids=task_ids,
            fingerprints=fingerprints,
            prompt_hashes=prompt_hashes,
            task_input_hashes=task_input_hashes,
        )
    except ReportError as exc:
        assert "did not become exactly complete" in str(exc)
    else:  # pragma: no cover - hydration cannot suppress any other reason.
        raise AssertionError("B2 hydration admitted an extra generation reason")

    changed_source_states = deepcopy(hydrated_classifier_states)
    changed_source_states[0]["source_line"] = 999
    try:
        validate_b2_validator_domain_delta(
            original_rows=primary_rows,
            original_states=original_classifier_states,
            hydrated_rows=primary_rows,
            hydrated_states=changed_source_states,
            task_ids=task_ids,
            fingerprints=fingerprints,
            prompt_hashes=prompt_hashes,
            task_input_hashes=task_input_hashes,
        )
    except ReportError as exc:
        assert "changed selected source/line/row SHA" in str(exc)
    else:  # pragma: no cover - selection identity must not change.
        raise AssertionError("B2 hydration admitted changed source selection")

    bad_surface_rows = deepcopy(primary_rows)
    bad_surface = next(
        row for row in bad_surface_rows if (row["group"], row["task_id"]) == ("B2", task_ids[0])
    )
    bad_surface["error"] = "hidden failure"
    bad_surface_sha = canonical_object_sha256(bad_surface)
    bad_surface_original_states = deepcopy(original_classifier_states)
    bad_surface_hydrated_states = deepcopy(hydrated_classifier_states)
    for states in (bad_surface_original_states, bad_surface_hydrated_states):
        target_state = next(
            state for state in states if (state["group"], state["task_id"]) == ("B2", task_ids[0])
        )
        target_state["row_sha256"] = bad_surface_sha
    try:
        validate_b2_validator_domain_delta(
            original_rows=bad_surface_rows,
            original_states=bad_surface_original_states,
            hydrated_rows=bad_surface_rows,
            hydrated_states=bad_surface_hydrated_states,
            task_ids=task_ids,
            fingerprints=fingerprints,
            prompt_hashes=prompt_hashes,
            task_input_hashes=task_input_hashes,
        )
    except ReportError as exc:
        assert "not independently complete" in str(exc)
    else:  # pragma: no cover - surface failures are never validator-only.
        raise AssertionError("B2 hydration admitted a surface-incomplete row")

    b2_failure = next(row for row in primary_rows if (row["group"], row["task_id"]) == INCIDENT_KEY)
    assert _strict_quorum_completed_counts(b2_failure) == (1, 2, 2)
    assert len(_attempt_ids(b2_failure, label="synthetic")) == 3
    assert len(_physical_attempt_ids(b2_failure, label="synthetic")) == 3

    def assert_failure_rejected(
        changed_rows: list[dict[str, Any]],
        *,
        expected_fragment: str,
        runner: Any = SyntheticFailureRunner,
    ) -> None:
        try:
            validate_primary_rows_with_incidents(
                changed_rows,
                task_ids=task_ids,
                fingerprints=fingerprints,
                prompt_hashes=prompt_hashes,
                task_input_hashes=task_input_hashes,
                policies=INCIDENT_POLICIES,
                runner=runner,
            )
        except ReportError as exc:
            assert expected_fragment in str(exc), str(exc)
        else:  # pragma: no cover - every mutation must remain fail-closed.
            raise AssertionError(f"native failure mutation admitted: {expected_fragment}")

    bad_budget = deepcopy(primary_rows)
    next(row for row in bad_budget if (row["group"], row["task_id"]) == INCIDENT_KEY)[
        "generation_attempt_budget_used"
    ] = 2
    assert_failure_rejected(bad_budget, expected_fragment="budget_used_not_3")

    bad_prompt_hash = deepcopy(primary_rows)
    next(row for row in bad_prompt_hash if (row["group"], row["task_id"]) == B4_INCIDENT_KEY)[
        "prompt_sha256"
    ] = "drifted"
    assert_failure_rejected(bad_prompt_hash, expected_fragment="prompt_hash_mismatch")

    bad_error = deepcopy(primary_rows)
    bad_b4 = next(row for row in bad_error if (row["group"], row["task_id"]) == B4_INCIDENT_KEY)
    bad_b4["execution"]["generation_attempts"][1]["run"]["error"] = "different"
    assert_failure_rejected(bad_error, expected_fragment="attempt_errors_mismatch")

    bad_top_error = deepcopy(primary_rows)
    next(row for row in bad_top_error if (row["group"], row["task_id"]) == B4_INCIDENT_KEY)[
        "error"
    ] = "different top-level error"
    assert_failure_rejected(
        bad_top_error,
        expected_fragment="top_level_attempt_error_mismatch",
    )

    fake_quality = deepcopy(primary_rows)
    next(row for row in fake_quality if (row["group"], row["task_id"]) == S4_INCIDENT_KEY)[
        "quality_total"
    ] = "junk"
    assert_failure_rejected(fake_quality, expected_fragment="quality_unexpected")

    fake_judge = deepcopy(primary_rows)
    next(row for row in fake_judge if (row["group"], row["task_id"]) == S4_INCIDENT_KEY)[
        "judge"
    ] = {"score_status": "complete", "judge_error_count": 0}
    assert_failure_rejected(fake_judge, expected_fragment="judge_unexpected")

    extra_failure = deepcopy(primary_rows)
    extra = next(
        row for row in extra_failure if (row["group"], row["task_id"]) == ("B1", INCIDENT_TASK_ID)
    )
    extra.update(
        {
            "error": "unauthorized",
            "selected_generation_succeeded": False,
            "final_text": "",
            "quality_total": None,
            "judge": None,
        }
    )
    assert_failure_rejected(extra_failure, expected_fragment="error:B1/")

    class BadAccountingRunner(SyntheticFailureRunner):
        @staticmethod
        def row_cost_accounting(row: dict[str, Any]) -> dict[str, Any]:
            result = deepcopy(SyntheticFailureRunner.row_cost_accounting(row))
            if (row["group"], row["task_id"]) == B4_INCIDENT_KEY:
                result["actual_llm_total"]["request_count"] += 1
            return result

    assert_failure_rejected(
        primary_rows,
        expected_fragment="actual_request_count_mismatch",
        runner=BadAccountingRunner,
    )

    class SelectedSpendRunner(SyntheticFailureRunner):
        @staticmethod
        def row_cost_accounting(row: dict[str, Any]) -> dict[str, Any]:
            result = deepcopy(SyntheticFailureRunner.row_cost_accounting(row))
            if (row["group"], row["task_id"]) == B4_INCIDENT_KEY:
                result["llm_total"]["request_count"] = 1
                result["llm_total"]["recorded_cost_usd"] = 0.01
            return result

    assert_failure_rejected(
        primary_rows,
        expected_fragment="failed_cell_selected_request_count_nonzero",
        runner=SelectedSpendRunner,
    )

    class WrongCostRunner(SyntheticFailureRunner):
        @staticmethod
        def row_cost_accounting(row: dict[str, Any]) -> dict[str, Any]:
            result = deepcopy(SyntheticFailureRunner.row_cost_accounting(row))
            if (row["group"], row["task_id"]) == S4_INCIDENT_KEY:
                result["actual_llm_total"]["recorded_cost_usd"] += 0.01
            return result

    assert_failure_rejected(
        primary_rows,
        expected_fragment="recorded_actual_cost_mismatch",
        runner=WrongCostRunner,
    )

    for group in ("B2", "B4", "S4"):
        summary_groups[group]["completed"] = 9
        summary_groups[group]["scored_rows"] = 9
        summary_groups[group]["avg_quality"] = float(
            failure_metrics[group]["scored_only_avg_quality"]
        )
    arm_contracts = {
        group: {"group_spec": deepcopy(synthetic_group_specs[group])} for group in ARMS
    }
    arm_manifest = {"group_specs": deepcopy(synthetic_group_specs)}
    arm_b2_config = {
        "ensemble": {
            "proposers": [
                {"provider": "openrouter", "model": f"proposer-{index}", "k": 1}
                for index in range(4)
            ],
            "aggregator": {"provider": "openrouter", "model": "z-ai/glm-5.2"},
        }
    }
    arm_definitions = build_arm_definitions(
        manifest=arm_manifest,
        contracts=arm_contracts,
        b2_experiment_config=arm_b2_config,
        rows=primary_rows,
    )
    assert [item["arm"] for item in arm_definitions] == list(ARMS)
    assert "qwen/qwen3-8b" in arm_definitions[-1]["definition"]
    bad_arm_manifest = deepcopy(arm_manifest)
    bad_arm_manifest["group_specs"]["S4"]["tier_models"]["c0"] = "drifted"
    try:
        build_arm_definitions(
            manifest=bad_arm_manifest,
            contracts=arm_contracts,
            b2_experiment_config=arm_b2_config,
            rows=primary_rows,
        )
    except ReportError as exc:
        assert "manifest/contract group spec differs" in str(exc)
    else:  # pragma: no cover - prose must stay bound to the frozen S4 roster.
        raise AssertionError("arm definition admitted S4 manifest drift")
    bad_provider_rows = deepcopy(primary_rows)
    bad_provider_rows[0]["provider_spec"]["label"] = "drifted"
    try:
        build_arm_definitions(
            manifest=arm_manifest,
            contracts=arm_contracts,
            b2_experiment_config=arm_b2_config,
            rows=bad_provider_rows,
        )
    except ReportError as exc:
        assert "selected row provider spec differs" in str(exc)
    else:  # pragma: no cover - row surfaces cannot disagree with the prose source.
        raise AssertionError("arm definition admitted provider-spec drift")
    bad_b2_arm_config = deepcopy(arm_b2_config)
    bad_b2_arm_config["ensemble"]["aggregator"]["model"] = "wrong"
    try:
        build_arm_definitions(
            manifest=arm_manifest,
            contracts=arm_contracts,
            b2_experiment_config=bad_b2_arm_config,
            rows=primary_rows,
        )
    except ReportError as exc:
        assert "aggregator is not OpenRouter GLM 5.2" in str(exc)
    else:  # pragma: no cover - B2 prose is bound to the authenticated config.
        raise AssertionError("arm definition admitted a different B2 aggregator")
    document = render_markdown(
        {
            "summary": {"groups": summary_groups},
            "failure_metrics": failure_metrics,
            "native_failures": native_failures,
            "cost_coverage": cost_coverage,
            "rows": primary_rows,
            "arm_definitions": arm_definitions,
            "task_ids": task_ids,
            "waves": [wave],
            "paired": failure_aware,
            "paired_complete_case": complete_case,
            "common_task_ranking": common_ranking,
            "paired_sensitivity": [],
            "bootstrap_samples": 200,
            "input_sha256": "input-sha",
            "selection": {
                "action_counts": {
                    "complete": 56,
                    "metadata_only": 1,
                    "regenerate": 3,
                },
                "metadata_only_pairs": [metadata_evidence],
                "validator_domain_correction": {
                    "applied": True,
                    "b2_applied": True,
                    "b2_corrected_keys": [f"B2/{task_id}" for task_id in task_ids],
                    "b2_config_hydration_proof": {
                        "compatibility_projection_sha256": projected_config_sha256,
                        "contract_pin": projected_config_sha256,
                    },
                    "b2_unpatched_action_counts": {"regenerate": 10},
                    "b2_hydrated_action_counts": {
                        "complete": 9,
                        "regenerate": 1,
                    },
                    "g1_applied": False,
                    "patched_action_counts": {
                        "complete": 56,
                        "metadata_only": 1,
                        "regenerate": 3,
                    },
                },
            },
            "incident": None,
            "source_provenance": {},
            "generated_at": "2026-08-19T00:00:00+00:00",
        }
    )
    assert document.startswith("# DRACO Mini")
    assert "可评分 `57/60`" in document
    assert "60/60 scored" not in document
    assert "## 成本与 coverage" in document
    assert "## 实验臂定义" in document
    assert "single_model_routing_restricted_4" in document
    assert "deepseek/deepseek-v4-pro" in document
    assert "## Relaxed cost-metadata audit" in document
    assert "## Primary failure-aware 同题配对比较（U，n=10）" in document
    assert "## Complete-case 共同 9 题诊断" in document
    assert document.count("EXEC_FAIL‡") == 3
    assert "AvgQ failure-adjusted" in document
    assert "Primary 未使用 fresh replacement" in document
    assert "missing_expected_b2_ensemble_contract" in document
    assert B2_INCIDENT_CONTRACT_REASON in document
    assert f"`G1/{task_ids[0]}`" in document
    assert "complete=false" in document
    receipt = {
        "schema": INCIDENT_SCHEMA,
        "incident_id": "synthetic",
        "group": INCIDENT_GROUP,
        "task_id": INCIDENT_TASK_ID,
        "primary": {
            "results_sha256": "1" * 64,
            "manifest_sha256": "2" * 64,
            "row_canonical_sha256": "3" * 64,
            "run_compatibility_fingerprint": "sha256:" + "4" * 64,
            "generation_attempt_budget_used": 3,
            "generation_attempt_budget_limit": 3,
            "completed_proposer_counts": [1, 2, 2],
            "actual_llm_request_count": 12,
            "actual_llm_unknown_request_count": 1,
            "recorded_actual_llm_cost_usd": "0.534564759",
        },
        "replacement": {
            "protocol": INCIDENT_REPLACEMENT_PROTOCOL,
            "results_sha256": "5" * 64,
            "manifest_sha256": "6" * 64,
            "row_canonical_sha256": "7" * 64,
            "run_compatibility_fingerprint": "sha256:" + "4" * 64,
            "fresh_generation_attempt_budget_limit": 3,
            "groups": ["B2", "G1"],
            "task_count": 10,
            "rows_written": 1,
            "only_group_task_keys_recorded_path": "/frozen/only.jsonl",
            "only_group_task_keys_raw_sha256": "8" * 64,
            "expected_compatibility_manifest_recorded_path": "/frozen/manifest.json",
            "expected_compatibility_manifest_raw_sha256": "9" * 64,
            "resume_runner_raw_sha256": "a" * 64,
            "resume_selection": expected_incident_resume_selection(),
        },
        "analysis_policy": {
            "confirmatory": "exclude_incident_cell",
            "sensitivity": "include_replacement_post_hoc",
        },
    }
    with tempfile.TemporaryDirectory() as directory:
        receipt_path = Path(directory) / "incident.json"
        exclusive_write(receipt_path, canonical_receipt_text(receipt))
        assert load_incident_spec(receipt_path) == receipt
        try:
            exclusive_write(receipt_path, canonical_receipt_text(receipt))
        except FileExistsError:
            pass
        else:  # pragma: no cover - O_EXCL is a core authorization guarantee.
            raise AssertionError("incident receipt overwrite was not rejected")
    print(
        "self-test: ok (60 observed / 57 scored; exact B2/B4/S4 3-of-3 failure "
        "allowlist; authenticated B2 validator hydration; manifest-bound arm definitions; "
        "failure-aware n=10; common complete-case n=9; EXEC_FAIL matrix; actual-vs-selected "
        "spend; closed metadata-only and validator/native-failure negatives; optional B2 "
        "receipt remains O_EXCL/post-hoc only)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--expected-manifest", type=Path)
    parser.add_argument("--results-jsonl", type=Path, action="append", default=[])
    parser.add_argument(
        "--incident-replacement-spec",
        type=Path,
        help="hashed authorization receipt for the one supported post-hoc replacement",
    )
    parser.add_argument(
        "--incident-replacement-results-jsonl",
        type=Path,
        help=(
            "no-history targeted resume-runner result; never include it in causal --results-jsonl"
        ),
    )
    parser.add_argument(
        "--incident-only-group-task-keys",
        type=Path,
        help="the exact one-row JSONL supplied to replacement --only-group-task-keys",
    )
    parser.add_argument(
        "--write-incident-receipt",
        type=Path,
        help=(
            "derive a receipt from sealed primary/replacement artifacts and create it "
            "with O_EXCL; cannot be combined with --incident-replacement-spec"
        ),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--bootstrap-samples", type=int, default=BOOTSTRAP_SAMPLES)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser


def run(args: argparse.Namespace) -> int:
    if args.self_test:
        self_test()
        return 0
    missing_options = [
        option
        for option, value in (
            ("--repo-root", args.repo_root),
            ("--input", args.input),
            ("--expected-manifest", args.expected_manifest),
            ("--results-jsonl", args.results_jsonl),
        )
        if not value
    ]
    receipt_mode = args.write_incident_receipt is not None
    if receipt_mode and args.validate_only:
        raise ReportError("--write-incident-receipt cannot be combined with --validate-only")
    if not args.validate_only and not receipt_mode and args.output is None:
        missing_options.append("--output")
    if missing_options:
        raise ReportError("missing required options: " + ", ".join(missing_options))
    if args.incident_replacement_spec is not None and receipt_mode:
        raise ReportError(
            "--incident-replacement-spec and --write-incident-receipt are mutually exclusive"
        )
    replacement_path_option = args.incident_replacement_results_jsonl
    only_keys_option = args.incident_only_group_task_keys
    authorization_option = args.incident_replacement_spec or args.write_incident_receipt
    incident_options = (replacement_path_option, only_keys_option, authorization_option)
    if any(incident_options) and not all(incident_options):
        raise ReportError(
            "incident mode requires replacement results, the exact only-keys file, and "
            "either an existing receipt or --write-incident-receipt"
        )
    incident_enabled = all(incident_options)
    result_paths = [path.resolve() for path in args.results_jsonl]
    if len(result_paths) != len(set(result_paths)):
        raise ReportError("duplicate --results-jsonl paths")
    replacement_path = (
        args.incident_replacement_results_jsonl.resolve() if incident_enabled else None
    )
    only_keys_path = args.incident_only_group_task_keys.resolve() if incident_enabled else None
    if replacement_path is not None and replacement_path in set(result_paths):
        raise ReportError(
            "no-history targeted replacement must not also appear in causal --results-jsonl"
        )
    helpers = load_repo_helpers(args.repo_root)
    expected_manifest, fingerprints, contracts = expected_compatibility(
        args.expected_manifest.resolve()
    )
    b2_experiment_config, b2_config_audit = load_b2_validator_experiment_config(
        manifest_path=args.expected_manifest.resolve(),
        manifest=expected_manifest,
        contracts=contracts,
        runner=helpers.runner,
    )
    _tasks, task_ids, prompt_hashes, task_input_hashes = load_tasks_and_hashes(
        args.input.resolve(),
        runner=helpers.runner,
        expected_manifest=expected_manifest,
    )
    waves = [
        validate_wave(
            path,
            helpers=helpers,
            expected_fingerprints=fingerprints,
        )
        for path in result_paths
    ]
    expected_manifest_sha256 = file_sha256(args.expected_manifest.resolve())
    if not any(wave.manifest_sha256 == expected_manifest_sha256 for wave in waves):
        raise ReportError(
            "--expected-manifest bytes are not one of the validated causal primary manifests"
        )
    if INCIDENT_TASK_ID not in task_ids:
        raise ReportError("frozen native-failure task is absent from the 10-task input")
    native_failure_keys = frozenset(policy.key for policy in INCIDENT_POLICIES)
    primary_rows, selection = select_primary_rows(
        result_paths=result_paths,
        task_ids=task_ids,
        prompt_hashes=prompt_hashes,
        task_input_hashes=task_input_hashes,
        fingerprints=fingerprints,
        contracts=contracts,
        b2_experiment_config=b2_experiment_config,
        b2_config_audit=b2_config_audit,
        runner=helpers.runner,
        incident_keys=native_failure_keys,
    )
    validate_primary_rows_with_incidents(
        primary_rows,
        task_ids=task_ids,
        fingerprints=fingerprints,
        prompt_hashes=prompt_hashes,
        task_input_hashes=task_input_hashes,
        policies=INCIDENT_POLICIES,
        runner=helpers.runner,
    )
    native_failures = build_native_failure_evidence(
        primary_rows,
        selected_sources=selection["selected_sources"],
        policies=INCIDENT_POLICIES,
        runner=helpers.runner,
    )
    incident: IncidentEvidence | None = None
    paired_sensitivity: list[dict[str, Any]] = []
    posthoc_rows: list[dict[str, Any]] | None = None
    if incident_enabled:
        if replacement_path is None:  # Defensive: paired option validation above is fail-closed.
            raise ReportError("incident replacement result path is missing")
        replacement_wave = validate_wave(
            replacement_path,
            helpers=helpers,
            expected_fingerprints=fingerprints,
        )
        replacement_contracts = {group: deepcopy(dict(contracts[group])) for group in ARMS}
        replacement_contracts["B2"]["experiment_config"] = deepcopy(b2_experiment_config)
        replacement_row, replacement_selection = select_replacement_row(
            result_path=replacement_path,
            incident_key=INCIDENT_KEY,
            prompt_hashes=prompt_hashes,
            task_input_hashes=task_input_hashes,
            fingerprints=fingerprints,
            contracts=replacement_contracts,
            runner=helpers.runner,
        )
        primary_by_key = {(str(row["group"]), str(row["task_id"])): row for row in primary_rows}
        primary_sources = [
            item
            for item in selection["selected_sources"]
            if (item["group"], item["task_id"]) == INCIDENT_KEY
        ]
        if len(primary_sources) != 1:
            raise ReportError("incident primary selection source is not unique")
        source_path = Path(str(primary_sources[0].get("source_path") or "")).resolve()
        source_wave = next(
            (wave for wave in waves if wave.results_path.resolve() == source_path), None
        )
        if source_wave is None:
            raise ReportError("cannot resolve incident primary source wave")
        if only_keys_path is None:
            raise ReportError("incident only-keys evidence path is missing")
        resume_runner_path = (
            args.repo_root.resolve() / "scripts" / "run_draco_routing_experiment_resume.py"
        )
        replacement_manifest = read_json_object(
            replacement_wave.manifest_path, label="replacement manifest"
        )
        derived_spec = derive_incident_receipt(
            primary_row=primary_by_key[INCIDENT_KEY],
            primary_wave=source_wave,
            replacement_row=replacement_row,
            replacement_wave=replacement_wave,
            expected_manifest_path=args.expected_manifest.resolve(),
            only_keys_path=only_keys_path,
            resume_runner_path=resume_runner_path,
            replacement_manifest=replacement_manifest,
            runner=helpers.runner,
        )
        if receipt_mode:
            incident_spec_path = args.write_incident_receipt.resolve()
            incident_spec = derived_spec
            receipt_text = canonical_receipt_text(incident_spec)
            incident_spec_sha256 = hashlib.sha256(receipt_text.encode("utf-8")).hexdigest()
        else:
            incident_spec_path = args.incident_replacement_spec.resolve()
            incident_spec = load_incident_spec(incident_spec_path)
            incident_spec_sha256 = file_sha256(incident_spec_path)
        incident = validate_incident_replacement(
            spec_path=incident_spec_path,
            spec_sha256=incident_spec_sha256,
            spec=incident_spec,
            primary_row=primary_by_key[INCIDENT_KEY],
            primary_selection_source=primary_sources[0],
            primary_waves=waves,
            replacement_row=replacement_row,
            replacement_wave=replacement_wave,
            task_ids=task_ids,
            expected_manifest_path=args.expected_manifest.resolve(),
            only_keys_path=only_keys_path,
            resume_runner_path=resume_runner_path,
            fingerprints=fingerprints,
            runner=helpers.runner,
        )
        if receipt_mode:
            exclusive_write(incident_spec_path, receipt_text)
            if file_sha256(incident_spec_path) != incident_spec_sha256:
                raise ReportError("new incident receipt failed post-write SHA verification")
            print(f"wrote incident receipt {incident_spec_path}; sha256={incident_spec_sha256}")
            return 0
        posthoc_rows = [
            replacement_row if (str(row["group"]), str(row["task_id"])) == INCIDENT_KEY else row
            for row in primary_rows
        ]
        selection["incident_replacement"] = replacement_selection
    rows = primary_rows
    actual_ledger_rows = primary_rows
    arm_definitions = build_arm_definitions(
        manifest=expected_manifest,
        contracts=contracts,
        b2_experiment_config=b2_experiment_config,
        rows=rows,
    )
    summary = helpers.runner.summarize(rows)
    failure_count_by_group = Counter(policy.group for policy in INCIDENT_POLICIES)
    for group in ARMS:
        item = summary.get("groups", {}).get(group)
        if not isinstance(item, Mapping) or item.get("rows") != EXPECTED_TASKS:
            raise ReportError(f"summary does not contain 10 rows for {group}")
        expected_scored = EXPECTED_TASKS - failure_count_by_group[group]
        if item.get("completed") != expected_scored or item.get("scored_rows") != expected_scored:
            raise ReportError(f"summary completion gate failed for {group}")
    failure_metrics = failure_aware_group_metrics(
        rows,
        failure_keys=native_failure_keys,
    )
    costs = aggregate_cost_coverage(
        rows,
        actual_ledger_rows=actual_ledger_rows,
        runner=helpers.runner,
    )
    metadata_only_pairs = list(selection.get("metadata_only_pairs") or [])
    validate_metadata_only_cost_coverage(costs, metadata_only_pairs)
    for group in ARMS:
        if costs[group]["actual_unit_count"] != EXPECTED_TASKS:
            raise ReportError(f"actual campaign ledger unit gate failed for {group}")
    paired = paired_quality_comparisons(
        rows,
        bootstrap_samples=args.bootstrap_samples,
        failure_keys=native_failure_keys,
        analysis_label="failure-aware-primary",
    )
    common_excluded_keys = frozenset((group, INCIDENT_TASK_ID) for group in ARMS)
    paired_complete_case = paired_quality_comparisons(
        rows,
        bootstrap_samples=args.bootstrap_samples,
        excluded_keys=common_excluded_keys,
        analysis_label="complete-case-common-9-task",
    )
    common_ranking = common_task_ranking(
        rows,
        excluded_task_ids=frozenset({INCIDENT_TASK_ID}),
    )
    if incident and posthoc_rows is not None:
        posthoc_failure_keys = native_failure_keys - {INCIDENT_KEY}
        paired_sensitivity = paired_quality_comparisons(
            posthoc_rows,
            bootstrap_samples=args.bootstrap_samples,
            failure_keys=posthoc_failure_keys,
            analysis_label="post-hoc-replacement",
        )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_sha256": file_sha256(args.input.resolve()),
        "task_ids": task_ids,
        "rows": rows,
        "arm_definitions": arm_definitions,
        "waves": waves,
        "summary": summary,
        "failure_metrics": failure_metrics,
        "native_failures": native_failures,
        "cost_coverage": costs,
        "paired": paired,
        "paired_complete_case": paired_complete_case,
        "common_task_ranking": common_ranking,
        "bootstrap_samples": args.bootstrap_samples,
        "selection": selection,
        "incident": incident,
        "paired_sensitivity": paired_sensitivity,
        "source_provenance": expected_manifest.get("source_provenance") or {},
    }
    document = render_markdown(report)
    if args.validate_only:
        primary_scoring_complete = EXPECTED_PAIRS - len(INCIDENT_POLICIES)
        print(
            f"validation: ok; primary_scoring_complete="
            f"{primary_scoring_complete}/{EXPECTED_PAIRS}; "
            f"observed={len(rows)}/{EXPECTED_PAIRS}; primary_waves={len(waves)}; "
            f"metadata_only={len(metadata_only_pairs)}; "
            f"incident_replacement={bool(incident)}; comparisons={len(paired)}"
        )
        return 0
    atomic_write(args.output, document)
    print(f"wrote {args.output.resolve()}")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except ReportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
