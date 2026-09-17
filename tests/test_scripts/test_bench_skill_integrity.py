from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_skill_integrity_benchmark_smoke_emits_stable_json(tmp_path: Path) -> None:
    output = tmp_path / "benchmark.json"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/bench_skill_integrity.py",
            "--profile",
            "smoke",
            "--iterations",
            "2",
            "--output-json",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(completed.stdout)
    assert payload == json.loads(output.read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == 2
    assert payload["profile"] == "smoke"
    assert payload["python"]
    assert payload["metricSemantics"] == {
        "throughputBasis": "fixtureLogicalSizeOnce",
        "memoryMetric": "pythonTracemallocIncremental",
        "memoryExcludes": [
            "nativeAllocations",
            "processRss",
            "filesystemCache",
            "fixtureConstruction",
        ],
    }
    assert payload["iterationPolicy"] == {
        "smallDefault": 20,
        "largeDefault": 5,
        "override": 2,
    }
    assert set(payload["results"]) == {"bytes_1kib", "files_1"}
    for result in payload["results"].values():
        assert set(result) == {
            "shape",
            "treeState",
            "treeSha256",
            "legacySha256",
            "pinnedResourceRead",
        }
        assert result["shape"]["files"] >= 1
        assert result["shape"]["bytes"] >= 1
        for operation in (
            "treeState",
            "treeSha256",
            "legacySha256",
            "pinnedResourceRead",
        ):
            measurement = result[operation]
            assert set(measurement) == {
                "iterations",
                "medianMs",
                "p95Ms",
                "minMs",
                "maxMs",
                "peakAllocatedMiB",
                "retainedMiB",
                "mibPerSecond",
                "filesPerSecond",
            }
            assert measurement["iterations"] == 2
            assert measurement["minMs"] <= measurement["medianMs"]
            assert measurement["medianMs"] <= measurement["p95Ms"]
            assert measurement["p95Ms"] <= measurement["maxMs"]
            assert measurement["peakAllocatedMiB"] >= 0
            assert measurement["mibPerSecond"] > 0
            assert measurement["filesPerSecond"] > 0
