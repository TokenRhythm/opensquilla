from __future__ import annotations

import json
import runpy
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

DURATION_SCRIPT = Path(".github/scripts/build_windows_test_durations.py")
DURATION_MODULE: dict[str, Any] = runpy.run_path(
    DURATION_SCRIPT.as_posix(), run_name="windows_duration_governance"
)
SHARD_NAMES: tuple[str, ...] = DURATION_MODULE["SHARD_NAMES"]
build_duration_payload = DURATION_MODULE["build_duration_payload"]
load_run_directory = DURATION_MODULE["load_run_directory"]

FILES_BY_SHARD = {
    "core": "tests/test_core.py",
    "gateway-sqlite": "tests/test_gateway/test_rpc.py",
    "recovery-migration": "tests/test_recovery/test_restore.py",
    "desktop-installer-contracts": "tests/test_desktop/test_startup.py",
}


def _classname(path: str) -> str:
    return path.removesuffix(".py").replace("/", ".")


def _write_run(
    root: Path,
    *,
    run_id: int,
    sha: str,
    assignment_sha256: str,
    seconds: dict[str, float],
    attempts: dict[str, int] | None = None,
    image_versions: dict[str, str] | None = None,
    duplicate_core_node_in: str | None = None,
    partitioned: bool = False,
) -> Path:
    run_dir = root / f"run-{run_id}"
    shard_names = DURATION_MODULE["WINDOWS_SHARD_NAMES"] if partitioned else SHARD_NAMES
    for shard in shard_names:
        family = shard.rsplit("-", 1)[0] if partitioned else shard
        attempt = (attempts or {}).get(shard, 1)
        shard_dir = run_dir / f"windows-high-risk-{shard}-attempt-{attempt}"
        shard_dir.mkdir(parents=True)
        base_path = FILES_BY_SHARD[family]
        path = (base_path.replace(".py", f"_{shard.rsplit('-', 1)[1]}.py")
                if partitioned else base_path)
        metadata = {
            "schema_version": 1,
            "platform": "windows",
            "run_id": run_id,
            "run_attempt": attempt,
            "sha": sha,
            "shard": shard,
            "assignment_sha256": assignment_sha256,
            "runtime": {
                "python_version": "3.12.10",
                "runner_os": "Windows",
                "runner_arch": "X64",
                "image_os": "win25",
                "image_version": (image_versions or {}).get(
                    shard, "20260728.188.1"
                ),
            },
            "test_files": [path],
            "execution": {
                "parallel": {"workers": 3, "dist": "loadfile", "marker": "not ci_serial"},
                "serial": {"workers": 1, "marker": "ci_serial"},
            },
        }
        if partitioned:
            metadata["partition_sha256"] = "e" * 64
        (shard_dir / "windows-shard-metadata.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        suites = ET.Element("testsuites")
        suite = ET.SubElement(
            suites,
            "testsuite",
            tests="1",
            errors="0",
            failures="0",
        )
        testcase_path = (
            FILES_BY_SHARD["core"] if duplicate_core_node_in == shard else path
        )
        ET.SubElement(
            suite,
            "testcase",
            classname=_classname(testcase_path),
            name="test_case",
            time=str(seconds[base_path]),
        )
        ET.ElementTree(suites).write(
            shard_dir / "junit.xml", encoding="utf-8", xml_declaration=True
        )
        if partitioned:
            shutil.copyfile(shard_dir / "junit.xml", shard_dir / "junit.parallel.xml")
            (shard_dir / "junit.serial.xml").write_text(
                '<testsuites><testsuite tests="0" failures="0" errors="0"/></testsuites>',
                encoding="utf-8",
            )
    return run_dir


def test_duration_builder_aggregates_three_comparable_runs(tmp_path: Path) -> None:
    assignment = "a" * 64
    run_dirs = [
        _write_run(
            tmp_path,
            run_id=100 + index,
            sha=str(index) * 40,
            assignment_sha256=assignment,
            attempts={"recovery-migration": 2} if index == 2 else None,
            seconds={
                FILES_BY_SHARD["core"]: core,
                FILES_BY_SHARD["gateway-sqlite"]: gateway,
                FILES_BY_SHARD["recovery-migration"]: recovery,
                FILES_BY_SHARD["desktop-installer-contracts"]: desktop,
            },
        )
        for index, (core, gateway, recovery, desktop) in enumerate(
            ((1.0, 4.0, 7.0, 10.0), (3.0, 8.0, 9.0, 12.0), (2.0, 6.0, 8.0, 11.0)),
            start=1,
        )
    ]

    observations = [load_run_directory(path) for path in run_dirs]
    payload = build_duration_payload(
        observations, expected_assignment_sha256=assignment
    )

    assert payload == build_duration_payload(
        observations, expected_assignment_sha256=assignment
    )
    assert payload["weights_seconds"] == {
        FILES_BY_SHARD["core"]: 2.0,
        FILES_BY_SHARD["desktop-installer-contracts"]: 11.0,
        FILES_BY_SHARD["gateway-sqlite"]: 6.0,
        FILES_BY_SHARD["recovery-migration"]: 8.0,
    }
    samples = payload["samples"]
    assert isinstance(samples, dict)
    assert samples[FILES_BY_SHARD["gateway-sqlite"]] == {
        "count": 3,
        "median_seconds": 6.0,
        "p75_seconds": 7.0,
        "min_seconds": 4.0,
        "max_seconds": 8.0,
    }
    source_runs = payload["source_runs"]
    assert isinstance(source_runs, list)
    assert [run["id"] for run in source_runs] == [101, 102, 103]
    assert all(run["node_count"] == 4 for run in source_runs)
    assert source_runs[1]["attempts"] == {
        "core": 1,
        "gateway-sqlite": 1,
        "recovery-migration": 2,
        "desktop-installer-contracts": 1,
    }


def test_duration_builder_rejects_incomplete_shard_artifacts(tmp_path: Path) -> None:
    run_dir = _write_run(
        tmp_path,
        run_id=200,
        sha="b" * 40,
        assignment_sha256="c" * 64,
        seconds={path: 1.0 for path in FILES_BY_SHARD.values()},
    )
    metadata = next(
        (run_dir / "windows-high-risk-core-attempt-1").glob(
            "windows-shard-metadata.json"
        )
    )
    metadata.unlink()

    with pytest.raises(ValueError, match="expected 4 Windows shard metadata"):
        load_run_directory(run_dir)


def test_duration_builder_records_runner_image_patch_drift(tmp_path: Path) -> None:
    run_dir = _write_run(
        tmp_path,
        run_id=250,
        sha="c" * 40,
        assignment_sha256="d" * 64,
        image_versions={
            "core": "20260714.173.1",
            "gateway-sqlite": "20260728.188.1",
            "recovery-migration": "20260728.188.1",
            "desktop-installer-contracts": "20260714.173.1",
        },
        seconds={path: 1.0 for path in FILES_BY_SHARD.values()},
    )

    observation = load_run_directory(run_dir)

    assert observation.runtime_compatibility == {
        "python_version": "3.12.10",
        "runner_os": "Windows",
        "runner_arch": "X64",
        "image_os": "win25",
    }
    assert observation.image_versions_by_shard == {
        "core": "20260714.173.1",
        "gateway-sqlite": "20260728.188.1",
        "recovery-migration": "20260728.188.1",
        "desktop-installer-contracts": "20260714.173.1",
    }


def test_duration_builder_rejects_incompatible_shard_runtime(tmp_path: Path) -> None:
    run_dir = _write_run(
        tmp_path,
        run_id=275,
        sha="d" * 40,
        assignment_sha256="e" * 64,
        seconds={path: 1.0 for path in FILES_BY_SHARD.values()},
    )
    metadata_path = (
        run_dir
        / "windows-high-risk-recovery-migration-attempt-1"
        / "windows-shard-metadata.json"
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["runtime"]["runner_arch"] = "ARM64"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ValueError, match="runtime compatibility metadata"):
        load_run_directory(run_dir)


def test_duration_builder_rejects_duplicate_testcases(tmp_path: Path) -> None:
    run_dir = _write_run(
        tmp_path,
        run_id=300,
        sha="d" * 40,
        assignment_sha256="e" * 64,
        seconds={path: 1.0 for path in FILES_BY_SHARD.values()},
        duplicate_core_node_in="gateway-sqlite",
    )

    with pytest.raises(ValueError, match="duplicate JUnit testcase"):
        load_run_directory(run_dir)


def test_duration_builder_rejects_assignment_hash_drift(tmp_path: Path) -> None:
    observations = [
        load_run_directory(
            _write_run(
                tmp_path,
                run_id=400 + index,
                sha="f" * 40,
                assignment_sha256="1" * 64,
                seconds={path: 1.0 for path in FILES_BY_SHARD.values()},
            )
        )
        for index in range(3)
    ]

    with pytest.raises(ValueError, match="does not match the current snapshot"):
        build_duration_payload(
            observations, expected_assignment_sha256="2" * 64
        )


def _partitioned_observations(tmp_path: Path) -> list[Any]:
    return [
        load_run_directory(_write_run(
            tmp_path, run_id=500 + index, sha="a" * 40,
            assignment_sha256="b" * 64,
            seconds={path: float(index + 1) for path in FILES_BY_SHARD.values()},
            partitioned=True,
        ))
        for index in range(3)
    ]


def test_duration_builder_requires_all_ten_execution_shards(tmp_path: Path) -> None:
    observations = _partitioned_observations(tmp_path)
    payload = build_duration_payload(
        observations, expected_assignment_sha256="b" * 64,
        expected_partition_sha256="e" * 64,
    )
    assert len(payload["weights_seconds"]) == 10
    catalog = json.loads(Path(".github/ci/suites.v1.json").read_text(encoding="utf-8"))
    assert tuple(catalog["full_python_matrix"]["windows"]) == DURATION_MODULE["WINDOWS_SHARD_NAMES"]
    assert all(value == {"parallel": 2.0, "serial": 0.0}
               for value in payload["phase_weights_seconds"].values())
    assert len(payload["source_runs"][0]["execution"]) == 10
    metadata_path = (tmp_path / "run-500" / "windows-high-risk-gateway-sqlite-4-attempt-1"
                     / "windows-shard-metadata.json")
    metadata_path.unlink()
    with pytest.raises(ValueError, match="expected 10 Windows shard metadata"):
        load_run_directory(tmp_path / "run-500")


def test_duration_builder_rejects_worker_count_drift(tmp_path: Path) -> None:
    observations = _partitioned_observations(tmp_path)
    observations[1].execution_by_shard["core-1"]["parallel"]["workers"] = 4
    with pytest.raises(ValueError, match="execution configuration differs"):
        build_duration_payload(
            observations, expected_assignment_sha256="b" * 64,
            expected_partition_sha256="e" * 64,
        )


def test_duration_builder_rejects_stale_partition_evidence(tmp_path: Path) -> None:
    observations = _partitioned_observations(tmp_path)
    with pytest.raises(ValueError, match="partition hash does not match"):
        build_duration_payload(
            observations, expected_assignment_sha256="b" * 64,
            expected_partition_sha256="f" * 64,
        )


@pytest.mark.parametrize("mutation", ["missing", "different", "duplicate", "duration"])
def test_duration_builder_rejects_incomplete_phase_evidence(
    tmp_path: Path, mutation: str,
) -> None:
    _partitioned_observations(tmp_path)
    report = next((tmp_path / "run-500").rglob("junit.parallel.xml"))
    if mutation == "missing":
        report.unlink()
        message = "cannot read JUnit report"
    elif mutation == "different":
        report.write_text(report.read_text().replace('name="test_case"', 'name="other"'))
        message = "phase JUnit collection differs"
    elif mutation == "duration":
        report.write_text(report.read_text().replace('time="1.0"', 'time="900.0"'))
        message = "phase JUnit duration differs"
    else:
        shutil.copyfile(report, report.with_name("junit.serial.xml"))
        message = "duplicate JUnit testcase"
    with pytest.raises(ValueError, match=message):
        load_run_directory(tmp_path / "run-500")
