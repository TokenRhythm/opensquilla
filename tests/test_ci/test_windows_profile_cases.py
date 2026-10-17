from __future__ import annotations

import hashlib
import json
import re
import runpy
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = runpy.run_path(str(ROOT / ".github/scripts/windows_profile_cases.py"))
CASES = MODULE["PROFILE_CASES"]
SHARDS = MODULE["PROFILE_SHARDS"]
select_cases = MODULE["select_cases"]
verify_reports = MODULE["verify_reports"]
validate_matrix = MODULE["validate_matrix"]
IDENTITY = {"GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}
MANIFEST = "windows-profile-manifest.json"
TELEMETRY = "desktop-e2e-cases.jsonl"


def _matrix() -> list[dict[str, str]]:
    return [
        {"os": "windows-latest", "shard": shard} for shard in SHARDS
    ] + [{"os": "macos-latest", "shard": "profiles"}]


@pytest.fixture
def evidence_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in IDENTITY.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RUNNER_OS", "Windows")
    monkeypatch.setitem(select_cases.__globals__, "_require_windows", lambda: None)


def _record(case: str, shard: str, *, failed: bool = False) -> dict[str, Any]:
    return {
        "case": case, "os": "Windows", "shard": shard, "attempt": 1,
        "start": "2026-10-17T07:32:46.378Z", "end": "2026-10-17T07:35:06.699Z",
        "duration": 140318.625, "duration_unit": "ms",
        "status": "failed" if failed else "passed",
        "details": {"exit_code": 1 if failed else 0, "signal": None},
    }


def _save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _reports(tmp_path: Path, *, failed: bool = False, root: Path = ROOT) -> list[Path]:
    paths = []
    for shard in SHARDS:
        directory = tmp_path / shard
        entries = select_cases(shard, _matrix(), directory, root=root)
        if entries:
            case = entries[0].split(":", 1)[0]
            _save(directory / TELEMETRY, _record(case, shard, failed=failed))
        paths.append(directory / MANIFEST)
    return paths


def test_six_case_mapping_matches_retained_profile_workflow_scripts() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    block = workflow.split("            profiles)", 1)[1].split("              ;;", 1)[0]
    retained = dict(re.findall(r"'([^':]+):(scripts/[^']+\.mjs)'", block))
    assert CASES == retained
    assert len(CASES) == 6
    inventory = MODULE["script_inventory"](ROOT)
    for case, script in CASES.items():
        source = ROOT / "desktop/electron" / script
        assert inventory[case] == {
            "script": script,
            "sha256_lf": hashlib.sha256(source.read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
        }


def test_script_hashes_ignore_checkout_line_endings_but_detect_content_changes(
    tmp_path: Path, evidence_environment: None
) -> None:
    source_root = tmp_path / "source"
    for script in CASES.values():
        path = source_root / "desktop/electron" / script
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"// synthetic fixture\r\nexport const value = 1\r\n")
    windows_inventory = MODULE["script_inventory"](source_root)
    reports_dir = tmp_path / "reports"
    _reports(reports_dir, root=source_root)
    for script in CASES.values():
        path = source_root / "desktop/electron" / script
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    assert MODULE["script_inventory"](source_root) == windows_inventory
    assert verify_reports(reports_dir, root=source_root)["coverage_complete"] is True
    changed = source_root / "desktop/electron" / next(iter(CASES.values()))
    changed.write_bytes(changed.read_bytes().replace(b"value = 1", b"value = 2"))
    assert MODULE["script_inventory"](source_root) != windows_inventory
    with pytest.raises(ValueError, match="source inventory"):
        verify_reports(reports_dir, root=source_root)


def test_select_writes_fresh_empty_owner_and_one_case_per_delegated_runner(
    tmp_path: Path, evidence_environment: None
) -> None:
    all_entries = []
    for shard in SHARDS:
        directory = tmp_path / shard
        entries = select_cases(shard, _matrix(), directory)
        assert len(entries) == (0 if shard == "profiles" else 1)
        assert (directory / TELEMETRY).read_bytes() == b""
        manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
        assert manifest["identity"] == IDENTITY
        assert manifest["script_inventory"] == MODULE["script_inventory"](ROOT)
        assert manifest["expected_cases"] == [entry.split(":", 1)[0] for entry in entries]
        all_entries.extend(entries)
    assert sorted(all_entries) == sorted(f"{case}:{script}" for case, script in CASES.items())


def test_selector_never_truncates_prior_evidence(
    tmp_path: Path, evidence_environment: None
) -> None:
    telemetry = tmp_path / TELEMETRY
    telemetry.write_text("existing execution evidence\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fresh"):
        select_cases("profiles", _matrix(), tmp_path)
    assert telemetry.read_text(encoding="utf-8") == "existing execution evidence\n"
    assert not (tmp_path / MANIFEST).exists()


def test_selector_rejects_non_native_windows_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNER_OS", "Linux")
    with pytest.raises(ValueError, match="native Windows"):
        select_cases("profiles", _matrix(), tmp_path)
    assert not (tmp_path / MANIFEST).exists()


@pytest.mark.parametrize("change", [
    "missing_owner", "missing_case", "duplicate", "unknown_case", "wrong_os", "invalid_cell",
])
def test_invalid_or_partial_plans_fail_closed(change: str) -> None:
    matrix = _matrix()
    if change == "missing_owner":
        matrix.pop(0)
    elif change == "missing_case":
        matrix.pop(1)
    elif change == "duplicate":
        matrix.append(matrix[1])
    elif change == "unknown_case":
        matrix.append({"os": "windows-latest", "shard": "profiles-unknown"})
    elif change == "wrong_os":
        matrix.append({"os": "ubuntu-latest", "shard": SHARDS[1]})
    elif change == "invalid_cell":
        matrix.append({"os": "windows-latest"})
    with pytest.raises(ValueError):
        validate_matrix(matrix, "profiles")


@pytest.mark.parametrize("failed", [False, True])
def test_completed_passes_and_failures_both_prove_exact_once_attempts(
    tmp_path: Path, evidence_environment: None, failed: bool
) -> None:
    _reports(tmp_path, failed=failed)
    result = verify_reports(tmp_path, expected_identity=IDENTITY)
    assert result["coverage_complete"] is True
    assert result["case_count"] == 6
    assert result["original_profiles_case_count"] == 0
    assert result["outcomes"] == {"passed": 0 if failed else 6, "failed": 6 if failed else 0}
    assert {record["case"] for record in result["cases"]} == set(CASES)


def test_failed_cleanup_preserves_completed_six_case_coverage(
    tmp_path: Path, evidence_environment: None
) -> None:
    paths = _reports(tmp_path)
    cleanup = next(path for path in paths if path.parent.name == "profiles-desktop-cleanup-flow")
    _save(cleanup.with_name(TELEMETRY), _record(
        "desktop-cleanup-flow", "profiles-desktop-cleanup-flow", failed=True
    ))
    result = verify_reports(tmp_path, expected_identity=IDENTITY)
    assert result["coverage_complete"] is True
    assert result["outcomes"] == {"passed": 5, "failed": 1}


@pytest.mark.parametrize("change", [
    "missing_owner", "duplicate_owner", "identity", "inventory", "hash", "plan",
    "wrong_owner_cases", "native_os", "owner_repeats_case", "missing_record", "duplicate_record",
    "case", "os", "shard", "retry", "skip", "cancelled", "incomplete", "nan", "negative",
    "unit", "timestamp", "reversed_time", "exit", "signal", "timeout", "spawn_error",
])
def test_verification_rejects_missing_duplicate_or_incomplete_evidence(
    tmp_path: Path, evidence_environment: None, change: str
) -> None:
    paths = _reports(tmp_path)
    path = paths[1]
    manifest = json.loads(path.read_text(encoding="utf-8"))
    telemetry_path = path.with_name(TELEMETRY)
    record = json.loads(telemetry_path.read_text(encoding="utf-8"))
    if change == "missing_owner":
        paths[0].unlink()
    elif change == "duplicate_owner":
        manifest["shard"] = "profiles"
    elif change == "identity":
        manifest["identity"]["GITHUB_RUN_ATTEMPT"] = "1"
    elif change == "inventory":
        manifest["script_inventory"].pop(next(iter(CASES)))
    elif change == "hash":
        manifest["script_inventory_sha256"] = "0" * 64
    elif change == "plan":
        manifest["planned_desktop_matrix"].pop()
    elif change == "wrong_owner_cases":
        manifest["expected_cases"] = []
    elif change == "native_os":
        manifest["runner_os"] = "Linux"
    elif change == "owner_repeats_case":
        _save(paths[0].with_name(TELEMETRY), record)
    elif change == "missing_record":
        telemetry_path.write_text("", encoding="utf-8")
    elif change == "duplicate_record":
        telemetry_path.write_text(
            json.dumps(record) + "\n" + json.dumps(record) + "\n", encoding="utf-8"
        )
    elif change in {"case", "os", "shard"}:
        record[change] = "foreign"
    elif change == "retry":
        record["attempt"] = 2
    elif change in {"skip", "cancelled"}:
        record["status"] = "skipped" if change == "skip" else "cancelled"
    elif change == "incomplete":
        record.pop("end")
    elif change == "nan":
        record["duration"] = float("nan")
    elif change == "negative":
        record["duration"] = -1
    elif change == "unit":
        record["duration_unit"] = "seconds"
    elif change == "timestamp":
        record["start"] = "invalid"
    elif change == "reversed_time":
        record["start"], record["end"] = record["end"], record["start"]
    elif change == "exit":
        record["details"]["exit_code"] = 1
    elif change == "signal":
        record["details"]["signal"] = "SIGTERM"
    elif change == "timeout":
        record["details"]["timed_out"] = True
    elif change == "spawn_error":
        record["details"] = {"spawn_error": "cannot spawn"}
    _save(path, manifest)
    if change not in {"missing_record", "duplicate_record"}:
        _save(telemetry_path, record)
    with pytest.raises(ValueError):
        verify_reports(tmp_path, expected_identity=IDENTITY)


def test_verifier_rejects_previous_run(
    tmp_path: Path, evidence_environment: None
) -> None:
    _reports(tmp_path)
    with pytest.raises(ValueError, match="different source/run/attempt"):
        verify_reports(tmp_path, expected_identity={**IDENTITY, "GITHUB_RUN_ID": "124"})
