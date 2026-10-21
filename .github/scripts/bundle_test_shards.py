#!/usr/bin/env python3
"""Run the audited bundle cases on four runners and prove exact-once coverage.

Each runner collects the original file, sorts its complete nodeid inventory,
and executes one ordinal-modulo-four partition without in-process concurrency.
Evidence includes pytest lifecycle records and JUnit nodeids, so collecting or
selecting a case alone never counts as executing it. Failed cases still count
as coverage, while the run command preserves pytest's failing exit status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

TEST_FILE = "tests/test_observability/test_bundle.py"
SHARD_COUNT = 4
EXPECTED_CASES = 66
SCHEMA_VERSION = 1
_PLUGIN_ENV = "OPENSQUILLA_BUNDLE_SHARD_CONFIG"
_IDENTITY_KEYS = ("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")
_FORBIDDEN_MARKERS = frozenset(
    {
        "ci_serial", "stress", "llm", "live_channel", "live_search", "live_skill_hub",
        "webui_browser", "tui_real_terminal", "local_golden", "agent_context_boundary",
        "llm_router_acc",
    }
)


def inventory_digest(nodeids: list[str]) -> str:
    return hashlib.sha256("\n".join(nodeids).encode("utf-8")).hexdigest()


def partition_nodeids(
    nodeids: list[str], shard: int, *, expected_count: int = EXPECTED_CASES
) -> list[str]:
    """Validate the full inventory before selecting its deterministic partition."""
    if type(shard) is not int or shard not in range(1, SHARD_COUNT + 1):
        raise ValueError("shard must be an integer from 1 through 4")
    if len(nodeids) != expected_count or len(set(nodeids)) != expected_count:
        raise ValueError(f"expected {expected_count} unique bundle cases")
    if any(not nodeid.startswith(f"{TEST_FILE}::") for nodeid in nodeids):
        raise ValueError("inventory contains a case outside the audited bundle file")
    return sorted(nodeids)[shard - 1 :: SHARD_COUNT]


def _identity() -> dict[str, str]:
    return {key: os.environ.get(key, "local") for key in _IDENTITY_KEYS}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class _ShardPlugin:
    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self.inventory: list[str] = []
        self.selected: list[str] = []
        self.started: list[str] = []
        self.phases: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.started_at = time.monotonic()

    def pytest_collection_modifyitems(self, config: Any, items: list[Any]) -> None:
        import pytest

        self.inventory = sorted(item.nodeid for item in items)
        try:
            self.selected = partition_nodeids(
                self.inventory,
                self.settings["shard"],
                expected_count=self.settings["expected_count"],
            )
            for item in items:
                forbidden = _FORBIDDEN_MARKERS.intersection(
                    marker.name for marker in item.iter_markers()
                )
                if forbidden:
                    raise ValueError(f"{item.nodeid}: unaudited markers {sorted(forbidden)}")
            if config.getoption("numprocesses", default=None) not in (None, 0):
                raise ValueError("bundle partitions must execute serially within each runner")
        except ValueError as error:
            self.errors.append(str(error))
            raise pytest.UsageError(str(error)) from error
        selected = set(self.selected)
        deselected = [item for item in items if item.nodeid not in selected]
        items[:] = sorted(
            (item for item in items if item.nodeid in selected), key=lambda item: item.nodeid
        )
        for item in items:
            item.user_properties.append(("bundle_nodeid", item.nodeid))
        config.hook.pytest_deselected(items=deselected)

    def pytest_collectreport(self, report: Any) -> None:
        if report.failed:
            self.errors.append(f"collection failed: {report.nodeid}")

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        self.started.append(nodeid)

    def pytest_runtest_logreport(self, report: Any) -> None:
        self.phases.append(
            {
                "nodeid": report.nodeid,
                "when": report.when,
                "outcome": report.outcome,
                "duration_seconds": report.duration,
            }
        )

    def pytest_sessionfinish(self, exitstatus: int) -> None:
        _write_json(
            Path(self.settings["report"]),
            {
                "schema_version": SCHEMA_VERSION,
                "shard": self.settings["shard"],
                "shard_count": SHARD_COUNT,
                "expected_count": self.settings["expected_count"],
                "identity": self.settings["identity"],
                "runtime": {
                    "python_version": platform.python_version(),
                    "runner_os": os.environ.get("RUNNER_OS"),
                    "runner_arch": os.environ.get("RUNNER_ARCH"),
                    "image_os": os.environ.get("ImageOS"),
                    "image_version": os.environ.get("ImageVersion"),
                },
                "inventory": self.inventory,
                "inventory_sha256": inventory_digest(self.inventory),
                "selected": self.selected,
                "started": self.started,
                "phases": self.phases,
                "errors": self.errors,
                "pytest_exit_code": int(exitstatus),
                "elapsed_seconds": time.monotonic() - self.started_at,
                "junit": Path(self.settings["junit"]).name,
            },
        )


# Run after pytest's own -k/-m selection; a narrowed collection fails the full
# inventory contract instead of quietly losing cases. The CLI clears addopts.
_ShardPlugin.pytest_collection_modifyitems.pytest_impl = {"trylast": True}


def pytest_configure(config: Any) -> None:
    settings = os.environ.get(_PLUGIN_ENV)
    if settings:
        config.pluginmanager.register(_ShardPlugin(json.loads(settings)), "bundle-shard-proof")


def run_shard(
    root: Path,
    shard: int,
    output_dir: Path,
    *,
    expected_count: int = EXPECTED_CASES,
) -> int:
    if type(shard) is not int or shard not in range(1, SHARD_COUNT + 1):
        raise ValueError("shard must be an integer from 1 through 4")
    root, output_dir = root.resolve(), output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = output_dir / f"bundle-shard-{shard}.json"
    junit = output_dir / f"bundle-shard-{shard}.xml"
    # Stale evidence cannot make a failed launch appear to have executed tests.
    for path in (report, junit):
        path.unlink(missing_ok=True)
    environment = os.environ.copy()
    # The runner's own contract tests can execute inside a parent xdist worker.
    # A new serial pytest process must not inherit that worker's identity.
    for key in (
        "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID",
        "OPENSQUILLA_PYTEST_XDIST_SCOPE",
    ):
        environment.pop(key, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTEST_ADDOPTS"] = ""
    environment["PYTHONPATH"] = os.pathsep.join(
        str(path)
        for path in (Path(__file__).resolve().parent, root, root / "src")
    )
    environment[_PLUGIN_ENV] = json.dumps(
        {
            "shard": shard,
            "expected_count": expected_count,
            "report": str(report),
            "junit": str(junit),
            "identity": _identity(),
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            TEST_FILE,
            "-p",
            "bundle_test_shards",
            "-o",
            "addopts=",
            "-n",
            "0",
            "-vv",
            "--tb=short",
            "-o",
            # Preserve the Windows CI guard against faulthandler's native AV.
            "faulthandler_timeout=0",
            "--durations=0",
            f"--junitxml={junit}",
            f"--basetemp={output_dir / f'tmp-{shard}'}",
        ],
        cwd=root,
        env=environment,
        check=False,
    )
    if result.returncode == 0 and not report.is_file():
        raise ValueError("pytest exited successfully without bundle execution evidence")
    return int(result.returncode)


def _verify_execution(report: dict[str, Any], junit: Path) -> Counter[str]:
    selected = report["selected"]
    if Counter(report["started"]) != Counter(selected):
        raise ValueError("execution starts do not match the selected cases exactly once")
    phases: dict[str, dict[str, str]] = defaultdict(dict)
    for phase in report["phases"]:
        nodeid, when, outcome = phase["nodeid"], phase["when"], phase["outcome"]
        if nodeid not in selected or when not in {"setup", "call", "teardown"}:
            raise ValueError("unexpected pytest phase or case")
        if when in phases[nodeid] or outcome not in {"passed", "failed", "skipped"}:
            raise ValueError("duplicate pytest phase or invalid outcome")
        phases[nodeid][when] = outcome
    expected_junit: dict[str, Counter[str]] = {}
    outcomes: Counter[str] = Counter()
    for nodeid in selected:
        case = phases[nodeid]
        required = {"setup", "teardown"}
        if case.get("setup") == "passed":
            required.add("call")
        if set(case) != required:
            raise ValueError(f"incomplete pytest lifecycle: {nodeid}")
        if case.get("teardown") == "skipped":
            raise ValueError(f"incomplete teardown: {nodeid}")
        outcome = (
            "failed" if "failed" in case.values()
            else "skipped" if "skipped" in case.values()
            else "passed"
        )
        if "skipped" in case.values():
            raise ValueError(f"audited bundle cases must execute without skips: {nodeid}")
        outcomes[outcome] += 1
        # pytest emits two JUnit testcase records when both call and teardown
        # fail. Lifecycle records above still prove only one test execution.
        junit_outcomes = [outcome]
        if case.get("call") == "failed" and case["teardown"] == "failed":
            junit_outcomes.append("failed")
        expected_junit[nodeid] = Counter(junit_outcomes)
    actual_junit: dict[str, Counter[str]] = defaultdict(Counter)
    for case in ET.parse(junit).iter("testcase"):
        properties = [
            prop.get("value")
            for prop in case.findall("./properties/property")
            if prop.get("name") == "bundle_nodeid"
        ]
        if len(properties) != 1 or properties[0] not in selected:
            raise ValueError("JUnit has a missing, duplicate, or unexpected bundle nodeid")
        outcome = (
            "failed" if case.find("failure") is not None or case.find("error") is not None
            else "skipped" if case.find("skipped") is not None
            else "passed"
        )
        actual_junit[properties[0]][outcome] += 1
    if dict(actual_junit) != expected_junit:
        raise ValueError("JUnit execution results do not match pytest lifecycle evidence")
    if report["pytest_exit_code"] not in (0, 1):
        raise ValueError("pytest did not complete normal test execution")
    if bool(outcomes["failed"]) != bool(report["pytest_exit_code"]):
        raise ValueError("pytest exit code contradicts the execution results")
    return outcomes


def verify_reports(
    reports_dir: Path,
    *,
    expected_count: int = EXPECTED_CASES,
    expected_identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    paths = sorted(reports_dir.rglob("bundle-shard-*.json"))
    if len(paths) != SHARD_COUNT:
        raise ValueError("expected exactly four bundle shard reports")
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if sorted(report["shard"] for report in reports) != list(range(1, SHARD_COUNT + 1)):
        raise ValueError("missing or duplicate bundle shard indices")
    inventory = reports[0]["inventory"]
    identity = reports[0]["identity"]
    if set(identity) != set(_IDENTITY_KEYS) or not all(identity.values()):
        raise ValueError("missing source/run/attempt identity")
    if expected_identity is not None and identity != expected_identity:
        raise ValueError("reports do not belong to the expected source/run/attempt")
    selected_all: list[str] = []
    outcomes: Counter[str] = Counter()
    for path, report in zip(paths, reports, strict=True):
        if report["schema_version"] != SCHEMA_VERSION or report["shard_count"] != SHARD_COUNT:
            raise ValueError("unsupported bundle evidence schema or shard count")
        if report["expected_count"] != expected_count or report["errors"]:
            raise ValueError("collection contract failed")
        if report["inventory"] != inventory or report["inventory"] != sorted(inventory):
            raise ValueError("runners collected different or unsorted inventories")
        if report["inventory_sha256"] != inventory_digest(inventory):
            raise ValueError("bundle inventory digest mismatch")
        if report["identity"] != identity:
            raise ValueError("runners have different source/run/attempt identities")
        expected = partition_nodeids(inventory, report["shard"], expected_count=expected_count)
        if report["selected"] != expected:
            raise ValueError("shard selection does not match its complete inventory partition")
        junit_name = f"bundle-shard-{report['shard']}.xml"
        if report["junit"] != junit_name:
            raise ValueError("unexpected JUnit evidence path")
        outcomes.update(_verify_execution(report, path.with_name(junit_name)))
        selected_all.extend(report["selected"])
    if Counter(selected_all) != Counter(inventory):
        raise ValueError("bundle coverage is not exactly once")
    return {
        "coverage_complete": True,
        "identity": identity,
        "inventory_sha256": inventory_digest(inventory),
        "case_count": len(inventory),
        "shard_counts": {str(report["shard"]): len(report["selected"]) for report in reports},
        "outcomes": dict(outcomes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--root", type=Path, default=Path.cwd())
    run.add_argument("--shard", type=int, choices=range(1, SHARD_COUNT + 1), required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--reports-dir", type=Path, required=True)
    verify.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "run":
            return run_shard(args.root, args.shard, args.output_dir)
        identity = _identity() if any(key in os.environ for key in _IDENTITY_KEYS) else None
        result = verify_reports(args.reports_dir, expected_identity=identity)
        if args.output:
            _write_json(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ValueError, KeyError, TypeError, OSError, ET.ParseError) as error:
        print(f"bundle shard contract failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
