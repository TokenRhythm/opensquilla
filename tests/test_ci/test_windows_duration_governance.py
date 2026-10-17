from __future__ import annotations

import json
import runpy
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
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


def test_duration_builder_requires_all_twenty_execution_shards(tmp_path: Path) -> None:
    observations = _partitioned_observations(tmp_path)
    payload = build_duration_payload(
        observations, expected_assignment_sha256="b" * 64,
        expected_partition_sha256="e" * 64,
    )
    assert len(payload["weights_seconds"]) == 20
    catalog = json.loads(Path(".github/ci/suites.v1.json").read_text(encoding="utf-8"))
    assert tuple(catalog["full_python_matrix"]["windows"]) == DURATION_MODULE["WINDOWS_SHARD_NAMES"]
    assert all(value == {"parallel": 2.0, "serial": 0.0}
               for value in payload["phase_weights_seconds"].values())
    assert len(payload["source_runs"][0]["execution"]) == 20
    metadata_path = (tmp_path / "run-500" / "windows-high-risk-gateway-sqlite-4-attempt-1"
                     / "windows-shard-metadata.json")
    metadata_path.unlink()
    with pytest.raises(ValueError, match="expected 20 Windows shard metadata"):
        load_run_directory(tmp_path / "run-500")


def _write_delegated_bundle_run(root: Path, run_id: int = 700) -> Path:
    run_dir = _write_run(
        root, run_id=run_id, sha="a" * 40, assignment_sha256="b" * 64,
        seconds={path: 1.0 for path in FILES_BY_SHARD.values()}, partitioned=True,
    )
    owner_path = (run_dir / "windows-high-risk-desktop-installer-contracts-1-attempt-1"
                  / "windows-shard-metadata.json")
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    bundle = DURATION_MODULE["BUNDLE_TEST_FILE"]
    owner["delegated_case_files"] = [{
        "path": bundle, "shards": list(DURATION_MODULE["BUNDLE_SHARDS"]),
    }]
    owner_path.write_text(json.dumps(owner), encoding="utf-8")
    helper = runpy.run_path(".github/scripts/bundle_test_shards.py")
    inventory = [f"{bundle}::test_case[{index:02d}]" for index in range(66)]
    for part in range(1, 5):
        directory = run_dir / f"windows-bundle-{part}"
        directory.mkdir()
        selected = helper["partition_nodeids"](inventory, part)
        junit = directory / f"bundle-shard-{part}.xml"
        report = {
            "schema_version": 1, "shard": part, "shard_count": 4, "expected_count": 66,
            "identity": {"GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": str(run_id),
                         "GITHUB_RUN_ATTEMPT": "1"},
            "runtime": owner["runtime"], "inventory": inventory,
            "inventory_sha256": helper["inventory_digest"](inventory),
            "selected": selected, "started": selected,
            "phases": [
                {"nodeid": nodeid, "when": when, "outcome": "passed"}
                for nodeid in selected for when in ("setup", "call", "teardown")
            ],
            "errors": [], "pytest_exit_code": 0, "junit": junit.name,
        }
        junit.with_suffix(".json").write_text(json.dumps(report), encoding="utf-8")
        suite = ET.Element("testsuite", tests=str(len(selected)), failures="0", errors="0")
        for nodeid in selected:
            case = ET.SubElement(
                suite, "testcase", classname=_classname(bundle),
                name=nodeid.split("::", 1)[1], time="2.0",
            )
            properties = ET.SubElement(case, "properties")
            ET.SubElement(properties, "property", name="bundle_nodeid", value=nodeid)
        ET.ElementTree(suite).write(junit, encoding="utf-8")
    return run_dir


def test_duration_builder_consumes_all_four_delegated_bundle_reports(tmp_path: Path) -> None:
    observations = [
        load_run_directory(_write_delegated_bundle_run(tmp_path, 700 + index))
        for index in range(3)
    ]
    bundle = DURATION_MODULE["BUNDLE_TEST_FILE"]
    assert all(len(run.node_ids) == 86 for run in observations)
    assert all(run.file_seconds[bundle] == 132.0 for run in observations)
    assert bundle in observations[0].files_by_shard["desktop-installer-contracts-1"]
    payload = build_duration_payload(
        observations, expected_assignment_sha256="b" * 64,
        expected_partition_sha256="e" * 64,
    )
    assert payload["weights_seconds"][bundle] == 132.0
    assert payload["phase_weights_seconds"][bundle] == {"parallel": 132.0, "serial": 0.0}
    source = payload["source_runs"][0]
    assert all(len(source[field]) == 24 for field in ("execution", "attempts", "image_versions"))
    assert source["execution"]["bundle-1"]["workers"] == 1


@pytest.mark.parametrize("mutation", [
    "missing_report", "missing_junit", "wrong_run", "wrong_attempt", "wrong_runtime",
    "missing_runtime", "duplicate_report", "duplicate_normal_owner", "missing_delegation",
    "partial_delegation", "wrong_delegator", "incomplete_execution", "failed_case",
])
def test_duration_builder_rejects_incomplete_or_incompatible_bundle_evidence(
    tmp_path: Path, mutation: str,
) -> None:
    run_dir = _write_delegated_bundle_run(tmp_path)
    owner_path = (run_dir / "windows-high-risk-desktop-installer-contracts-1-attempt-1"
                  / "windows-shard-metadata.json")
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    path = run_dir / "windows-bundle-1" / "bundle-shard-1.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "missing_report":
        path.unlink()
    elif mutation == "missing_junit":
        path.with_suffix(".xml").unlink()
    elif mutation == "wrong_run":
        report["identity"]["GITHUB_RUN_ID"] = "999"
    elif mutation == "wrong_attempt":
        report["identity"]["GITHUB_RUN_ATTEMPT"] = "2"
    elif mutation == "wrong_runtime":
        report["runtime"]["python_version"] = "3.11.0"
    elif mutation == "missing_runtime":
        report.pop("runtime")
    elif mutation == "duplicate_report":
        path.with_name("bundle-shard-duplicate.json").write_text(
            json.dumps(report), encoding="utf-8",
        )
    elif mutation == "duplicate_normal_owner":
        owner["test_files"].append(DURATION_MODULE["BUNDLE_TEST_FILE"])
        owner["test_files"].sort()
    elif mutation == "missing_delegation":
        owner.pop("delegated_case_files")
    elif mutation == "partial_delegation":
        owner["delegated_case_files"][0]["shards"].pop()
    elif mutation == "wrong_delegator":
        other_path = run_dir / "windows-high-risk-core-1-attempt-1" / owner_path.name
        other = json.loads(other_path.read_text(encoding="utf-8"))
        other["delegated_case_files"] = owner.pop("delegated_case_files")
        other_path.write_text(json.dumps(other), encoding="utf-8")
    elif mutation == "incomplete_execution":
        report["started"].pop()
    elif mutation == "failed_case":
        report["phases"][1]["outcome"] = "failed"
        report["pytest_exit_code"] = 1
        junit = ET.parse(path.with_suffix(".xml"))
        ET.SubElement(next(junit.iter("testcase")), "failure", message="test failed")
        junit.write(path.with_suffix(".xml"), encoding="utf-8")
    if mutation != "missing_report":
        path.write_text(json.dumps(report), encoding="utf-8")
    owner_path.write_text(json.dumps(owner), encoding="utf-8")
    with pytest.raises(ValueError):
        load_run_directory(run_dir)


@pytest.mark.parametrize("mutation", [
    "workers", "marker_base", "legacy_and_filtered", "item_parallel_files",
])
def test_duration_builder_rejects_execution_drift(tmp_path: Path, mutation: str) -> None:
    observations = _partitioned_observations(tmp_path)
    if mutation == "workers":
        observations[1].execution_by_shard["core-1"]["parallel"]["workers"] = 4
    elif mutation == "item_parallel_files":
        observations[1].execution_by_shard["core-1"]["parallel"]["item_parallel_files"] = [
            "tests/test_core_1.py",
        ]
    else:
        for index, observation in enumerate(observations):
            if mutation == "legacy_and_filtered" and index == 0:
                continue
            base = "not llm" if mutation == "marker_base" and index == 1 else "not stress"
            for execution in observation.execution_by_shard.values():
                execution["parallel"]["marker"] = f"({base}) and (not ci_serial)"
                execution["serial"]["marker"] = f"({base}) and (ci_serial)"
    with pytest.raises(ValueError, match="execution configuration differs"):
        build_duration_payload(
            observations, expected_assignment_sha256="b" * 64,
            expected_partition_sha256="e" * 64,
        )


@pytest.mark.parametrize("item_parallel", [False, True])
def test_duration_builder_normalizes_item_parallel_policy(
    tmp_path: Path, item_parallel: bool,
) -> None:
    observations = []
    for index in range(3):
        run_dir = _write_run(
            tmp_path, run_id=800 + index, sha="a" * 40,
            assignment_sha256="b" * 64,
            seconds={path: float(index + 1) for path in FILES_BY_SHARD.values()},
            partitioned=True,
        )
        for metadata_path in run_dir.rglob("windows-shard-metadata.json"):
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if item_parallel and metadata["shard"] == "core-1":
                metadata["execution"]["parallel"]["item_parallel_files"] = metadata[
                    "test_files"
                ]
            elif index > 0:
                # Legacy missing policy and a new explicit empty policy have
                # exactly the same whole-file scheduling semantics.
                metadata["execution"]["parallel"]["item_parallel_files"] = []
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        observations.append(load_run_directory(run_dir))

    payload = build_duration_payload(
        observations, expected_assignment_sha256="b" * 64,
        expected_partition_sha256="e" * 64,
    )
    for source_run in payload["source_runs"]:
        for shard, execution in source_run["execution"].items():
            assert execution["parallel"]["item_parallel_files"] == (
                ["tests/test_core_1.py"] if item_parallel and shard == "core-1" else []
            )


@pytest.mark.parametrize("policy", [
    None,
    False,
    "tests/test_core_1.py",
    {},
    [None],
    ["tests/test_core_1.py", "tests/test_core_1.py"],
    ["tests/test_other.py", "tests/test_core_1.py"],
    ["tests/../test_escape.py"],
    ["tests\\test_core_1.py"],
    ["/tests/test_core_1.py"],
    ["tests/helper.py"],
    ["tests/test_core_1.txt"],
    ["tests/test_other.py"],
])
def test_duration_builder_rejects_invalid_item_parallel_policy(
    tmp_path: Path, policy: object,
) -> None:
    run_dir = _write_run(
        tmp_path, run_id=850, sha="a" * 40, assignment_sha256="b" * 64,
        seconds={path: 1.0 for path in FILES_BY_SHARD.values()}, partitioned=True,
    )
    metadata_path = (
        run_dir / "windows-high-risk-core-1-attempt-1" / "windows-shard-metadata.json"
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["execution"]["parallel"]["item_parallel_files"] = policy
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ValueError, match="item_parallel_files|test path"):
        load_run_directory(run_dir)


@pytest.mark.parametrize("marker", [
    None,
    "",
    "not stress and not llm and not live_search and not live_skill_hub and not live_channel "
    "and not webui_browser and not tui_real_terminal and not local_golden "
    "and not agent_context_boundary and not llm_router_acc",
], ids=["default", "explicit-all", "ci-offline"])
def test_duration_builder_consumes_runner_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str | None,
) -> None:
    runner = runpy.run_path(".github/scripts/windows_test_shards.py")
    write_metadata = runner["_write_run_metadata"]
    assignment = runner["assignment_snapshot_fingerprint"]()
    partition = runner["partition_snapshot_fingerprint"]()
    observations = []
    for index in range(3):
        run_dir = _write_run(
            tmp_path, run_id=600 + index, sha="a" * 40,
            assignment_sha256=assignment,
            seconds={path: float(index + 1) for path in FILES_BY_SHARD.values()},
            partitioned=True,
        )
        # Simulate only the CI process environment, without changing os.name
        # globally or depending on the host running this governance test.
        environment = {
            "GITHUB_RUN_ID": str(600 + index), "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_SHA": "a" * 40, "RUNNER_OS": "Windows",
            "RUNNER_ARCH": "X64", "ImageOS": "win25", "ImageVersion": "test",
        }
        monkeypatch.setitem(
            write_metadata.__globals__, "os", SimpleNamespace(name="nt", environ=environment),
        )
        for metadata_path in run_dir.rglob("windows-shard-metadata.json"):
            previous = json.loads(metadata_path.read_text(encoding="utf-8"))
            write_metadata(
                metadata_path, previous["shard"], tuple(previous["test_files"]),
                parallel_workers=3, marker_expression=marker,
            )
        observations.append(load_run_directory(run_dir))

    payload = build_duration_payload(
        observations, expected_assignment_sha256=assignment,
        expected_partition_sha256=partition,
    )
    assert len(payload["weights_seconds"]) == 20
    base = "not stress" if marker is None else marker
    for execution in payload["source_runs"][0]["execution"].values():
        assert execution["parallel"]["marker"] == (
            f"({base}) and (not ci_serial)" if base else "not ci_serial"
        )
        assert execution["serial"]["marker"] == (
            f"({base}) and (ci_serial)" if base else "ci_serial"
        )


@pytest.mark.parametrize(("parallel", "serial"), [
    ("(not stress) and (not ci_serial)", "(not llm) and (ci_serial)"),
    ("(not stress) and (ci_serial)", "(not stress) and (not ci_serial)"),
    ("(not stress) and (not ci_serial)", "ci_serial"),
    ("not ci_serial", "(not stress) and (ci_serial)"),
    ("() and (not ci_serial)", "() and (ci_serial)"),
    (None, "ci_serial"),
    ("not ci_serial", ["ci_serial"]),
])
def test_duration_builder_rejects_mismatched_phase_markers(
    tmp_path: Path, parallel: object, serial: object,
) -> None:
    run_dir = _write_run(
        tmp_path, run_id=700, sha="a" * 40, assignment_sha256="b" * 64,
        seconds={path: 1.0 for path in FILES_BY_SHARD.values()}, partitioned=True,
    )
    metadata_path = next(run_dir.rglob("windows-shard-metadata.json"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["execution"]["parallel"]["marker"] = parallel
    metadata["execution"]["serial"]["marker"] = serial
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid execution configuration"):
        load_run_directory(run_dir)


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
