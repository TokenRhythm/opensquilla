from __future__ import annotations

import json
from importlib import resources
from pathlib import Path


def test_c1_benchmark_baseline_matches_packaged_runtime_policy() -> None:
    repository = Path(__file__).resolve().parents[1]
    baseline = json.loads(
        (repository / "configs/benchmarks/draco_c1_baseline_20260825.json").read_text()
    )
    packaged = json.loads(
        resources.files("opensquilla.provider")
        .joinpath("router_dynamic_ranking_config.json")
        .read_text()
    )

    assert baseline["baseline_id"] == "C1"
    assert baseline["status"] == "formal_default"
    assert baseline["ranking_config_version"] == packaged["config_version"]
    assert baseline["ranking_parameters"]["normalization"] == {
        key: packaged["normalization"][key]
        for key in (
            "price_reference_usd_per_million",
            "price_input_weight",
            "price_output_weight",
        )
    }
    assert baseline["ranking_parameters"]["penalties"]["task_cost_weights"] == (
        packaged["penalties"]["task_cost_weights"]
    )
    assert baseline["ranking_parameters"]["task_match"] == {
        key: packaged["task_match"][key]
        for key in ("capability_weight", "domain_weight", "tier_weight")
    }
    assert baseline["single_route_calibration"] == packaged["single_route_calibration"]
    assert baseline["candidate_pool"] == packaged["single_route_calibration"][
        "activation_model_identities"
    ]
