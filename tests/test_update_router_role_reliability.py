from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

from opensquilla.provider import ranking_router

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "experiments"
    / "update_router_role_reliability.py"
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("update_router_role_reliability", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _profiles() -> dict:
    def row(model_id: str) -> dict:
        return {
            "registry_facts": {
                "provider": "openrouter",
                "model_id": model_id,
            },
            "online_profile": {"error_rates": {"timeout": 0.1}},
        }

    return {
        "schema_version": "test-model-registry-v1",
        "snapshot_version": "base-snapshot",
        "models": [
            row("model-a"),
            row("model-b"),
            row("model-c"),
            row("unobserved"),
        ],
    }


def _breakdown_row(model_id: str, role: str, count: int) -> dict:
    return {
        "provider": "openrouter",
        "model": model_id,
        "role": role,
        "request_count": count,
    }


def _write_output(
    path: Path,
    *,
    finished_at: str,
    breakdown: list[dict],
    trace: dict,
    run_id: str,
    task_id: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "benchmark_id": "pinch-bench",
                "run_id": run_id,
                "results": [
                    {
                        "task_id": task_id,
                        "judge": {
                            "metadata": {
                                "usage": {
                                    "model_usage_breakdown": breakdown,
                                    "ensemble_traces": [trace],
                                }
                            }
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    path.with_name(f"{path.stem}.meta.json").write_text(
        json.dumps({"finished_at": finished_at}),
        encoding="utf-8",
    )


def _candidate(
    model_id: str,
    attempt_id: str,
    *,
    ok: bool,
    outcome: str,
    usage_reported: bool,
    stop_reason: str = "",
) -> dict:
    candidate = {
        "provider": "openrouter",
        "model": model_id,
        "requested_provider": "openrouter",
        "requested_model": model_id,
        "ok": ok,
        "completion_outcome": "complete" if ok else "partial_usable",
        "request_started": True,
        "usage_reported": usage_reported,
        "stop_reason": stop_reason,
        "execution": {
            "provider": "openrouter",
            "model": model_id,
            "physical_attempts": [
                {
                    "identity": f"openrouter:{model_id}",
                    "physical_attempt_id": attempt_id,
                    "request_started": True,
                    "outcome": outcome,
                }
            ],
        },
    }
    if usage_reported:
        candidate["model_usage_breakdown"] = [
            {
                "provider": "openrouter",
                "model": model_id,
                "role": "proposer",
                "physical_attempt_id": attempt_id,
                "request_count": 1,
            }
        ]
    return candidate


def _aggregator_attempt(
    attempt_id: str,
    *,
    tools_enabled: bool,
    tool_count: int,
    tool_names: list[str],
    effective_tool_choice: str | None = None,
    request_started: bool = True,
) -> dict:
    return {
        "actual_provider": "openrouter",
        "actual_model": "model-c",
        "physical_attempt_id": attempt_id,
        "request_started": request_started,
        "outcome": "succeeded",
        "execution": {
            "tools_enabled": tools_enabled,
            "tool_count": tool_count,
            "tool_names": tool_names,
            "effective_tool_choice": effective_tool_choice,
        },
    }


def test_reconciles_physical_calls_and_neutralizes_framework_tool_removal(
    tmp_path: Path,
) -> None:
    updater = _load_module()
    output = tmp_path / "outputs" / "G1" / "001" / "attempt-1.json"
    _write_output(
        output,
        finished_at="2026-08-05T01:00:00+00:00",
        run_id="run-1",
        task_id="task-1",
        breakdown=[
            _breakdown_row("model-a", "proposer", 3),
            _breakdown_row("model-b", "proposer", 1),
            _breakdown_row("model-c", "aggregator", 2),
            {"label": "usage_missing", "request_count": 1},
        ],
        trace={
            "physical_request_count": 7,
            "aggregator_tools": True,
            "candidates": [
                _candidate(
                    "model-a", "a-success", ok=True, outcome="succeeded", usage_reported=True
                ),
                _candidate(
                    "model-b", "b-partial", ok=False, outcome="succeeded", usage_reported=True
                ),
                _candidate(
                    "model-a", "a-failure", ok=False, outcome="failed", usage_reported=False
                ),
            ],
            "proposer_recovery": {
                "attempts": [
                    {
                        "target_identity": "openrouter:model-a",
                        "physical_attempt_id": "a-failure",
                        "request_started": True,
                        "outcome": "failed",
                    }
                ]
            },
            "aggregator_recovery": {
                "attempts": [
                    {
                        "actual_provider": "openrouter",
                        "actual_model": "model-c",
                        "physical_attempt_id": "c-success",
                        "request_started": True,
                        "outcome": "succeeded",
                        "execution": {
                            "tools_enabled": True,
                            "tool_count": 2,
                            "tool_names": ["web_search", "web_fetch"],
                            "effective_tool_choice": None,
                        },
                    },
                    {
                        "actual_provider": "openrouter",
                        "actual_model": "model-c",
                        "physical_attempt_id": "c-framework-neutral",
                        "request_started": True,
                        "outcome": "succeeded",
                        "execution": {
                            "tools_enabled": False,
                            "tool_count": 0,
                            "tool_names": [],
                            "effective_tool_choice": None,
                        },
                    },
                ]
            },
        },
    )

    collection = updater.collect_observations(
        [tmp_path],
        allow_incomplete=True,
        max_unknown_outcome_rate=1.0,
    )
    assert collection.physical_requests_seen == 7
    assert collection.framework_excluded == 1
    assert collection.duplicate_attempts == 1
    assert collection.unknown_outcomes == 2
    assert len(collection.observations) == 4

    updated, summary = updater.update_profiles(
        _profiles(),
        collection,
        window_size=50,
        generated_at="2026-08-05T02:00:00+00:00",
        source_artifacts=[str(tmp_path)],
    )
    rows = {row["registry_facts"]["model_id"]: row for row in updated["models"]}
    assert rows["model-a"]["online_profile"]["role_reliability"] == {
        "window_size": 50,
        "proposer": {"success": 1, "failure": 1},
        "aggregator": {"success": 0, "failure": 0},
        "source": "aef_experiment_artifacts",
    }
    assert rows["model-b"]["online_profile"]["role_reliability"]["proposer"] == {
        "success": 0,
        "failure": 1,
    }
    assert rows["model-c"]["online_profile"]["role_reliability"]["aggregator"] == {
        "success": 1,
        "failure": 0,
    }
    assert rows["unobserved"]["online_profile"]["role_reliability"]["proposer"] == {
        "success": 0,
        "failure": 0,
    }
    assert summary == {
        "observed_models": 3,
        "raw_physical_calls": 7,
        "framework_neutral_calls": 1,
        "attributable_calls": 4,
        "unknown_outcome_calls": 2,
        "window_calls": 4,
        "unclassified_requests": 0,
    }
    snapshot = updated["role_reliability_snapshot"]
    assert snapshot["schema_version"] == "role-reliability-snapshot-v2"
    assert snapshot["observation_policy"] == "aef-physical-model-calls-v5"
    assert snapshot["completion_gate"] == "fixture_without_final_audit"
    assert snapshot["ordering_granularity"] == "task_aggregate"
    assert snapshot["unknown_outcome_calls"] == 2
    assert snapshot["max_unknown_outcome_rate"] == 1.0
    assert {row["kind"] for row in snapshot["source_evidence_file_hashes"]} == {
        "timing_metadata"
    }
    assert snapshot["base_snapshot_version"] == "base-snapshot"
    assert snapshot["source_artifacts"] == ["artifact-root://0"]
    assert all(
        value.startswith("artifact-sha256://")
        for value in snapshot["source_output_files"]
    )
    assert str(tmp_path) not in json.dumps(snapshot)
    assert updated["snapshot_version"] == (
        "base-snapshot-reliability-20260805T020000Z-"
        f"{snapshot['content_sha256'][:12]}"
    )


def test_legacy_complete_length_capped_proposer_counts_as_failure(
    tmp_path: Path,
) -> None:
    updater = _load_module()
    output = tmp_path / "outputs" / "B2" / "001" / "attempt-1.json"
    _write_output(
        output,
        finished_at="2026-08-05T01:00:00+00:00",
        run_id="run-length-capped",
        task_id="task-length-capped",
        breakdown=[_breakdown_row("model-a", "proposer", 1)],
        trace={
            "physical_request_count": 1,
            "candidates": [
                _candidate(
                    "model-a",
                    "length-capped",
                    ok=True,
                    outcome="succeeded",
                    usage_reported=True,
                    stop_reason="length",
                )
            ],
        },
    )

    collection = updater.collect_observations([tmp_path], allow_incomplete=True)

    assert len(collection.observations) == 1
    assert collection.observations[0].model_id == "model-a"
    assert collection.observations[0].role == "proposer"
    assert collection.observations[0].success is False


@pytest.mark.parametrize(
    ("placement", "schema", "marker_isolated"),
    [
        (
            "partial",
            "opensquilla.ensemble-partial-proposer-quorum/v1",
            True,
        ),
        (
            "native_cleanup",
            "opensquilla.proposer-cleanup-quorum-bypass/v1",
            None,
        ),
        (
            "router_cleanup",
            "opensquilla.router-dynamic-proposer-cleanup-quorum-bypass/v1",
            None,
        ),
    ],
)
def test_exact_v1_isolation_markers_neutralize_started_aggregator(
    placement: str,
    schema: str,
    marker_isolated: bool | None,
) -> None:
    updater = _load_module()
    attempt = _aggregator_attempt(
        "isolated",
        tools_enabled=False,
        tool_count=0,
        tool_names=[],
    )
    marker = {
        "schema": schema,
        "applied": True,
        "aggregator_tools_disabled": True,
    }
    if marker_isolated is not None:
        marker["aggregator_isolated"] = marker_isolated
    trace = {
        "aggregator_tools": True,
        "aggregator_isolated": True,
        "aggregator_recovery": {"attempts": [attempt]},
    }
    if placement == "partial":
        trace["proposer_partial_quorum"] = marker
    elif placement == "native_cleanup":
        trace["proposer_cleanup_quorum_bypass"] = marker
    else:
        trace["proposer_recovery"] = {"cleanup_quorum_bypass": marker}

    assert updater._framework_attributed_aggregator_attempt(
        trace,
        attempt,
        attempt_index=0,
    )


def test_v2_marker_and_owner_label_do_not_neutralize_model_call(tmp_path: Path) -> None:
    updater = _load_module()
    output = tmp_path / "outputs" / "G1" / "001" / "attempt-1.json"
    attempt = _aggregator_attempt(
        "v2-isolated",
        tools_enabled=False,
        tool_count=0,
        tool_names=[],
    )
    attempt["failure_owner"] = "framework"
    _write_output(
        output,
        finished_at="2026-08-05T01:00:00+00:00",
        run_id="run-v2",
        task_id="task-v2",
        breakdown=[_breakdown_row("model-c", "aggregator", 1)],
        trace={
            "physical_request_count": 1,
            "aggregator_tools": True,
            "aggregator_isolated": True,
            "proposer_partial_quorum": {
                "schema": "opensquilla.ensemble-partial-proposer-quorum/v2",
                "applied": True,
                "aggregator_isolated": True,
                "candidate_drafts_untrusted": True,
                "aggregator_tools_disabled_by_isolation": False,
            },
            "aggregator_recovery": {"attempts": [attempt]},
        },
    )

    collection = updater.collect_observations([tmp_path], allow_incomplete=True)
    assert collection.framework_excluded == 0
    assert len(collection.observations) == 1
    assert collection.observations[0].model_id == "model-c"


def test_v1_marker_requires_strict_current_disabled_state() -> None:
    updater = _load_module()
    attempt = _aggregator_attempt(
        "incomplete-isolated",
        tools_enabled=False,
        tool_count=0,
        tool_names=[],
    )
    attempt["execution"].pop("effective_tool_choice")
    trace = {
        "aggregator_tools": True,
        "aggregator_isolated": True,
        "proposer_partial_quorum": {
            "schema": "opensquilla.ensemble-partial-proposer-quorum/v1",
            "applied": True,
            "aggregator_isolated": True,
            "aggregator_tools_disabled": True,
        },
        "aggregator_recovery": {"attempts": [attempt]},
    }

    assert not updater._framework_attributed_aggregator_attempt(
        trace,
        attempt,
        attempt_index=0,
    )


def test_tool_removal_transition_requires_complete_ordered_evidence() -> None:
    updater = _load_module()
    enabled = _aggregator_attempt(
        "enabled",
        tools_enabled=True,
        tool_count=1,
        tool_names=["web_search"],
    )
    incomplete_disabled = _aggregator_attempt(
        "incomplete-disabled",
        tools_enabled=False,
        tool_count=0,
        tool_names=[],
    )
    incomplete_disabled["execution"].pop("effective_tool_choice")
    trace = {
        "aggregator_tools": True,
        "aggregator_recovery": {"attempts": [enabled, incomplete_disabled]},
    }
    assert not updater._framework_attributed_aggregator_attempt(
        trace,
        incomplete_disabled,
        attempt_index=1,
    )

    not_started_enabled = _aggregator_attempt(
        "not-started",
        tools_enabled=True,
        tool_count=1,
        tool_names=["web_search"],
        request_started=False,
    )
    disabled = _aggregator_attempt(
        "disabled",
        tools_enabled=False,
        tool_count=0,
        tool_names=[],
    )
    trace["aggregator_recovery"]["attempts"] = [not_started_enabled, disabled]
    assert not updater._framework_attributed_aggregator_attempt(
        trace,
        disabled,
        attempt_index=1,
    )


@pytest.mark.parametrize("contradiction", ["marker", "current_tools", "earlier_names"])
def test_contradictory_framework_neutral_evidence_fails_closed(
    contradiction: str,
) -> None:
    updater = _load_module()
    enabled = _aggregator_attempt(
        "enabled",
        tools_enabled=True,
        tool_count=1,
        tool_names=["web_search"],
    )
    disabled = _aggregator_attempt(
        "disabled",
        tools_enabled=False,
        tool_count=0,
        tool_names=[],
    )
    trace = {
        "aggregator_tools": True,
        "aggregator_recovery": {"attempts": [enabled, disabled]},
    }
    if contradiction == "marker":
        trace.update(
            {
                "aggregator_isolated": False,
                "proposer_partial_quorum": {
                    "schema": "opensquilla.ensemble-partial-proposer-quorum/v1",
                    "applied": True,
                    "aggregator_isolated": True,
                    "aggregator_tools_disabled": True,
                },
            }
        )
    elif contradiction == "current_tools":
        disabled["execution"]["tool_count"] = 1
    else:
        enabled["execution"].update(
            {"tool_count": 2, "tool_names": ["web_search", "web_search"]}
        )

    with pytest.raises(ValueError):
        updater._framework_attributed_aggregator_attempt(
            trace,
            disabled,
            attempt_index=1,
        )


def test_recent_window_is_ordered_by_experiment_metadata(tmp_path: Path) -> None:
    updater = _load_module()
    for index, ok in enumerate([False, *([True] * 50)]):
        attempt_id = f"attempt-{index}"
        _write_output(
            tmp_path / "outputs" / "G1" / f"{index:03d}" / "attempt-1.json",
            finished_at=f"2026-08-05T01:{index:02d}:00+00:00",
            run_id=f"run-{index}",
            task_id=f"task-{index}",
            breakdown=[_breakdown_row("model-a", "proposer", 1)],
            trace={
                "physical_request_count": 1,
                "candidates": [
                    _candidate(
                        "model-a",
                        attempt_id,
                        ok=ok,
                        outcome="succeeded",
                        usage_reported=True,
                    )
                ],
            },
        )

    collection = updater.collect_observations([tmp_path], allow_incomplete=True)
    updated, summary = updater.update_profiles(
        _profiles(),
        collection,
        window_size=50,
        generated_at="2026-08-05T04:00:00+00:00",
        source_artifacts=[str(tmp_path)],
    )
    reliability = updated["models"][0]["online_profile"]["role_reliability"]
    assert reliability["proposer"] == {"success": 50, "failure": 0}
    assert reliability["aggregator"] == {"success": 0, "failure": 0}
    assert summary["raw_physical_calls"] == 51
    assert summary["attributable_calls"] == 51
    assert summary["window_calls"] == 50


def test_output_records_deduplicate_by_stable_identity_and_reject_conflicts(
    tmp_path: Path,
) -> None:
    updater = _load_module()
    first = tmp_path / "one" / "attempt-1.json"
    _write_output(
        first,
        finished_at="2026-08-05T01:00:00+00:00",
        run_id="same-run",
        task_id="same-task",
        breakdown=[_breakdown_row("model-a", "proposer", 1)],
        trace={
            "physical_request_count": 1,
            "candidates": [
                _candidate(
                    "model-a",
                    "audit-gate-success",
                    ok=True,
                    outcome="succeeded",
                    usage_reported=True,
                )
            ],
        },
    )
    duplicate = tmp_path / "two" / "attempt-1.json"
    duplicate.parent.mkdir(parents=True)
    shutil.copyfile(first, duplicate)
    discovered = updater.discover_output_files([tmp_path])
    assert len(discovered) == 1
    assert discovered[0] in {first.resolve(), duplicate.resolve()}

    payload = json.loads(duplicate.read_text(encoding="utf-8"))
    payload["changed"] = True
    duplicate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="conflicting AEF outputs"):
        updater.discover_output_files([tmp_path])


def test_completion_gate_requires_clean_final_audit(tmp_path: Path) -> None:
    updater = _load_module()
    output = tmp_path / "outputs" / "G1" / "001" / "attempt-1.json"
    _write_output(
        output,
        finished_at="2026-08-05T01:00:00+00:00",
        run_id="run-1",
        task_id="task-1",
        breakdown=[_breakdown_row("model-a", "proposer", 1)],
        trace={
            "physical_request_count": 1,
            "candidates": [
                _candidate(
                    "model-a",
                    "audit-gate-success",
                    ok=True,
                    outcome="succeeded",
                    usage_reported=True,
                )
            ],
        },
    )
    with pytest.raises(ValueError, match="no summary/final-audit.json"):
        updater.collect_observations([tmp_path / "outputs" / "G1"])

    summary = tmp_path / "summary"
    summary.mkdir()
    audit_path = summary / "final-audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "complete": False,
                "ok": False,
                "integrity_ok": False,
                "issue_count": 1,
                "issues": [{"code": "still_running"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="experiment is not complete"):
        updater.collect_observations(
            [tmp_path / "outputs" / "G1"],
            allowed_audit_issue_codes=("still_running",),
        )

    audit_path.write_text(
        json.dumps(
            {
                "complete": True,
                "ok": False,
                "integrity_ok": False,
                "issue_count": 1,
                "issues": [{"code": "usage_artifact_missing"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not cleanly auditable"):
        updater.collect_observations([tmp_path / "outputs" / "G1"])
    overridden = updater.collect_observations(
        [tmp_path / "outputs" / "G1"],
        allowed_audit_issue_codes=("usage_artifact_missing",),
    )
    assert overridden.physical_requests_seen == 1
    assert overridden.completion_gate == "complete_with_audit_issue_allowlist"
    assert overridden.allowed_audit_issue_codes == ("usage_artifact_missing",)

    audit_path.write_text(
        json.dumps(
            {
                "complete": True,
                "ok": True,
                "integrity_ok": True,
                "issue_count": 0,
                "issues": [],
            }
        ),
        encoding="utf-8",
    )
    collection = updater.collect_observations([tmp_path / "outputs" / "G1"])
    assert collection.physical_requests_seen == 1
    assert collection.completion_gate == "clean_final_audit"
    assert {kind for kind, _path, _digest in collection.evidence_file_hashes} == {
        "final_audit",
        "timing_metadata",
    }


def test_cli_defaults_to_a_new_file_and_requires_explicit_in_place(
    tmp_path: Path,
) -> None:
    updater = _load_module()
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(json.dumps(_profiles()), encoding="utf-8")
    output = tmp_path / "outputs" / "G1" / "001" / "attempt-1.json"
    _write_output(
        output,
        finished_at="2026-08-05T01:00:00+00:00",
        run_id="run-1",
        task_id="task-1",
        breakdown=[_breakdown_row("model-a", "proposer", 1)],
        trace={
            "physical_request_count": 1,
            "candidates": [
                _candidate(
                    "model-a",
                    "cli-success",
                    ok=True,
                    outcome="succeeded",
                    usage_reported=True,
                )
            ],
        },
    )
    summary = tmp_path / "summary"
    summary.mkdir()
    (summary / "final-audit.json").write_text(
        json.dumps(
            {
                "complete": True,
                "ok": False,
                "integrity_ok": False,
                "issue_count": 1,
                "issues": [{"code": "usage_artifact_missing"}],
            }
        ),
        encoding="utf-8",
    )

    common = [
        str(tmp_path),
        "--profiles",
        str(profiles_path),
        "--allow-audit-issue-code",
        "usage_artifact_missing",
        "--generated-at",
        "2026-08-05T02:00:00+00:00",
    ]
    assert updater.main(common) == 0
    default_output = tmp_path / "profiles.updated.json"
    assert default_output.exists()
    original = json.loads(profiles_path.read_text(encoding="utf-8"))
    assert "role_reliability_snapshot" not in original

    assert updater.main([*common, "--in-place"]) == 0
    in_place = json.loads(profiles_path.read_text(encoding="utf-8"))
    proposer = in_place["models"][0]["online_profile"]["role_reliability"]["proposer"]
    assert proposer == {"success": 1, "failure": 0}
    assert (
        in_place["role_reliability_snapshot"]["completion_gate"]
        == "complete_with_audit_issue_allowlist"
    )

    with pytest.raises(SystemExit):
        updater.build_parser().parse_args([str(tmp_path), "--allow-incomplete"])


def test_repeated_updates_preserve_base_version_and_portable_provenance(
    tmp_path: Path,
) -> None:
    updater = _load_module()
    run_root = tmp_path / "reports" / "pinch-bench" / "run-1"
    output = run_root / "outputs" / "G1" / "001" / "attempt-1.json"
    output.parent.mkdir(parents=True)
    output.write_text("{}", encoding="utf-8")
    output_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    collection = updater.CollectionResult(
        observations=(),
        output_files=(str(output),),
        physical_requests_seen=0,
        framework_excluded=0,
        duplicate_attempts=0,
        unclassified_requests=0,
        completion_gate="clean_final_audit",
        output_file_hashes=((str(output), output_hash),),
    )

    first, _ = updater.update_profiles(
        _profiles(),
        collection,
        generated_at="2026-08-05T02:00:00+00:00",
        source_artifacts=[str(run_root)],
    )
    second, _ = updater.update_profiles(
        first,
        collection,
        generated_at="2026-08-06T02:00:00+00:00",
        source_artifacts=[str(run_root)],
    )

    assert first["snapshot_version"] == (
        "base-snapshot-reliability-20260805T020000Z-"
        f"{first['role_reliability_snapshot']['content_sha256'][:12]}"
    )
    assert second["snapshot_version"] == (
        "base-snapshot-reliability-20260806T020000Z-"
        f"{second['role_reliability_snapshot']['content_sha256'][:12]}"
    )
    assert second["role_reliability_snapshot"]["base_snapshot_version"] == (
        "base-snapshot"
    )
    assert second["role_reliability_snapshot"]["source_artifacts"] == [
        "aef-report://pinch-bench/run-1"
    ]
    assert second["role_reliability_snapshot"]["source_output_files"] == [
        "aef-report://pinch-bench/run-1/outputs/G1/001/attempt-1.json"
    ]
    assert second["role_reliability_snapshot"]["source_output_file_hashes"] == [
        {
            "uri": "aef-report://pinch-bench/run-1/outputs/G1/001/attempt-1.json",
            "sha256": output_hash,
        }
    ]
    assert second["role_reliability_snapshot"]["source_evidence_file_hashes"] == []


@pytest.mark.parametrize(
    "provenance",
    [
        [],
        {"schema_version": "wrong", "base_snapshot_version": "base-snapshot"},
        {"schema_version": "role-reliability-snapshot-v1"},
    ],
)
def test_repeated_update_rejects_invalid_reliability_provenance(
    provenance: object,
) -> None:
    updater = _load_module()
    profiles = _profiles()
    profiles["role_reliability_snapshot"] = provenance
    collection = updater.CollectionResult((), (), 0, 0, 0, 0, "clean_final_audit")

    with pytest.raises(ValueError, match="role_reliability_snapshot"):
        updater.update_profiles(
            profiles,
            collection,
            generated_at="2026-08-05T02:00:00+00:00",
            source_artifacts=[],
        )


def test_residual_ledger_rows_are_unknown_outcomes_not_successes(
    tmp_path: Path,
) -> None:
    updater = _load_module()
    output = tmp_path / "outputs" / "G1" / "001" / "attempt-1.json"
    _write_output(
        output,
        finished_at="2026-08-05T01:00:00+00:00",
        run_id="run-residual",
        task_id="task-residual",
        breakdown=[_breakdown_row("model-a", "proposer", 1)],
        trace={"physical_request_count": 1, "candidates": []},
    )

    with pytest.raises(ValueError, match="unknown outcomes exceed"):
        updater.collect_observations([tmp_path], allow_incomplete=True)

    collection = updater.collect_observations(
        [tmp_path],
        allow_incomplete=True,
        max_unknown_outcome_rate=1.0,
    )
    assert collection.observations == ()
    assert collection.unknown_outcomes == 1
    assert collection.physical_requests_seen == 1
    assert collection.output_file_hashes == (
        (str(output.resolve()), hashlib.sha256(output.read_bytes()).hexdigest()),
    )


@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf"), True, "0"])
def test_unknown_outcome_threshold_is_strictly_validated(value: object) -> None:
    updater = _load_module()
    with pytest.raises(ValueError, match="max_unknown_outcome_rate"):
        updater.collect_observations(
            [],
            allow_incomplete=True,
            max_unknown_outcome_rate=value,
        )


def test_attempt_id_reuse_across_output_files_fails_closed(tmp_path: Path) -> None:
    updater = _load_module()
    for index in range(2):
        _write_output(
            tmp_path / "outputs" / "G1" / f"{index:03d}" / "attempt-1.json",
            finished_at=f"2026-08-05T01:0{index}:00+00:00",
            run_id=f"run-{index}",
            task_id=f"task-{index}",
            breakdown=[_breakdown_row("model-a", "proposer", 1)],
            trace={
                "physical_request_count": 1,
                "candidates": [
                    _candidate(
                        "model-a",
                        "reused-attempt-id",
                        ok=True,
                        outcome="succeeded",
                        usage_reported=True,
                    )
                ],
            },
        )

    with pytest.raises(ValueError, match="reused across usage records"):
        updater.collect_observations([tmp_path], allow_incomplete=True)


def test_conflicting_duplicate_attempt_evidence_fails_closed(tmp_path: Path) -> None:
    updater = _load_module()
    output = tmp_path / "outputs" / "G1" / "001" / "attempt-1.json"
    _write_output(
        output,
        finished_at="2026-08-05T01:00:00+00:00",
        run_id="run-conflict",
        task_id="task-conflict",
        breakdown=[_breakdown_row("model-a", "proposer", 1)],
        trace={
            "physical_request_count": 1,
            "candidates": [
                _candidate(
                    "model-a",
                    "conflicting-attempt",
                    ok=True,
                    outcome="succeeded",
                    usage_reported=True,
                )
            ],
            "proposer_recovery": {
                "attempts": [
                    {
                        "target_identity": "openrouter:model-a",
                        "physical_attempt_id": "conflicting-attempt",
                        "request_started": True,
                        "outcome": "failed",
                    }
                ]
            },
        },
    )

    with pytest.raises(ValueError, match="conflicting duplicate evidence"):
        updater.collect_observations([tmp_path], allow_incomplete=True)


def test_audit_issue_allowlist_rejects_any_unlisted_or_malformed_issue(
    tmp_path: Path,
) -> None:
    updater = _load_module()
    output = tmp_path / "outputs" / "G1" / "001" / "attempt-1.json"
    _write_output(
        output,
        finished_at="2026-08-05T01:00:00+00:00",
        run_id="run-audit",
        task_id="task-audit",
        breakdown=[_breakdown_row("model-a", "proposer", 1)],
        trace={
            "physical_request_count": 1,
            "candidates": [
                _candidate(
                    "model-a",
                    "audit-attempt",
                    ok=True,
                    outcome="succeeded",
                    usage_reported=True,
                )
            ],
        },
    )
    audit_path = tmp_path / "summary" / "final-audit.json"
    audit_path.parent.mkdir()
    audit = {
        "complete": True,
        "ok": False,
        "integrity_ok": False,
        "issue_count": 2,
        "issues": [
            {"code": "usage_artifact_missing"},
            {"code": "score_mismatch"},
        ],
    }
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    with pytest.raises(ValueError, match="score_mismatch"):
        updater.collect_observations(
            [tmp_path],
            allowed_audit_issue_codes=("usage_artifact_missing",),
        )
    allowed = updater.collect_observations(
        [tmp_path],
        allowed_audit_issue_codes=("score_mismatch", "usage_artifact_missing"),
    )
    assert allowed.allowed_audit_issue_codes == (
        "score_mismatch",
        "usage_artifact_missing",
    )

    audit["issue_count"] = 1
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    with pytest.raises(ValueError, match="issue_count disagrees"):
        updater.collect_observations(
            [tmp_path],
            allowed_audit_issue_codes=("score_mismatch", "usage_artifact_missing"),
        )


@pytest.mark.parametrize("timestamp", ["not-a-time", "2026-08-05T01:00:00", ""])
def test_output_timestamps_must_be_valid_and_timezone_aware(
    tmp_path: Path,
    timestamp: str,
) -> None:
    updater = _load_module()
    output = tmp_path / "outputs" / "G1" / "001" / "attempt-1.json"
    _write_output(
        output,
        finished_at=timestamp,
        run_id="run-time",
        task_id="task-time",
        breakdown=[_breakdown_row("model-a", "proposer", 1)],
        trace={
            "physical_request_count": 1,
            "candidates": [
                _candidate(
                    "model-a",
                    "time-attempt",
                    ok=True,
                    outcome="succeeded",
                    usage_reported=True,
                )
            ],
        },
    )

    with pytest.raises(ValueError, match="timestamp|finished_at/started_at"):
        updater.collect_observations([tmp_path], allow_incomplete=True)


def test_missing_timing_metadata_fails_closed(tmp_path: Path) -> None:
    updater = _load_module()
    output = tmp_path / "outputs" / "G1" / "001" / "attempt-1.json"
    _write_output(
        output,
        finished_at="2026-08-05T01:00:00+00:00",
        run_id="run-no-meta",
        task_id="task-no-meta",
        breakdown=[_breakdown_row("model-a", "proposer", 1)],
        trace={
            "physical_request_count": 1,
            "candidates": [
                _candidate(
                    "model-a",
                    "no-meta-attempt",
                    ok=True,
                    outcome="succeeded",
                    usage_reported=True,
                )
            ],
        },
    )
    output.with_name(f"{output.stem}.meta.json").unlink()

    with pytest.raises(ValueError, match="timing metadata"):
        updater.collect_observations([tmp_path], allow_incomplete=True)


def test_custom_snapshot_version_preserves_original_base_across_refreshes() -> None:
    updater = _load_module()
    collection = updater.CollectionResult((), (), 0, 0, 0, 0, "clean_final_audit")
    first, _ = updater.update_profiles(
        _profiles(),
        collection,
        generated_at="2026-08-05T10:00:00+08:00",
        source_artifacts=[],
        snapshot_version="base-snapshot-reliability-manual.1",
    )
    second, _ = updater.update_profiles(
        first,
        collection,
        generated_at="2026-08-06T10:00:00+08:00",
        source_artifacts=[],
        snapshot_version="base-snapshot-reliability-manual.2",
    )

    assert first["role_reliability_snapshot"]["generated_at"] == (
        "2026-08-05T02:00:00+00:00"
    )
    assert second["snapshot_version"] == "base-snapshot-reliability-manual.2"
    assert second["role_reliability_snapshot"]["base_snapshot_version"] == (
        "base-snapshot"
    )
    assert second["role_reliability_snapshot"]["schema_version"] == (
        "role-reliability-snapshot-v2"
    )


@pytest.mark.parametrize(
    "snapshot_version",
    [
        "other-base-reliability-manual",
        "base-snapshot-reliability-",
        "base-snapshot-reliability-invalid/path",
        " base-snapshot-reliability-space inside ",
    ],
)
def test_custom_snapshot_version_rejects_wrong_base_or_nonportable_value(
    snapshot_version: str,
) -> None:
    updater = _load_module()
    collection = updater.CollectionResult((), (), 0, 0, 0, 0, "clean_final_audit")

    with pytest.raises(ValueError, match="snapshot_version"):
        updater.update_profiles(
            _profiles(),
            collection,
            generated_at="2026-08-05T02:00:00+00:00",
            source_artifacts=[],
            snapshot_version=snapshot_version,
        )


def test_v1_snapshot_provenance_remains_refreshable() -> None:
    updater = _load_module()
    profiles = _profiles()
    profiles["snapshot_version"] = "base-snapshot-reliability-legacy"
    profiles["role_reliability_snapshot"] = {
        "schema_version": "role-reliability-snapshot-v1",
        "base_snapshot_version": "base-snapshot",
    }
    collection = updater.CollectionResult((), (), 0, 0, 0, 0, "clean_final_audit")

    updated, _ = updater.update_profiles(
        profiles,
        collection,
        generated_at="2026-08-05T02:00:00+00:00",
        source_artifacts=[],
    )

    assert updated["snapshot_version"] == (
        "base-snapshot-reliability-20260805T020000Z-"
        f"{updated['role_reliability_snapshot']['content_sha256'][:12]}"
    )
    assert updated["role_reliability_snapshot"]["schema_version"] == (
        "role-reliability-snapshot-v2"
    )


def test_output_hash_manifest_rejects_post_collection_tampering(tmp_path: Path) -> None:
    updater = _load_module()
    output = tmp_path / "reports" / "pinch-bench" / "run-1" / "result.json"
    output.parent.mkdir(parents=True)
    output.write_text("original", encoding="utf-8")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    collection = updater.CollectionResult(
        (),
        (str(output),),
        0,
        0,
        0,
        0,
        "clean_final_audit",
        output_file_hashes=((str(output), digest),),
    )
    output.write_text("tampered", encoding="utf-8")

    with pytest.raises(ValueError, match="changed after collection"):
        updater.update_profiles(
            _profiles(),
            collection,
            generated_at="2026-08-05T02:00:00+00:00",
            source_artifacts=[],
        )


def test_evidence_hash_manifest_rejects_timing_metadata_tampering(
    tmp_path: Path,
) -> None:
    updater = _load_module()
    output = tmp_path / "outputs" / "G1" / "001" / "attempt-1.json"
    _write_output(
        output,
        finished_at="2026-08-05T01:00:00+00:00",
        run_id="run-evidence",
        task_id="task-evidence",
        breakdown=[_breakdown_row("model-a", "proposer", 1)],
        trace={
            "physical_request_count": 1,
            "candidates": [
                _candidate(
                    "model-a",
                    "evidence-attempt",
                    ok=True,
                    outcome="succeeded",
                    usage_reported=True,
                )
            ],
        },
    )
    collection = updater.collect_observations([tmp_path], allow_incomplete=True)
    metadata_path = output.with_name(f"{output.stem}.meta.json")
    metadata_path.write_text(
        json.dumps({"finished_at": "2026-08-06T01:00:00+00:00"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="evidence file changed after collection"):
        updater.update_profiles(
            _profiles(),
            collection,
            generated_at="2026-08-05T02:00:00+00:00",
            source_artifacts=[],
        )


def test_repeated_real_snapshot_refresh_preserves_historical_base_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = _load_module()
    profiles = json.loads(updater.default_profiles_path().read_text(encoding="utf-8"))
    original_historical = ranking_router.load_model_registry_snapshot(
        base_version="curated-openrouter-step2-2026-07-31.1"
    )
    collection = updater.CollectionResult((), (), 0, 0, 0, 0, "clean_final_audit")
    first, _ = updater.update_profiles(
        profiles,
        collection,
        generated_at="2026-08-05T02:00:00+00:00",
        source_artifacts=[],
    )
    second, _ = updater.update_profiles(
        first,
        collection,
        generated_at="2026-08-06T02:00:00+00:00",
        source_artifacts=[],
    )
    monkeypatch.setattr(ranking_router, "_packaged_registry_snapshot", lambda: second)

    replayed = ranking_router.load_model_registry_snapshot(
        base_version="curated-openrouter-step2-2026-07-31.1"
    )
    assert replayed == original_historical


def test_default_snapshot_versions_bind_content_and_reject_same_content_collision() -> None:
    updater = _load_module()
    collection = updater.CollectionResult((), (), 0, 0, 0, 0, "clean_final_audit")
    generated_at = "2026-08-05T02:00:00+00:00"
    first, _ = updater.update_profiles(
        _profiles(),
        collection,
        generated_at=generated_at,
        source_artifacts=[],
    )

    with pytest.raises(ValueError, match="must differ from the input"):
        updater.update_profiles(
            first,
            collection,
            generated_at=generated_at,
            source_artifacts=[],
        )

    different, _ = updater.update_profiles(
        first,
        collection,
        generated_at=generated_at,
        source_artifacts=["different-artifact-set"],
    )
    assert different["snapshot_version"] != first["snapshot_version"]
    assert different["snapshot_version"].endswith(
        different["role_reliability_snapshot"]["content_sha256"][:12]
    )


def test_explicit_snapshot_version_cannot_reuse_current_version() -> None:
    updater = _load_module()
    collection = updater.CollectionResult((), (), 0, 0, 0, 0, "clean_final_audit")
    first, _ = updater.update_profiles(
        _profiles(),
        collection,
        generated_at="2026-08-05T02:00:00+00:00",
        source_artifacts=[],
        snapshot_version="base-snapshot-reliability-manual.1",
    )

    with pytest.raises(ValueError, match="must differ from the input"):
        updater.update_profiles(
            first,
            collection,
            generated_at="2026-08-06T02:00:00+00:00",
            source_artifacts=[],
            snapshot_version=first["snapshot_version"],
        )


def test_repeated_update_rejects_snapshot_content_tampering() -> None:
    updater = _load_module()
    collection = updater.CollectionResult((), (), 0, 0, 0, 0, "clean_final_audit")
    first, _ = updater.update_profiles(
        _profiles(),
        collection,
        generated_at="2026-08-05T02:00:00+00:00",
        source_artifacts=[],
    )
    tampered = json.loads(json.dumps(first))
    tampered["models"][0]["online_profile"]["role_reliability"]["proposer"][
        "success"
    ] = 1

    with pytest.raises(ValueError, match="content_sha256 differs"):
        updater.update_profiles(
            tampered,
            collection,
            generated_at="2026-08-06T02:00:00+00:00",
            source_artifacts=[],
        )
