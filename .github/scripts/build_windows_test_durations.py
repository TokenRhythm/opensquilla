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
SHARED_SHARD_NAMES: Final[tuple[str, ...]] = tuple(
    f"{family}-{partition}"
    for family in SHARD_NAMES
    for partition in range(1, 9 if family == "gateway-sqlite" else 5)
)
WINDOWS_SHARD_NAMES: Final[tuple[str, ...]] = tuple(
    f"{family}-{partition}"
    for family in SHARD_NAMES
    for partition in range(1, 12 if family == "gateway-sqlite" else 5)
)
METADATA_NAME: Final[str] = "windows-shard-metadata.json"
JUNIT_NAME: Final[str] = "junit.xml"
PROVISIONAL_FLOOR_SECONDS: Final[float] = 0.01
BUNDLE_TEST_FILE: Final[str] = "tests/test_observability/test_bundle.py"
BUNDLE_OWNER: Final[str] = "desktop-installer-contracts-1"
BUNDLE_SHARDS: Final[tuple[str, ...]] = tuple(f"bundle-{part}" for part in range(1, 5))


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
    partition_sha256: str | None
    execution_by_shard: dict[str, dict[str, object]]
    file_phase_seconds: dict[str, dict[str, float]]


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON metadata from {path}") from exc


def _validated_test_files(
    raw: object,
    *,
    metadata_path: Path,
    field_name: str = "test_files",
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(raw, list) or (not raw and not allow_empty):
        raise ValueError(f"invalid or missing {field_name} in {metadata_path}")
    files: list[str] = []
    for raw_path in raw:
        if not isinstance(raw_path, str):
            raise ValueError(f"non-string test path in {metadata_path}")
        path = PurePosixPath(raw_path).as_posix()
        if (
            path != raw_path
            or "\\" in path
            or ".." in PurePosixPath(path).parts
            or not path.startswith("tests/")
            or not PurePosixPath(path).name.startswith("test_")
            or not path.endswith(".py")
        ):
            raise ValueError(f"invalid test path in {metadata_path}: {raw_path!r}")
        files.append(path)
    if files != sorted(files) or len(files) != len(set(files)):
        raise ValueError(f"{field_name} must be unique and sorted in {metadata_path}")
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
    *,
    allow_empty: bool = False,
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
    if not nodes and not allow_empty:
        raise ValueError(f"JUnit report contains no testcases: {junit_path}")
    seen_nodes.update(nodes)
    return nodes, dict(file_seconds)


def _matching_phase_markers(parallel: object, serial: object) -> bool:
    """Accept legacy phases or the runner's paired, identical base filter."""

    if (parallel, serial) == ("not ci_serial", "ci_serial"):
        return True
    suffix = ") and (not ci_serial)"
    if (
        not isinstance(parallel, str)
        or not parallel.startswith("(")
        or not parallel.endswith(suffix)
    ):
        return False
    base = parallel[1:-len(suffix)]
    return bool(base.strip()) and serial == f"({base}) and (ci_serial)"


def load_run_directory(run_dir: Path) -> RunObservation:
    """Load a complete legacy or partitioned Windows run or fail closed."""

    metadata_paths = sorted(run_dir.rglob(METADATA_NAME))
    payloads = [_load_json(path) for path in metadata_paths]
    physical = any(
        isinstance(payload, dict) and payload.get("shard") in WINDOWS_SHARD_NAMES
        for payload in payloads
    )
    declared = [payload.get("execution_shards") for payload in payloads
                if isinstance(payload, dict)]
    if any(value is not None for value in declared):
        # Every new report declares the full native matrix, including targeted
        # runs. Removing all three added reports must not look like an old full
        # twenty-shard run. Legacy evidence without this field stays readable.
        if not physical or any(value != list(WINDOWS_SHARD_NAMES) for value in declared):
            raise ValueError("inconsistent or invalid Windows execution_shards inventory")
        shard_names = WINDOWS_SHARD_NAMES
    elif physical:
        extra_names = set(WINDOWS_SHARD_NAMES) - set(SHARED_SHARD_NAMES)
        has_extra = any(
            isinstance(payload, dict) and payload.get("shard") in extra_names
            for payload in payloads
        )
        shard_names = WINDOWS_SHARD_NAMES if has_extra else SHARED_SHARD_NAMES
    else:
        shard_names = SHARD_NAMES
    if len(metadata_paths) != len(shard_names):
        raise ValueError(
            f"expected {len(shard_names)} Windows shard metadata files in {run_dir}, "
            f"found {len(metadata_paths)}"
        )

    common: tuple[int, str, str] | None = None
    common_runtime_compatibility: dict[str, str | None] | None = None
    attempts_by_shard: dict[str, int] = {}
    image_versions_by_shard: dict[str, str | None] = {}
    files_by_shard: dict[str, tuple[str, ...]] = {}
    junit_by_shard: dict[str, Path] = {}
    execution_by_shard: dict[str, dict[str, object]] = {}
    partition_hashes: set[str] = set()
    all_files: set[str] = set()
    bundle_delegated = False
    for metadata_path, payload in zip(metadata_paths, payloads, strict=True):
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
        if shard not in shard_names or shard in files_by_shard:
            raise ValueError(f"invalid or duplicate shard in {metadata_path}")
        delegation = payload.get("delegated_case_files", [])
        if delegation != []:
            if not physical or shard != BUNDLE_OWNER or delegation != [
                {"path": BUNDLE_TEST_FILE, "shards": list(BUNDLE_SHARDS)}
            ]:
                raise ValueError(f"invalid delegated_case_files in {metadata_path}")
            bundle_delegated = True
        if physical:
            partition_hash = payload.get("partition_sha256")
            if (
                not isinstance(partition_hash, str)
                or len(partition_hash) != 64
                or any(char not in "0123456789abcdef" for char in partition_hash)
            ):
                raise ValueError(f"invalid partition_sha256 in {metadata_path}")
            partition_hashes.add(partition_hash)
            if len(partition_hashes) != 1:
                raise ValueError("inconsistent Windows partition fingerprints")
        execution = payload.get("execution")
        if not isinstance(execution, dict) or set(execution) != {"parallel", "serial"}:
            raise ValueError(f"invalid execution configuration in {metadata_path}")
        parallel = execution["parallel"]
        serial = execution["serial"]
        if (
            not isinstance(parallel, dict)
            or set(parallel) not in (
                {"workers", "dist", "marker"},
                {"workers", "dist", "marker", "item_parallel_files"},
            )
            or type(parallel.get("workers")) is not int
            or parallel["workers"] < 1
            or parallel.get("dist") != "loadfile"
            or not isinstance(serial, dict)
            or set(serial) != {"workers", "marker"}
            or type(serial.get("workers")) is not int
            or serial["workers"] != 1
            or not _matching_phase_markers(parallel.get("marker"), serial.get("marker"))
        ):
            raise ValueError(f"invalid execution configuration in {metadata_path}")
        item_parallel_files = _validated_test_files(
            parallel.get("item_parallel_files", []),
            metadata_path=metadata_path,
            field_name="item_parallel_files",
            allow_empty=True,
        )
        # Older runners always used whole-file scheduling. Normalize that
        # implicit policy so explicit [] remains comparable, while an audited
        # per-item override changes the execution provenance used below.
        execution_by_shard[str(shard)] = {
            "parallel": {**parallel, "item_parallel_files": list(item_parallel_files)},
            "serial": serial,
        }
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
        if not set(item_parallel_files).issubset(files):
            raise ValueError(f"item_parallel_files must be within test_files in {metadata_path}")
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

    if set(files_by_shard) != set(shard_names) or common is None:
        raise ValueError(f"incomplete Windows shard set in {run_dir}")

    if bundle_delegated:
        if BUNDLE_TEST_FILE in all_files:
            raise ValueError("delegated bundle file is also assigned to a normal Windows shard")
        all_files.add(BUNDLE_TEST_FILE)
        files_by_shard[BUNDLE_OWNER] = tuple(sorted(
            (*files_by_shard[BUNDLE_OWNER], BUNDLE_TEST_FILE)
        ))
    elif any(run_dir.rglob("bundle-shard-*.json")):
        raise ValueError("bundle shard reports lack an explicit owner delegation")

    module_index = _module_index(all_files)
    node_ids: set[str] = set()
    file_seconds: dict[str, float] = defaultdict(float)
    file_phase_seconds: dict[str, dict[str, float]] = {}
    for shard in shard_names:
        nodes, shard_seconds = _parse_junit(
            junit_by_shard[shard], module_index, node_ids
        )
        if bundle_delegated and BUNDLE_TEST_FILE in shard_seconds:
            raise ValueError("delegated bundle cases also ran in a normal Windows shard")
        node_ids.update(nodes)
        for path, seconds in shard_seconds.items():
            file_seconds[path] += seconds
        phase_paths = {
            phase: junit_by_shard[shard].with_name(f"junit.{phase}.xml")
            for phase in ("parallel", "serial")
        }
        if physical or any(path.exists() for path in phase_paths.values()):
            phase_nodes: set[str] = set()
            for phase, phase_path in phase_paths.items():
                _, seconds_by_file = _parse_junit(
                    phase_path, module_index, phase_nodes, allow_empty=True
                )
                for path, seconds in seconds_by_file.items():
                    file_phase_seconds.setdefault(
                        path, {"parallel": 0.0, "serial": 0.0}
                    )[phase] = seconds
            if phase_nodes != nodes:
                raise ValueError(f"phase JUnit collection differs from merged report: {shard}")
            for path, seconds in shard_seconds.items():
                if not math.isclose(
                    sum(file_phase_seconds[path].values()), seconds,
                    rel_tol=1e-9, abs_tol=1e-6,
                ):
                    raise ValueError(f"phase JUnit duration differs from merged report: {path}")

    if bundle_delegated:
        helper = runpy.run_path(str(Path(__file__).with_name("bundle_test_shards.py")))
        identity = {
            "GITHUB_RUN_ID": str(common[0]), "GITHUB_SHA": common[1],
            "GITHUB_RUN_ATTEMPT": str(attempts_by_shard[BUNDLE_OWNER]),
        }
        try:
            proof = helper["verify_reports"](run_dir, expected_identity=identity)
        except (ValueError, KeyError, TypeError, OSError, ET.ParseError) as exc:
            raise ValueError(
                "delegated bundle requires four complete, matching execution reports: "
                f"{exc}"
            ) from exc
        if proof["outcomes"].get("failed", 0):
            raise ValueError("delegated bundle JUnit report is not successful")
        bundle_seconds = 0.0
        for report_path in sorted(run_dir.rglob("bundle-shard-*.json")):
            report = _load_json(report_path)
            assert isinstance(report, dict)  # verify_reports validated the schema.
            runtime = report.get("runtime")
            compatibility = common_runtime_compatibility or {}
            if (
                not isinstance(runtime, dict)
                or set(runtime) != {*compatibility, "image_version"}
                or any(
                    value is not None and not isinstance(value, str) for value in runtime.values()
                )
                or {key: value for key, value in runtime.items() if key != "image_version"}
                != compatibility
            ):
                raise ValueError(f"incompatible delegated bundle runtime in {report_path}")
            part = f"bundle-{report['shard']}"
            _, seconds = _parse_junit(
                report_path.with_name(str(report["junit"])), module_index, node_ids,
            )
            if set(seconds) != {BUNDLE_TEST_FILE}:
                raise ValueError("delegated bundle JUnit contains an unexpected test file")
            bundle_seconds += seconds[BUNDLE_TEST_FILE]
            attempts_by_shard[part] = int(identity["GITHUB_RUN_ATTEMPT"])
            image_versions_by_shard[part] = runtime["image_version"]
            execution_by_shard[part] = {
                "case_file": BUNDLE_TEST_FILE, "workers": 1,
                "partition": int(report["shard"]), "partition_count": 4,
                "selection": "sorted-nodeid-modulo", "dist": "no",
            }
        file_seconds[BUNDLE_TEST_FILE] = bundle_seconds
        file_phase_seconds[BUNDLE_TEST_FILE] = {"parallel": bundle_seconds, "serial": 0.0}

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
        partition_sha256=next(iter(partition_hashes), None),
        execution_by_shard=execution_by_shard,
        file_phase_seconds=file_phase_seconds,
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
    expected_partition_sha256: str | None = None,
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
        if run.partition_sha256 != expected_partition_sha256:
            raise ValueError("Windows run partition hash does not match the current snapshot")
        if run.execution_by_shard != reference.execution_by_shard:
            raise ValueError("Windows execution configuration differs across source runs")
        if set(run.file_phase_seconds) != set(reference.file_phase_seconds):
            raise ValueError("Windows phase duration coverage differs across source runs")
        if run.runtime_compatibility != reference.runtime_compatibility:
            raise ValueError("Windows runtime metadata differs across source runs")
        if run.files_by_shard != reference.files_by_shard:
            raise ValueError("Windows test file assignments differ across source runs")
        if run.node_ids != reference.node_ids:
            raise ValueError("Windows JUnit testcase collection differs across source runs")
        if set(run.file_seconds) != set(reference.file_seconds):
            raise ValueError("Windows JUnit test file collection differs across source runs")

    weights: dict[str, float] = {}
    phase_weights: dict[str, dict[str, float]] = {}
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
        if path in reference.file_phase_seconds:
            phase_weights[path] = {
                phase: round(statistics.median(
                    run.file_phase_seconds[path][phase] for run in observations
                ), 3)
                for phase in ("parallel", "serial")
            }

    source_runs = [
        {
            "id": run.run_id,
            "sha": run.sha,
            "attempts": {
                shard: run.attempts_by_shard[shard] for shard in sorted(run.attempts_by_shard)
            },
            "assignment_sha256": run.assignment_sha256,
            "partition_sha256": run.partition_sha256,
            "execution": run.execution_by_shard,
            "runtime_compatibility": run.runtime_compatibility,
            "image_versions": {
                shard: run.image_versions_by_shard[shard]
                for shard in sorted(run.image_versions_by_shard)
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
        "phase_weights_seconds": phase_weights,
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
    shard_module = runpy.run_path(
        (root / ".github/scripts/windows_test_shards.py").as_posix(),
        run_name="windows_partition_snapshot",
    )
    payload = build_duration_payload(
        observations,
        expected_assignment_sha256=_current_assignment_fingerprint(root),
        expected_partition_sha256=(
            shard_module["partition_snapshot_fingerprint"]()
            if observations and observations[0].partition_sha256 is not None else None
        ),
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
