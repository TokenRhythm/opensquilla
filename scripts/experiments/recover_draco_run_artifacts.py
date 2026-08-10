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
    DRACO_DURABLE_RESULT_ROW_FIELD,
    DRACO_RUN_MANIFEST_SCHEMA_V2,
    DracoArtifactDurabilityError,
    DracoArtifactRunLock,
    DurableDracoArtifactWriter,
    atomic_write_bytes,
    atomic_write_text,
    durable_artifact_capability_contract,
    fsync_directory,
    verify_durable_draco_artifacts,
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
    return expected


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


def _durable_keys(rows: list[Mapping[str, Any]]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for row in rows:
        if row.get(DRACO_DURABLE_RESULT_ROW_FIELD) != (durable_artifact_capability_contract()):
            raise DracoArtifactDurabilityError(
                "recovered shard lacks its sealed durable row marker"
            )
        key = (str(row.get("group") or ""), str(row.get("task_id") or ""))
        if not all(key) or key in keys:
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


def _read_verified_result_rows(results_path: Path) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for raw in results_path.read_bytes().split(b"\n"):
        if not raw:
            continue
        value = json.loads(raw)
        if not isinstance(value, Mapping):
            raise DracoArtifactDurabilityError("verified result artifact contains a non-object row")
        rows.append(value)
    return rows


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
            verification = verify_durable_draco_artifacts(
                results_path=results_path,
                trace_path=trace_path,
                checkpoint_path=checkpoint_path,
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
                durable_keys = _durable_keys(_read_verified_result_rows(results_path))
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
            rows = writer.paired_result_rows
        verification = verify_durable_draco_artifacts(
            results_path=results_path,
            trace_path=trace_path,
            checkpoint_path=checkpoint_path,
        )
        durable_keys = _durable_keys(rows)
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
        manifest.pop("resume_selection", None)
        manifest["artifacts"]["pre_recovery_manifest_json"] = str(prior_manifest_path)
        if manifest.get("failure") is None:
            manifest["failure"] = {
                "stage": "unclean_run_recovered",
                "model_or_judge_started": bool(rows or ambiguous),
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
