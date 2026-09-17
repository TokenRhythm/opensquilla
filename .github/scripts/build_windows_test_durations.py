#!/usr/bin/env python3
"""Build a review-only Windows duration proposal from comparable JUnit artifacts."""

from __future__ import annotations

import argparse
import json
import math
import runpy
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

SHARD_NAMES: Final[tuple[str, ...]] = (
    "core",
    "gateway-sqlite",
    "recovery-migration",
    "desktop-installer-contracts",
)
METADATA_NAME: Final[str] = "windows-shard-metadata.json"
JUNIT_NAME: Final[str] = "junit.xml"
PROVISIONAL_FLOOR_SECONDS: Final[float] = 0.01


@dataclass(frozen=True)
class RunObservation:
    run_id: int
    sha: str
    assignment_sha256: str
    attempts_by_shard: dict[str, int]
    runtime_compatibility: dict[str, str | None]
    image_versions_by_shard: dict[str, str | None]
    files_by_shard: dict[str, tuple[str, ...]]
    node_ids: frozenset[str]
    file_seconds: dict[str, float]


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON metadata from {path}") from exc


def _validated_test_files(raw: object, *, metadata_path: Path) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"missing test_files in {metadata_path}")
    files: list[str] = []
    for raw_path in raw:
        if not isinstance(raw_path, str):
            raise ValueError(f"non-string test path in {metadata_path}")
        path = PurePosixPath(raw_path).as_posix()
        if (
            path != raw_path
            or not path.startswith("tests/")
            or not PurePosixPath(path).name.startswith("test_")
            or not path.endswith(".py")
        ):
            raise ValueError(f"invalid test path in {metadata_path}: {raw_path!r}")
        files.append(path)
    if files != sorted(files) or len(files) != len(set(files)):
        raise ValueError(f"test_files must be unique and sorted in {metadata_path}")
    return tuple(files)


def _module_index(files: set[str]) -> tuple[tuple[str, str], ...]:
    modules = ((path.removesuffix(".py").replace("/", "."), path) for path in files)
    return tuple(sorted(modules, key=lambda item: (-len(item[0]), item[0])))


def _test_file_for_classname(
    classname: str, module_index: tuple[tuple[str, str], ...], junit_path: Path
) -> str:
    for module, path in module_index:
        if classname == module or classname.startswith(f"{module}."):
            return path
    raise ValueError(f"cannot map JUnit classname {classname!r} in {junit_path}")


def _parse_junit(
    junit_path: Path,
    module_index: tuple[tuple[str, str], ...],
    seen_nodes: set[str],
) -> tuple[set[str], dict[str, float]]:
    try:
        root = ET.parse(junit_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"cannot read JUnit report from {junit_path}") from exc

    for suite in root.iter("testsuite"):
        for attribute in ("errors", "failures"):
            raw = suite.get(attribute, "0")
            try:
                count = int(raw)
            except ValueError as exc:
                raise ValueError(
                    f"invalid {attribute} count in JUnit report {junit_path}"
                ) from exc
            if count:
                raise ValueError(f"JUnit report is not successful: {junit_path}")

    nodes: set[str] = set()
    file_seconds: dict[str, float] = defaultdict(float)
    for testcase in root.iter("testcase"):
        classname = testcase.get("classname")
        name = testcase.get("name")
        if not classname or not name:
            raise ValueError(f"JUnit testcase lacks identity in {junit_path}")
        node_id = f"{classname}::{name}"
        if node_id in seen_nodes or node_id in nodes:
            raise ValueError(f"duplicate JUnit testcase across Windows shards: {node_id}")
        if testcase.find("failure") is not None or testcase.find("error") is not None:
            raise ValueError(f"JUnit testcase is not successful: {node_id}")
        raw_time = testcase.get("time", "0")
        try:
            seconds = float(raw_time)
        except ValueError as exc:
            raise ValueError(f"invalid JUnit duration for {node_id}: {raw_time!r}") from exc
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"invalid JUnit duration for {node_id}: {raw_time!r}")
        path = _test_file_for_classname(classname, module_index, junit_path)
        nodes.add(node_id)
        file_seconds[path] += seconds
    if not nodes:
        raise ValueError(f"JUnit report contains no testcases: {junit_path}")
    seen_nodes.update(nodes)
    return nodes, dict(file_seconds)


def load_run_directory(run_dir: Path) -> RunObservation:
    """Load one complete four-shard Windows run or fail closed."""

    metadata_paths = sorted(run_dir.rglob(METADATA_NAME))
    if len(metadata_paths) != len(SHARD_NAMES):
        raise ValueError(
            f"expected {len(SHARD_NAMES)} Windows shard metadata files in {run_dir}, "
            f"found {len(metadata_paths)}"
        )

    common: tuple[int, str, str] | None = None
    common_runtime_compatibility: dict[str, str | None] | None = None
    attempts_by_shard: dict[str, int] = {}
    image_versions_by_shard: dict[str, str | None] = {}
    files_by_shard: dict[str, tuple[str, ...]] = {}
    junit_by_shard: dict[str, Path] = {}
    all_files: set[str] = set()
    for metadata_path in metadata_paths:
        payload = _load_json(metadata_path)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError(f"unsupported Windows shard metadata in {metadata_path}")
        if payload.get("platform") != "windows":
            raise ValueError(f"non-Windows shard metadata in {metadata_path}")
        run_id = payload.get("run_id")
        attempt = payload.get("run_attempt")
        sha = payload.get("sha")
        assignment_sha256 = payload.get("assignment_sha256")
        runtime = payload.get("runtime")
        shard = payload.get("shard")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            raise ValueError(f"invalid run_id in {metadata_path}")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
            raise ValueError(f"invalid run_attempt in {metadata_path}")
        if (
            not isinstance(sha, str)
            or len(sha) < 40
            or any(char not in "0123456789abcdef" for char in sha)
        ):
            raise ValueError(f"invalid sha in {metadata_path}")
        if (
            not isinstance(assignment_sha256, str)
            or len(assignment_sha256) != 64
            or any(char not in "0123456789abcdef" for char in assignment_sha256)
        ):
            raise ValueError(f"invalid assignment_sha256 in {metadata_path}")
        if shard not in SHARD_NAMES or shard in files_by_shard:
            raise ValueError(f"invalid or duplicate shard in {metadata_path}")
        expected_runtime_keys = {
            "python_version",
            "runner_os",
            "runner_arch",
            "image_os",
            "image_version",
        }
        if not isinstance(runtime, dict) or set(runtime) != expected_runtime_keys:
            raise ValueError(f"invalid runtime metadata in {metadata_path}")
        if not isinstance(runtime.get("python_version"), str) or not isinstance(
            runtime.get("runner_os"), str
        ):
            raise ValueError(f"incomplete runtime metadata in {metadata_path}")
        if any(value is not None and not isinstance(value, str) for value in runtime.values()):
            raise ValueError(f"invalid runtime metadata value in {metadata_path}")
        normalized_runtime = {
            key: str(value) if value is not None else None for key, value in runtime.items()
        }
        runtime_compatibility = {
            key: value
            for key, value in normalized_runtime.items()
            if key != "image_version"
        }
        run_common = (run_id, sha, assignment_sha256)
        if common is None:
            common = run_common
            common_runtime_compatibility = runtime_compatibility
        elif run_common != common:
            raise ValueError(f"inconsistent run metadata in {run_dir}")
        elif runtime_compatibility != common_runtime_compatibility:
            raise ValueError(f"inconsistent runtime compatibility metadata in {run_dir}")
        files = _validated_test_files(payload.get("test_files"), metadata_path=metadata_path)
        overlap = all_files.intersection(files)
        if overlap:
            sample = sorted(overlap)[:3]
            raise ValueError(f"test files assigned to multiple Windows shards: {sample}")
        all_files.update(files)
        files_by_shard[str(shard)] = files
        attempts_by_shard[str(shard)] = attempt
        image_versions_by_shard[str(shard)] = normalized_runtime["image_version"]
        junit_path = metadata_path.with_name(JUNIT_NAME)
        if not junit_path.is_file():
            raise ValueError(f"missing JUnit report beside {metadata_path}")
        junit_by_shard[str(shard)] = junit_path

    if set(files_by_shard) != set(SHARD_NAMES) or common is None:
        raise ValueError(f"incomplete Windows shard set in {run_dir}")

    module_index = _module_index(all_files)
    node_ids: set[str] = set()
    file_seconds: dict[str, float] = defaultdict(float)
    for shard in SHARD_NAMES:
        nodes, shard_seconds = _parse_junit(
            junit_by_shard[shard], module_index, node_ids
        )
        node_ids.update(nodes)
        for path, seconds in shard_seconds.items():
            file_seconds[path] += seconds

    return RunObservation(
        run_id=common[0],
        sha=common[1],
        assignment_sha256=common[2],
        attempts_by_shard=attempts_by_shard,
        runtime_compatibility=common_runtime_compatibility or {},
        image_versions_by_shard=image_versions_by_shard,
        files_by_shard=files_by_shard,
        node_ids=frozenset(node_ids),
        file_seconds=dict(file_seconds),
    )


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def build_duration_payload(
    observations: list[RunObservation],
    *,
    expected_assignment_sha256: str,
    minimum_runs: int = 3,
) -> dict[str, object]:
    """Aggregate comparable successful runs into a deterministic proposal."""

    if minimum_runs < 3:
        raise ValueError("minimum_runs cannot be lower than three")
    if len(observations) < minimum_runs:
        raise ValueError(f"at least {minimum_runs} comparable Windows runs are required")
    identities = {run.run_id for run in observations}
    if len(identities) != len(observations):
        raise ValueError("duplicate Windows run supplied")

    reference = observations[0]
    for run in observations:
        if run.assignment_sha256 != expected_assignment_sha256:
            raise ValueError("Windows run assignment hash does not match the current snapshot")
        if run.runtime_compatibility != reference.runtime_compatibility:
            raise ValueError("Windows runtime metadata differs across source runs")
        if run.files_by_shard != reference.files_by_shard:
            raise ValueError("Windows test file assignments differ across source runs")
        if run.node_ids != reference.node_ids:
            raise ValueError("Windows JUnit testcase collection differs across source runs")
        if set(run.file_seconds) != set(reference.file_seconds):
            raise ValueError("Windows JUnit test file collection differs across source runs")

    weights: dict[str, float] = {}
    samples: dict[str, dict[str, float | int]] = {}
    for path in sorted(reference.file_seconds):
        values = [run.file_seconds[path] for run in observations]
        median = max(PROVISIONAL_FLOOR_SECONDS, statistics.median(values))
        weights[path] = round(median, 3)
        samples[path] = {
            "count": len(values),
            "median_seconds": round(median, 3),
            "p75_seconds": round(max(PROVISIONAL_FLOOR_SECONDS, _percentile(values, 0.75)), 3),
            "min_seconds": round(min(values), 3),
            "max_seconds": round(max(values), 3),
        }

    source_runs = [
        {
            "id": run.run_id,
            "sha": run.sha,
            "attempts": {
                shard: run.attempts_by_shard[shard] for shard in SHARD_NAMES
            },
            "assignment_sha256": run.assignment_sha256,
            "runtime_compatibility": run.runtime_compatibility,
            "image_versions": {
                shard: run.image_versions_by_shard[shard] for shard in SHARD_NAMES
            },
            "node_count": len(run.node_ids),
            "weighted_file_count": len(run.file_seconds),
        }
        for run in sorted(observations, key=lambda item: item.run_id)
    ]
    return {
        "description": (
            "Review-only median per-file pytest seconds from comparable successful "
            "Windows full-shard runs. Applying this proposal requires a separate "
            "reviewed assignment change."
        ),
        "schema_version": 1,
        "source_runs": source_runs,
        "samples": samples,
        "weights_seconds": weights,
    }


def _current_assignment_fingerprint(root: Path) -> str:
    shard_script = root / ".github" / "scripts" / "windows_test_shards.py"
    module = runpy.run_path(shard_script.as_posix(), run_name="windows_test_shards")
    fingerprint = module["assignment_snapshot_fingerprint"]
    return str(fingerprint())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--run-dir", type=Path, action="append", required=True)
    parser.add_argument("--minimum-runs", type=int, default=3)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    root = args.root.resolve()
    observations = [load_run_directory(path.resolve()) for path in args.run_dir]
    payload = build_duration_payload(
        observations,
        expected_assignment_sha256=_current_assignment_fingerprint(root),
        minimum_runs=args.minimum_runs,
    )
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
        return 0
    output = args.output.resolve()
    canonical = (root / ".github" / "scripts" / "windows_test_durations.json").resolve()
    if output == canonical:
        raise ValueError("duration builder is review-only and cannot overwrite canonical data")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
