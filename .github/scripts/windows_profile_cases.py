#!/usr/bin/env python3
"""Delegate six unchanged native Windows profile flows with exact-once proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROFILE_CASES = {
    "onboarding-flow": "scripts/test-onboarding-flow.mjs",
    "onboarding-first-chat": "scripts/test-onboarding-first-chat.mjs",
    "profile-consolidation-flow": "scripts/test-profile-consolidation-flow.mjs",
    "primary-repair-accessibility": "scripts/test-primary-repair-accessibility.mjs",
    "profile-import-flow": "scripts/test-profile-import-flow.mjs",
    "desktop-cleanup-flow": "scripts/test-desktop-cleanup-flow.mjs",
}
PROFILE_SHARDS = ("profiles", *(f"profiles-{case}" for case in PROFILE_CASES))
MANIFEST_NAME = "windows-profile-manifest.json"
TELEMETRY_NAME = "desktop-e2e-cases.jsonl"
_IDENTITY_KEYS = ("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identity() -> dict[str, str]:
    identity = {key: os.environ.get(key, "") for key in _IDENTITY_KEYS}
    if not re.fullmatch(r"[0-9a-fA-F]{40}", identity["GITHUB_SHA"]):
        raise ValueError("GITHUB_SHA must identify the checked-out source commit")
    for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        if not identity[key].isdigit() or int(identity[key]) < 1:
            raise ValueError(f"{key} must be a positive integer")
    return identity


def _require_windows() -> None:
    if os.name != "nt" or sys.platform != "win32" or os.environ.get("RUNNER_OS") != "Windows":
        raise ValueError("profile delegation requires a native Windows runner")


def script_inventory(root: Path) -> dict[str, dict[str, str]]:
    # Git may check out .mjs files as CRLF on Windows and LF on the Ubuntu
    # coverage verifier. Hash identical source content in canonical LF form.
    return {
        case: {"script": script,
               "sha256_lf": hashlib.sha256(
                   (root / "desktop/electron" / script).read_bytes().replace(b"\r\n", b"\n")
               ).hexdigest()}
        for case, script in PROFILE_CASES.items()
    }


def validate_matrix(matrix: Any, shard: str) -> list[dict[str, str]]:
    if shard not in PROFILE_SHARDS:
        raise ValueError(f"unknown Windows profile shard: {shard}")
    if not isinstance(matrix, list) or not matrix:
        raise ValueError("planned desktop matrix must be a nonempty array")
    pairs = []
    for cell in matrix:
        if not isinstance(cell, dict) or set(cell) != {"os", "shard"}:
            raise ValueError("invalid desktop matrix cell")
        runner, cell_shard = cell["os"], cell["shard"]
        if not isinstance(runner, str) or not isinstance(cell_shard, str):
            raise ValueError("desktop matrix os and shard must be strings")
        if not runner or not cell_shard:
            raise ValueError("desktop matrix os and shard must not be empty")
        if cell_shard.startswith("profiles-") and (
            runner != "windows-latest" or cell_shard not in PROFILE_SHARDS
        ):
            raise ValueError("unknown or non-Windows delegated profile cell")
        pairs.append((runner, cell_shard))
    if len(pairs) != len(set(pairs)):
        raise ValueError("duplicate desktop matrix cells")
    planned = {cell_shard for runner, cell_shard in pairs
               if runner == "windows-latest" and cell_shard in PROFILE_SHARDS}
    if planned != set(PROFILE_SHARDS):
        raise ValueError("Windows profiles owner and all six delegated cells are required")
    return [{"os": runner, "shard": cell_shard} for runner, cell_shard in sorted(pairs)]


def expected_cases(shard: str) -> list[str]:
    if shard == "profiles":
        return []
    if shard in PROFILE_SHARDS:
        return [shard.removeprefix("profiles-")]
    raise ValueError(f"unknown Windows profile shard: {shard}")


def select_cases(
    shard: str, matrix: Any, report_dir: Path, *, root: Path = ROOT
) -> list[str]:
    _require_windows()
    planned = validate_matrix(matrix, shard)
    inventory = script_inventory(root)
    manifest = {
        "schema_version": 1,
        "identity": _identity(),
        "runner_os": "Windows",
        "native_platform": "win32",
        "shard": shard,
        "expected_cases": expected_cases(shard),
        "planned_desktop_matrix": planned,
        "planned_desktop_matrix_sha256": _digest(planned),
        "script_inventory": inventory,
        "script_inventory_sha256": _digest(inventory),
        "telemetry_file": TELEMETRY_NAME,
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    manifest_path, telemetry_path = report_dir / MANIFEST_NAME, report_dir / TELEMETRY_NAME
    if manifest_path.exists() or telemetry_path.exists():
        raise ValueError("profile selection requires fresh manifest and telemetry paths")
    # Exclusive creation never truncates evidence from an earlier execution.
    with manifest_path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    with telemetry_path.open("x", encoding="utf-8"):
        pass
    return [f"{case}:{PROFILE_CASES[case]}" for case in manifest["expected_cases"]]


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value
    ):
        raise ValueError("profile telemetry timestamp must use the wrapper's UTC format")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_record(record: Any, *, case: str, shard: str) -> dict[str, Any]:
    required = {
        "case", "os", "shard", "attempt", "start", "end", "duration",
        "duration_unit", "status", "details",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise ValueError("incomplete or unknown profile telemetry fields")
    if record["case"] != case or record["os"] != "Windows" or record["shard"] != shard:
        raise ValueError("profile telemetry case/OS/shard does not match its manifest")
    if type(record["attempt"]) is not int or record["attempt"] != 1:
        raise ValueError("delegated profile cases must execute exactly one attempt")
    if record["status"] not in {"passed", "failed"}:
        raise ValueError("skipped or interrupted profile cases are not execution coverage")
    duration = record["duration"]
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        raise ValueError("profile duration must be finite and nonnegative")
    if record["duration_unit"] != "ms":
        raise ValueError("profile duration must use milliseconds")
    start, end = _timestamp(record["start"]), _timestamp(record["end"])
    if end < start:
        raise ValueError("profile telemetry end precedes start")
    details = record["details"]
    if not isinstance(details, dict) or set(details) != {"exit_code", "signal"}:
        raise ValueError("profile process did not complete normally (timeout/spawn/interruption)")
    code = details["exit_code"]
    if type(code) is not int or code < 0 or details["signal"] is not None:
        raise ValueError("profile process has no normal exit code")
    if (record["status"] == "passed") != (code == 0):
        raise ValueError("profile status contradicts the process exit code")
    return record


def verify_reports(
    reports_dir: Path, *, root: Path = ROOT,
    expected_identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    paths = sorted(reports_dir.rglob(MANIFEST_NAME))
    if len(paths) != len(PROFILE_SHARDS):
        raise ValueError("expected exactly seven Windows profile manifests")
    manifests = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if sorted(manifest["shard"] for manifest in manifests) != sorted(PROFILE_SHARDS):
        raise ValueError("missing or duplicate Windows profile owners")
    inventory = script_inventory(root)
    identity = manifests[0]["identity"]
    if not isinstance(identity, dict) or set(identity) != set(_IDENTITY_KEYS):
        raise ValueError("profile source/run/attempt identity is missing")
    if not all(isinstance(value, str) and value for value in identity.values()):
        raise ValueError("invalid profile source/run/attempt identity")
    if expected_identity is not None and identity != expected_identity:
        raise ValueError("profile evidence belongs to a different source/run/attempt")
    plan = manifests[0]["planned_desktop_matrix"]
    records = []
    for path, manifest in zip(paths, manifests, strict=True):
        shard = manifest["shard"]
        if manifest["schema_version"] != 1:
            raise ValueError("unsupported profile manifest schema")
        if manifest["identity"] != identity:
            raise ValueError("profile manifests have different source/run/attempt identities")
        if manifest["runner_os"] != "Windows" or manifest["native_platform"] != "win32":
            raise ValueError("profile evidence is not native Windows")
        if manifest["planned_desktop_matrix"] != plan or validate_matrix(plan, shard) != plan:
            raise ValueError("profile manifests have different or noncanonical plans")
        if manifest["planned_desktop_matrix_sha256"] != _digest(plan):
            raise ValueError("profile matrix digest mismatch")
        if manifest["script_inventory"] != inventory:
            raise ValueError("profile scripts do not match the full six-case source inventory")
        if manifest["script_inventory_sha256"] != _digest(inventory):
            raise ValueError("profile script inventory digest mismatch")
        if manifest["expected_cases"] != expected_cases(shard):
            raise ValueError("profile manifest has the wrong case ownership")
        if manifest["telemetry_file"] != TELEMETRY_NAME:
            raise ValueError("unexpected profile telemetry path")
        lines = path.with_name(TELEMETRY_NAME).read_text(encoding="utf-8").splitlines()
        if len(lines) != len(manifest["expected_cases"]):
            raise ValueError("missing or duplicate profile telemetry executions")
        for line, case in zip(lines, manifest["expected_cases"], strict=True):
            records.append(validate_record(json.loads(line), case=case, shard=shard))
    if sorted(record["case"] for record in records) != sorted(PROFILE_CASES):
        raise ValueError("profile flow coverage is not exactly once")
    return {
        "coverage_complete": True,
        "identity": identity,
        "case_count": len(records),
        "original_profiles_case_count": 0,
        "script_inventory_sha256": _digest(inventory),
        "planned_desktop_matrix_sha256": _digest(plan),
        "outcomes": {
            status: sum(record["status"] == status for record in records)
            for status in ("passed", "failed")
        },
        "cases": sorted(records, key=lambda record: record["case"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select")
    select.add_argument("--shard", required=True, choices=PROFILE_SHARDS)
    select.add_argument("--planned-desktop-matrix", required=True)
    select.add_argument("--report-dir", type=Path, required=True)
    select.add_argument("--root", type=Path, default=ROOT)
    verify = commands.add_parser("verify")
    verify.add_argument("--reports-dir", type=Path, required=True)
    verify.add_argument("--output", type=Path)
    verify.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        if args.command == "select":
            entries = select_cases(
                args.shard, json.loads(args.planned_desktop_matrix), args.report_dir, root=args.root
            )
            for entry in entries:
                print(entry)
            return 0
        result = verify_reports(args.reports_dir, root=args.root, expected_identity=_identity())
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"Windows profile coverage contract failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
