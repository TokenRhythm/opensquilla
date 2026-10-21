from __future__ import annotations

import copy
import json
import os
import runpy
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/bundle_test_shards.py"
MODULE = runpy.run_path(str(SCRIPT), run_name="bundle_shard_contract")
TEST_FILE = MODULE["TEST_FILE"]
partition_nodeids = MODULE["partition_nodeids"]
inventory_digest = MODULE["inventory_digest"]
run_shard = MODULE["run_shard"]
verify_reports = MODULE["verify_reports"]
IDENTITY = {"GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}


def _inventory(count: int = 66) -> list[str]:
    return [f"{TEST_FILE}::test_synthetic[{index:02d}]" for index in range(count)]


def _save(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _proofs(tmp_path: Path) -> list[Path]:
    inventory = _inventory()
    paths = []
    for shard in range(1, 5):
        selected = partition_nodeids(inventory, shard)
        path = tmp_path / f"bundle-shard-{shard}.json"
        junit = path.with_suffix(".xml")
        _save(
            path,
            {
                "schema_version": 1,
                "shard": shard,
                "shard_count": 4,
                "expected_count": 66,
                "identity": IDENTITY,
                "inventory": inventory,
                "inventory_sha256": inventory_digest(inventory),
                "selected": selected,
                "started": selected,
                "phases": [
                    {"nodeid": nodeid, "when": when, "outcome": "passed"}
                    for nodeid in selected
                    for when in ("setup", "call", "teardown")
                ],
                "errors": [],
                "pytest_exit_code": 0,
                "junit": junit.name,
            },
        )
        suite = ET.Element("testsuite")
        for nodeid in selected:
            case = ET.SubElement(suite, "testcase", name=nodeid)
            properties = ET.SubElement(case, "properties")
            ET.SubElement(properties, "property", name="bundle_nodeid", value=nodeid)
        ET.ElementTree(suite).write(junit, encoding="utf-8")
        paths.append(path)
    return paths


def test_current_bundle_collection_partitions_all_66_cases_exactly_once() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(ROOT), str(ROOT / "src")))
    environment["PYTEST_ADDOPTS"] = ""
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_FILE, "--collect-only", "-q", "-o", "addopts="],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    inventory = [line.strip() for line in result.stdout.splitlines() if line.startswith(TEST_FILE)]
    assert len(inventory) == 66
    partitions = [partition_nodeids(inventory, shard) for shard in range(1, 5)]
    assert [len(partition) for partition in partitions] == [17, 17, 16, 16]
    assert Counter(nodeid for partition in partitions for nodeid in partition) == Counter(inventory)
    assert partition_nodeids(list(reversed(inventory)), 2) == partitions[1]


@pytest.mark.parametrize("shard", [0, 5, True, "1"])
def test_partition_rejects_invalid_shard(shard: Any) -> None:
    with pytest.raises(ValueError, match="integer from 1 through 4"):
        partition_nodeids(_inventory(), shard)


@pytest.mark.parametrize("change", ["missing", "duplicate", "foreign"])
def test_partition_rejects_changed_inventory(change: str) -> None:
    inventory = _inventory()
    if change == "missing":
        inventory.pop()
    elif change == "duplicate":
        inventory[-1] = inventory[0]
    else:
        inventory[-1] = "tests/test_other.py::test_foreign"
    with pytest.raises(ValueError):
        partition_nodeids(inventory, 1)


def test_verifier_accepts_complete_identical_inventory_and_execution(tmp_path: Path) -> None:
    _proofs(tmp_path)
    result = verify_reports(tmp_path, expected_identity=IDENTITY)
    assert result["coverage_complete"] is True
    assert result["case_count"] == 66
    assert result["outcomes"] == {"passed": 66}
    assert result["shard_counts"] == {"1": 17, "2": 17, "3": 16, "4": 16}


@pytest.mark.parametrize(
    "change",
    [
        "missing_shard", "duplicate_shard", "different_inventory", "digest",
        "source", "attempt", "missing_selection", "duplicate_selection",
        "missing_start", "duplicate_start", "missing_phase", "duplicate_phase",
        "missing_junit", "duplicate_junit", "foreign_junit", "collection_error",
        "pytest_interrupted", "false_success", "schema", "count", "junit_path", "skipped",
    ],
)
def test_verifier_fails_closed_on_incomplete_or_inconsistent_evidence(
    tmp_path: Path, change: str
) -> None:
    paths = _proofs(tmp_path)
    path = paths[1]
    report = json.loads(path.read_text(encoding="utf-8"))
    if change == "missing_shard":
        path.unlink()
    elif change == "duplicate_shard":
        report["shard"] = 1
    elif change == "different_inventory":
        report["inventory"][-1] += "-changed"
        report["inventory_sha256"] = inventory_digest(report["inventory"])
    elif change == "digest":
        report["inventory_sha256"] = "0" * 64
    elif change in {"source", "attempt"}:
        key = "GITHUB_SHA" if change == "source" else "GITHUB_RUN_ATTEMPT"
        report["identity"][key] = "different"
    elif change == "missing_selection":
        report["selected"].pop()
    elif change == "duplicate_selection":
        report["selected"].append(report["selected"][0])
    elif change == "missing_start":
        report["started"].pop()
    elif change == "duplicate_start":
        report["started"].append(report["started"][0])
    elif change == "missing_phase":
        report["phases"].pop()
    elif change == "duplicate_phase":
        report["phases"].append(report["phases"][0])
    elif change in {"missing_junit", "duplicate_junit", "foreign_junit"}:
        tree = ET.parse(path.with_suffix(".xml"))
        suite = tree.getroot()
        if change == "missing_junit":
            suite.remove(suite[0])
        elif change == "duplicate_junit":
            suite.append(copy.deepcopy(suite[0]))
        else:
            suite[0].find("./properties/property").set("value", "foreign::test")
        tree.write(path.with_suffix(".xml"))
    elif change == "collection_error":
        report["errors"].append("collection failed")
    elif change == "pytest_interrupted":
        report["pytest_exit_code"] = 2
    elif change == "false_success":
        report["phases"][1]["outcome"] = "failed"
    elif change == "skipped":
        report["phases"][1]["outcome"] = "skipped"
        tree = ET.parse(path.with_suffix(".xml"))
        ET.SubElement(tree.getroot()[0], "skipped")
        tree.write(path.with_suffix(".xml"))
    elif change == "schema":
        report["schema_version"] = 999
    elif change == "count":
        report["expected_count"] = 65
    elif change == "junit_path":
        report["junit"] = "../elsewhere.xml"
    if change != "missing_shard":
        _save(path, report)
    with pytest.raises(ValueError):
        verify_reports(tmp_path)


def test_verifier_rejects_previous_run_evidence(tmp_path: Path) -> None:
    _proofs(tmp_path)
    with pytest.raises(ValueError, match="expected source/run/attempt"):
        verify_reports(tmp_path, expected_identity={**IDENTITY, "GITHUB_RUN_ID": "124"})


def _synthetic_root(tmp_path: Path, source: str) -> Path:
    root = tmp_path / "repository"
    target = root / TEST_FILE
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\nmarkers = ci_serial\n", encoding="utf-8")
    return root


def test_real_subprocesses_execute_all_cases_and_preserve_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Include a setup failure and a call+teardown failure to exercise pytest's
    # real lifecycle and its two-record JUnit representation for the latter.
    root = _synthetic_root(
        tmp_path,
        "import os\nimport pytest\n"
        "@pytest.fixture\n"
        "def sample(request):\n"
        "    assert not any(name in os.environ for name in (\n"
        "        'PYTEST_XDIST_WORKER', 'PYTEST_XDIST_WORKER_COUNT',\n"
        "        'PYTEST_XDIST_TESTRUNUID', 'OPENSQUILLA_PYTEST_XDIST_SCOPE'))\n"
        "    value = request.param\n"
        "    if value == 0: raise RuntimeError('setup failure')\n"
        "    yield value\n"
        "    if value == 1: raise RuntimeError('teardown failure')\n"
        "@pytest.mark.parametrize('sample', range(8), indirect=True)\n"
        "def test_case(sample):\n"
        "    assert sample != 1\n",
    )
    for key, value in IDENTITY.items():
        monkeypatch.setenv(key, value)
    # These inherited options would omit tests and abort early if not cleared.
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k never_matches --maxfail=1")
    for key in (
        "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID",
        "OPENSQUILLA_PYTEST_XDIST_SCOPE",
    ):
        monkeypatch.setenv(key, "outer-worker")
    output = tmp_path / "reports"
    exit_codes = [run_shard(root, shard, output, expected_count=8) for shard in range(1, 5)]
    assert exit_codes == [1, 1, 0, 0]
    result = verify_reports(output, expected_count=8, expected_identity=IDENTITY)
    assert result["outcomes"] == {"failed": 2, "passed": 6}
    assert result["case_count"] == 8


@pytest.mark.parametrize("source", [
    "import pytest\n@pytest.mark.ci_serial\ndef test_case(): pass\n",
    "def test_case(:\n",
])
def test_real_subprocess_rejects_serial_marker_and_collection_failure(
    tmp_path: Path, source: str
) -> None:
    root = _synthetic_root(tmp_path, source)
    output = tmp_path / "reports"
    assert run_shard(root, 1, output, expected_count=1) != 0
    report = json.loads((output / "bundle-shard-1.json").read_text(encoding="utf-8"))
    assert report["errors"]
    assert report["started"] == []
