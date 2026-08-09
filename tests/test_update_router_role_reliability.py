from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

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

    collection = updater.collect_observations([tmp_path], allow_incomplete=True)
    assert collection.physical_requests_seen == 7
    assert collection.framework_excluded == 1
    assert collection.duplicate_attempts == 1
    assert len(collection.observations) == 6

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
        "proposer": {"success": 3, "failure": 1},
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
        "attributable_calls": 6,
        "window_calls": 6,
        "unclassified_requests": 0,
    }
    snapshot = updated["role_reliability_snapshot"]
    assert snapshot["observation_policy"] == "aef-physical-model-calls-v3"
    assert snapshot["completion_gate"] == "fixture_without_final_audit"
    assert snapshot["ordering_granularity"] == "task_aggregate"
    assert snapshot["base_snapshot_version"] == "base-snapshot"
    assert updated["snapshot_version"] == "base-snapshot-reliability-20260805T020000Z"


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
        trace={"physical_request_count": 1, "candidates": []},
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
        trace={"physical_request_count": 1, "candidates": []},
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
            allow_audit_issues=True,
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
        allow_audit_issues=True,
    )
    assert overridden.physical_requests_seen == 1
    assert overridden.completion_gate == "complete_with_audit_override"

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
        trace={"physical_request_count": 1, "candidates": []},
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
        "--allow-audit-issues",
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
        == "complete_with_audit_override"
    )

    with pytest.raises(SystemExit):
        updater.build_parser().parse_args([str(tmp_path), "--allow-incomplete"])
