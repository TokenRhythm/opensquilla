#!/usr/bin/env python3
"""Route offline pytest files into stable Windows CI responsibility shards."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Final

# Skill manifests fingerprint complete bundled trees.  Keep imports performed
# by this runner and both pytest phases from creating derived files in those
# trees while they are being scanned.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

SHARD_NAMES: Final[tuple[str, ...]] = (
    "core",
    "gateway-sqlite",
    "recovery-migration",
    "desktop-installer-contracts",
)
DEFAULT_PARALLEL_WORKERS: Final[int] = 4
_CORE_WHEEL_FIXTURE: Final[str] = "isolated_core_wheel"
_CORE_WHEEL_ENV: Final[str] = "OPENSQUILLA_TEST_CORE_WHEEL"
_CORE_WHEEL_SHA_ENV: Final[str] = "OPENSQUILLA_TEST_CORE_WHEEL_SHA256"

_GATEWAY_SQLITE_PREFIXES: Final[tuple[str, ...]] = (
    "tests/test_gateway/",
    "tests/test_health/",
    "tests/test_observability/",
    "tests/test_persistence/",
    "tests/test_scheduler/",
    "tests/test_search/",
    "tests/test_session/",
)
_RECOVERY_MIGRATION_PREFIXES: Final[tuple[str, ...]] = (
    "tests/test_migration/",
    "tests/test_migrations/",
    "tests/test_recovery/",
)
_DESKTOP_INSTALLER_PREFIXES: Final[tuple[str, ...]] = (
    "tests/test_desktop/",
    "tests/test_dist/",
    "tests/test_packaging/",
    "tests/test_uninstall/",
)

_GATEWAY_SQLITE_NAME_TOKENS: Final[tuple[str, ...]] = (
    "database",
    "gateway",
    "memory",
    "scheduler",
    "session",
    "sqlite",
)
_RECOVERY_MIGRATION_NAME_TOKENS: Final[tuple[str, ...]] = (
    "legacy_config",
    "migrate",
    "migration",
    "recovery",
)
_DESKTOP_INSTALLER_NAME_TOKENS: Final[tuple[str, ...]] = (
    "artifact",
    "desktop",
    "install",
    "release",
    "uninstall",
    "wheelhouse",
)
_DESKTOP_INSTALLER_EXACT: Final[frozenset[str]] = frozenset(
    {
        "tests/test_compose_yaml_shape.py",
        "tests/test_root_start_scripts.py",
    }
)
_CORE_EXACT: Final[frozenset[str]] = frozenset(
    {
        "tests/test_ci/test_router_artifact_manifest.py",
        # This parity test executes Bun. Only the core job installs Bun and the
        # OpenTUI host dependencies, so it must not be moved by the balancer.
        "tests/unit/cli/tui/test_opentui_fuzzy_rank.py",
    }
)
_HARD_PINNED_SHARDS: Final[dict[str, str]] = {
    **{path: "core" for path in _CORE_EXACT},
    # This file consumes the two distinct roots provisioned only by the
    # recovery-migration Windows job. Other recovery tests use the same Python
    # environment as every shard and may safely participate in load balancing.
    "tests/test_recovery/test_atomic_and_locking.py": "recovery-migration",
}
_DURATION_FILE: Final[Path] = Path(__file__).with_name("windows_test_durations.json")
_ASSIGNMENT_FILE: Final[Path] = Path(__file__).with_name("windows_test_assignments.json")


def discover_test_files(root: Path) -> tuple[str, ...]:
    """Return every pytest file below ``tests/`` as a repository-relative path."""

    tests_root = root / "tests"
    excluded = _pytest_excluded_prefixes(root)
    relative_paths = (
        path.relative_to(root).as_posix() for path in tests_root.rglob("test_*.py")
    )
    return tuple(
        sorted(
            relative
            for relative in relative_paths
            if not any(relative.startswith(prefix) for prefix in excluded)
        )
    )


def _pytest_excluded_prefixes(root: Path) -> tuple[str, ...]:
    pyproject = root / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read pytest collection contract from {pyproject}") from exc
    configured = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    return tuple(
        f"{PurePosixPath(path).as_posix().rstrip('/')}/"
        for path in configured.get("norecursedirs", ())
        if PurePosixPath(path).as_posix().startswith("tests/")
    )


def matching_specialized_shards(path: str) -> tuple[str, ...]:
    """Return specialized shards whose responsibility rules match ``path``."""

    normalized = PurePosixPath(path).as_posix()
    if normalized in _CORE_EXACT:
        return ()
    name = PurePosixPath(normalized).name.casefold()
    prefix_matches: list[str] = []
    if normalized.startswith(_GATEWAY_SQLITE_PREFIXES):
        prefix_matches.append("gateway-sqlite")
    if normalized.startswith(_RECOVERY_MIGRATION_PREFIXES):
        prefix_matches.append("recovery-migration")
    if normalized.startswith(_DESKTOP_INSTALLER_PREFIXES):
        prefix_matches.append("desktop-installer-contracts")
    if prefix_matches:
        return tuple(prefix_matches)

    matches: list[str] = []
    if any(token in name for token in _GATEWAY_SQLITE_NAME_TOKENS):
        matches.append("gateway-sqlite")
    if any(token in name for token in _RECOVERY_MIGRATION_NAME_TOKENS):
        matches.append("recovery-migration")
    if normalized in _DESKTOP_INSTALLER_EXACT or any(
        token in name for token in _DESKTOP_INSTALLER_NAME_TOKENS
    ):
        matches.append("desktop-installer-contracts")

    return tuple(matches)


@cache
def historical_test_weights() -> dict[str, float]:
    """Load validated per-file Windows durations used by the stable balancer."""

    try:
        payload = json.loads(_DURATION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read Windows test durations from {_DURATION_FILE}") from exc

    if payload.get("schema_version") != 1:
        raise ValueError(f"unsupported Windows test duration schema in {_DURATION_FILE}")
    raw_weights = payload.get("weights_seconds")
    if not isinstance(raw_weights, dict):
        raise ValueError(f"missing weights_seconds mapping in {_DURATION_FILE}")

    weights: dict[str, float] = {}
    for raw_path, raw_weight in raw_weights.items():
        if not isinstance(raw_path, str):
            raise ValueError(f"non-string Windows test path in {_DURATION_FILE}")
        path = PurePosixPath(raw_path).as_posix()
        if (
            path != raw_path
            or not path.startswith("tests/")
            or not PurePosixPath(path).name.startswith("test_")
            or not path.endswith(".py")
        ):
            raise ValueError(f"invalid Windows test path in duration data: {raw_path!r}")
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float)):
            raise ValueError(f"invalid Windows test weight for {path}: {raw_weight!r}")
        weight = float(raw_weight)
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError(f"invalid Windows test weight for {path}: {raw_weight!r}")
        weights[path] = weight
    if not weights:
        raise ValueError(f"empty Windows test duration data in {_DURATION_FILE}")
    return weights


@cache
def assignment_governance() -> tuple[
    dict[str, str], dict[str, str], dict[str, float | int], tuple[dict[str, object], ...]
]:
    """Load the stable assignment snapshot and validate every movement guardrail."""

    try:
        payload = json.loads(_ASSIGNMENT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read Windows test assignments from {_ASSIGNMENT_FILE}") from exc
    return validate_assignment_payload(payload, historical_test_weights())


def validate_assignment_payload(
    payload: object, weights: dict[str, float]
) -> tuple[
    dict[str, str], dict[str, str], dict[str, float | int], tuple[dict[str, object], ...]
]:
    """Validate a snapshot without consulting git history or the GitHub API."""

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported Windows test assignment schema")
    raw_guardrails = payload.get("guardrails")
    if not isinstance(raw_guardrails, dict):
        raise ValueError("missing Windows test assignment guardrails")

    max_files = raw_guardrails.get("max_moved_files")
    max_fraction = raw_guardrails.get("max_moved_fraction")
    min_improvement = raw_guardrails.get(
        "minimum_predicted_max_shard_improvement_seconds"
    )
    if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files <= 0:
        raise ValueError("max_moved_files must be a positive integer")
    if (
        isinstance(max_fraction, bool)
        or not isinstance(max_fraction, (int, float))
        or not 0 < float(max_fraction) <= 1
    ):
        raise ValueError("max_moved_fraction must be in (0, 1]")
    if (
        isinstance(min_improvement, bool)
        or not isinstance(min_improvement, (int, float))
        or not math.isfinite(float(min_improvement))
        or float(min_improvement) <= 0
    ):
        raise ValueError("minimum predicted improvement must be positive")
    guardrails: dict[str, float | int] = {
        "max_moved_files": max_files,
        "max_moved_fraction": float(max_fraction),
        "minimum_predicted_max_shard_improvement_seconds": float(min_improvement),
    }

    raw_baseline = payload.get("baseline_assignments")
    if not isinstance(raw_baseline, dict) or set(raw_baseline) != set(SHARD_NAMES):
        raise ValueError("baseline_assignments must contain every Windows shard exactly once")

    baseline: dict[str, str] = {}
    for shard in SHARD_NAMES:
        raw_paths = raw_baseline.get(shard)
        if not isinstance(raw_paths, list) or not raw_paths:
            raise ValueError(f"Windows shard {shard!r} has no baseline assignments")
        if raw_paths != sorted(raw_paths):
            raise ValueError(f"Windows shard {shard!r} baseline is not sorted")
        for raw_path in raw_paths:
            if not isinstance(raw_path, str):
                raise ValueError(f"non-string baseline assignment in {shard!r}")
            path = PurePosixPath(raw_path).as_posix()
            if (
                path != raw_path
                or not path.startswith("tests/")
                or not PurePosixPath(path).name.startswith("test_")
                or not path.endswith(".py")
            ):
                raise ValueError(f"invalid baseline Windows test path: {raw_path!r}")
            if path in baseline:
                raise ValueError(f"duplicate baseline Windows test path: {path}")
            baseline[path] = shard

    if set(baseline) != set(weights):
        missing = sorted(set(weights) - set(baseline))[:3]
        stale = sorted(set(baseline) - set(weights))[:3]
        raise ValueError(
            "Windows assignment baseline and duration weights differ "
            f"(missing={missing}, stale={stale})"
        )

    raw_overrides = payload.get("overrides")
    if not isinstance(raw_overrides, list):
        raise ValueError("Windows assignment overrides must be a list")
    if len(raw_overrides) > max_files or (
        baseline and len(raw_overrides) / len(baseline) > float(max_fraction)
    ):
        raise ValueError("Windows assignment overrides exceed the movement budget")

    assignments = dict(baseline)
    overrides: list[dict[str, object]] = []
    moved_paths: set[str] = set()
    for raw_override in raw_overrides:
        if not isinstance(raw_override, dict):
            raise ValueError("Windows assignment override must be an object")
        allowed_keys = {"path", "from", "to", "reason", "affinity_exception"}
        if not set(raw_override) <= allowed_keys:
            raise ValueError("Windows assignment override contains unknown fields")
        path = raw_override.get("path")
        from_shard = raw_override.get("from")
        to_shard = raw_override.get("to")
        reason = raw_override.get("reason")
        if not isinstance(path, str) or path not in baseline:
            raise ValueError(f"unknown Windows assignment override path: {path!r}")
        if path in moved_paths:
            raise ValueError(f"duplicate Windows assignment override path: {path}")
        if from_shard != baseline[path]:
            raise ValueError(f"incorrect source shard for Windows assignment override: {path}")
        if to_shard not in SHARD_NAMES or to_shard == from_shard:
            raise ValueError(f"invalid destination shard for Windows assignment override: {path}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"missing reason for Windows assignment override: {path}")
        matches = matching_specialized_shards(path)
        if matches and to_shard != matches[0] and raw_override.get(
            "affinity_exception"
        ) is not True:
            raise ValueError(f"affinity exception is not reviewed for: {path}")
        if path in _HARD_PINNED_SHARDS:
            raise ValueError(f"hard-pinned Windows test cannot move: {path}")
        moved_paths.add(path)
        assignments[path] = str(to_shard)
        overrides.append(dict(raw_override))

    for path, shard in _HARD_PINNED_SHARDS.items():
        if path in assignments and assignments[path] != shard:
            raise ValueError(f"hard-pinned Windows test moved: {path}")

    if overrides:
        baseline_totals = dict.fromkeys(SHARD_NAMES, 0.0)
        proposed_totals = dict.fromkeys(SHARD_NAMES, 0.0)
        for path, weight in weights.items():
            baseline_totals[baseline[path]] += weight
            proposed_totals[assignments[path]] += weight
        improvement = max(baseline_totals.values()) - max(proposed_totals.values())
        if improvement < float(min_improvement):
            raise ValueError(
                "Windows assignment proposal does not meet the minimum predicted "
                f"improvement ({improvement:.1f}s)"
            )

    return baseline, assignments, guardrails, tuple(overrides)


def assignment_snapshot_fingerprint() -> str:
    """Return a deterministic hash of the effective governed assignment map."""

    _, assignments, _, _ = assignment_governance()
    canonical = json.dumps(assignments, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def shard_for_test(path: str) -> str:
    """Return the one responsibility/balance shard for ``path`` or fail safely."""

    normalized = PurePosixPath(path).as_posix()
    _, assignments, _, _ = assignment_governance()
    assignment = assignments.get(normalized)
    if assignment is not None:
        return assignment
    hard_pinned = _HARD_PINNED_SHARDS.get(normalized)
    if hard_pinned is not None:
        return hard_pinned
    matches = matching_specialized_shards(normalized)
    if len(matches) > 1:
        joined = ", ".join(matches)
        raise ValueError(f"test file matches multiple Windows shards: {normalized} ({joined})")
    if matches:
        return matches[0]
    return "core"


def files_for_shard(root: Path, shard: str) -> tuple[str, ...]:
    if shard not in SHARD_NAMES:
        raise ValueError(f"unknown Windows shard: {shard}")
    return tuple(path for path in discover_test_files(root) if shard_for_test(path) == shard)


def validated_files_for_shard(root: Path, shard: str) -> tuple[str, ...]:
    """Return one shard after validating the complete offline file inventory."""

    discovered = set(discover_test_files(root))
    by_shard = {name: set(files_for_shard(root, name)) for name in SHARD_NAMES}
    assigned = set().union(*by_shard.values())
    assignment_count = sum(len(paths) for paths in by_shard.values())
    if assigned != discovered or assignment_count != len(discovered):
        missing = sorted(discovered - assigned)[:3]
        duplicate_count = assignment_count - len(assigned)
        raise ValueError(
            "Windows shard inventory is incomplete or duplicated "
            f"(missing={missing}, duplicate_count={duplicate_count})"
        )
    return tuple(sorted(by_shard[shard]))


def shard_weight_summary(root: Path) -> dict[str, tuple[int, float, int]]:
    """Return file count, historical seconds, and unweighted count per shard."""

    weights = historical_test_weights()
    summary: dict[str, tuple[int, float, int]] = {}
    for shard in SHARD_NAMES:
        files = files_for_shard(root, shard)
        summary[shard] = (
            len(files),
            sum(weights.get(path, 0.0) for path in files),
            sum(path not in weights for path in files),
        )
    return summary


def assignment_governance_summary(root: Path) -> dict[str, object]:
    """Return a machine-readable baseline/proposal report for review."""

    baseline, assignments, guardrails, overrides = assignment_governance()
    weights = historical_test_weights()
    baseline_seconds = dict.fromkeys(SHARD_NAMES, 0.0)
    current_seconds = dict.fromkeys(SHARD_NAMES, 0.0)
    for path, weight in weights.items():
        baseline_seconds[baseline[path]] += weight
        current_seconds[assignments[path]] += weight
    baseline_max = max(baseline_seconds.values())
    current_max = max(current_seconds.values())
    return {
        "schema_version": 1,
        "assignment_sha256": assignment_snapshot_fingerprint(),
        "guardrails": guardrails,
        "overrides": list(overrides),
        "baseline_predicted_seconds": {
            shard: round(baseline_seconds[shard], 3) for shard in SHARD_NAMES
        },
        "current_predicted_seconds": {
            shard: round(current_seconds[shard], 3) for shard in SHARD_NAMES
        },
        "predicted_max_shard_improvement_seconds": round(
            baseline_max - current_max, 3
        ),
        "files": {
            shard: len(files_for_shard(root, shard)) for shard in SHARD_NAMES
        },
    }


def _ci_environment_int(name: str) -> int | None:
    raw = os.environ.get(name, "")
    return int(raw) if raw.isdigit() else None


def _inventory_fingerprint(files: tuple[str, ...]) -> str:
    canonical = json.dumps(files, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _write_run_metadata(
    path: Path,
    shard: str,
    files: tuple[str, ...],
    *,
    parallel_workers: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "platform": "windows" if os.name == "nt" else sys.platform,
        "run_id": _ci_environment_int("GITHUB_RUN_ID"),
        "run_attempt": _ci_environment_int("GITHUB_RUN_ATTEMPT"),
        "sha": os.environ.get("GITHUB_SHA") or None,
        "shard": shard,
        "assignment_sha256": assignment_snapshot_fingerprint(),
        "runtime": {
            "python_version": platform.python_version(),
            "runner_os": os.environ.get("RUNNER_OS"),
            "runner_arch": os.environ.get("RUNNER_ARCH"),
            "image_os": os.environ.get("ImageOS"),
            "image_version": os.environ.get("ImageVersion"),
        },
        "test_files": list(files),
        "test_files_sha256": _inventory_fingerprint(files),
        "execution": {
            "parallel": {
                "workers": parallel_workers,
                "dist": "loadfile",
                "marker": "not ci_serial",
            },
            "serial": {
                "workers": 1,
                "marker": "ci_serial",
            },
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_failure_summary(
    junit_path: Path,
    summary_path: Path,
    exit_code: int,
    *,
    parallel_exit_code: int | None = None,
    serial_exit_code: int | None = None,
) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"pytest_exit_code={exit_code}"]
    if parallel_exit_code is not None:
        lines.append(f"parallel_pytest_exit_code={parallel_exit_code}")
    if serial_exit_code is not None:
        lines.append(f"serial_pytest_exit_code={serial_exit_code}")
    if not junit_path.is_file():
        lines.append("junit_status=unavailable")
        summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    try:
        root = ET.parse(junit_path).getroot()
    except (ET.ParseError, OSError) as exc:
        lines.extend(("junit_status=unreadable", f"detail={type(exc).__name__}"))
        summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    for testcase in root.iter("testcase"):
        failure = testcase.find("failure")
        if failure is None:
            failure = testcase.find("error")
        if failure is None:
            continue
        class_name = testcase.get("classname", "")
        test_name = testcase.get("name", "unknown")
        node = f"{class_name}::{test_name}" if class_name else test_name
        detail = (failure.text or failure.get("message") or "failure details unavailable").strip()
        lines.extend(
            (
                "junit_status=failed",
                f"first_failure={node}",
                "detail:",
                detail[:12_000],
            )
        )
        break
    else:
        lines.append("junit_status=passed" if exit_code == 0 else "junit_status=no-test-failure")

    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _phase_junit_path(destination: Path, phase: str) -> Path:
    suffix = destination.suffix or ".xml"
    return destination.with_name(f"{destination.stem}.{phase}{suffix}")


def _merge_junit_reports(destination: Path, reports: tuple[Path, ...]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    combined = ET.Element("testsuites", {"name": "pytest-ci-shard"})
    integer_totals = dict.fromkeys(("tests", "failures", "errors", "skipped", "disabled"), 0)
    elapsed = 0.0
    found = False
    for report in reports:
        if not report.is_file():
            continue
        parsed_root = ET.parse(report).getroot()
        suites = (
            [parsed_root]
            if parsed_root.tag == "testsuite"
            else list(parsed_root.findall("testsuite"))
        )
        for suite in suites:
            found = True
            combined.append(suite)
            for key in integer_totals:
                raw = suite.get(key, "0")
                try:
                    integer_totals[key] += int(raw)
                except ValueError:
                    # Ignore malformed totals so one partial report cannot block merging shards.
                    continue
            try:
                elapsed += float(suite.get("time", "0"))
            except ValueError:
                # Ignore malformed durations for the same partial-report tolerance.
                continue
    if not found:
        raise ValueError("pytest did not produce any readable JUnit suites")
    combined.attrib.update({key: str(value) for key, value in integer_totals.items()})
    combined.set("time", f"{elapsed:.3f}")
    tree = ET.ElementTree(combined)
    ET.indent(tree, space="  ")
    tree.write(destination, encoding="utf-8", xml_declaration=True)


def _write_runner_error_junit(destination: Path, exc: BaseException) -> None:
    """Write a synthetic JUnit error when the runner fails outside pytest."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    detail = f"{type(exc).__name__}: {exc}"
    root = ET.Element(
        "testsuites",
        {
            "name": "pytest-ci-shard",
            "tests": "1",
            "failures": "0",
            "errors": "1",
            "skipped": "0",
            "disabled": "0",
            "time": "0.000",
        },
    )
    suite = ET.SubElement(
        root,
        "testsuite",
        {
            "name": "ci-shard-runner",
            "tests": "1",
            "failures": "0",
            "errors": "1",
            "skipped": "0",
            "disabled": "0",
            "time": "0.000",
        },
    )
    testcase = ET.SubElement(
        suite,
        "testcase",
        {
            "classname": "ci.shard_runner",
            "name": "prepare_and_run",
            "time": "0.000",
        },
    )
    error = ET.SubElement(
        testcase,
        "error",
        {"type": type(exc).__name__, "message": str(exc)},
    )
    error.text = detail
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(destination, encoding="utf-8", xml_declaration=True)


def _combined_pytest_exit_code(
    parallel_exit_code: int,
    serial_exit_code: int,
    *,
    no_tests_collected: int,
) -> int:
    """Combine split-phase exits while keeping an entirely empty shard fail-closed."""

    executed = [
        code
        for code in (parallel_exit_code, serial_exit_code)
        if code != no_tests_collected
    ]
    if not executed:
        return no_tests_collected
    for code in executed:
        if code != 0:
            return code
    return 0


def _pytest_phase_inputs(
    raw_args: list[str],
) -> tuple[list[str], str | None]:
    """Separate the caller's marker expression from runner-owned pytest flags."""

    args = list(raw_args)
    if args[:1] == ["--"]:
        args = args[1:]
    cleaned: list[str] = []
    marker_expression: str | None = None
    index = 0
    options_with_values = {"-n", "--numprocesses", "--dist", "--junitxml", "--junit-xml"}
    owned_prefixes = tuple(
        f"{option}=" for option in options_with_values if option.startswith("--")
    )
    while index < len(args):
        argument = args[index]
        if argument == "-m":
            if marker_expression is not None or index + 1 >= len(args):
                raise ValueError("pytest marker expression must be provided exactly once")
            marker_expression = args[index + 1]
            index += 2
            continue
        if argument.startswith("-m") and argument != "-m":
            if marker_expression is not None:
                raise ValueError("pytest marker expression must be provided exactly once")
            marker_expression = argument[2:]
            index += 1
            continue
        if (
            argument in options_with_values
            or argument.startswith(owned_prefixes)
            or (argument.startswith("-n") and argument != "-n")
        ):
            raise ValueError(f"pytest option is owned by the CI shard runner: {argument}")
        cleaned.append(argument)
        index += 1
    return cleaned, marker_expression


def _phase_marker(base_expression: str | None, selection: str) -> str:
    if not base_expression:
        return selection
    return f"({base_expression}) and ({selection})"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _requires_isolated_core_wheel(root: Path, files: tuple[str, ...]) -> bool:
    """Return whether any selected test function requests the wheel fixture."""

    for relative in files:
        path = root / relative
        parsed = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.arg) and node.arg == _CORE_WHEEL_FIXTURE
            for node in ast.walk(parsed)
        ):
            return True
    return False


def _load_core_wheel_builder(root: Path) -> Callable[[Path, Path], Path]:
    builder_path = root / "scripts" / "build_test_core_wheel.py"
    if not builder_path.is_file():
        raise RuntimeError(f"missing isolated core wheel builder: {builder_path}")
    module_name = f"_opensquilla_ci_core_wheel_{hashlib.sha256(str(root).encode()).hexdigest()}"
    spec = importlib.util.spec_from_file_location(module_name, builder_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load isolated core wheel builder: {builder_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    builder = getattr(module, "build_isolated_core_wheel", None)
    if not callable(builder):
        raise RuntimeError(f"isolated core wheel builder is not callable: {builder_path}")
    return builder


def _build_shared_core_wheel(root: Path, temp_root: Path) -> tuple[Path, str]:
    builder = _load_core_wheel_builder(root)
    built = Path(builder(root, temp_root / "build")).resolve()
    if not built.is_file() or built.suffix != ".whl":
        raise RuntimeError(f"isolated core wheel builder returned an invalid file: {built}")

    digest = _sha256_file(built)
    shared_dir = temp_root / "shared" / digest
    shared_dir.mkdir(parents=True)
    shared = shared_dir / built.name
    shutil.copy2(built, shared)
    if _sha256_file(shared) != digest:
        raise RuntimeError("isolated core wheel changed while preparing the shared copy")
    # Workers only receive this content-addressed, read-only copy. The context
    # resets its mode before deleting the temporary directory on Windows.
    shared.chmod(shared.stat().st_mode & ~0o222)
    return shared, digest


@contextmanager
def _prebuilt_core_wheel_environment(
    root: Path,
    files: tuple[str, ...],
) -> Iterator[Path | None]:
    if not _requires_isolated_core_wheel(root, files):
        yield None
        return

    previous = {
        _CORE_WHEEL_ENV: os.environ.get(_CORE_WHEEL_ENV),
        _CORE_WHEEL_SHA_ENV: os.environ.get(_CORE_WHEEL_SHA_ENV),
    }
    temp_directory: tempfile.TemporaryDirectory[str] | None = None
    owned_wheel: Path | None = None
    try:
        configured = previous[_CORE_WHEEL_ENV]
        if configured:
            wheel = Path(configured).resolve()
            if not wheel.is_file() or wheel.suffix != ".whl":
                raise RuntimeError(f"invalid configured isolated core wheel: {wheel}")
            digest = _sha256_file(wheel)
            expected = previous[_CORE_WHEEL_SHA_ENV]
            if expected and expected != digest:
                raise RuntimeError(
                    "configured isolated core wheel SHA-256 mismatch "
                    f"(expected {expected}, got {digest})"
                )
        else:
            temp_parent = os.environ.get("RUNNER_TEMP")
            temp_directory = tempfile.TemporaryDirectory(
                prefix="opensquilla-ci-core-wheel-",
                dir=temp_parent or None,
            )
            wheel, digest = _build_shared_core_wheel(
                root,
                Path(temp_directory.name),
            )
            owned_wheel = wheel

        os.environ[_CORE_WHEEL_ENV] = str(wheel)
        os.environ[_CORE_WHEEL_SHA_ENV] = digest
        print(
            "Prepared one shared isolated core wheel for all pytest phases "
            f"(sha256={digest[:12]}...)"
        )
        yield wheel
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if owned_wheel is not None and owned_wheel.exists():
            owned_wheel.chmod(owned_wheel.stat().st_mode | 0o200)
        if temp_directory is not None:
            temp_directory.cleanup()


def _run(args: argparse.Namespace) -> int:
    import pytest

    root = args.root.resolve()
    files = validated_files_for_shard(root, args.shard)
    if not files:
        print(f"Windows shard {args.shard!r} has no tests", file=sys.stderr)
        return 2

    args.junit.parent.mkdir(parents=True, exist_ok=True)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text("pytest_status=started\n", encoding="utf-8")
    if args.metadata is not None:
        _write_run_metadata(
            args.metadata,
            args.shard,
            files,
            parallel_workers=args.workers,
        )

    pytest_args, marker_expression = _pytest_phase_inputs(args.pytest_args)
    file_args = [str(root / path) for path in files]
    parallel_junit = _phase_junit_path(args.junit, "parallel")
    serial_junit = _phase_junit_path(args.junit, "serial")
    runner_error_junit = _phase_junit_path(args.junit, "runner-error")
    parallel_junit.unlink(missing_ok=True)
    serial_junit.unlink(missing_ok=True)
    runner_error_junit.unlink(missing_ok=True)
    args.junit.unlink(missing_ok=True)

    _, weight, unweighted = shard_weight_summary(root)[args.shard]
    print(
        f"Running {len(files)} test files in CI shard {args.shard} "
        f"(historical weight: {weight:.1f}s; unweighted: {unweighted})"
    )
    parallel_exit_code: int | None = None
    raw_serial_exit_code: int | None = None
    try:
        with _prebuilt_core_wheel_environment(root, files):
            parallel_args = [
                *pytest_args,
                "-m",
                _phase_marker(marker_expression, "not ci_serial"),
                "-n",
                str(args.workers),
                "--dist",
                "loadfile",
                *file_args,
                f"--junitxml={parallel_junit}",
            ]
            print(
                f"Running parallel bulk phase with {args.workers} workers "
                "(--dist loadfile; excludes ci_serial)"
            )
            parallel_exit_code = int(pytest.main(parallel_args))

            serial_args = [
                *pytest_args,
                "-m",
                _phase_marker(marker_expression, "ci_serial"),
                *file_args,
                f"--junitxml={serial_junit}",
            ]
            print("Running serial phase in the controller process (ci_serial only)")
            raw_serial_exit_code = int(pytest.main(serial_args))
    except (
        ImportError,
        OSError,
        RuntimeError,
        SyntaxError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Failed to prepare or run the CI shard: {exc}", file=sys.stderr)
        _write_runner_error_junit(runner_error_junit, exc)
        try:
            _merge_junit_reports(
                args.junit,
                (parallel_junit, serial_junit, runner_error_junit),
            )
        except (ET.ParseError, OSError, ValueError) as merge_exc:
            print(f"Failed to merge pytest JUnit reports: {merge_exc}", file=sys.stderr)
            _write_runner_error_junit(args.junit, exc)
        _write_failure_summary(
            args.junit,
            args.summary,
            2,
            parallel_exit_code=parallel_exit_code,
            serial_exit_code=raw_serial_exit_code,
        )
        return 2

    assert parallel_exit_code is not None
    assert raw_serial_exit_code is not None
    no_tests_collected = int(pytest.ExitCode.NO_TESTS_COLLECTED)
    exit_code = _combined_pytest_exit_code(
        parallel_exit_code,
        raw_serial_exit_code,
        no_tests_collected=no_tests_collected,
    )
    try:
        _merge_junit_reports(args.junit, (parallel_junit, serial_junit))
    except (ET.ParseError, OSError, ValueError) as exc:
        print(f"Failed to merge pytest JUnit reports: {exc}", file=sys.stderr)
        if exit_code == 0:
            exit_code = 2
    _write_failure_summary(
        args.junit,
        args.summary,
        exit_code,
        parallel_exit_code=parallel_exit_code,
        serial_exit_code=raw_serial_exit_code,
    )
    return exit_code


def _list(args: argparse.Namespace) -> int:
    for path in files_for_shard(args.root.resolve(), args.shard):
        print(path)
    return 0


def _report(args: argparse.Namespace) -> int:
    report = assignment_governance_summary(args.root.resolve())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list files assigned to one shard")
    list_parser.add_argument("shard", choices=SHARD_NAMES)
    list_parser.add_argument("--root", type=Path, default=Path.cwd())
    list_parser.set_defaults(handler=_list)

    report_parser = subparsers.add_parser(
        "report", help="report governed assignments and predicted shard weights"
    )
    report_parser.add_argument("--root", type=Path, default=Path.cwd())
    report_parser.set_defaults(handler=_report)

    run_parser = subparsers.add_parser("run", help="run one shard through pytest")
    run_parser.add_argument("shard", choices=SHARD_NAMES)
    run_parser.add_argument("--root", type=Path, default=Path.cwd())
    run_parser.add_argument("--junit", type=Path, required=True)
    run_parser.add_argument("--summary", type=Path, required=True)
    run_parser.add_argument("--metadata", type=Path)
    run_parser.add_argument(
        "--workers",
        type=_positive_int,
        default=DEFAULT_PARALLEL_WORKERS,
        help="xdist workers for the parallel bulk phase (default: 4)",
    )
    run_parser.set_defaults(handler=_run)
    return parser


def main() -> int:
    parser = _parser()
    args, pytest_args = parser.parse_known_args()
    if args.command != "run" and pytest_args:
        parser.error(f"unrecognized arguments: {' '.join(pytest_args)}")
    args.pytest_args = pytest_args
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
