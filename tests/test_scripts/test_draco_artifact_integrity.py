from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.eval.draco_artifact_integrity import (
    seal_result_row,
    trace_row_from_result,
    verify_result_row_evidence,
)
from opensquilla.provider import ranking_router

ROOT = Path(__file__).resolve().parents[2]
PREPARE_CANARY = ROOT / "scripts" / "experiments" / "prepare_draco_b2_canary.py"
SEAL_ARTIFACTS = ROOT / "scripts" / "experiments" / "seal_draco_b2_artifacts.py"
CAPTURE_RUNTIME = ROOT / "scripts" / "experiments" / "capture_draco_runtime_environment.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_sealer_registry_snapshot_selects_by_version_and_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_registry_select_test")
    raw = {
        "schema_version": "raw",
        "snapshot_version": "same-version",
        "models": [{"raw": True}],
    }
    legacy = {
        "schema_version": "legacy",
        "snapshot_version": "same-version",
        "models": [{"raw": False}],
    }
    requested_versions: list[str | None] = []

    def load_snapshot(*, base_version: str | None = None):
        requested_versions.append(base_version)
        return raw

    monkeypatch.setattr(ranking_router, "load_model_registry_snapshot", load_snapshot)
    monkeypatch.setattr(
        ranking_router,
        "_legacy_registry_snapshot_projection",
        lambda snapshot: legacy,
    )
    raw_contract = SimpleNamespace(
        source_registry_snapshot_version="same-version",
        expected_source_registry_snapshot_sha256=module.canonical_sha256(raw),
    )
    legacy_contract = SimpleNamespace(
        source_registry_snapshot_version="same-version",
        expected_source_registry_snapshot_sha256=module.canonical_sha256(legacy),
    )

    assert module._formal_registry_snapshot(raw_contract) is raw
    assert module._formal_registry_snapshot(legacy_contract) is legacy
    assert requested_versions == ["same-version", "same-version"]


def test_sealer_registry_snapshot_fails_closed_without_exact_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_registry_reject_test")
    raw = {"snapshot_version": "same-version", "models": [{"raw": True}]}
    legacy = {"snapshot_version": "same-version", "models": [{"raw": False}]}
    def load_snapshot(*, base_version: str | None = None):
        assert base_version is not None
        return raw

    monkeypatch.setattr(ranking_router, "load_model_registry_snapshot", load_snapshot)
    monkeypatch.setattr(
        ranking_router,
        "_legacy_registry_snapshot_projection",
        lambda snapshot: legacy,
    )
    wrong_hash = SimpleNamespace(
        source_registry_snapshot_version="same-version",
        expected_source_registry_snapshot_sha256="0" * 64,
    )
    wrong_version = SimpleNamespace(
        source_registry_snapshot_version="other-version",
        expected_source_registry_snapshot_sha256=module.canonical_sha256(raw),
    )

    with pytest.raises(ValueError, match="hash differs"):
        module._formal_registry_snapshot(wrong_hash)
    with pytest.raises(ValueError, match="version differs"):
        module._formal_registry_snapshot(wrong_version)


def test_full_result_hash_and_exact_trace_projection_fail_on_mutation() -> None:
    row = seal_result_row(
        {
            "row_index": 1,
            "group": "B2",
            "task_id": "task-1",
            "final_text": "answer",
            "execution": {"generation_attempts": [{"attempt": 1}]},
            "usage": {"billed_cost": 0.25},
        }
    )
    assert verify_result_row_evidence(row) is True
    trace = trace_row_from_result(row)
    assert trace == trace_row_from_result(row)

    changed_result = {**row, "final_text": "different"}
    assert verify_result_row_evidence(changed_result) is False
    changed_trace = json.loads(json.dumps(trace))
    changed_trace["execution"]["generation_attempts"][0]["attempt"] = 2
    assert changed_trace != trace_row_from_result(row)


def test_canary_is_disjoint_and_only_changes_scheduling_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load(PREPARE_CANARY, "prepare_draco_canary_test")
    benchmark = tmp_path / "mini.jsonl"
    benchmark.write_text(
        json.dumps({"id": "formal-task", "prompt": "formal prompt"}) + "\n",
        encoding="utf-8",
    )
    base = {
        "profile_id": "frozen",
        "benchmark_input": {"task_count": 1},
        "runner": {"mode": "agent_loop", "concurrency": 5},
        "judge": {"model": "judge", "repeats": 3, "concurrency": 6},
        "ensemble": {"members": ["unchanged"]},
    }
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    output_input = tmp_path / "canary.jsonl"
    output_config = tmp_path / "canary-config.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(PREPARE_CANARY),
            "--base-config",
            str(base_path),
            "--benchmark-input",
            str(benchmark),
            "--output-input",
            str(output_input),
            "--output-config",
            str(output_config),
        ],
    )

    assert module.main() == 0
    canary = json.loads(output_input.read_text(encoding="utf-8"))
    config = json.loads(output_config.read_text(encoding="utf-8"))
    assert canary["id"] != "formal-task"
    assert canary["prompt"] != "formal prompt"
    assert "web_search" in canary["prompt"] and "web_fetch" in canary["prompt"]
    assert config["runner"] == {"mode": "agent_loop", "concurrency": 1}
    assert config["judge"] == {
        "model": "judge",
        "repeats": 3,
        "concurrency": 1,
    }
    assert config["ensemble"] == base["ensemble"]
    assert os.stat(output_input).st_mode & 0o777 == 0o600
    assert os.stat(output_config).st_mode & 0o777 == 0o600


def test_artifact_snapshot_detects_post_audit_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_test")
    artifact = tmp_path / "result.jsonl"
    artifact.write_text("sealed\n", encoding="utf-8")
    artifact.chmod(0o600)
    snapshot = tmp_path / "snapshot.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SEAL_ARTIFACTS),
            "snapshot",
            str(snapshot),
            "--root",
            str(tmp_path),
            "--file",
            str(artifact),
        ],
    )
    assert module.main() == 0
    module.verify_snapshot(snapshot)

    artifact.write_text("mutated\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after audit"):
        module.verify_snapshot(snapshot)


def test_recursive_snapshot_is_closed_and_portable_after_archiving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_recursive_test")
    source = tmp_path / "source"
    nested = source / "strict-structure-audit"
    nested.mkdir(parents=True)
    result = source / "result.jsonl"
    report = nested / "report.json"
    result.write_text("sealed\n", encoding="utf-8")
    report.write_text("{}\n", encoding="utf-8")
    result.chmod(0o600)
    report.chmod(0o600)
    snapshot = source / "artifact-snapshot.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SEAL_ARTIFACTS),
            "snapshot",
            str(snapshot),
            "--root",
            str(source),
            "--recursive",
            "--allow-after",
            "FORMAL_RUN_SUCCESS.json",
        ],
    )
    assert module.main() == 0
    module.verify_snapshot(snapshot)

    extra = source / "unexpected.jsonl"
    extra.write_text("pollution\n", encoding="utf-8")
    extra.chmod(0o600)
    with pytest.raises(ValueError, match="artifact set changed"):
        module.verify_snapshot(snapshot)
    extra.unlink()

    archived = tmp_path / "archived"
    shutil.copytree(source, archived)
    archived_snapshot = archived / snapshot.name
    module.verify_snapshot(archived_snapshot)
    (archived / "result.jsonl").write_text("mutated copy\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after audit"):
        module.verify_snapshot(archived_snapshot)


def test_runtime_environment_capture_is_verifiable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load(CAPTURE_RUNTIME, "capture_draco_runtime_test")
    evidence = tmp_path / "runtime-environment.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(CAPTURE_RUNTIME),
            "capture",
            str(evidence),
            "--repo",
            str(ROOT),
        ],
    )
    assert module.main() == 0
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(CAPTURE_RUNTIME),
            "verify",
            str(evidence),
            "--repo",
            str(ROOT),
        ],
    )
    assert module.main() == 0

    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["environment_sha256"] = "0" * 64
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="runtime changed"):
        module.main()


def _owner_only_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _formal_g1_config(
    module,
    tmp_path: Path,
    *,
    backup_count: int | None = None,
) -> tuple[Path, dict[str, object], str]:
    source = ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"
    config = json.loads(source.read_text(encoding="utf-8"))
    if backup_count is not None:
        config["router_dynamic_ranking_override"] = {
            "proposer_count": {"backup_count": backup_count}
        }
    path = tmp_path / "experiment-config.json"
    _owner_only_json(path, config)
    return path, config, module.file_sha256(path)


def _v2_route_preflight(
    module,
    *,
    config_path: Path,
    config: dict[str, object],
    config_sha256: str,
    inline_overlay: dict[str, object] | None = None,
) -> dict[str, object]:
    raw_g1 = config["g1_routing"]
    assert isinstance(raw_g1, dict)
    inline_overlay_json = json.dumps(inline_overlay) if inline_overlay is not None else None
    resolved, resolved_contract = module._resolved_g1_contract(
        config_path,
        inline_overlay_json=inline_overlay_json,
    )
    experiment = module.load_draco_experiment_config(
        config_path,
        inline_overlay_json=inline_overlay_json,
    ).config
    ranking_resolution = module._effective_ranking_resolution(experiment)
    routes = dict(resolved_contract["expected_routes"])
    candidate_scope = str(resolved_contract["candidate_scope"])
    candidate_policy = str(resolved_contract["policy"])
    required_parameters = module._formal_required_parameters(routes)
    return {
        "schema": module.ROUTE_PREFLIGHT_V2_SCHEMA,
        "api_origin": "https://openrouter.ai",
        "scope": "formal",
        "trust_env": False,
        "providers_response_sha256": "1" * 64,
        "candidate_scope": candidate_scope,
        "candidate_policy": candidate_policy,
        "expected_routes": routes,
        "expected_routes_sha256": module.canonical_sha256(routes),
        "experiment_config": {
            "path": str(config_path.resolve()),
            "sha256": config_sha256,
            "inline_overlay": inline_overlay,
            "inline_overlay_sha256": (
                module.canonical_sha256(inline_overlay)
                if inline_overlay is not None
                else None
            ),
            "g1_routing_profile_id": resolved.profile_id,
            "source_registry_snapshot_version": resolved.source_registry_snapshot_version,
        },
        "ranking_config_resolution": ranking_resolution,
        "task_analyzer": module._resolved_task_analyzer_policy(ranking_resolution),
        "required_parameters_sha256": module.canonical_sha256(required_parameters),
        "models": {
            model: {
                "expected_provider": provider,
                "response_model_id": model,
                "response_sha256": "2" * 64,
                "matching_endpoints": [
                    {
                        "tag": "any-upstream" if provider == "auto" else provider,
                        "provider_name": (
                            "Any Upstream"
                            if provider == "auto"
                            else module.EXPECTED_PROVIDER_NAMES[provider]
                        ),
                        "model_id": model,
                        "status": 0,
                        "supported_parameters": list(required_parameters[model]),
                    }
                ],
                "operational_match_count": 1,
                "compatible_operational_match_count": 1,
                "required_parameters": list(required_parameters[model]),
            }
            for model, provider in routes.items()
        },
        "route_metadata_pass": True,
    }


def _refresh_v3_availability_summary(payload: dict[str, object]) -> None:
    models = payload["models"]
    assert isinstance(models, dict)
    proposer_models = sorted(
        model
        for model, row in models.items()
        if isinstance(row, dict) and int(row["proposer_compatible_operational_match_count"]) > 0
    )
    aggregator_models = sorted(
        model
        for model, row in models.items()
        if isinstance(row, dict) and int(row["aggregator_compatible_operational_match_count"]) > 0
    )
    payload["proposer_compatible_candidate_count"] = len(proposer_models)
    payload["aggregator_compatible_candidate_count"] = len(aggregator_models)
    payload["proposer_compatible_models"] = proposer_models
    payload["aggregator_compatible_models"] = aggregator_models
    payload["unavailable_models"] = sorted(set(models) - set(proposer_models))
    payload["aggregator_ineligible_models"] = sorted(set(models) - set(aggregator_models))
    payload["availability_status"] = (
        "complete"
        if not payload["unavailable_models"] and not payload["aggregator_ineligible_models"]
        else "degraded"
    )
    payload["candidate_capacity_pass"] = len(proposer_models) >= int(
        payload["required_proposer_compatible_candidate_count"]
    ) and len(aggregator_models) >= int(payload["required_aggregator_compatible_candidate_count"])


def _v3_route_preflight(
    module,
    *,
    config_path: Path,
    config: dict[str, object],
    config_sha256: str,
    inline_overlay: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = _v2_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
        inline_overlay=inline_overlay,
    )
    payload["schema"] = module.ROUTE_PREFLIGHT_V3_SCHEMA
    payload["groups"] = ["G1"]
    payload["availability_policy"] = "registry_capacity"
    routes = payload["expected_routes"]
    models = payload["models"]
    assert isinstance(routes, dict)
    assert isinstance(models, dict)
    inline_overlay_json = json.dumps(inline_overlay) if inline_overlay is not None else None
    _, resolved_contract = module._resolved_g1_contract(
        config_path,
        inline_overlay_json=inline_overlay_json,
    )
    proposer_required = module._formal_proposer_required_parameters(
        routes,
        reasoning_ineligible_models=set(resolved_contract["reasoning_ineligible_models"]),
    )
    aggregator_required = module._formal_required_parameters(
        routes,
        reasoning_ineligible_models=set(resolved_contract["reasoning_ineligible_models"]),
    )
    for model, row in models.items():
        assert isinstance(row, dict)
        row.update(
            {
                "requested_model_id": model,
                "endpoint_fetch_outcome": "ok",
                "endpoint_http_status": 200,
                "response_sha256_kind": "canonical_json",
                "proposer_required_parameters": proposer_required[model],
                "aggregator_required_parameters": aggregator_required[model],
                "proposer_compatible_operational_match_count": 1,
                "aggregator_compatible_operational_match_count": 1,
                "availability_status": "compatible",
            }
        )
    payload["proposer_required_parameters_sha256"] = module.canonical_sha256(proposer_required)
    experiment = module.load_draco_experiment_config(
        config_path,
        inline_overlay_json=inline_overlay_json,
    ).config
    fixed_routes, fixed_parameters = module._required_fixed_route_specs(
        experiment=experiment,
        groups=("G1",),
        proposer_required_parameters=proposer_required,
    )
    fixed_endpoint_rows: dict[str, list[dict[str, object]]] = {}
    for model, provider in fixed_routes.items():
        endpoint = {
            "tag": provider,
            "provider_name": module.EXPECTED_PROVIDER_NAMES[provider],
            "model_id": model,
            "status": 0,
            "supported_parameters": aggregator_required[model],
        }
        fixed_endpoint_rows[model] = [endpoint]
        models[model]["matching_endpoints"] = [dict(endpoint)]
    fixed_contract = {
        model: {
            "provider": fixed_routes[model],
            "required_parameters": fixed_parameters[model],
        }
        for model in fixed_routes
    }
    payload["required_fixed_routes"] = fixed_contract
    payload["required_fixed_routes_sha256"] = module.canonical_sha256(fixed_contract)
    payload["fixed_route_checks"] = {
        model: {
            "expected_provider": provider,
            "requested_model_id": model,
            "endpoint_fetch_outcome": "ok",
            "endpoint_http_status": 200,
            "response_sha256_kind": "canonical_json",
            "response_model_id": model,
            "response_sha256": models[model]["response_sha256"],
            "matching_endpoints": fixed_endpoint_rows[model],
            "operational_match_count": 1,
            "compatible_operational_match_count": 1,
            "required_parameters": fixed_parameters[model],
        }
        for model, provider in fixed_routes.items()
    }
    payload["fixed_routes_pass"] = True
    required_proposer_count, required_aggregator_count = module._required_role_capacity(
        experiment,
        ("G1",),
        ranking_resolution=payload["ranking_config_resolution"],
    )
    payload["required_proposer_compatible_candidate_count"] = required_proposer_count
    payload["required_aggregator_compatible_candidate_count"] = required_aggregator_count
    _refresh_v3_availability_summary(payload)
    return payload


def _run_formal_success(
    module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    payloads: list[dict[str, object]],
    experiment_config_sha256: str,
) -> dict[str, object]:
    snapshots: list[Path] = []
    for index in range(3):
        artifact = tmp_path / f"artifact-{index}.jsonl"
        artifact.write_text(f"sealed-{index}\n", encoding="utf-8")
        artifact.chmod(0o600)
        snapshot = tmp_path / f"snapshot-{index}.json"
        module.atomic_write_json(
            snapshot,
            {
                "schema": module.SNAPSHOT_SCHEMA,
                "created_at": "2026-07-25T00:00:00+00:00",
                "root": ".",
                "closed_world": False,
                "allowed_after_snapshot": [],
                "artifacts": [module.relative_file_record(tmp_path, artifact)],
            },
        )
        snapshots.append(snapshot)
    evidence_paths: list[Path] = []
    for index, payload in enumerate(payloads):
        path = tmp_path / f"route-preflight-{index}.json"
        module.atomic_write_json(path, payload)
        evidence_paths.append(path)
    output = tmp_path / "FORMAL_RUN_SUCCESS.json"
    argv = [
        str(SEAL_ARTIFACTS),
        "success",
        str(output),
        "--source-git-head",
        "a" * 40,
        "--input-sha256",
        "b" * 64,
        "--gateway-config-sha256",
        "c" * 64,
        "--experiment-config-sha256",
        experiment_config_sha256,
    ]
    for snapshot in snapshots:
        argv.extend(("--snapshot", str(snapshot)))
    for evidence in evidence_paths:
        argv.extend(("--evidence", str(evidence)))
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == 0
    value = json.loads(output.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_formal_success_accepts_recomputed_v2_route_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load(
        SEAL_ARTIFACTS,
        "seal_draco_artifacts_v2_success_test",
    )
    config_path, config, config_sha256 = _formal_g1_config(module, tmp_path)
    payload = _v2_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )
    success = _run_formal_success(
        module,
        monkeypatch,
        tmp_path,
        payloads=[payload, json.loads(json.dumps(payload))],
        experiment_config_sha256=config_sha256,
    )
    evidence = success["route_preflight_evidence"]
    assert isinstance(evidence, list)
    assert {row["route_preflight_schema"] for row in evidence} == {payload["schema"]}
    contract = evidence[0]["formal_g1_contract"]
    _, resolved_contract = module._resolved_g1_contract(config_path)
    assert contract["candidate_scope"] == resolved_contract["candidate_scope"]
    assert contract["candidate_policy"] == resolved_contract["policy"]
    assert contract["expected_candidate_count"] == resolved_contract["expected_candidate_count"]
    assert contract["expected_routes_sha256"] == resolved_contract["expected_routes_sha256"]


def test_v3_route_preflight_accepts_one_unavailable_dynamic_candidate(
    tmp_path: Path,
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_v3_degraded_test")
    config_path, config, config_sha256 = _formal_g1_config(module, tmp_path)
    payload = _v3_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )
    model = "mistralai/ministral-14b-2512"
    row = payload["models"][model]
    row.update(
        {
            "matching_endpoints": [],
            "operational_match_count": 0,
            "compatible_operational_match_count": 0,
            "proposer_compatible_operational_match_count": 0,
            "aggregator_compatible_operational_match_count": 0,
            "availability_status": "no_matching_endpoint",
        }
    )
    _refresh_v3_availability_summary(payload)

    validation = module.validate_route_preflight_payload(
        payload,
        experiment_config_sha256=config_sha256,
        label="v3 evidence",
    )

    assert payload["proposer_compatible_candidate_count"] == 78
    assert payload["unavailable_models"] == [model]
    assert validation["groups"] == ["G1"]
    assert validation["availability_policy"] == "registry_capacity"


@pytest.mark.parametrize(
    ("backup_count", "required_proposer_count"),
    [(0, 8), (1, 9), (2, 10)],
)
def test_v3_route_preflight_capacity_uses_frozen_ranking_backup_override(
    tmp_path: Path,
    backup_count: int,
    required_proposer_count: int,
) -> None:
    module = _load(SEAL_ARTIFACTS, f"seal_draco_artifacts_v3_backup_{backup_count}_test")
    config_path, config, config_sha256 = _formal_g1_config(
        module,
        tmp_path,
        backup_count=backup_count,
    )
    payload = _v3_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )

    validation = module.validate_route_preflight_payload(
        payload,
        experiment_config_sha256=config_sha256,
        label="ranking override evidence",
    )

    assert payload["required_proposer_compatible_candidate_count"] == (
        required_proposer_count
    )
    assert validation["required_proposer_compatible_candidate_count"] == (
        required_proposer_count
    )

    experiment = module.load_draco_experiment_config(config_path).config
    resolution = module._effective_ranking_resolution(experiment)
    object.__setattr__(experiment.ensemble, "proposer_backup_count", 2 - backup_count)
    assert module._required_role_capacity(
        experiment,
        ("G1",),
        ranking_resolution=resolution,
    ) == (required_proposer_count, 3)


def test_v3_route_preflight_rejects_tampered_frozen_ranking_resolution(
    tmp_path: Path,
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_v3_ranking_tamper_test")
    config_path, config, config_sha256 = _formal_g1_config(
        module,
        tmp_path,
        backup_count=1,
    )
    payload = _v3_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )
    payload["ranking_config_resolution"]["effective_config"]["proposer_count"][
        "backup_count"
    ] = 2

    with pytest.raises(ValueError, match="frozen ranking resolution differs"):
        module.validate_route_preflight_payload(
            payload,
            experiment_config_sha256=config_sha256,
            label="tampered ranking evidence",
        )


def test_v3_route_preflight_authenticates_inline_ranking_override(
    tmp_path: Path,
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_v3_inline_override_test")
    config_path, config, config_sha256 = _formal_g1_config(module, tmp_path)
    inline_overlay = {
        "router_dynamic_ranking_override": {
            "proposer_count": {"backup_count": 1}
        }
    }
    payload = _v3_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
        inline_overlay=inline_overlay,
    )

    validation = module.validate_route_preflight_payload(
        payload,
        experiment_config_sha256=config_sha256,
        label="inline ranking override evidence",
    )

    assert payload["required_proposer_compatible_candidate_count"] == 9
    assert validation["required_proposer_compatible_candidate_count"] == 9
    assert validation["experiment_config_inline_overlay_sha256"] == (
        module.canonical_sha256(inline_overlay)
    )


def test_v3_route_preflight_authenticates_private_effective_config_without_raw_overlay(
    tmp_path: Path,
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_v3_private_effective_test")
    config_path, config, config_sha256 = _formal_g1_config(module, tmp_path)
    marker = "private-overlay-secret-marker"
    inline_overlay = {"reference": {"repository": marker}}
    payload = _v3_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
        inline_overlay=inline_overlay,
    )
    effective = module.load_draco_experiment_config(
        config_path,
        inline_overlay_json=json.dumps(inline_overlay),
    ).config.model_dump(mode="json")
    effective_path = tmp_path / "experiment-config.effective.json"
    _owner_only_json(effective_path, effective)
    evidence = payload["experiment_config"]
    assert isinstance(evidence, dict)
    evidence.pop("inline_overlay")
    evidence["effective_config"] = {
        "path": str(effective_path.resolve()),
        "sha256": module.canonical_sha256(effective),
    }

    validation = module.validate_route_preflight_payload(
        payload,
        experiment_config_sha256=config_sha256,
        label="private effective config evidence",
    )

    assert marker not in json.dumps(payload, ensure_ascii=False)
    assert validation["experiment_config_inline_overlay_sha256"] == (
        module.canonical_sha256(inline_overlay)
    )


def test_v3_route_preflight_accepts_explicit_model_endpoint_404(
    tmp_path: Path,
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_v3_404_test")
    config_path, config, config_sha256 = _formal_g1_config(module, tmp_path)
    payload = _v3_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )
    model = "mistralai/ministral-14b-2512"
    row = payload["models"][model]
    row.update(
        {
            "endpoint_fetch_outcome": "model_not_found",
            "endpoint_http_status": 404,
            "response_sha256_kind": "raw_body",
            "response_model_id": None,
            "matching_endpoints": [],
            "operational_match_count": 0,
            "compatible_operational_match_count": 0,
            "proposer_compatible_operational_match_count": 0,
            "aggregator_compatible_operational_match_count": 0,
            "availability_status": "model_endpoint_not_found",
        }
    )
    _refresh_v3_availability_summary(payload)

    validation = module.validate_route_preflight_payload(
        payload,
        experiment_config_sha256=config_sha256,
        label="v3 404 evidence",
    )

    assert validation["schema"] == module.ROUTE_PREFLIGHT_V3_SCHEMA
    assert payload["unavailable_models"] == [model]


def test_v3_route_preflight_rejects_tampered_availability_or_fixed_route(
    tmp_path: Path,
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_v3_tamper_test")
    config_path, config, config_sha256 = _formal_g1_config(module, tmp_path)
    payload = _v3_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )
    model = "mistralai/ministral-14b-2512"
    payload["models"][model]["availability_status"] = "no_matching_endpoint"
    with pytest.raises(ValueError, match="precomputed availability"):
        module.validate_route_preflight_payload(
            payload,
            experiment_config_sha256=config_sha256,
            label="tampered availability",
        )

    payload = _v3_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )
    fixed_model = "anthropic/claude-opus-4.8"
    payload["fixed_route_checks"][fixed_model]["matching_endpoints"] = []
    with pytest.raises(ValueError, match="fixed-route projection differs"):
        module.validate_route_preflight_payload(
            payload,
            experiment_config_sha256=config_sha256,
            label="tampered fixed route",
        )

    payload = _v3_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )
    payload["fixed_route_checks"][fixed_model]["response_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="fixed-route response differs"):
        module.validate_route_preflight_payload(
            payload,
            experiment_config_sha256=config_sha256,
            label="detached fixed route",
        )

    payload = _v3_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )
    source_row = payload["models"][fixed_model]
    source_row.update(
        {
            "matching_endpoints": [],
            "operational_match_count": 0,
            "compatible_operational_match_count": 0,
            "proposer_compatible_operational_match_count": 0,
            "aggregator_compatible_operational_match_count": 0,
            "availability_status": "no_matching_endpoint",
        }
    )
    _refresh_v3_availability_summary(payload)
    with pytest.raises(ValueError, match="fixed-route projection differs"):
        module.validate_route_preflight_payload(
            payload,
            experiment_config_sha256=config_sha256,
            label="contradictory fixed route",
        )


def test_formal_success_rejects_v1_without_endpoint_details(tmp_path: Path) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_v1_rejected_test")

    with pytest.raises(ValueError, match="lacks endpoint details"):
        module.validate_route_preflight_payload(
            {"schema": module.ROUTE_PREFLIGHT_V1_SCHEMA, "pass": True},
            experiment_config_sha256="a" * 64,
            label="legacy evidence",
        )


def test_route_preflight_auto_provider_still_binds_model_and_parameters() -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_auto_provider_test")
    parameters = ["max_tokens", "tools"]
    endpoints = [
        {
            "tag": "unfrozen-upstream",
            "provider_name": "Unfrozen Upstream",
            "model_id": "vendor/model",
            "status": 0,
            "supported_parameters": parameters,
        }
    ]

    assert module._recompute_endpoint_counts(
        model="vendor/model",
        expected_provider="auto",
        required_parameters=parameters,
        endpoints=endpoints,
        label="test",
    ) == (1, 1)

    endpoints[0]["model_id"] = "vendor/other"
    with pytest.raises(ValueError, match="no compatible route"):
        module._recompute_endpoint_counts(
            model="vendor/model",
            expected_provider="auto",
            required_parameters=parameters,
            endpoints=endpoints,
            label="test",
        )


def test_route_preflight_reasoning_requirements_follow_frozen_registry(
    tmp_path: Path,
) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_reasoning_registry_test")
    config_path, _, _ = _formal_g1_config(module, tmp_path)
    _, resolved_contract = module._resolved_g1_contract(config_path)
    ineligible = set(resolved_contract["reasoning_ineligible_models"])
    routes = dict(resolved_contract["expected_routes"])
    required = module._formal_required_parameters(
        routes,
        reasoning_ineligible_models=ineligible,
    )

    assert len(ineligible) == 14
    assert "qwen/qwen3-coder-next" in ineligible
    assert "reasoning" not in required["qwen/qwen3-coder-next"]
    assert "reasoning" in required["deepseek/deepseek-v4-pro"]
    assert required["anthropic/claude-fable-5"] == [
        "max_tokens",
        "reasoning",
        "tools",
    ]
    assert required["openai/gpt-5.3-codex"] == [
        "max_tokens",
        "reasoning",
        "tools",
    ]
    assert required["openai/gpt-5.6-terra"] == [
        "max_completion_tokens",
        "reasoning",
        "tools",
    ]


@pytest.mark.parametrize(
    ("bad_field", "error_match"),
    [
        ("route_metadata_pass", "metadata did not pass"),
        ("scope", "scope must be formal"),
        ("expected_routes_sha256", "expected-routes hash differs"),
        ("g1_routing_profile_id", "G1 profile differs"),
        ("models", "model evidence set differs"),
        ("required_parameters_sha256", "required-parameters hash differs"),
        ("experiment_config_sha256", "experiment config hash differs"),
    ],
)
def test_v2_route_preflight_rejects_bad_contract_fields(
    bad_field: str,
    error_match: str,
    tmp_path: Path,
) -> None:
    module = _load(
        SEAL_ARTIFACTS,
        f"seal_draco_artifacts_v2_bad_{bad_field}_test",
    )
    config_path, config, config_sha256 = _formal_g1_config(module, tmp_path)
    payload = _v2_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )
    if bad_field == "route_metadata_pass":
        payload["route_metadata_pass"] = False
    elif bad_field == "scope":
        payload["scope"] = "b2"
    elif bad_field == "expected_routes_sha256":
        payload["expected_routes_sha256"] = "0" * 64
    elif bad_field == "g1_routing_profile_id":
        payload["experiment_config"]["g1_routing_profile_id"] = "wrong-profile"
    elif bad_field == "models":
        payload["models"].pop(next(iter(payload["models"])))
    elif bad_field == "required_parameters_sha256":
        payload["required_parameters_sha256"] = "0" * 64
    else:
        payload["experiment_config"]["sha256"] = "0" * 64

    with pytest.raises(ValueError, match=error_match):
        module.validate_route_preflight_payload(
            payload,
            experiment_config_sha256=config_sha256,
            label="test evidence",
        )


def test_route_preflight_set_rejects_mixed_v1_v2_schemas(tmp_path: Path) -> None:
    module = _load(SEAL_ARTIFACTS, "seal_draco_artifacts_mixed_schema_test")
    config_path, config, config_sha256 = _formal_g1_config(module, tmp_path)
    v2_payload = _v2_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )
    with pytest.raises(ValueError, match="lacks endpoint details"):
        module.validate_route_preflight_set(
            [
                {"schema": module.ROUTE_PREFLIGHT_V1_SCHEMA, "pass": True},
                v2_payload,
            ],
            experiment_config_sha256=config_sha256,
            labels=["before canary", "before full"],
        )


@pytest.mark.parametrize(
    ("mutation", "error_match"),
    [
        ("inflated_count", "precomputed endpoint counts differ"),
        ("wrong_status", "no compatible route"),
        ("wrong_provider_name", "no compatible route"),
        ("wrong_model_id", "no compatible route"),
        ("missing_parameter", "no compatible route"),
        ("wrong_tag", "endpoint provider tag differs"),
        ("weakened_required_parameters", "frozen parameters differ"),
    ],
)
def test_v2_route_preflight_recomputes_saved_endpoint_compatibility(
    mutation: str,
    error_match: str,
    tmp_path: Path,
) -> None:
    module = _load(
        SEAL_ARTIFACTS,
        f"seal_draco_artifacts_v2_endpoint_{mutation}_test",
    )
    config_path, config, config_sha256 = _formal_g1_config(module, tmp_path)
    payload = _v2_route_preflight(
        module,
        config_path=config_path,
        config=config,
        config_sha256=config_sha256,
    )
    model = next(iter(payload["models"]))
    model_row = payload["models"][model]
    endpoint = model_row["matching_endpoints"][0]
    if mutation == "inflated_count":
        model_row["compatible_operational_match_count"] = 2
    elif mutation == "wrong_status":
        endpoint["status"] = 1
    elif mutation == "wrong_provider_name":
        endpoint["provider_name"] = "Wrong Provider"
    elif mutation == "wrong_model_id":
        endpoint["model_id"] = "wrong/model"
    elif mutation == "missing_parameter":
        endpoint["supported_parameters"].pop()
    elif mutation == "wrong_tag":
        endpoint["tag"] = "wrong-provider"
    else:
        model_row["required_parameters"] = model_row["required_parameters"][:-1]
        payload["required_parameters_sha256"] = module.canonical_sha256(
            {
                route_model: payload["models"][route_model]["required_parameters"]
                for route_model in payload["expected_routes"]
            }
        )

    if model_row["expected_provider"] == "auto" and mutation in {
        "wrong_provider_name",
        "wrong_tag",
    }:
        # ``auto`` authenticates an operational, model-matching, compatible
        # endpoint without freezing its upstream provider identity.
        module.validate_route_preflight_payload(
            payload,
            experiment_config_sha256=config_sha256,
            label="unfrozen provider evidence",
        )
        return

    with pytest.raises(ValueError, match=error_match):
        module.validate_route_preflight_payload(
            payload,
            experiment_config_sha256=config_sha256,
            label="tampered evidence",
        )
