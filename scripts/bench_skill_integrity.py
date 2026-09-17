#!/usr/bin/env python3
"""Opt-in benchmark for Skill tree probes and pinned resource verification."""

from __future__ import annotations

import argparse
import gc
import json
import math
import platform
import shutil
import statistics
import tempfile
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opensquilla.skills.hub.lockfile import compute_sha256
from opensquilla.skills.resources import SkillResources
from opensquilla.skills.tree import compute_tree_sha256, compute_tree_state

_KIB = 1024
_MIB = 1024 * 1024
_SMALL_ITERATIONS = 20
_LARGE_ITERATIONS = 5
_SCENARIOS: dict[str, tuple[str, int, bool]] = {
    "bytes_1kib": ("bytes", 1 * _KIB, False),
    "bytes_1mib": ("bytes", 1 * _MIB, False),
    "bytes_10mib": ("bytes", 10 * _MIB, True),
    "bytes_50mib": ("bytes", 50 * _MIB, True),
    "files_1": ("files", 1, False),
    "files_50": ("files", 50, False),
    "files_512": ("files", 512, True),
    "files_2048": ("files", 2_048, True),
}


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _write_manifest(root: Path, name: str) -> tuple[Path, int]:
    skill = root / name
    skill.mkdir(parents=True)
    manifest = (
        f"---\nname: {name}\ndescription: integrity benchmark fixture\n---\nBody.\n"
    ).encode()
    (skill / "SKILL.md").write_bytes(manifest)
    return skill, len(manifest)


def _write_probe(skill: Path) -> int:
    resource = b"benchmark resource\n"
    (skill / "references").mkdir(exist_ok=True)
    (skill / "references" / "probe.txt").write_bytes(resource)
    return len(resource)


def _build_fixture(root: Path, scenario: str) -> tuple[Path, str]:
    kind, magnitude, _large = _SCENARIOS[scenario]
    skill, fixed_bytes = _write_manifest(root, scenario.replace("_", "-"))
    if kind == "bytes":
        fixed_bytes += _write_probe(skill)
        remaining = magnitude - fixed_bytes
        if remaining < 0:  # pragma: no cover - protected by scenario constants
            raise ValueError(f"byte fixture is smaller than its manifest: {scenario}")
        block = bytes(range(256)) * 4_096
        with (skill / "payload.bin").open("wb") as handle:
            while remaining:
                chunk = block[: min(len(block), remaining)]
                handle.write(chunk)
                remaining -= len(chunk)
        return skill, "references/probe.txt"

    current_files = 1
    resource_path = "SKILL.md"
    if magnitude >= 2:
        _write_probe(skill)
        current_files += 1
        resource_path = "references/probe.txt"
    for index in range(magnitude - current_files):
        bucket = skill / "references" / f"bucket-{index // 128:02d}"
        bucket.mkdir(parents=True, exist_ok=True)
        (bucket / f"file-{index:04d}.txt").write_text(
            f"{index:04d}" * 32,
            encoding="utf-8",
        )
    return skill, resource_path


def _tree_shape(skill: Path) -> dict[str, int]:
    files = [path for path in skill.rglob("*") if path.is_file()]
    return {
        "files": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "maxDepth": max(len(path.relative_to(skill).parts) for path in files),
    }


def _measure(
    operation: Callable[[], object],
    iterations: int,
    *,
    total_bytes: int,
    total_files: int,
) -> dict[str, Any]:
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        operation()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)

    median_ms = statistics.median(samples)
    elapsed_seconds = median_ms / 1000
    gc.collect()
    tracemalloc.start()
    before, _ = tracemalloc.get_traced_memory()
    operation()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "iterations": iterations,
        "medianMs": round(median_ms, 3),
        "p95Ms": round(_nearest_rank(samples, 0.95), 3),
        "minMs": round(min(samples), 3),
        "maxMs": round(max(samples), 3),
        "peakAllocatedMiB": round((peak - before) / 1024 / 1024, 3),
        "retainedMiB": round((current - before) / 1024 / 1024, 3),
        "mibPerSecond": round((total_bytes / _MIB) / elapsed_seconds, 3),
        "filesPerSecond": round(total_files / elapsed_seconds, 3),
    }


def _benchmark_shape(
    skill: Path,
    resource_path: str,
    iterations: int,
) -> dict[str, Any]:
    expected_digest = compute_tree_sha256(skill)
    resources = SkillResources(skill)
    shape = _tree_shape(skill)

    def pinned_resource_read() -> str:
        if compute_tree_sha256(skill) != expected_digest:
            raise RuntimeError("fixture changed before resource read")
        content = resources.read_resource(resource_path)
        if compute_tree_sha256(skill) != expected_digest:
            raise RuntimeError("fixture changed after resource read")
        if content is None:
            raise RuntimeError("fixture resource is missing")
        return content

    return {
        "shape": shape,
        "treeState": _measure(
            lambda: compute_tree_state(skill),
            iterations,
            total_bytes=shape["bytes"],
            total_files=shape["files"],
        ),
        "treeSha256": _measure(
            lambda: compute_tree_sha256(skill),
            iterations,
            total_bytes=shape["bytes"],
            total_files=shape["files"],
        ),
        "legacySha256": _measure(
            lambda: compute_sha256(skill),
            iterations,
            total_bytes=shape["bytes"],
            total_files=shape["files"],
        ),
        "pinnedResourceRead": _measure(
            pinned_resource_read,
            iterations,
            total_bytes=shape["bytes"],
            total_files=shape["files"],
        ),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("smoke", "limits"),
        default="limits",
        help="smoke uses a tiny fixture; limits exercises accepted worst-case shapes",
    )
    parser.add_argument("--iterations", type=int, default=None)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()
    if args.iterations is not None and args.iterations <= 0:
        parser.error("--iterations must be positive")
    return args


def main() -> int:
    args = _parse_args()
    scenarios = ("bytes_1kib", "files_1") if args.profile == "smoke" else tuple(_SCENARIOS)
    fixture_root = Path(tempfile.mkdtemp(prefix="opensquilla-skill-benchmark-"))
    try:
        results: dict[str, Any] = {}
        for scenario in scenarios:
            skill, resource_path = _build_fixture(fixture_root, scenario)
            default_iterations = _LARGE_ITERATIONS if _SCENARIOS[scenario][2] else _SMALL_ITERATIONS
            results[scenario] = _benchmark_shape(
                skill,
                resource_path,
                args.iterations or default_iterations,
            )
    finally:
        shutil.rmtree(fixture_root)

    payload = {
        "schemaVersion": 2,
        "profile": args.profile,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "metricSemantics": {
            "throughputBasis": "fixtureLogicalSizeOnce",
            "memoryMetric": "pythonTracemallocIncremental",
            "memoryExcludes": [
                "nativeAllocations",
                "processRss",
                "filesystemCache",
                "fixtureConstruction",
            ],
        },
        "iterationPolicy": {
            "smallDefault": _SMALL_ITERATIONS,
            "largeDefault": _LARGE_ITERATIONS,
            "override": args.iterations,
        },
        "results": results,
    }
    serialized = json.dumps(payload, sort_keys=True)
    if args.output_json is not None:
        args.output_json.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
