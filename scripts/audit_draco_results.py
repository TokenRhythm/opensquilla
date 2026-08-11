#!/usr/bin/env python3
"""Audit DRACO result shards and materialize retry/final JSONL artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from opensquilla.eval.draco_artifact_integrity import verify_result_row_evidence
from opensquilla.eval.draco_resume_source_index import (
    ResumeRowLocator,
    ResumeSourceIndex,
)
from opensquilla.eval.draco_selection_plan_evidence import (
    selection_plan_reference_signal,
    selection_plan_row_capability_signal,
)

GROUPS = ("B0", "B1", "B2", "B3", "B4", "G1")
FIXED_MODELS = {
    "B0": "anthropic/claude-fable-5",
    "B4": "openai/gpt-5.6-sol",
}
_DIRECTORY_SIGNATURE_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)


def _directory_signature(value: os.stat_result) -> tuple[int, ...]:
    return tuple(
        int(getattr(value, field)) for field in _DIRECTORY_SIGNATURE_FIELDS
    )


def _open_bound_source_directory(path: Path) -> tuple[int, tuple[int, ...]]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot bind audit source directory: {path}") from exc
    try:
        directory_stat = os.fstat(fd)
        signature = _directory_signature(directory_stat)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise ValueError(f"audit source parent is not a directory: {path}")
        if _directory_signature(os.stat(path)) != signature:
            raise ValueError(f"audit source directory changed while binding: {path}")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return fd, signature


def _verify_bound_source_directory(
    fd: int,
    path: Path,
    *,
    signature: tuple[int, ...],
) -> None:
    try:
        descriptor_signature = _directory_signature(os.fstat(fd))
        pathname_signature = _directory_signature(os.stat(path))
    except OSError as exc:
        raise ValueError(f"audit source directory disappeared: {path}") from exc
    if descriptor_signature != signature or pathname_signature != signature:
        raise ValueError(f"audit source directory changed after binding: {path}")


def _bound_pathname_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError(f"cannot classify reserved audit artifact: {name}") from exc
    return True


def _bound_pathname_presence_snapshot(
    directory_fd: int,
    name: str,
    *,
    directory_path: Path,
    directory_signature: tuple[int, ...],
) -> bool:
    present = _bound_pathname_exists(directory_fd, name)
    # Presence is useful only when the directory snapshot is verified after
    # the sample. A hide/restore inside the sample changes directory ctime.
    _verify_bound_source_directory(
        directory_fd,
        directory_path,
        signature=directory_signature,
    )
    return present


def canonical_json_sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def parse_maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def read_input_tasks(path: Path) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"input row is not an object at {path}:{line_number}")
        task_id = str(value.get("id") or value.get("task_id") or "").strip()
        prompt = str(value.get("prompt") or value.get("problem") or "").strip()
        if not task_id or not prompt:
            raise ValueError(f"{path}:{line_number} requires id/task_id and prompt/problem")
        value["id"] = task_id
        value["prompt"] = prompt
        if "rubric" in value:
            value["rubric"] = parse_maybe_json(value["rubric"])
        elif "answer" in value:
            value["rubric"] = parse_maybe_json(value["answer"])
        tasks.append(value)
    return tasks


def parse_groups(value: str) -> tuple[str, ...]:
    groups = tuple(item.strip() for item in value.split(",") if item.strip())
    unknown = [group for group in groups if group not in GROUPS]
    if not groups or unknown or len(groups) != len(set(groups)):
        raise ValueError(f"invalid audit groups: {value}")
    return groups


def load_expected_fingerprints(
    path: Path, *, groups: tuple[str, ...] = GROUPS
) -> dict[str, str]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    compatibility = manifest.get("run_compatibility") if isinstance(manifest, dict) else None
    fingerprints = compatibility.get("fingerprints") if isinstance(compatibility, dict) else None
    if not isinstance(fingerprints, dict):
        raise ValueError(f"expected manifest lacks run_compatibility fingerprints: {path}")
    missing = [group for group in groups if not str(fingerprints.get(group) or "")]
    if missing:
        raise ValueError(
            f"expected manifest lacks fingerprints for groups: {', '.join(missing)}"
        )
    return {group: str(fingerprints[group]) for group in groups}


def _read_unbound_jsonl(path: Path) -> list[dict[str, Any]]:
    """Preserve the legacy parser and its public error semantics exactly."""

    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL row is not an object at {path}:{line_number}")
            if (
                selection_plan_row_capability_signal(value)
                or selection_plan_reference_signal(value)
            ):
                raise ValueError(
                    "content-addressed selection-plan evidence requires a standard "
                    f"authenticated sibling binding at {path}:{line_number}"
                )
            value["_audit_source"] = str(path)
            value["_audit_source_line"] = line_number
            rows.append(value)
    return rows


def _read_standard_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read one stamped source through a single identity-bound lifecycle."""

    standard = ResumeSourceIndex._standard_manifest_path(path)
    if standard is None:  # pragma: no cover - caller guards this boundary.
        raise ValueError("standard DRACO result path is malformed")
    stamp, manifest_path = standard
    reserved_pack_name = f"draco_run_{stamp}.selection-plan.pack.jsonl"
    pending: list[tuple[dict[str, Any] | None, ResumeRowLocator | None, int]] = []
    directory_fd, directory_signature = _open_bound_source_directory(
        manifest_path.parent
    )
    try:
        with ResumeSourceIndex([path], force_spool=False) as source_index:
            with source_index.open_source(path, source_index=0) as indexed_rows:
                for indexed in indexed_rows:
                    if indexed.parsed_row is not None:
                        pending.append(
                            (indexed.parsed_row, None, indexed.locator.line_number)
                        )
                        continue
                    text = indexed.payload.decode("utf-8")
                    if not text.strip():
                        continue
                    try:
                        value = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"invalid JSONL at {path}:"
                            f"{indexed.locator.line_number}: {exc}"
                        ) from exc
                    if not isinstance(value, dict):
                        raise ValueError(
                            "JSONL row is not an object at "
                            f"{path}:{indexed.locator.line_number}"
                        )
                    locator = indexed.locator.bind(
                        group=str(value.get("group") or ""),
                        task_id=str(value.get("task_id") or ""),
                    )
                    _view, compact = source_index.classification_row(locator, value)
                    if not compact:
                        raise ValueError(
                            "compact audit source lacks its authenticated sibling "
                            "binding"
                        )
                    del _view
                    pending.append((None, locator, locator.line_number))
            authenticated_compact = source_index.source_is_compact_authenticated(
                source_index=0
            )
            reserved_pack_present = _bound_pathname_presence_snapshot(
                directory_fd,
                reserved_pack_name,
                directory_path=manifest_path.parent,
                directory_signature=directory_signature,
            )
            if reserved_pack_present and not authenticated_compact:
                raise ValueError(
                    "reserved selection-plan pack lacks its authenticated sibling "
                    "binding"
                )
            source_index.seal()
            rows: list[dict[str, Any]] = []
            for legacy_row, locator, line_number in pending:
                if locator is None:
                    assert legacy_row is not None
                    value = legacy_row
                else:
                    value = source_index.consume_row(locator)
                value["_audit_source"] = str(path)
                value["_audit_source_line"] = line_number
                rows.append(value)
            current_pack_present = _bound_pathname_presence_snapshot(
                directory_fd,
                reserved_pack_name,
                directory_path=manifest_path.parent,
                directory_signature=directory_signature,
            )
            if current_pack_present != reserved_pack_present:
                raise ValueError(
                    "reserved selection-plan pack changed during audit binding"
                )
        current_pack_present = _bound_pathname_presence_snapshot(
            directory_fd,
            reserved_pack_name,
            directory_path=manifest_path.parent,
            directory_signature=directory_signature,
        )
        if current_pack_present != reserved_pack_present:
            raise ValueError("reserved selection-plan pack changed during audit binding")
        return rows
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            pass


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if ResumeSourceIndex._standard_manifest_path(path) is None:
        return _read_unbound_jsonl(path)
    # A stamped result must never mix rows from one pathname snapshot with a
    # sibling-manifest decision from another. ResumeSourceIndex owns the first
    # open, row parsing, optional compact binding, and final pathname check.
    return _read_standard_jsonl(path)


def invalid_reasons(
    row: dict[str, Any],
    *,
    expected_task_input_sha256: str,
    expected_run_compatibility_fingerprint: str,
    require_result_evidence: bool = False,
) -> list[str]:
    reasons: list[str] = []
    if require_result_evidence and not verify_result_row_evidence(clean_row(row)):
        reasons.append("invalid_result_evidence")
    if row.get("error"):
        reasons.append("error")
    if not str(row.get("final_text") or "").strip():
        reasons.append("empty_final_text")
    if row.get("quality_total") is None:
        reasons.append("missing_quality_total")
    task_hash = str(row.get("task_input_sha256") or "")
    if not task_hash:
        reasons.append("missing_task_input_sha256")
    elif task_hash != expected_task_input_sha256:
        reasons.append("task_input_hash_mismatch")
    fingerprint = str(row.get("run_compatibility_fingerprint") or "")
    if not fingerprint:
        reasons.append("missing_run_compatibility_fingerprint")
    elif fingerprint != expected_run_compatibility_fingerprint:
        reasons.append("run_compatibility_fingerprint_mismatch")

    judge = row.get("judge") or {}
    if judge.get("score_status") != "complete":
        reasons.append("judge_incomplete")
    if judge.get("judge_error_count") != 0:
        reasons.append("judge_errors")

    trace = row.get("ensemble_trace") or {}
    total = trace.get("total_candidates")
    successful = trace.get("successful_proposers")
    if total is not None and (successful is None or successful < total):
        reasons.append("incomplete_ensemble")

    group = str(row.get("group") or "")
    expected_model = FIXED_MODELS.get(group)
    if expected_model is not None:
        actual_models: set[str] = set()
        execution = row.get("execution")
        attempts = (
            execution.get("generation_attempts")
            if isinstance(execution, dict)
            else None
        )
        usages: list[dict[str, Any]] = []
        if isinstance(attempts, list):
            for attempt in attempts:
                run = attempt.get("run") if isinstance(attempt, dict) else None
                usage = run.get("usage") if isinstance(run, dict) else None
                if isinstance(usage, dict):
                    usages.append(usage)
        if not usages and isinstance(row.get("usage"), dict):
            usages.append(row["usage"])
        for usage in usages:
            breakdown = usage.get("model_usage_breakdown")
            if isinstance(breakdown, list) and breakdown:
                actual_models.update(
                    str(item.get("model") or "")
                    for item in breakdown
                    if isinstance(item, dict)
                )
            elif usage.get("model"):
                actual_models.add(str(usage["model"]))
        actual_models.discard("")
        if actual_models != {expected_model}:
            reasons.append("wrong_fixed_model")
    return reasons


def clean_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_audit_")}


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--result", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--expected-manifest",
        type=Path,
        required=True,
        help="Manifest whose per-group run compatibility fingerprints are authoritative.",
    )
    parser.add_argument("--prefix", default="draco_audit")
    parser.add_argument("--groups", default=",".join(GROUPS))
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=0,
        help="Audit only the first N normalized input tasks; 0 audits all tasks.",
    )
    parser.add_argument(
        "--skip-final",
        action="store_true",
        help="Skip writing the large merged final JSONL during intermediate retry audits.",
    )
    parser.add_argument(
        "--require-result-evidence",
        action="store_true",
        help=(
            "Reject legacy or mutated rows whose complete result-evidence hash "
            "cannot be verified."
        ),
    )
    args = parser.parse_args()

    groups = parse_groups(args.groups)
    input_rows = read_input_tasks(args.input)
    if args.max_tasks < 0:
        raise ValueError("--max-tasks must be non-negative")
    if args.max_tasks:
        input_rows = input_rows[: args.max_tasks]
    task_ids = [str(row.get("id") or "") for row in input_rows]
    if not task_ids or any(not task_id for task_id in task_ids):
        raise ValueError("input contains a missing task id")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("input contains duplicate task ids")
    expected_keys = {(group, task_id) for group in groups for task_id in task_ids}
    task_input_hashes = {
        str(row["id"]): canonical_json_sha256(row) for row in input_rows
    }
    expected_fingerprints = load_expected_fingerprints(
        args.expected_manifest,
        groups=groups,
    )

    rows: list[dict[str, Any]] = []
    for path in args.result:
        rows.extend(read_jsonl(path))

    rows_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    unexpected_rows: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row.get("group") or ""), str(row.get("task_id") or ""))
        if key not in expected_keys:
            unexpected_rows.append(row)
        else:
            rows_by_key[key].append(row)
    observed_keys = set(rows_by_key)

    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    latest_reasons: dict[tuple[str, str], list[str]] = {}
    reason_counts: Counter[str] = Counter()
    attempt_reason_counts: Counter[str] = Counter()
    for key, attempts in rows_by_key.items():
        expected_reasons_args = {
            "expected_task_input_sha256": task_input_hashes[key[1]],
            "expected_run_compatibility_fingerprint": expected_fingerprints[key[0]],
            "require_result_evidence": bool(args.require_result_evidence),
        }
        valid_attempts = [
            row for row in attempts if not invalid_reasons(row, **expected_reasons_args)
        ]
        if valid_attempts:
            chosen[key] = valid_attempts[-1]
        else:
            reasons = invalid_reasons(attempts[-1], **expected_reasons_args)
            latest_reasons[key] = reasons
            reason_counts.update(reasons)
        for attempt in attempts:
            attempt_reason_counts.update(invalid_reasons(attempt, **expected_reasons_args))

    unresolved_keys = sorted(
        expected_keys - set(chosen),
        key=lambda item: (groups.index(item[0]), task_ids.index(item[1])),
    )
    retry_rows = [{"group": group, "task_id": task_id} for group, task_id in unresolved_keys]
    final_rows = [
        clean_row(chosen[(group, task_id)])
        for group in groups
        for task_id in task_ids
        if (group, task_id) in chosen
    ]

    per_group = {}
    for group in groups:
        expected = {(group, task_id) for task_id in task_ids}
        valid = expected & set(chosen)
        per_group[group] = {
            "expected": len(expected),
            "raw_attempts": sum(len(rows_by_key.get(key, [])) for key in expected),
            "present_unique": len(expected & observed_keys),
            "valid_unique": len(valid),
            "unresolved": len(expected - valid),
        }

    report = {
        "groups": list(groups),
        "compatibility_enforced": True,
        "result_evidence_enforced": bool(args.require_result_evidence),
        "compatibility_manifest": str(args.expected_manifest),
        "run_compatibility_fingerprints": expected_fingerprints,
        "input_task_count": len(task_ids),
        "expected_unique": len(expected_keys),
        "raw_attempts": len(rows),
        "raw_unique_expected": len(observed_keys),
        "raw_duplicate_attempts": sum(
            max(0, len(attempts) - 1) for attempts in rows_by_key.values()
        ),
        "unexpected_rows": len(unexpected_rows),
        "valid_unique": len(chosen),
        "unresolved_unique": len(unresolved_keys),
        "unresolved_missing": sum(1 for key in unresolved_keys if key not in observed_keys),
        "unresolved_present_but_invalid": sum(1 for key in unresolved_keys if key in observed_keys),
        "latest_invalid_reason_counts": dict(sorted(reason_counts.items())),
        "all_attempt_invalid_reason_counts": dict(sorted(attempt_reason_counts.items())),
        "per_group": per_group,
        "complete": (
            len(chosen) == len(expected_keys)
            and not unresolved_keys
            and not unexpected_rows
            and len(final_rows) == len(expected_keys)
        ),
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / f"{args.prefix}.json"
    retry_path = args.output_dir / f"{args.prefix}.retry_keys.jsonl"
    final_path = args.output_dir / f"{args.prefix}.final.jsonl"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_jsonl(retry_path, retry_rows)
    if not args.skip_final:
        write_jsonl(final_path, final_rows)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report={report_path}")
    print(f"retry_keys={retry_path}")
    if not args.skip_final:
        print(f"final={final_path}")
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
