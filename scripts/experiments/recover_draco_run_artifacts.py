#!/usr/bin/env python3
"""Recover one interrupted DRACO result shard without calling any model."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from opensquilla.eval.draco_artifact_io import (
    DRACO_RUN_MANIFEST_SCHEMA_V2,
    DracoArtifactDurabilityError,
    DracoArtifactRunLock,
    DurableDracoArtifactWriter,
    atomic_write_bytes,
    atomic_write_text,
    durable_artifact_capability_contract,
    fsync_directory,
    verify_durable_artifact_path_snapshots,
    verify_durable_draco_artifacts,
)
from opensquilla.eval.draco_selection_plan_evidence import (
    SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD,
    SELECTION_PLAN_EVIDENCE_ROW_FIELD,
    SELECTION_PLAN_PACK_ARTIFACT_FIELD,
    SelectionPlanEvidenceError,
    SelectionPlanPackReader,
    selection_plan_evidence_capability_contract,
    selection_plan_evidence_manifest_binding,
    selection_plan_reference_signal,
    selection_plan_row_capability_signal,
    validate_compact_selection_plan_evidence_row,
    validate_selection_plan_evidence_manifest_binding,
)

RECOVERY_SCHEMA = "opensquilla.draco-artifact-recovery/v1"
STAMP_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}$")
RECOVERABLE_RESUME_ACTIONS = {
    "regenerate",
    "model_regenerate",
    "judge_only",
    "metadata_only",
    "audit_only",
}


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_object(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise DracoArtifactDurabilityError(f"{label} is not a regular non-symlink file: {path}")
    payload = path.read_bytes()
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DracoArtifactDurabilityError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise DracoArtifactDurabilityError(f"{label} is not a JSON object")
    return value, payload


def _bound_standard_artifact_paths(
    manifest: Mapping[str, Any],
    manifest_path: Path,
) -> dict[str, Path]:
    """Bind recovery to one runner-owned stamp and its standard filenames."""

    artifacts = manifest.get("artifacts")
    stamp = manifest.get("stamp")
    expected_capability = durable_artifact_capability_contract()
    compatibility = manifest.get("run_compatibility")
    contracts = compatibility.get("contracts") if isinstance(compatibility, Mapping) else None
    groups = manifest.get("groups")
    if (
        manifest.get("schema") != DRACO_RUN_MANIFEST_SCHEMA_V2
        or manifest.get("durable_artifact_capability") != expected_capability
        or not isinstance(groups, list)
        or not isinstance(contracts, Mapping)
        or any(
            not isinstance(contracts.get(str(group)), Mapping)
            or contracts[str(group)].get("durable_artifact_capability") != expected_capability
            for group in groups
        )
    ):
        raise DracoArtifactDurabilityError(
            "source manifest lacks its durable v2 capability contract"
        )
    if not isinstance(artifacts, Mapping) or not isinstance(stamp, str):
        raise DracoArtifactDurabilityError("source manifest lacks its artifact map or stamp")
    if not STAMP_PATTERN.fullmatch(stamp):
        raise DracoArtifactDurabilityError("source manifest stamp is malformed")
    parent = manifest_path.parent
    expected = {
        "results_jsonl": parent / f"draco_ensemble_{stamp}.jsonl",
        "trace_jsonl": parent / f"draco_run_{stamp}.trace.jsonl",
        "checkpoint_json": parent / f"draco_run_{stamp}.checkpoint.json",
        "manifest_json": parent / f"draco_run_{stamp}.manifest.json",
    }
    for key, expected_path in expected.items():
        raw = artifacts.get(key)
        if not isinstance(raw, str) or not raw:
            raise DracoArtifactDurabilityError(f"source manifest lacks artifacts.{key}")
        if not Path(raw).is_absolute():
            raise DracoArtifactDurabilityError(f"source manifest artifacts.{key} is not absolute")
        candidate = Path(os.path.abspath(Path(raw).expanduser()))
        if candidate != expected_path:
            raise DracoArtifactDurabilityError(
                f"source manifest artifacts.{key} is not the standard stamp path"
            )
    if expected["manifest_json"] != manifest_path:
        raise DracoArtifactDurabilityError("source manifest filename is not bound to its stamp")
    capability_present = SELECTION_PLAN_EVIDENCE_ROW_FIELD in manifest
    binding_present = SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD in manifest
    pack_present = SELECTION_PLAN_PACK_ARTIFACT_FIELD in artifacts
    if capability_present or binding_present or pack_present:
        if manifest.get(SELECTION_PLAN_EVIDENCE_ROW_FIELD) != (
            selection_plan_evidence_capability_contract()
        ):
            raise DracoArtifactDurabilityError(
                "selection-plan recovery capability is incomplete"
            )
        raw_pack_path = artifacts.get(SELECTION_PLAN_PACK_ARTIFACT_FIELD)
        pack_path = parent / f"draco_run_{stamp}.selection-plan.pack.jsonl"
        if (
            not isinstance(raw_pack_path, str)
            or not Path(raw_pack_path).is_absolute()
            or Path(os.path.abspath(raw_pack_path)) != pack_path
        ):
            raise DracoArtifactDurabilityError(
                "selection-plan recovery pack is not the standard stamp path"
            )
        status = str(manifest.get("status") or "")
        binding = manifest.get(SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD)
        if status == "running":
            if binding_present:
                raise DracoArtifactDurabilityError(
                    "running selection-plan manifest contains a partial terminal binding"
                )
        elif not isinstance(binding, Mapping):
            raise DracoArtifactDurabilityError(
                "terminal selection-plan manifest lacks its durable binding"
            )
        expected[SELECTION_PLAN_PACK_ARTIFACT_FIELD] = pack_path
    return expected


def _verify_bound_artifacts(
    manifest: Mapping[str, Any],
    bound_paths: Mapping[str, Path],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Verify durable rows and compact refs in one result-fd scan."""

    pack_path = bound_paths.get(SELECTION_PLAN_PACK_ARTIFACT_FIELD)
    compact_row_count = 0

    def reject_compact_downgrade(row: Mapping[str, Any]) -> None:
        if selection_plan_row_capability_signal(row) or selection_plan_reference_signal(row):
            raise DracoArtifactDurabilityError(
                "legacy recovery row contains undeclared compact selection-plan evidence"
            )

    if pack_path is None:
        path_snapshots: dict[str, tuple[int, ...]] = {}
        verification = verify_durable_draco_artifacts(
            results_path=bound_paths["results_jsonl"],
            trace_path=bound_paths["trace_jsonl"],
            checkpoint_path=bound_paths["checkpoint_json"],
            result_row_observer=reject_compact_downgrade,
            path_snapshot_out=path_snapshots,
        )
        verify_durable_artifact_path_snapshots(
            path_snapshots,
            results_path=bound_paths["results_jsonl"],
            trace_path=bound_paths["trace_jsonl"],
            checkpoint_path=bound_paths["checkpoint_json"],
        )
        return verification, None

    try:
        with SelectionPlanPackReader(pack_path, owner_only=True) as reader:
            path_snapshots = {}
            def validate_compact_row(row: Mapping[str, Any]) -> None:
                nonlocal compact_row_count
                compact_row_count += int(
                    validate_compact_selection_plan_evidence_row(
                        row,
                        reader=reader,
                    )
                )

            verification = verify_durable_draco_artifacts(
                results_path=bound_paths["results_jsonl"],
                trace_path=bound_paths["trace_jsonl"],
                checkpoint_path=bound_paths["checkpoint_json"],
                result_row_observer=validate_compact_row,
                path_snapshot_out=path_snapshots,
            )
            reader.verify_snapshot()
            verify_durable_artifact_path_snapshots(
                path_snapshots,
                results_path=bound_paths["results_jsonl"],
                trace_path=bound_paths["trace_jsonl"],
                checkpoint_path=bound_paths["checkpoint_json"],
            )
            binding = selection_plan_evidence_manifest_binding(
                pack_index=reader.index,
                durable_artifact_verification=verification,
                compact_row_count=compact_row_count,
            )
            declared_binding = manifest.get(
                SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD
            )
            if declared_binding is not None:
                validate_selection_plan_evidence_manifest_binding(
                    declared_binding,
                    pack_index=reader.index,
                    durable_artifact_verification=verification,
                    compact_row_count=compact_row_count,
                )
            return verification, binding
    except (OSError, SelectionPlanEvidenceError) as exc:
        raise DracoArtifactDurabilityError(
            f"selection-plan recovery evidence is invalid: {exc}"
        ) from exc


def _expected_keys(manifest: Mapping[str, Any]) -> set[tuple[str, str]]:
    resume = manifest.get("resume_selection")
    scheduled = resume.get("scheduled_pairs") if isinstance(resume, Mapping) else None
    if isinstance(resume, Mapping) and not isinstance(scheduled, list):
        raise DracoArtifactDurabilityError("manifest resume scheduled_pairs is malformed")
    if isinstance(scheduled, list):
        keys: set[tuple[str, str]] = set()
        for item in scheduled:
            if not isinstance(item, Mapping):
                raise DracoArtifactDurabilityError("manifest resume scheduled pair is malformed")
            group = item.get("group")
            task_id = item.get("task_id")
            if not isinstance(group, str) or not isinstance(task_id, str):
                raise DracoArtifactDurabilityError(
                    "manifest resume scheduled pair lacks group/task_id"
                )
            keys.add((group, task_id))
        return keys
    groups = manifest.get("groups")
    task_ids = manifest.get("task_ids")
    if not isinstance(groups, list) or not isinstance(task_ids, list):
        raise DracoArtifactDurabilityError("manifest lacks the original group/task schedule")
    return {(str(group), str(task_id)) for group in groups for task_id in task_ids}


def _durable_keys_from_verification(
    verification: Mapping[str, Any],
) -> set[tuple[str, str]]:
    rows_written = verification.get("rows_written")
    raw_keys = verification.get("durable_result_keys")
    if (
        not isinstance(rows_written, int)
        or isinstance(rows_written, bool)
        or rows_written < 0
        or not isinstance(raw_keys, tuple)
        or len(raw_keys) != rows_written
    ):
        raise DracoArtifactDurabilityError(
            "recovered shard lacks its sealed durable row marker"
        )
    keys: set[tuple[str, str]] = set()
    for raw_key in raw_keys:
        if (
            not isinstance(raw_key, tuple)
            or len(raw_key) != 2
            or not all(isinstance(value, str) and value for value in raw_key)
        ):
            raise DracoArtifactDurabilityError(
                "verified shard contains a malformed durable group/task key"
            )
        key = (raw_key[0], raw_key[1])
        if key in keys:
            raise DracoArtifactDurabilityError(
                f"recovered shard has a missing or duplicate group/task key: {key!r}"
            )
        keys.add(key)
    return keys


def _recovery_ledgers(
    prior_manifest: Mapping[str, Any],
    durable_keys: set[tuple[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    expected_keys = _expected_keys(prior_manifest)
    if not durable_keys <= expected_keys:
        raise DracoArtifactDurabilityError(
            "recovered shard contains rows outside its manifest schedule"
        )
    ambiguous = [
        {"group": group, "task_id": task_id}
        for group, task_id in sorted(expected_keys - durable_keys)
    ]
    prior_resume = prior_manifest.get("resume_selection")
    prior_scheduled = (
        prior_resume.get("scheduled_pairs", []) if isinstance(prior_resume, Mapping) else []
    )
    if not isinstance(prior_scheduled, list):
        raise DracoArtifactDurabilityError("manifest resume scheduled_pairs is malformed")
    normalized_schedule: list[dict[str, str]] = []
    seen_schedule: set[tuple[str, str]] = set()
    for item in prior_scheduled:
        if not isinstance(item, Mapping):
            raise DracoArtifactDurabilityError("manifest resume scheduled pair is malformed")
        normalized = {
            "group": str(item.get("group") or ""),
            "task_id": str(item.get("task_id") or ""),
            "action": str(item.get("action") or ""),
        }
        key = (normalized["group"], normalized["task_id"])
        if (
            not all(key)
            or normalized["action"] not in RECOVERABLE_RESUME_ACTIONS
            or key in seen_schedule
        ):
            raise DracoArtifactDurabilityError(
                "manifest resume scheduled pair is not uniquely recoverable"
            )
        seen_schedule.add(key)
        normalized_schedule.append(normalized)
    durable_schedule = [
        item for item in normalized_schedule if (item["group"], item["task_id"]) in durable_keys
    ]
    return ambiguous, durable_schedule


def recover_run(manifest_path: Path) -> dict[str, Any]:
    """Seal the durable prefix and publish a finalizable terminal manifest."""

    manifest_path = manifest_path.expanduser()
    manifest, manifest_bytes = _load_object(manifest_path, label="source manifest")
    manifest_path = manifest_path.resolve(strict=True)
    observed_manifest_sha256 = _sha256_bytes(manifest_bytes)
    prior_status = str(manifest.get("status") or "")
    bound_paths = _bound_standard_artifact_paths(manifest, manifest_path)
    results_path = bound_paths["results_jsonl"]
    trace_path = bound_paths["trace_jsonl"]
    checkpoint_path = bound_paths["checkpoint_json"]
    lock_path = checkpoint_path.with_suffix(checkpoint_path.suffix + ".lock")
    with DracoArtifactRunLock(lock_path):
        # Re-read after acquiring the lock so a racing terminal publisher cannot
        # be overwritten by stale recovery state.
        manifest, manifest_bytes = _load_object(manifest_path, label="source manifest")
        locked_status = str(manifest.get("status") or "")
        if (
            locked_status != prior_status
            or _sha256_bytes(manifest_bytes) != observed_manifest_sha256
        ):
            raise DracoArtifactDurabilityError(
                "source manifest changed while recovery acquired its lock"
            )
        locked_paths = _bound_standard_artifact_paths(manifest, manifest_path)
        if locked_paths != bound_paths:
            raise DracoArtifactDurabilityError(
                "source manifest artifact targets changed while recovery acquired its lock"
            )
        if manifest.get("artifact_recovery") is not None:
            verification, selection_plan_binding = _verify_bound_artifacts(
                manifest,
                bound_paths,
            )
            recovery = manifest["artifact_recovery"]
            prior_manifest_path = Path(
                str(recovery.get("prior_manifest_path") if isinstance(recovery, Mapping) else "")
            )
            prior_manifest_payload = (
                prior_manifest_path.read_bytes()
                if prior_manifest_path.is_file() and not prior_manifest_path.is_symlink()
                else b""
            )
            try:
                prior_manifest = json.loads(prior_manifest_payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                prior_manifest = None
            recomputed_ambiguous: list[dict[str, str]] | None = None
            recomputed_schedule: list[dict[str, str]] | None = None
            if isinstance(prior_manifest, Mapping):
                durable_keys = _durable_keys_from_verification(verification)
                recomputed_ambiguous, recomputed_schedule = _recovery_ledgers(
                    prior_manifest,
                    durable_keys,
                )
            if (
                locked_status != "result_incomplete"
                or not isinstance(recovery, Mapping)
                or recovery.get("schema") != RECOVERY_SCHEMA
                or recovery.get("status") != "sealed_after_unclean_exit"
                or recovery.get("physical_request_policy") != "no_model_or_provider_calls"
                or recovery.get("automatic_rerun_allowed") is not False
                or recovery.get("ambiguous_pairs") != recomputed_ambiguous
                or recovery.get("ambiguous_pair_count") != len(recomputed_ambiguous or [])
                or recovery.get("durable_scheduled_pairs") != recomputed_schedule
                or recovery.get("rows_written") != verification["rows_written"]
                or recovery.get("results_sha256") != verification["results_sha256"]
                or recovery.get("trace_sha256") != verification["trace_sha256"]
                or recovery.get("checkpoint_sha256") != verification["checkpoint_sha256"]
                or (
                    recovery.get(SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD)
                    != selection_plan_binding
                    if selection_plan_binding is not None
                    else SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD in recovery
                )
                or manifest.get("artifacts", {}).get("pre_recovery_manifest_json")
                != str(prior_manifest_path)
                or _sha256_bytes(prior_manifest_payload) != recovery.get("prior_manifest_sha256")
                or not isinstance(prior_manifest, dict)
                or prior_manifest.get("status") != recovery.get("prior_status")
                or manifest.get("resume_selection") is not None
            ):
                raise DracoArtifactDurabilityError(
                    "existing artifact recovery manifest is not idempotently valid"
                )
            fsync_directory(manifest_path.parent)
            return dict(recovery)
        if locked_status not in {"running", "aborted"}:
            raise DracoArtifactDurabilityError(
                f"manifest status is not recoverable: {locked_status!r}"
            )
        with DurableDracoArtifactWriter(
            results_path=results_path,
            trace_path=trace_path,
            checkpoint_path=checkpoint_path,
            create=False,
        ) as writer:
            writer.repair_unpaired_result()
        verification, selection_plan_binding = _verify_bound_artifacts(
            manifest,
            bound_paths,
        )
        durable_keys = _durable_keys_from_verification(verification)
        ambiguous, durable_scheduled_pairs = _recovery_ledgers(
            manifest,
            durable_keys,
        )
        recovered_at = time.time()
        prior_manifest_path = manifest_path.with_name(f"{manifest_path.name}.pre-recovery")
        if prior_manifest_path.exists():
            if (
                prior_manifest_path.is_symlink()
                or prior_manifest_path.read_bytes() != manifest_bytes
            ):
                raise DracoArtifactDurabilityError(
                    "pre-recovery manifest sidecar conflicts with source manifest"
                )
        else:
            atomic_write_bytes(prior_manifest_path, manifest_bytes)
        os.chmod(prior_manifest_path, 0o600)
        with prior_manifest_path.open("rb") as prior_handle:
            os.fsync(prior_handle.fileno())
        fsync_directory(prior_manifest_path.parent)
        recovery = {
            "schema": RECOVERY_SCHEMA,
            "status": "sealed_after_unclean_exit",
            "prior_status": prior_status,
            "prior_manifest_sha256": _sha256_bytes(manifest_bytes),
            "prior_manifest_path": str(prior_manifest_path),
            "recovered_at": recovered_at,
            "rows_written": verification["rows_written"],
            "results_sha256": verification["results_sha256"],
            "trace_sha256": verification["trace_sha256"],
            "checkpoint_sha256": verification["checkpoint_sha256"],
            "ambiguous_pair_count": len(ambiguous),
            "ambiguous_pairs": ambiguous,
            "durable_scheduled_pairs": durable_scheduled_pairs,
            "physical_request_policy": "no_model_or_provider_calls",
            "automatic_rerun_allowed": False,
        }
        if selection_plan_binding is not None:
            recovery[SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD] = (
                selection_plan_binding
            )
        manifest["status"] = "result_incomplete"
        manifest["finished_at"] = recovered_at
        started_at = manifest.get("started_at")
        manifest["elapsed_ms"] = (
            int((recovered_at - float(started_at)) * 1000)
            if isinstance(started_at, int | float)
            else None
        )
        manifest["rows_written"] = verification["rows_written"]
        manifest["artifact_recovery"] = recovery
        if selection_plan_binding is not None:
            manifest[SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD] = (
                selection_plan_binding
            )
        manifest.pop("resume_selection", None)
        manifest["artifacts"]["pre_recovery_manifest_json"] = str(prior_manifest_path)
        if manifest.get("failure") is None:
            manifest["failure"] = {
                "stage": "unclean_run_recovered",
                "model_or_judge_started": bool(durable_keys or ambiguous),
                "ambiguous_pair_count": len(ambiguous),
            }
        atomic_write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        return recovery


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    return parser


def main() -> int:
    recovery = recover_run(build_parser().parse_args().manifest)
    print(json.dumps(recovery, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
