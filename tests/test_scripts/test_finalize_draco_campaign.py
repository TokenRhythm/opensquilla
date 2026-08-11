from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from opensquilla.eval.draco_artifact_integrity import (
    trace_row_from_result as canonical_trace_row_from_result,
)
from opensquilla.eval.draco_artifact_io import DurableDracoArtifactWriter
from opensquilla.eval.draco_experiment_config import load_draco_experiment_config
from opensquilla.eval.draco_selection_plan_evidence import (
    SelectionPlanPackAppender,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "experiments" / "finalize_draco_campaign.py"
TEST_G1_RUNTIME_REGISTRY_HASH = "d" * 64


def _frozen_registry_all_routes(module) -> dict[str, str]:
    return {
        f"test-vendor/model-{index:02d}": "auto"
        for index in range(module.FORMAL_G1_REGISTRY_ALL_CANDIDATE_COUNT)
    }


def _registry_all_contract(module) -> dict[str, object]:
    routes = _frozen_registry_all_routes(module)
    return {
        "profile_id": "test-registry-all",
        "selection_mode": "router_dynamic",
        "candidate_scope": "registry_all",
        "policy": "all_registry_models",
        "user_profile_enabled": False,
        "source_registry_snapshot_version": (module.FORMAL_G1_FULL_REGISTRY_SNAPSHOT_VERSION),
        "expected_routes": routes,
        "expected_routes_sha256": module.canonical_sha256(routes),
        "expected_candidate_count": len(routes),
        "expected_source_registry_snapshot_sha256": (
            module.FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256
        ),
        "expected_ranking_config_schema_version": (module.FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION),
        "expected_ranking_config_version": module.FORMAL_G1_RANKING_CONFIG_VERSION,
        "expected_ranking_config_sha256": module.FORMAL_G1_RANKING_CONFIG_SHA256,
    }


def _load():
    spec = importlib.util.spec_from_file_location("finalize_draco_campaign_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module():
    return _load()


def test_formal_model_thinking_levels_match_campaign_config(module) -> None:
    config_path = ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert module.FORMAL_MODEL_THINKING_LEVELS == config["generation"]["model_thinking_levels"]
    assert module.FORMAL_MODEL_THINKING_LEVELS["moonshotai/kimi-k2.7-code"] == "high"
    assert module.FORMAL_MODEL_THINKING_LEVELS["qwen/qwen3.7-max"] == "high"
    resolved = load_draco_experiment_config(config_path).config.g1_routing
    assert resolved is not None
    if getattr(resolved, "candidate_scope", "exact_routes") == "registry_all":
        assert resolved.expected_routes is None
    else:
        assert resolved.expected_routes is not None
        assert set(resolved.expected_routes) <= set(module.FORMAL_MODEL_THINKING_LEVELS)


def test_finalizer_version_covers_canonical_usage_evidence(module) -> None:
    assert module.FINALIZER_VERSION == 9


def test_legacy_managed_v3_source_requires_exact_external_identity(
    module,
) -> None:
    identity = deepcopy(module.LEGACY_MANAGED_V3_SOURCE_IDENTITY)
    assert module.legacy_managed_v3_source_authenticated({"source_identity": identity})
    assert not module.legacy_managed_v3_source_authenticated(
        {"source_identity": {**identity, "unexpected": True}}
    )
    assert not module.legacy_managed_v3_source_authenticated(
        {
            "source_identity": {
                **identity,
                "git_head": "0" * 40,
            }
        }
    )
    assert not module.legacy_managed_v3_source_authenticated(
        {
            "source_identity": {
                **identity,
                "source_tree_sha256": "0" * 64,
            }
        }
    )


def test_g1_thinking_physical_usage_requires_exact_id_multiset(
    module,
) -> None:
    proposer_id = "a" * 32
    aggregator_id = "b" * 32
    call = {
        "selection_plan": {
            "thinking_physical_evidence_schema": (module.THINKING_PHYSICAL_EVIDENCE_SCHEMA)
        },
        "candidates": [
            {
                "request_started": True,
                "execution": {
                    "physical_attempts": [
                        {
                            "request_started": True,
                            "physical_attempt_id": proposer_id,
                        }
                    ]
                },
            }
        ],
        "aggregator_recovery": {
            "attempts": [
                {
                    "request_started": True,
                    "physical_attempt_id": aggregator_id,
                }
            ]
        },
    }

    def usage_unit(
        attempt_id: str,
        *,
        role: str,
    ) -> dict[str, object]:
        return {
            "role": role,
            "provider": "openrouter",
            "requested_provider": "openrouter",
            "model": "test/model",
            "requested_model": "test/model",
            "request_count": 1,
            "physical_attempt_id": attempt_id,
            "input_tokens": 1,
            "output_tokens": 1,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "billed_cost": 0.0,
            "cost_source": "none",
            "provider_usage": {
                "physical_attempt_id": attempt_id,
            },
        }

    run = {
        "llm_request_count": 2,
        "ensemble_trace": call,
        "usage": {
            "model_usage_breakdown": [
                usage_unit(proposer_id, role="proposer"),
                usage_unit(aggregator_id, role="aggregator"),
            ]
        },
    }

    assert module.g1_thinking_physical_usage_binding_reasons(run) == []

    duplicate_usage = deepcopy(run)
    duplicate_usage["usage"]["model_usage_breakdown"][1]["physical_attempt_id"] = proposer_id
    duplicate_usage["usage"]["model_usage_breakdown"][1]["provider_usage"][
        "physical_attempt_id"
    ] = proposer_id
    assert (
        "g1_thinking_physical_usage_set_mismatch"
        in module.g1_thinking_physical_usage_binding_reasons(duplicate_usage)
    )

    duplicate_ledger = deepcopy(run)
    duplicate_ledger["ensemble_trace"]["aggregator_recovery"]["attempts"][0][
        "physical_attempt_id"
    ] = proposer_id
    assert (
        "invalid_g1_thinking_physical_attempt_set"
        in module.g1_thinking_physical_usage_binding_reasons(duplicate_ledger)
    )

    cross_role_collision = deepcopy(run)
    cross_role_collision["setup_usage"] = [usage_unit(proposer_id, role="task_analyzer")]
    generation_ids = module._strict_g1_physical_generation_usage_ids(
        cross_role_collision,
        identity_seed="cross-role-collision",
    )
    assert proposer_id in generation_ids
    owners: dict[
        str,
        tuple[tuple[str, str], str, str],
    ] = {}
    assert (
        module.register_physical_attempt_owners(
            [proposer_id],
            owner=(("G1", "task-1"), "1" * 32, "task_analyzer"),
            owners=owners,
        )
        == []
    )
    assert module.register_physical_attempt_owners(
        generation_ids,
        owner=(("G1", "task-1"), "1" * 32, "managed_generation"),
        owners=owners,
    ) == ["physical_attempt_id_reused_across_generation_attempts"]


def test_physical_attempt_ids_are_unique_across_request_owners(
    module,
) -> None:
    attempt_id = "c" * 32
    owners: dict[
        str,
        tuple[tuple[str, str], str, str],
    ] = {}

    assert (
        module.register_physical_attempt_owners(
            [attempt_id],
            owner=(("G1", "task-1"), "1" * 32, "task_analyzer"),
            owners=owners,
        )
        == []
    )
    assert (
        module.register_physical_attempt_owners(
            [attempt_id],
            owner=(("G1", "task-1"), "1" * 32, "task_analyzer"),
            owners=owners,
        )
        == []
    )
    assert module.register_physical_attempt_owners(
        [attempt_id],
        owner=(("G1", "task-1"), "1" * 32, "managed_generation"),
        owners=owners,
    ) == ["physical_attempt_id_reused_across_generation_attempts"]
    assert module.register_physical_attempt_owners(
        [attempt_id],
        owner=(("G1", "task-2"), "2" * 32, "task_analyzer"),
        owners=owners,
    ) == ["physical_attempt_id_reused_across_generation_attempts"]


def test_provider_native_physical_ids_are_unique_without_thinking_schema(
    module,
) -> None:
    shared_physical_attempt_id = "d" * 32
    contract = _contract(module, "G1", "a" * 64)
    records = []
    for source_index in (1, 2):
        task_id = f"task-{source_index}"
        task = {
            "id": task_id,
            "prompt": "research this",
            "rubric": {
                "id": "rubric-1",
                "sections": [
                    {
                        "id": "quality",
                        "title": "Quality",
                        "criteria": [
                            {
                                "id": "core",
                                "weight": 100,
                                "requirement": "core",
                            }
                        ],
                    }
                ],
            },
        }
        row = _row(
            module,
            group="G1",
            task=task,
            fingerprint="fingerprint",
            response_prefix=f"physical-owner-{source_index}",
        )
        attempt = row["execution"]["generation_attempts"][0]
        run = attempt["run"]
        run["ensemble_trace"] = deepcopy(row["ensemble_trace"])
        call = run["ensemble_trace"]["calls"][0]
        plan = call["selection_plan"]
        plan.pop("thinking_physical_evidence_schema", None)
        assert plan.get("proposer_recovery_policy") is not None
        physical = call["candidates"][0]["execution"]["physical_attempts"][0]
        prior_physical_attempt_id = physical["physical_attempt_id"]
        physical["physical_attempt_id"] = shared_physical_attempt_id
        usage_units = run["usage"]["model_usage_breakdown"]
        matching_units = [
            unit
            for unit in usage_units
            if unit.get("physical_attempt_id") == prior_physical_attempt_id
        ]
        assert len(matching_units) == 1
        usage_unit = matching_units[0]
        usage_unit["physical_attempt_id"] = shared_physical_attempt_id
        usage_unit["provider_usage"]["physical_attempt_id"] = shared_physical_attempt_id
        assert module.g1_thinking_physical_usage_binding_reasons(run) == []
        records.append(
            module.SourceRecord(
                path=Path(f"wave-{source_index}.jsonl"),
                source_index=source_index,
                line=1,
                row=row,
            )
        )

    with pytest.raises(
        module.FinalizationError,
        match="physical_attempt_id_reused_across_generation_attempts",
    ):
        module.validate_physical_generation_routes(
            records,
            contracts={"G1": contract},
        )


def test_text_sha256_matches_runner_wire_format(module) -> None:
    assert module.text_sha256("value") == hashlib.sha256(b"value").hexdigest()


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_load_jsonl_rows_preserves_unicode_line_separators(
    module,
    tmp_path: Path,
    separator: str,
) -> None:
    path = tmp_path / "rows.jsonl"
    value = {"content": f"before{separator}after"}
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    assert module.load_jsonl_rows(
        path,
        owner_only=True,
        source_label="test JSONL",
    ) == [(1, value)]


def test_run_expected_request_count_does_not_double_count_placeholder(module) -> None:
    run = {
        "llm_request_count": 2,
        "usage": {
            "usage_missing_count": 1,
            "model_usage_breakdown": [
                {"role": "proposer"},
                {"role": "agent_llm_request_unknown"},
            ],
        },
    }

    assert module.run_expected_request_count(run) == 2


@pytest.mark.parametrize("ordinal", range(5))
def test_counter_only_failed_judge_request_is_canonical_and_route_bound(
    module,
    tmp_path: Path,
    ordinal: int,
) -> None:
    """The five observed 503 shapes each become one strict unknown receipt."""

    attempt_id = f"{0x5100 + ordinal:032x}"
    run = {
        "error": (
            "OpenRouter chat request failed (HTTP 503): Cloudflare Worker exceeded resource limits"
        ),
        "llm_request_count": 1,
        "usage_unknown_count": 1,
        "usage": {},
        "trace_events": [
            {
                "kind": "error",
                "code": "503",
                "request_started": None,
                "physical_request_count": None,
            }
        ],
    }
    wave1 = module.SourceRecord(
        path=tmp_path / "wave-1.jsonl",
        source_index=0,
        line=ordinal + 1,
        row={"group": "B1", "task_id": f"task-{ordinal}"},
    )
    wave2 = module.SourceRecord(
        path=tmp_path / "wave-2.jsonl",
        source_index=1,
        line=ordinal + 1,
        row={"group": "B1", "task_id": f"task-{ordinal}"},
    )

    _, canonical = module.validate_and_select_monotonic_run_version(
        [(wave1, run), (wave2, deepcopy(run))],
        label=f"Judge attempt {attempt_id}",
        identity_seed=f"judge-attempt:{attempt_id}",
        requested_provider="openrouter",
        requested_model=module.JUDGE_MODEL,
        role="unknown_request",
    )
    units = module.canonical_run_usage_units(
        canonical,
        identity_seed=f"judge-attempt:{attempt_id}",
    )

    assert module.run_expected_request_count(canonical) == 1
    assert len(units) == 1
    assert module._is_unknown_judge_placeholder(units[0]) is True
    assert units[0]["provider"] == ""
    assert units[0]["model"] == ""
    assert units[0]["requested_provider"] == "openrouter"
    assert units[0]["requested_model"] == module.JUDGE_MODEL
    ledger_entries = {}
    module._record_run(
        ledger_entries,
        {},
        run=canonical,
        scope="judge",
        base_identity=f"judge:B1:task-{ordinal}:judge:criterion/x/0/{attempt_id}",
        reference={"group": "B1", "task_id": f"task-{ordinal}"},
        occurrence_counter=module.Counter(),
    )
    assert len(ledger_entries) == 1
    assert (
        next(iter(ledger_entries.values())).units[0]["usage_evidence_id"]
        == units[0]["usage_evidence_id"]
    )
    assert (
        module.usage_route_reasons(
            canonical["usage"],
            allowed_models={module.JUDGE_MODEL},
            provider_pins={module.JUDGE_MODEL: module.FORMAL_UPSTREAM_PINS[module.JUDGE_MODEL]},
            allow_unknown_judge_attempts=True,
        )
        == []
    )
    assert (
        module.proof_only_usage_evidence_reasons(
            {
                "judge": {
                    "criterion_judgments": [
                        {
                            "id": "criterion",
                            "repeat_index": 0,
                            "judge_attempts": [
                                {
                                    "attempt_id": attempt_id,
                                    "attempt": 1,
                                    "run": deepcopy(run),
                                }
                            ],
                        }
                    ]
                }
            }
        )
        == []
    )


def test_unknown_judge_placeholder_cannot_claim_success_or_actual_identity(
    module,
    tmp_path: Path,
) -> None:
    attempt_id = "5" * 32
    record = module.SourceRecord(
        path=tmp_path / "wave.jsonl",
        source_index=0,
        line=1,
        row={"group": "B1", "task_id": "task"},
    )
    _, canonical = module.validate_and_select_monotonic_run_version(
        [
            (
                record,
                {
                    "error": "HTTP 503",
                    "llm_request_count": 1,
                    "usage_unknown_count": 1,
                    "usage": {},
                },
            )
        ],
        label=f"Judge attempt {attempt_id}",
        identity_seed=f"judge-attempt:{attempt_id}",
        requested_provider="openrouter",
        requested_model=module.JUDGE_MODEL,
        role="unknown_request",
    )
    tampered = deepcopy(canonical["usage"])
    tampered["model_usage_breakdown"][0]["provider"] = "openrouter"

    assert set(
        module.usage_route_reasons(
            tampered,
            allowed_models={module.JUDGE_MODEL},
            provider_pins={module.JUDGE_MODEL: module.FORMAL_UPSTREAM_PINS[module.JUDGE_MODEL]},
            allow_unknown_judge_attempts=True,
        )
    ) == {
        "invalid_unknown_judge_usage_placeholder",
        "missing_generation_usage_route_evidence",
    }
    tampered_run = deepcopy(canonical)
    tampered_run["usage"] = tampered
    proof_reasons = module.proof_only_usage_evidence_reasons(
        {
            "judge": {
                "criterion_judgments": [
                    {
                        "id": "criterion",
                        "repeat_index": 0,
                        "judge_attempts": [
                            {
                                "attempt_id": attempt_id,
                                "attempt": 1,
                                "run": tampered_run,
                            }
                        ],
                    }
                ]
            }
        }
    )
    assert any("invalid_unknown_judge_usage_placeholder" in reason for reason in proof_reasons)


def test_selected_endpoint_receipt_binds_serving_alias_and_provider(module) -> None:
    unit = {
        "role": "agent_llm_call",
        "provider": "openrouter",
        "model": module.B0_MODEL,
        "requested_provider": "openrouter",
        "requested_model": module.B0_MODEL,
        "billed_cost": 0.1,
        "provider_usage": {
            "is_byok": False,
            "provider_reported_cost": 0.1,
            "response_ids": ["response-1"],
            "router_metadata": {
                "requested": module.B0_MODEL,
                "is_byok": False,
                "endpoints": {
                    "available": [
                        {
                            "provider": "Amazon Bedrock",
                            "model": "anthropic/claude-5-fable-20260609",
                            "selected": True,
                        }
                    ]
                },
            },
        },
    }

    assert (
        module.usage_route_reasons(
            {
                "model_usage_breakdown": [
                    unit,
                    {"role": "agent_llm_request_unknown"},
                ]
            },
            allowed_models={module.B0_MODEL},
            provider_pins={module.B0_MODEL: module.FORMAL_UPSTREAM_PINS[module.B0_MODEL]},
        )
        == []
    )
    assert module.unit_exact_non_byok(unit) is True
    assert module._formal_openrouter_models_equivalent(
        module.B0_MODEL,
        "anthropic/claude-5-fable-20260609",
    )
    assert module._formal_openrouter_models_equivalent(
        "anthropic/claude-sonnet-5",
        "anthropic/claude-sonnet-5-20260630",
    )

    bad_placeholder = {
        "role": "agent_llm_request_unknown",
        "provider_usage": {
            "requested_provider": "direct",
            "requested_model": module.B4_MODEL,
            "router_metadata": {"requested": module.B4_MODEL},
        },
    }
    reasons = module.usage_route_reasons(
        {"model_usage_breakdown": [unit, bad_placeholder]},
        allowed_models={module.B0_MODEL},
        provider_pins={module.B0_MODEL: module.FORMAL_UPSTREAM_PINS[module.B0_MODEL]},
    )
    assert "wrong_generation_provider_route" in reasons
    assert "wrong_generation_model_route" in reasons

    unit["provider_usage"]["router_metadata"]["endpoints"]["available"].append(
        {
            "provider": "Azure",
            "model": module.B4_MODEL,
            "selected": True,
        }
    )
    reasons = module.usage_route_reasons(
        {"model_usage_breakdown": [unit]},
        allowed_models={module.B0_MODEL},
        provider_pins={module.B0_MODEL: module.FORMAL_UPSTREAM_PINS[module.B0_MODEL]},
    )
    assert "conflicting_successful_router_receipt" in reasons
    assert module.unit_exact_non_byok(unit) is False


@pytest.mark.parametrize(
    ("requested_model", "serving_model", "upstream_provider"),
    [
        (
            "x-ai/grok-4.5",
            "x-ai/grok-4.5-20260708",
            "xai",
        ),
        (
            "anthropic/claude-sonnet-5",
            "anthropic/claude-sonnet-5-20260630",
            "anthropic",
        ),
    ],
)
def test_frozen_g1_serving_aliases_bind_router_receipts(
    module,
    requested_model: str,
    serving_model: str,
    upstream_provider: str,
) -> None:
    unit = {
        "role": "proposer",
        "provider": "openrouter",
        "model": requested_model,
        "requested_provider": "openrouter",
        "requested_model": requested_model,
        "provider_usage": {
            "router_metadata": {
                "requested": requested_model,
                "attempts": [
                    {
                        "provider": upstream_provider,
                        "model": serving_model,
                        "status": 200,
                    }
                ],
            },
        },
    }

    assert (
        module.usage_route_reasons(
            {"model_usage_breakdown": [unit]},
            allowed_models={requested_model},
            provider_pins={requested_model: upstream_provider},
        )
        == []
    )
    assert module._formal_openrouter_models_equivalent(
        requested_model,
        serving_model,
    )

    outside_snapshot = deepcopy(unit)
    outside_model = f"{serving_model}-outside-snapshot"
    outside_snapshot["provider_usage"]["router_metadata"]["attempts"][0]["model"] = outside_model
    assert not module._formal_openrouter_models_equivalent(
        requested_model,
        outside_model,
    )
    assert "router_receipt_model_not_bound_to_formal_route" in (
        module.usage_route_reasons(
            {"model_usage_breakdown": [outside_snapshot]},
            allowed_models={requested_model},
            provider_pins={requested_model: upstream_provider},
        )
    )


def test_registry_all_contract_authenticates_its_frozen_route_projection(module) -> None:
    contract = _registry_all_contract(module)
    routes = module.authenticated_registry_all_routes(contract)

    assert routes is not None
    assert len(routes) == module.FORMAL_G1_REGISTRY_ALL_CANDIDATE_COUNT == 79
    assert set(routes.values()) == {"auto"}
    assert all(model == model.strip().lower() and "/" in model for model in routes)


def test_registry_all_contract_never_loads_the_mutable_packaged_profile(
    module,
    monkeypatch,
) -> None:
    import opensquilla.provider.ranking_router as ranking_router

    monkeypatch.setattr(
        ranking_router,
        "load_model_registry_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("mutable profile was loaded")),
    )

    assert module.authenticated_registry_all_routes(_registry_all_contract(module)) is not None
    module._formal_openrouter_model_aliases.cache_clear()
    assert module._formal_openrouter_models_equivalent(
        "anthropic/claude-sonnet-5",
        "anthropic/claude-sonnet-5-20260630",
    )


@pytest.mark.parametrize(
    "mutation",
    ["count", "routes_hash", "source_hash", "provider", "model_identity"],
)
def test_registry_all_contract_rejects_frozen_route_tampering(module, mutation: str) -> None:
    contract = _registry_all_contract(module)
    if mutation == "count":
        contract["expected_candidate_count"] = 78
    elif mutation == "routes_hash":
        contract["expected_routes_sha256"] = "0" * 64
    elif mutation == "source_hash":
        contract["expected_source_registry_snapshot_sha256"] = "0" * 64
    elif mutation == "provider":
        contract["expected_routes"]["test-vendor/model-00"] = "some-provider"
        contract["expected_routes_sha256"] = module.canonical_sha256(contract["expected_routes"])
    else:
        contract["expected_routes"]["TEST-VENDOR/model-00"] = contract["expected_routes"].pop(
            "test-vendor/model-00"
        )
        contract["expected_routes_sha256"] = module.canonical_sha256(contract["expected_routes"])

    assert module.authenticated_registry_all_routes(contract) is None


def test_auto_candidate_provider_accepts_any_successful_upstream_but_role_pin_remains(
    module,
) -> None:
    requested_model = "x-ai/grok-4.5"
    unit = {
        "role": "proposer",
        "provider": "openrouter",
        "model": requested_model,
        "requested_provider": "openrouter",
        "requested_model": requested_model,
        "provider_usage": {
            "requested_provider": "openrouter",
            "requested_model": requested_model,
            "router_metadata": {
                "requested_provider": "openrouter",
                "requested": requested_model,
                "attempts": [
                    {
                        "provider": "some-operational-upstream",
                        "model": requested_model,
                        "status": 200,
                    }
                ],
            },
        },
    }

    assert (
        module.usage_route_reasons(
            {"model_usage_breakdown": [unit]},
            allowed_models={requested_model},
            provider_pins={requested_model: "auto"},
        )
        == []
    )

    analyzer = deepcopy(unit)
    analyzer.update(
        {
            "role": "task_analyzer",
            "model": module.TASK_ANALYZER_MODEL,
            "requested_model": module.TASK_ANALYZER_MODEL,
        }
    )
    analyzer["provider_usage"]["requested_model"] = module.TASK_ANALYZER_MODEL
    analyzer["provider_usage"]["router_metadata"]["requested"] = module.TASK_ANALYZER_MODEL
    analyzer["provider_usage"]["router_metadata"]["attempts"][0]["model"] = (
        module.TASK_ANALYZER_MODEL
    )
    reasons = module.usage_route_reasons(
        {"model_usage_breakdown": [analyzer]},
        allowed_models={requested_model},
        provider_pins={module.TASK_ANALYZER_MODEL: "auto"},
        role_model_pins={"task_analyzer": module.TASK_ANALYZER_MODEL},
        role_provider_pins={
            "task_analyzer": module.FORMAL_UPSTREAM_PINS[module.TASK_ANALYZER_MODEL],
        },
    )
    assert "router_receipt_provider_not_bound_to_formal_route" in reasons


def test_registry_all_auto_preserves_concrete_runtime_provider_pins(module) -> None:
    pinned_model = "x-ai/grok-4.5"
    unpinned_model = "meta-llama/llama-4-scout"
    contract = {
        "resolved_llm_runtime": {
            "provider_routing": {
                pinned_model.upper(): "xAI",
            }
        },
        "g1_registry_contract": {
            "candidate_scope": "registry_all",
            "expected_routes": {
                pinned_model: "auto",
                unpinned_model: "auto",
            },
        },
    }

    assert module.contract_provider_pins(contract) == {
        pinned_model: "xai",
        unpinned_model: "auto",
    }

    unit = {
        "role": "proposer",
        "provider": "openrouter",
        "model": pinned_model,
        "requested_provider": "openrouter",
        "requested_model": pinned_model,
        "provider_usage": {
            "requested_provider": "openrouter",
            "requested_model": pinned_model,
            "router_metadata": {
                "requested_provider": "openrouter",
                "requested": pinned_model,
                "attempts": [
                    {
                        "provider": "different-upstream",
                        "model": pinned_model,
                        "status": 200,
                    }
                ],
            },
        },
    }
    reasons = module.usage_route_reasons(
        {"model_usage_breakdown": [unit]},
        allowed_models={pinned_model},
        provider_pins=module.contract_provider_pins(contract),
    )
    assert "router_receipt_provider_not_bound_to_formal_route" in reasons


def test_registry_all_plan_requires_all_registry_models_policy_and_pool(module) -> None:
    routes = _frozen_registry_all_routes(module)
    routes_hash = module.canonical_sha256(routes)
    profile_id = "test-registry-all"
    source_version = module.FORMAL_G1_FULL_REGISTRY_SNAPSHOT_VERSION
    filtered_version = f"{source_version}+{profile_id}+{routes_hash[:12]}"
    identities = sorted(f"openrouter:{model}" for model in routes)
    contract = {
        "profile_id": profile_id,
        "selection_mode": "router_dynamic",
        "candidate_scope": "registry_all",
        "policy": "all_registry_models",
        "user_profile_enabled": False,
        "source_registry_snapshot_version": source_version,
        "expected_routes": routes,
        "expected_routes_sha256": routes_hash,
        "expected_candidate_count": len(routes),
        "expected_source_registry_snapshot_sha256": (
            module.FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256
        ),
        "expected_ranking_config_schema_version": (module.FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION),
        "expected_ranking_config_version": module.FORMAL_G1_RANKING_CONFIG_VERSION,
        "expected_ranking_config_sha256": module.FORMAL_G1_RANKING_CONFIG_SHA256,
        "expected_proposer_count_max": module.FORMAL_G1_PROPOSER_COUNT_MAX,
        "task_analyzer": _default_task_analyzer_policy(),
    }
    plan = {
        "candidate_allowlist": {
            "policy": "all_registry_models",
            "profile_id": profile_id,
            "source_registry_snapshot_version": source_version,
            "expected_source_registry_snapshot_sha256": (
                module.FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256
            ),
            "filtered_registry_snapshot_version": filtered_version,
            "expected_routes_sha256": routes_hash,
            "expected_candidate_count": len(routes),
            "candidate_count": len(routes),
            "expected_identities": identities,
        },
        "candidate_pool_size": len(routes),
        "candidate_pool": [{"identity": identity} for identity in identities],
        "registry_snapshot_version": filtered_version,
        "registry_snapshot_hash": "d" * 64,
    }

    reasons, _, _ = module.g1_registry_plan_reasons(plan, contract=contract)
    assert "invalid_g1_registry_contract" not in reasons
    assert "wrong_g1_candidate_pool" not in reasons
    assert not any(reason.startswith("wrong_g1_candidate_allowlist") for reason in reasons)

    plan["candidate_allowlist"]["policy"] = "exact_openrouter_routes"
    reasons, _, _ = module.g1_registry_plan_reasons(plan, contract=contract)
    assert "wrong_g1_candidate_allowlist_policy" in reasons


def _owner_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def _assert_account_audit_conflict(
    module,
    args: argparse.Namespace,
    *,
    warning: str,
) -> dict[str, object]:
    manifest = module.run_finalization(args)
    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    audit = json.loads((args.output_dir / "audit.json").read_text())

    assert manifest["status"] == "complete"
    assert manifest["execution_pass"] is True
    assert manifest["policy_pass"] is False
    assert manifest["reconciliation"] == {
        "pass": False,
        "status": "audit_conflict",
        "gap_usd": None,
        "tolerance_usd": None,
    }
    assert proof["pass"] is False
    assert proof["execution_pass"] is True
    assert proof["policy_pass"] is False
    assert proof["status"] == "audit_conflict"
    assert proof["publication_eligible"] is True
    assert proof["audit_conflict_kind"] == "account_proof_incomplete"
    assert warning in "\n".join(proof["warnings"])
    assert audit["pass"] is False
    assert audit["execution_pass"] is True
    assert audit["policy_pass"] is False
    assert audit["status"] == "complete_with_warnings"
    assert warning in "\n".join(audit["warnings"])
    return manifest


def test_account_proof_allowlist_is_closed(module) -> None:
    assert (
        module.account_proof_error_is_audit_only(
            module.FinalizationError("account reconciliation is not stable")
        )
        is True
    )
    for message in (
        "runtime/account evidence uses different API keys",
        "reconciliation runtime file hash differs",
        "prior account windows overlap",
        "account counters decreased",
        "stable observation usage is not monotonic",
        "campaign account counters are not continuous between windows",
    ):
        assert module.account_proof_error_is_audit_only(module.FinalizationError(message)) is False


def _receipt(
    response_id: str,
    model: str,
    *,
    cost: float = 0.1,
    exact: bool = True,
    is_byok: bool = False,
) -> dict[str, object]:
    upstream_provider = (
        "amazonbedrock"
        if model == "anthropic/claude-fable-5"
        else "azure"
        if model == "openai/gpt-5.6-sol"
        else "deepseek"
        if model.startswith("deepseek/")
        else "z-ai"
        if model.startswith("z-ai/")
        else "alibaba"
        if model.startswith("qwen/")
        else "moonshotai"
        if model.startswith("moonshotai/")
        else "anthropic"
        if model.startswith("anthropic/")
        else "openai"
        if model.startswith("openai/")
        else "google-ai-studio"
        if model.startswith("google/")
        else "upstream"
    )
    provider_usage: dict[str, object] = {
        "response_ids": [response_id],
        "provider_reported_cost": cost,
        "router_metadata": {
            "requested": model,
            "attempts": [
                {
                    "provider": upstream_provider,
                    "model": model,
                    "status": 200,
                }
            ],
        },
    }
    if exact:
        provider_usage.update(
            {
                "is_byok": is_byok,
            }
        )
        provider_usage["router_metadata"]["is_byok"] = is_byok
    return {
        "provider": "openrouter",
        "model": model,
        "requested_provider": "openrouter",
        "requested_model": model,
        "input_tokens": 10,
        "output_tokens": 5,
        "billed_cost": cost,
        "cost_source": "provider_billed",
        "provider_usage": provider_usage,
    }


def _physical_attempt_id(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _ensemble_trace(
    proposers: list[str],
    aggregator: str,
    *,
    final_text: str,
    selection_mode: str,
    successful: int | None = None,
) -> dict[str, object]:
    def output_binding(text: str) -> dict[str, object]:
        return {
            "text": text,
            "chars": len(text),
            "truncated": False,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }

    candidates = []
    for model in proposers:
        text = f"candidate from {model}"
        candidates.append(
            {
                "ok": True,
                "request_started": True,
                "physical_request_count": 1,
                "error": "",
                "completion_outcome": "complete",
                "stop_reason": "stop",
                "provider": "openrouter",
                "model": model,
                "requested_provider": "openrouter",
                "requested_model": model,
                "content": {"text": text, "chars": len(text), "truncated": False},
            }
        )
    aggregator_identity = f"openrouter:{aggregator}"
    physical = {
        "agent_call_index": 1,
        "output_binding_schema": "opensquilla.ensemble-output-binding/v1",
        "assembled_output": output_binding(final_text),
        "output_components": [
            {
                "attempt": 1,
                "kind": "primary",
                "fallback_index": 0,
                "requested_provider": "openrouter",
                "requested_model": aggregator,
                "assembled_start": 0,
                "assembled_end": len(final_text),
                "physical_output": output_binding(final_text),
                "assembled_contribution": output_binding(final_text),
                "assembled_prefix_sha256": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
            }
        ],
        "request_outcome": "llm_response",
        "total_candidates": len(proposers),
        "successful_proposers": (len(proposers) if successful is None else successful),
        "fallback_used": False,
        "fallback_reason": "",
        "executed_A": aggregator_identity,
        "run_outcome": "success",
        "delivery_outcome": "complete",
        "final_request_role": "aggregator",
        "selection_plan": {
            "strategy": selection_mode,
            "selection_mode": selection_mode,
            "proposer_models": proposers,
            "aggregator_model": aggregator,
            "aggregator_candidates": [aggregator_identity],
        },
        "candidates": candidates,
        "aggregator_recovery": {
            "schema": "opensquilla.ensemble-aggregator-recovery/v1",
            "mode": "experiment",
            "candidate_count": 1,
            "candidate_ids": [aggregator_identity],
            "max_tokens_cap": 65_536,
            "visible_answer_reserve_tokens": 8_192,
            "attempts": [
                {
                    "attempt": 1,
                    "physical_attempt_index": 1,
                    "physical_request_count": 1,
                    "kind": "primary",
                    "fallback_index": 0,
                    "trigger": "",
                    "request_started": True,
                    "visible_output_emitted": True,
                    "stream_closed": True,
                    "outcome": "succeeded",
                    "stop_reason": "stop",
                    "requested_provider": "openrouter",
                    "requested_model": aggregator,
                    "actual_provider": "openrouter",
                    "actual_model": aggregator,
                }
            ],
            "proposer_reused": True,
            "success": True,
            "degraded": False,
            "selected_attempt": 1,
            "selected_kind": "primary",
            "fallback_index": 0,
            "fallback_reason": "",
            "executed_A": aggregator_identity,
            "continuation_count": 0,
            "same_model_recovery_count": 0,
        },
        "final_request": {
            "role": "aggregator",
            "request_started": True,
            "error": "",
            "execution": {
                "actual_provider": "openrouter",
                "actual_model": aggregator,
                "requested_provider": "openrouter",
                "requested_model": aggregator,
            },
            "usage": {
                "provider": "openrouter",
                "model": aggregator,
                "requested_provider": "openrouter",
                "requested_model": aggregator,
            },
            "output": {
                "text": final_text,
                "chars": len(final_text),
                "truncated": False,
                "sha256": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
            },
        },
        "llm_request_count": len(proposers) + 1,
        "physical_request_count": len(proposers) + 1,
    }
    return {
        "mode": "agent_loop",
        "agent_llm_call_count": 1,
        "untraced_agent_llm_call_count": 0,
        "calls": [physical],
    }


def _nonterminal_fallback_call(
    terminal_call: dict[str, object],
    *,
    successful: int,
    output: str = "",
) -> dict[str, object]:
    call = deepcopy(terminal_call)
    call["agent_call_index"] = 1
    call["successful_proposers"] = successful
    call["fallback_used"] = True
    call["final_request_role"] = "fallback_single"
    call.pop("aggregator_recovery", None)
    call.pop("executed_A", None)
    call.pop("run_outcome", None)
    call.pop("delivery_outcome", None)
    candidates = call["candidates"]
    assert isinstance(candidates, list)
    for index, candidate in enumerate(candidates):
        assert isinstance(candidate, dict)
        if index < successful:
            continue
        candidate.update(
            {
                "ok": False,
                "error": "test proposer failure",
                "content": {"text": "", "chars": 0, "truncated": False},
            }
        )
    plan = call["selection_plan"]
    assert isinstance(plan, dict)
    fallback_model = str(plan["proposer_models"][0])
    final_request = call["final_request"]
    assert isinstance(final_request, dict)
    final_request.update(
        {
            "role": "fallback_single",
            "execution": {
                "role": "fallback_single",
                "provider": "openrouter",
                "actual_provider": "openrouter",
                "model": fallback_model,
                "actual_model": fallback_model,
                "requested_provider": "openrouter",
                "requested_model": fallback_model,
            },
            "usage": {
                "provider": "openrouter",
                "model": fallback_model,
                "requested_provider": "openrouter",
                "requested_model": fallback_model,
                "stop_reason": "stop",
            },
            "output": {
                "text": output,
                "chars": len(output),
                "truncated": False,
                "sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            },
        }
    )
    call["assembled_output"] = deepcopy(final_request["output"])
    call["output_components"] = []
    return call


def _ranked_aggregator_fallback_call(
    module,
    plan: dict[str, object],
    *,
    final_text: str,
    fallback_index: int,
    trigger: str = "reasoning_only_length",
) -> dict[str, object]:
    proposers = [str(value) for value in plan["proposer_models"]]
    primary_model = str(plan["aggregator_model"])
    call = _ensemble_trace(
        proposers,
        primary_model,
        final_text=final_text,
        selection_mode="router_dynamic",
    )["calls"][0]
    assert isinstance(call, dict)
    call["selection_plan"] = deepcopy(plan)
    chain = [str(value) for value in plan["aggregator_candidates"]]
    selected_identity = chain[fallback_index]
    selected_provider, _, selected_model = selected_identity.partition(":")
    primary_provider, _, primary_model = chain[0].partition(":")
    attempts = [
        {
            "attempt": 1,
            "kind": "primary",
            "fallback_index": 0,
            "trigger": trigger,
            "request_started": True,
            "physical_attempt_index": 1,
            "physical_request_count": 1,
            "visible_output_emitted": False,
            "stream_closed": True,
            "outcome": "abandoned",
            "requested_provider": primary_provider,
            "requested_model": primary_model,
        }
    ]
    if fallback_index == 2:
        skipped_provider, _, skipped_model = chain[1].partition(":")
        attempts.append(
            {
                "attempt": 2,
                "kind": "model_fallback",
                "fallback_index": 1,
                "trigger": trigger,
                "request_started": False,
                "physical_attempt_index": None,
                "physical_request_count": 0,
                "outcome": "member_unavailable",
                "requested_provider": skipped_provider,
                "requested_model": skipped_model,
            }
        )
    selected_attempt = len(attempts) + 1
    attempts.append(
        {
            "attempt": selected_attempt,
            "kind": "model_fallback",
            "fallback_index": fallback_index,
            "trigger": trigger,
            "request_started": True,
            "physical_attempt_index": 2,
            "physical_request_count": 1,
            "visible_output_emitted": True,
            "stream_closed": True,
            "outcome": "succeeded",
            "requested_provider": selected_provider,
            "requested_model": selected_model,
            "actual_provider": selected_provider,
            "actual_model": selected_model,
        }
    )
    call.update(
        {
            "fallback_used": True,
            "fallback_reason": trigger,
            "executed_A": selected_identity,
            "run_outcome": "aggregator_recovered",
            "delivery_outcome": "complete",
            "aggregator_recovery": {
                "schema": "opensquilla.ensemble-aggregator-recovery/v1",
                "mode": "experiment",
                "candidate_count": len(chain),
                "candidate_ids": chain,
                "max_tokens_cap": 65_536,
                "visible_answer_reserve_tokens": 8_192,
                "attempts": attempts,
                "proposer_reused": True,
                "success": True,
                "degraded": False,
                "selected_attempt": selected_attempt,
                "selected_kind": "model_fallback",
                "fallback_index": fallback_index,
                "fallback_reason": trigger,
                "executed_A": selected_identity,
                "continuation_count": 0,
                "same_model_recovery_count": 0,
            },
            "llm_request_count": len(proposers) + 2,
            "physical_request_count": len(proposers) + 2,
        }
    )
    final_request = call["final_request"]
    assert isinstance(final_request, dict)
    final_request.update(
        {
            "role": "aggregator",
            "execution": {
                "provider": selected_provider,
                "model": selected_model,
                "actual_provider": selected_provider,
                "actual_model": selected_model,
                "requested_provider": selected_provider,
                "requested_model": selected_model,
            },
            "usage": {
                "provider": selected_provider,
                "model": selected_model,
                "requested_provider": selected_provider,
                "requested_model": selected_model,
            },
        }
    )
    output = final_request["output"]
    assert isinstance(output, dict)
    call["output_components"] = [
        {
            "attempt": selected_attempt,
            "kind": "model_fallback",
            "fallback_index": fallback_index,
            "requested_provider": selected_provider,
            "requested_model": selected_model,
            "assembled_start": 0,
            "assembled_end": len(final_text),
            "physical_output": deepcopy(output),
            "assembled_contribution": deepcopy(call["assembled_output"]),
            "assembled_prefix_sha256": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
        }
    ]
    if plan.get("proposer_recovery_policy") is not None:
        _formalize_provider_native_call(
            module,
            call,
            plan,
            scope_id=(f"ranked-fallback-{fallback_index}-{module.text_sha256(final_text)[:16]}"),
        )
    return call


def _provider_native_recovery_call(
    module,
) -> tuple[dict[str, object], dict[str, object]]:
    selected = [
        "openrouter:model-a",
        "openrouter:model-b",
        "openrouter:model-c",
    ]
    backups = ["openrouter:model-d", "openrouter:model-e"]
    plan: dict[str, object] = {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "selected_P": selected,
        "proposer_models": ["model-a", "model-b", "model-c"],
        "proposer_sample_count": 3,
        "effective_min_successful_proposers": 2,
        "backup_P": backups,
        "configured_proposer_backup_count": 2,
        "effective_proposer_backup_count": 2,
        "selected_A": "openrouter:model-f",
        "aggregator_candidates": [
            "openrouter:model-f",
            "openrouter:model-g",
            "openrouter:model-h",
        ],
        "proposer_recovery_policy": deepcopy(module.FORMAL_PROPOSER_RECOVERY_POLICY),
    }
    from opensquilla.provider.protocol import (
        provider_retry_roster_fingerprint,
    )

    fingerprint = provider_retry_roster_fingerprint(plan)
    primary_a, recovered_a, primary_b, primary_c = (
        "a" * 32,
        "b" * 32,
        "c" * 32,
        "d" * 32,
    )

    def physical(
        ordinal: int,
        attempt_id: str,
        identity: str,
        outcome: str,
    ) -> dict[str, object]:
        return {
            "attempt": ordinal,
            "request_started": True,
            "stream_closed": True,
            "physical_attempt_id": attempt_id,
            "identity": identity,
            "outcome": outcome,
        }

    def candidate(
        *,
        index: int,
        model: str,
        ok: bool,
        attempts: list[dict[str, object]],
    ) -> dict[str, object]:
        text = f"candidate-{index}" if ok else ""
        return {
            "index": index,
            "ok": ok,
            "usable_for_aggregation": ok,
            "completion_outcome": "complete" if ok else "failed",
            "request_started": bool(attempts),
            "physical_request_count": len(attempts),
            "usage_reported": True,
            "usage_missing_count": 0,
            "requested_provider": "openrouter",
            "requested_model": model,
            "provider": "openrouter" if ok else "",
            "model": model if ok else "",
            "content": {
                "text": text,
                "chars": len(text),
                "truncated": False,
            },
            "execution": {"physical_attempts": attempts},
        }

    call: dict[str, object] = {
        "selection_plan": plan,
        "successful_proposers": 2,
        "usable_proposers": 2,
        "partial_proposers": 0,
        "strict_quorum_met": True,
        "execution_quorum_required": 2,
        "execution_quorum_met": True,
        "candidates": [
            candidate(
                index=0,
                model="model-a",
                ok=True,
                attempts=[
                    physical(
                        1,
                        primary_a,
                        "openrouter:model-a",
                        "failed",
                    ),
                    physical(
                        2,
                        recovered_a,
                        "openrouter:model-a",
                        "succeeded",
                    ),
                ],
            ),
            candidate(
                index=1,
                model="model-b",
                ok=True,
                attempts=[
                    physical(
                        1,
                        primary_b,
                        "openrouter:model-b",
                        "succeeded",
                    )
                ],
            ),
            candidate(
                index=2,
                model="model-c",
                ok=False,
                attempts=[
                    physical(
                        1,
                        primary_c,
                        "openrouter:model-c",
                        "failed",
                    )
                ],
            ),
        ],
        "proposer_recovery": {
            "schema": module.FORMAL_PROPOSER_RECOVERY_SCHEMA,
            "selection_plan_fingerprint": fingerprint,
            "scope": "run_turn",
            "scope_id": "scope-1",
            "max_additional_physical_requests": 3,
            "external_physical_requests_reserved": 0,
            "additional_physical_requests_started": 1,
            "remaining_additional_physical_requests": 2,
            "quorum_required": 2,
            "strict_successful_proposers": 2,
            "usable_proposers": 2,
            "quorum_reached": True,
            "cumulative_excluded_identities": [],
            "visited_identities": [],
            "executed_proposer_roster_before": list(selected),
            "executed_proposer_roster_after": list(selected),
            "attempts": [
                {
                    "sequence": 1,
                    "slot_index": 0,
                    "kind": "thinking_downgrade",
                    "source_identity": selected[0],
                    "target_identity": selected[0],
                    "failure_kind": "reasoning_only_length",
                    "reason": "reasoning_only_length",
                    "thinking_before": "high",
                    "thinking_after": "medium",
                    "request_started": True,
                    "physical_request_count": 1,
                    "physical_attempt_id": recovered_a,
                    "stream_closed": True,
                    "usage_reported": True,
                    "usage_missing_count": 0,
                    "outcome": "succeeded",
                }
            ],
        },
    }
    return call, plan


def test_provider_native_proposer_recovery_binds_receipt_and_fingerprint(
    module,
) -> None:
    call, plan = _provider_native_recovery_call(module)

    final_identities, recovery_ids, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert reasons == []
    assert final_identities == {
        0: "openrouter:model-a",
        1: "openrouter:model-b",
        2: "openrouter:model-c",
    }
    assert recovery_ids == {"b" * 32}


def test_provider_native_recovery_accepts_per_dispatch_attempt_ordinals(module) -> None:
    call, plan = _provider_native_recovery_call(module)
    recovery_physical = call["candidates"][0]["execution"]["physical_attempts"][1]
    recovery_physical["attempt"] = 1

    final_identities, recovery_ids, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert reasons == []
    assert final_identities[0] == "openrouter:model-a"
    assert recovery_ids == {"b" * 32}

    recovery_physical["attempt"] = 3
    _, _, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )
    assert "invalid_proposer_recovery_physical_attempt" in reasons


def _provider_native_cumulative_recovery_call(
    module,
) -> tuple[dict[str, object], dict[str, object]]:
    call, plan = _provider_native_recovery_call(module)
    receipt = call["proposer_recovery"]
    current_receipt = receipt["attempts"][0]
    prior_receipt = deepcopy(current_receipt)
    prior_receipt.update(
        {
            "sequence": 1,
            "physical_attempt_id": "e" * 32,
        }
    )
    current_receipt["sequence"] = 2
    receipt["attempts"] = [prior_receipt, current_receipt]
    receipt["additional_physical_requests_started"] = 2
    receipt["remaining_additional_physical_requests"] = 1
    recovery_physical = call["candidates"][0]["execution"]["physical_attempts"][1]
    recovery_physical["attempt"] = 1
    return call, plan


def test_provider_native_local_attempt_ordinal_ignores_prior_cumulative_receipt_ids(
    module,
) -> None:
    call, plan = _provider_native_cumulative_recovery_call(module)

    final_identities, recovery_ids, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert reasons == []
    assert final_identities[0] == "openrouter:model-a"
    assert recovery_ids == {"b" * 32}


@pytest.mark.parametrize("mutation", ["wrong_slot", "bool_attempt"])
def test_provider_native_cumulative_receipt_keeps_current_local_attempt_strict(
    module,
    mutation: str,
) -> None:
    call, plan = _provider_native_cumulative_recovery_call(module)
    if mutation == "wrong_slot":
        call["proposer_recovery"]["attempts"][-1]["slot_index"] = 1
    else:
        call["candidates"][0]["execution"]["physical_attempts"][1]["attempt"] = True

    _, _, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert "invalid_proposer_recovery_physical_attempt" in reasons


def test_provider_native_local_attempt_ordinal_requires_receipt_slot_binding(
    module,
) -> None:
    call, plan = _provider_native_recovery_call(module)
    recovery_physical = call["candidates"][0]["execution"]["physical_attempts"][1]
    recovery_physical["attempt"] = 1
    receipt_attempt = call["proposer_recovery"]["attempts"][0]
    receipt_attempt["slot_index"] = 1

    _, _, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert "invalid_proposer_recovery_physical_attempt" in reasons


def test_provider_native_physical_attempt_ordinal_rejects_bool(module) -> None:
    call, plan = _provider_native_recovery_call(module)
    recovery_physical = call["candidates"][0]["execution"]["physical_attempts"][1]
    recovery_physical["attempt"] = True

    _, _, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert "invalid_proposer_recovery_physical_attempt" in reasons


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("physical_request_count", True),
        ("physical_request_count", "1"),
        ("physical_attempt_id", "not-a-physical-id"),
    ],
)
def test_provider_native_local_attempt_ordinal_rejects_malformed_receipt(
    module,
    field_name: str,
    value: object,
) -> None:
    call, plan = _provider_native_recovery_call(module)
    recovery_physical = call["candidates"][0]["execution"]["physical_attempts"][1]
    recovery_physical["attempt"] = 1
    receipt_attempt = call["proposer_recovery"]["attempts"][0]
    receipt_attempt[field_name] = value

    _, _, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert "invalid_proposer_recovery_physical_attempt" in reasons
    assert "invalid_proposer_recovery_attempt" in reasons


def _provider_native_outer_call_reasons(
    module,
    call: dict[str, object],
    plan: dict[str, object],
) -> list[str]:
    call["final_request_role"] = "aggregator"
    call["total_candidates"] = len(plan["proposer_models"])
    plan["aggregator_model"] = "model-f"
    return module.ensemble_physical_call_reasons(
        call,
        expected_proposers=plan["proposer_models"],
        expected_aggregator="model-f",
        final_text="",
        require_output_binding=False,
    )


def test_ensemble_physical_call_accepts_receipt_bound_local_proposer_attempt(
    module,
) -> None:
    call, plan = _provider_native_recovery_call(module)
    call["candidates"][0]["execution"]["physical_attempts"][1]["attempt"] = 1

    reasons = _provider_native_outer_call_reasons(module, call, plan)

    assert "invalid_proposer_physical_attempt" not in reasons


@pytest.mark.parametrize(
    "mutation",
    ["wrong_slot", "bool_attempt", "bool_receipt_count", "malformed_receipt_id"],
)
def test_ensemble_physical_call_keeps_local_proposer_attempt_receipt_bound(
    module,
    mutation: str,
) -> None:
    call, plan = _provider_native_recovery_call(module)
    recovery_physical = call["candidates"][0]["execution"]["physical_attempts"][1]
    recovery_physical["attempt"] = 1
    receipt_attempt = call["proposer_recovery"]["attempts"][0]
    if mutation == "wrong_slot":
        receipt_attempt["slot_index"] = 1
    elif mutation == "bool_attempt":
        recovery_physical["attempt"] = True
    elif mutation == "bool_receipt_count":
        receipt_attempt["physical_request_count"] = True
    else:
        receipt_attempt["physical_attempt_id"] = "not-a-physical-id"

    reasons = _provider_native_outer_call_reasons(module, call, plan)

    assert "invalid_proposer_physical_attempt" in reasons


def test_provider_native_proposer_recovery_uses_authenticated_dynamic_policy(
    module,
) -> None:
    call, plan = _provider_native_recovery_call(module)
    policy = {
        **deepcopy(module.FORMAL_PROPOSER_RECOVERY_POLICY),
        "configured_backup_count": 1,
        "effective_backup_count": 1,
        "max_additional_physical_requests": 2,
    }
    plan["backup_P"] = plan["backup_P"][:1]
    plan["configured_proposer_backup_count"] = 1
    plan["effective_proposer_backup_count"] = 1
    plan["proposer_recovery_policy"] = policy

    from opensquilla.provider.protocol import provider_retry_roster_fingerprint

    receipt = call["proposer_recovery"]
    receipt["selection_plan_fingerprint"] = provider_retry_roster_fingerprint(plan)
    receipt["max_additional_physical_requests"] = 2
    receipt["remaining_additional_physical_requests"] = 1

    _, _, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
        expected_policy=policy,
    )

    assert reasons == []


def test_provider_native_proposer_recovery_accepts_effective_backup_shortfall(
    module,
) -> None:
    call, plan = _provider_native_recovery_call(module)
    plan["backup_P"] = plan["backup_P"][:1]
    plan["effective_proposer_backup_count"] = 1
    plan["proposer_recovery_policy"] = {
        **plan["proposer_recovery_policy"],
        "effective_backup_count": 1,
    }

    from opensquilla.provider.protocol import provider_retry_roster_fingerprint

    call["proposer_recovery"]["selection_plan_fingerprint"] = provider_retry_roster_fingerprint(
        plan
    )
    _, _, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert reasons == []


def test_provider_shaped_dynamic_partial_quorum_separates_strict_and_usable(
    module,
) -> None:
    call, plan = _provider_native_recovery_call(module)
    candidate = call["candidates"][1]
    assert isinstance(candidate, dict)
    physical = candidate["execution"]["physical_attempts"][0]
    assert isinstance(physical, dict)
    physical["outcome"] = "failed"
    candidate.update(
        {
            "ok": False,
            "error": "provider stream ended after a useful prefix",
            "error_code": "upstream_interrupted",
            "stream_closed": True,
            "usable_for_aggregation": True,
            "completion_outcome": "partial_usable",
            "provider": "",
            "model": "",
            "content": {
                "text": "",
                "chars": 24,
                "truncated": True,
                "sha256": "a" * 64,
            },
        }
    )
    call.update(
        {
            "successful_proposers": 1,
            "usable_proposers": 2,
            "partial_proposers": 1,
            "strict_quorum_met": False,
            "execution_quorum_required": 2,
            "execution_quorum_met": True,
        }
    )
    receipt = call["proposer_recovery"]
    assert isinstance(receipt, dict)
    receipt.update(
        {
            "strict_successful_proposers": 1,
            "usable_proposers": 2,
            "quorum_reached": True,
        }
    )

    _, _, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert module.successful_candidate(candidate) is False
    assert module.partial_usable_candidate(candidate) is True
    assert reasons == []

    outer_reasons = module.ensemble_physical_call_reasons(
        call,
        expected_proposers=plan["proposer_models"],
        expected_aggregator="model-f",
        final_text="",
        require_output_binding=False,
    )
    assert "wrong_actual_proposer_provider" not in outer_reasons
    assert "wrong_actual_proposer_model" not in outer_reasons


def test_proposer_transport_success_does_not_promote_semantic_failure(
    module,
) -> None:
    call, plan = _provider_native_recovery_call(module)
    failed_candidate = call["candidates"][2]
    failed_candidate["execution"]["physical_attempts"][0]["outcome"] = "succeeded"

    _, _, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert "proposer_recovery_success_count_mismatch" not in reasons
    assert reasons == []


def _legacy_excluded_zero_request_recovery_call(module):
    call, plan = _provider_native_recovery_call(module)
    identity = "openrouter:model-c"
    call["candidates"][2] = {
        "index": 2,
        "label": "proposer_3",
        "provider": "",
        "requested_provider": "openrouter",
        "model": "",
        "requested_model": "model-c",
        "ok": False,
        "usable_for_aggregation": False,
        "completion_outcome": "failed",
        "request_started": False,
        "stream_closed": True,
        "usage_reported": False,
        "physical_request_count": 0,
        "usage_missing_count": 0,
        "stop_reason": "",
        "elapsed_ms": 0,
        "ttft_ms": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
        "content": {"text": "", "chars": 0, "truncated": False},
        "error": ("proposer identity was excluded after an earlier failure in this retry scope"),
        "error_code": "proposer_recovery_identity_excluded",
        "execution": {
            "request_started": False,
            "stream_closed": True,
            "blocked_reason": "scope_failed_identity",
            "blocked_identity": identity,
        },
    }
    call["proposer_recovery"]["cumulative_excluded_identities"] = [identity]
    return call, plan


def test_legacy_excluded_zero_request_candidate_gets_narrow_empty_ledger_compat(
    module,
) -> None:
    call, plan = _legacy_excluded_zero_request_recovery_call(module)

    _, _, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert reasons == []


@pytest.mark.parametrize(
    "mutation",
    ["started", "cost", "text", "wrong_identity", "unexpected_execution"],
)
def test_legacy_excluded_zero_request_candidate_compat_stays_fail_closed(
    module,
    mutation: str,
) -> None:
    call, plan = _legacy_excluded_zero_request_recovery_call(module)
    candidate = call["candidates"][2]
    if mutation == "started":
        candidate["request_started"] = True
    elif mutation == "cost":
        candidate["billed_cost"] = 0.01
    elif mutation == "text":
        candidate["content"] = {
            "text": "draft",
            "chars": 5,
            "truncated": False,
        }
    elif mutation == "wrong_identity":
        candidate["execution"]["blocked_identity"] = "openrouter:model-b"
    else:
        candidate["execution"]["unexpected"] = True

    _, _, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert "invalid_proposer_recovery_physical_ledger" in reasons


@pytest.mark.parametrize(
    "mutation",
    ["missing_explicit_flag", "contract_violation", "hard_error"],
)
def test_dynamic_partial_quorum_rejects_incomplete_or_hard_partial_evidence(
    module,
    mutation: str,
) -> None:
    candidate = {
        "ok": False,
        "request_started": True,
        "physical_request_count": 1,
        "usable_for_aggregation": True,
        "completion_outcome": "partial_usable",
        "error_code": "ensemble_proposer_close_timeout",
        "content": {"text": "draft", "chars": 5, "truncated": False},
        "execution": {"candidate_mode_contract_violation": False},
    }
    if mutation == "missing_explicit_flag":
        candidate.pop("usable_for_aggregation")
    elif mutation == "contract_violation":
        candidate["execution"]["candidate_mode_contract_violation"] = True
    else:
        candidate["error_code"] = "proposer_recovery_evidence_unproven"

    assert module.partial_usable_candidate(candidate) is False


def _provider_native_cleanup_quorum_bypass_call(
    module,
) -> tuple[dict[str, object], dict[str, object]]:
    call, plan = _provider_native_recovery_call(module)
    candidate = call["candidates"][2]
    assert isinstance(candidate, dict)
    execution = candidate["execution"]
    assert isinstance(execution, dict)
    physical = execution["physical_attempts"][0]
    assert isinstance(physical, dict)
    physical.update(
        {
            "stream_closed": False,
            "outcome": "interrupted",
        }
    )
    candidate.update(
        {
            "stream_closed": False,
            "error_code": "ensemble_proposer_close_timeout",
        }
    )
    receipt = call["proposer_recovery"]
    assert isinstance(receipt, dict)
    receipt["cumulative_excluded_identities"] = [
        "openrouter:model-c",
    ]
    receipt["cleanup_quorum_bypass"] = {
        "schema": ("opensquilla.router-dynamic-proposer-cleanup-quorum-bypass/v1"),
        "applied": True,
        "quorum_required": 2,
        "successful_proposers": 2,
        "usable_proposers": 2,
        "candidate_indexes": [2],
        "physical_attempt_ids": ["d" * 32],
        "recovery_skipped": True,
        "aggregator_tools_disabled": True,
    }
    return call, plan


def test_provider_native_cleanup_quorum_bypass_is_auditable(
    module,
) -> None:
    call, plan = _provider_native_cleanup_quorum_bypass_call(module)

    final_identities, recovery_ids, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert reasons == []
    assert final_identities == {
        0: "openrouter:model-a",
        1: "openrouter:model-b",
        2: "openrouter:model-c",
    }
    assert recovery_ids == {"b" * 32}


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("missing_marker", "invalid_proposer_recovery_physical_attempt"),
        (
            "wrong_physical_id",
            "proposer_cleanup_quorum_bypass_mismatch",
        ),
        (
            "tools_enabled",
            "invalid_proposer_cleanup_quorum_bypass",
        ),
        (
            "wrong_success_count",
            "proposer_cleanup_quorum_bypass_success_mismatch",
        ),
    ],
)
def test_provider_native_cleanup_quorum_bypass_fails_closed(
    module,
    mutation: str,
    expected_reason: str,
) -> None:
    call, plan = _provider_native_cleanup_quorum_bypass_call(module)
    receipt = call["proposer_recovery"]
    assert isinstance(receipt, dict)
    marker = receipt["cleanup_quorum_bypass"]
    assert isinstance(marker, dict)
    if mutation == "missing_marker":
        receipt.pop("cleanup_quorum_bypass")
    elif mutation == "wrong_physical_id":
        marker["physical_attempt_ids"] = ["e" * 32]
    elif mutation == "tools_enabled":
        marker["aggregator_tools_disabled"] = False
    else:
        marker["successful_proposers"] = 3

    _, _, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert expected_reason in reasons


def test_proposer_recovery_identity_accepts_valid_model_suffixes(
    module,
) -> None:
    assert (
        module._canonical_proposer_recovery_identity("openrouter:model:free")
        == "openrouter:model:free"
    )
    assert (
        module._canonical_proposer_recovery_identity("openrouter:Vendor/Model-X")
        == "openrouter:Vendor/Model-X"
    )
    assert module._canonical_proposer_recovery_identity("openrouter:model::free") == ""


def test_expanded_proposer_slots_allow_positional_cross_provider_model(
    module,
) -> None:
    slots, reasons = module.expanded_proposer_slot_identities(
        {
            "selected_P": [
                "openrouter:model/shared",
                "anthropic:model/shared",
            ],
            "proposer_models": [
                "model/shared",
                "model/shared",
            ],
        }
    )

    assert slots == (
        "openrouter:model/shared",
        "anthropic:model/shared",
    )
    assert reasons == []


def test_expanded_proposer_slots_reject_ambiguous_cross_provider_k_expansion(
    module,
) -> None:
    slots, reasons = module.expanded_proposer_slot_identities(
        {
            "selected_P": [
                "openrouter:model/shared",
                "anthropic:model/shared",
            ],
            "proposer_models": [
                "model/shared",
                "model/shared",
                "model/shared",
            ],
        }
    )

    assert slots == ()
    assert reasons == ["invalid_expanded_proposer_sample_roster"]


def test_provider_native_proposer_recovery_binds_expanded_k_slots(
    module,
) -> None:
    """Recovery receipts index physical samples, not distinct selected_P rows."""

    call, plan = _provider_native_recovery_call(module)
    selected = list(plan["selected_P"])
    plan["proposer_models"] = [
        "model-a",
        "model-a",
        "model-b",
        "model-c",
    ]
    plan["proposer_sample_count"] = 4
    from opensquilla.provider.protocol import (
        provider_retry_roster_fingerprint,
    )

    receipt = call["proposer_recovery"]
    assert isinstance(receipt, dict)
    receipt["selection_plan_fingerprint"] = provider_retry_roster_fingerprint(plan)
    receipt["executed_proposer_roster_before"] = [
        selected[0],
        selected[0],
        selected[1],
        selected[2],
    ]
    receipt["executed_proposer_roster_after"] = list(receipt["executed_proposer_roster_before"])

    candidates = call["candidates"]
    assert isinstance(candidates, list)
    duplicate_a = deepcopy(candidates[0])
    duplicate_a.update(
        {
            "index": 1,
            "ok": True,
            "requested_model": "model-a",
            "provider": "openrouter",
            "model": "model-a",
            "content": {
                "text": "duplicate-a",
                "chars": len("duplicate-a"),
                "truncated": False,
            },
            "physical_request_count": 1,
        }
    )
    duplicate_a["execution"] = {
        "physical_attempts": [
            {
                "attempt": 1,
                "request_started": True,
                "stream_closed": True,
                "physical_attempt_id": "e" * 32,
                "identity": selected[0],
                "outcome": "succeeded",
            }
        ]
    }
    candidates.insert(1, duplicate_a)
    for index, candidate in enumerate(candidates):
        assert isinstance(candidate, dict)
        candidate["index"] = index
    failed_b = candidates[2]
    assert isinstance(failed_b, dict)
    failed_b.update(
        {
            "ok": False,
            "provider": "",
            "model": "",
            "content": {"text": "", "chars": 0, "truncated": False},
        }
    )
    failed_b_attempts = failed_b["execution"]["physical_attempts"]
    assert isinstance(failed_b_attempts, list)
    assert isinstance(failed_b_attempts[0], dict)
    failed_b_attempts[0]["outcome"] = "failed"
    call["successful_proposers"] = 2

    final_identities, recovery_ids, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert reasons == []
    assert final_identities == {
        0: selected[0],
        1: selected[0],
        2: selected[1],
        3: selected[2],
    }
    assert recovery_ids == {"b" * 32}

    wrong_slot = deepcopy(call)
    wrong_receipt = wrong_slot["proposer_recovery"]
    assert isinstance(wrong_receipt, dict)
    attempts = wrong_receipt["attempts"]
    assert isinstance(attempts, list)
    assert isinstance(attempts[0], dict)
    attempts[0]["slot_index"] = 2
    _, _, wrong_reasons = module.proposer_recovery_execution_reasons(
        wrong_slot,
        executed_plan=plan,
    )
    assert "invalid_proposer_recovery_executed_roster" in wrong_reasons


def test_provider_native_backup_roster_is_reused_by_the_next_agent_call(
    module,
) -> None:
    first, plan = _provider_native_recovery_call(module)
    first_candidate = first["candidates"][0]
    first_receipt = first["proposer_recovery"]
    first_attempt = first_receipt["attempts"][0]
    backup_identity = str(plan["backup_P"][0])
    backup_model = backup_identity.partition(":")[2]
    first_candidate.update(
        {
            "requested_model": backup_model,
            "provider": "openrouter",
            "model": backup_model,
        }
    )
    first_candidate["execution"]["physical_attempts"][1]["identity"] = backup_identity
    first_attempt.update(
        {
            "kind": "backup_replacement",
            "target_identity": backup_identity,
            "reason": "frozen_backup_order",
        }
    )
    first_attempt.pop("thinking_before")
    first_attempt.pop("thinking_after")
    first_receipt["visited_identities"] = [backup_identity]
    first_receipt["executed_proposer_roster_after"][0] = backup_identity

    first_final, _, first_reasons = module.proposer_recovery_execution_reasons(
        first,
        executed_plan=plan,
    )
    assert first_reasons == []
    assert first_final[0] == backup_identity

    second = deepcopy(first)
    second_receipt = second["proposer_recovery"]
    effective_roster = [
        backup_identity,
        "openrouter:model-b",
        "openrouter:model-c",
    ]
    second_receipt["executed_proposer_roster_before"] = list(effective_roster)
    second_receipt["executed_proposer_roster_after"] = list(effective_roster)
    for candidate, attempt_id, identity, outcome in zip(
        second["candidates"],
        ("e" * 32, "f" * 32, "1" * 32),
        effective_roster,
        ("succeeded", "succeeded", "failed"),
        strict=True,
    ):
        model = identity.partition(":")[2]
        candidate.update(
            {
                "physical_request_count": 1,
                "requested_provider": "openrouter",
                "requested_model": model,
                "provider": "openrouter" if outcome == "succeeded" else "",
                "model": model if outcome == "succeeded" else "",
            }
        )
        candidate["execution"]["physical_attempts"] = [
            {
                "attempt": 1,
                "request_started": True,
                "stream_closed": True,
                "physical_attempt_id": attempt_id,
                "identity": identity,
                "outcome": outcome,
            }
        ]

    second_final, second_recovery_ids, second_reasons = module.proposer_recovery_execution_reasons(
        second,
        executed_plan=plan,
    )
    assert second_reasons == []
    assert second_final[0] == backup_identity
    assert second_recovery_ids == set()

    first["agent_call_index"] = 1
    second["agent_call_index"] = 2
    physical_models = (
        ("a" * 32, "model-a"),
        ("b" * 32, backup_model),
        ("c" * 32, "model-b"),
        ("d" * 32, "model-c"),
        ("e" * 32, backup_model),
        ("f" * 32, "model-b"),
        ("1" * 32, "model-c"),
    )

    def usage_unit(
        physical_attempt_id: str,
        model: str,
    ) -> dict[str, object]:
        return {
            "role": "proposer",
            "provider": "openrouter",
            "requested_provider": "openrouter",
            "model": model,
            "requested_model": model,
            "request_count": 1,
            "physical_attempt_id": physical_attempt_id,
            "input_tokens": 1,
            "output_tokens": 1,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "billed_cost": 0.0,
            "cost_source": "none",
            "provider_usage": {
                "physical_attempt_id": physical_attempt_id,
            },
        }

    run = {
        "llm_request_count": len(physical_models),
        "physical_request_count": len(physical_models),
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 2,
            "untraced_agent_llm_call_count": 0,
            "calls": [first, second],
        },
        "usage": {
            "model_usage_breakdown": [
                usage_unit(attempt_id, model) for attempt_id, model in physical_models
            ]
        },
    }
    assert module.g1_thinking_physical_usage_binding_reasons(run) == []

    tampered_continuity = deepcopy(run)
    tampered_continuity["ensemble_trace"]["calls"][1]["proposer_recovery"][
        "executed_proposer_roster_before"
    ] = list(plan["selected_P"])
    assert (
        "proposer_recovery_roster_prefix_changed"
        in module.g1_thinking_physical_usage_binding_reasons(tampered_continuity)
    )

    tampered_first_prefix = deepcopy(run)
    tampered_first_prefix["ensemble_trace"]["calls"][0]["proposer_recovery"][
        "executed_proposer_roster_before"
    ] = list(effective_roster)
    assert (
        "proposer_recovery_roster_prefix_changed"
        in module.g1_thinking_physical_usage_binding_reasons(tampered_first_prefix)
    )


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("fingerprint", "invalid_proposer_recovery_receipt"),
        ("budget", "invalid_proposer_recovery_receipt"),
        ("external_reserved", "invalid_proposer_recovery_receipt"),
        ("chat_scope", "invalid_proposer_recovery_receipt"),
        ("empty_scope", "invalid_proposer_recovery_receipt"),
        ("usage", "invalid_proposer_recovery_attempt"),
        ("skip_backup", "proposer_recovery_skipped_ordered_backup"),
        (
            "continued_after_quorum",
            "proposer_recovery_continued_after_quorum",
        ),
    ],
)
def test_provider_native_proposer_recovery_fails_closed(
    module,
    mutation: str,
    expected_reason: str,
) -> None:
    call, plan = _provider_native_recovery_call(module)
    receipt = call["proposer_recovery"]
    candidates = call["candidates"]
    assert isinstance(receipt, dict)
    assert isinstance(candidates, list)
    attempts = receipt["attempts"]
    assert isinstance(attempts, list)
    if mutation == "fingerprint":
        receipt["selection_plan_fingerprint"] = "0" * 64
    elif mutation == "budget":
        receipt["remaining_additional_physical_requests"] = 3
    elif mutation == "external_reserved":
        receipt["external_physical_requests_reserved"] = 1
    elif mutation == "chat_scope":
        receipt["scope"] = "chat"
    elif mutation == "empty_scope":
        receipt["scope_id"] = ""
    elif mutation == "usage":
        attempts[0]["usage_missing_count"] = 1
    elif mutation == "skip_backup":
        candidate = candidates[0]
        candidate["requested_model"] = "model-e"
        candidate["provider"] = "openrouter"
        candidate["model"] = "model-e"
        candidate["execution"]["physical_attempts"][1]["identity"] = "openrouter:model-e"
        attempts[0].update(
            {
                "kind": "backup_replacement",
                "target_identity": "openrouter:model-e",
            }
        )
        attempts[0].pop("thinking_before")
        attempts[0].pop("thinking_after")
        receipt["visited_identities"] = ["openrouter:model-e"]
        receipt["executed_proposer_roster_after"][0] = "openrouter:model-e"
    else:
        candidate = candidates[2]
        physical_rows = candidate["execution"]["physical_attempts"]
        physical_rows.append(
            {
                "attempt": 2,
                "request_started": True,
                "stream_closed": True,
                "physical_attempt_id": "e" * 32,
                "identity": "openrouter:model-c",
                "outcome": "failed",
            }
        )
        candidate["physical_request_count"] = 2
        attempts.append(
            {
                "sequence": 2,
                "slot_index": 2,
                "kind": "transient_retry",
                "source_identity": "openrouter:model-c",
                "target_identity": "openrouter:model-c",
                "failure_kind": "timeout",
                "reason": "transient_same_model_retry",
                "backoff_s": 0.0,
                "request_started": True,
                "physical_request_count": 1,
                "physical_attempt_id": "e" * 32,
                "stream_closed": True,
                "usage_reported": True,
                "usage_missing_count": 0,
                "outcome": "failed",
            }
        )
        receipt["additional_physical_requests_started"] = 2
        receipt["remaining_additional_physical_requests"] = 1

    _, _, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )
    assert expected_reason in reasons


def test_provider_native_recovery_allows_unstarted_primary_then_backup(
    module,
) -> None:
    call, plan = _provider_native_recovery_call(module)
    candidate = call["candidates"][0]
    receipt = call["proposer_recovery"]
    attempt = receipt["attempts"][0]
    backup_identity = plan["backup_P"][0]
    candidate.update(
        {
            "physical_request_count": 1,
            "requested_model": backup_identity.partition(":")[2],
            "provider": "openrouter",
            "model": backup_identity.partition(":")[2],
        }
    )
    backup_physical = candidate["execution"]["physical_attempts"][1]
    backup_physical.update(
        {
            "attempt": 1,
            "identity": backup_identity,
        }
    )
    candidate["execution"]["physical_attempts"] = [backup_physical]
    attempt.update(
        {
            "kind": "backup_replacement",
            "target_identity": backup_identity,
            "reason": "frozen_backup_order",
        }
    )
    attempt.pop("thinking_before")
    attempt.pop("thinking_after")
    receipt["visited_identities"] = [backup_identity]
    receipt["executed_proposer_roster_after"][0] = backup_identity

    final_identities, recovery_ids, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
    )

    assert reasons == []
    assert final_identities[0] == backup_identity
    assert recovery_ids == {"b" * 32}
    run = {
        "llm_request_count": 3,
        "ensemble_trace": call,
        "usage": {
            "model_usage_breakdown": [
                {
                    "role": "proposer",
                    "provider": "openrouter",
                    "requested_provider": "openrouter",
                    "model": model,
                    "requested_model": model,
                    "request_count": 1,
                    "physical_attempt_id": attempt_id,
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "reasoning_tokens": 0,
                    "cached_tokens": 0,
                    "cache_write_tokens": 0,
                    "billed_cost": 0.0,
                    "cost_source": "none",
                    "provider_usage": {
                        "physical_attempt_id": attempt_id,
                    },
                }
                for attempt_id, model in (
                    ("b" * 32, "model-d"),
                    ("c" * 32, "model-b"),
                    ("d" * 32, "model-c"),
                )
            ]
        },
    }
    assert module.g1_thinking_physical_usage_binding_reasons(run) == []


def test_provider_native_formal_quorum_is_two_for_five_proposers(
    module,
) -> None:
    call, plan = _provider_native_recovery_call(module)
    selected = list(plan["selected_P"]) + [
        "openrouter:model-i",
        "openrouter:model-j",
    ]
    plan["selected_P"] = selected
    plan["proposer_models"] = [identity.partition(":")[2] for identity in selected]
    plan["proposer_sample_count"] = len(selected)
    receipt = call["proposer_recovery"]
    receipt["executed_proposer_roster_before"] = list(selected)
    receipt["executed_proposer_roster_after"] = list(selected)
    from opensquilla.provider.protocol import (
        provider_retry_roster_fingerprint,
    )

    receipt["selection_plan_fingerprint"] = provider_retry_roster_fingerprint(plan)
    for index, (model, attempt_id) in enumerate(
        (("model-i", "e" * 32), ("model-j", "f" * 32)),
        start=3,
    ):
        call["candidates"].append(
            {
                "index": index,
                "ok": False,
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": True,
                "usage_missing_count": 0,
                "requested_provider": "openrouter",
                "requested_model": model,
                "provider": "",
                "model": "",
                "content": {"text": "", "chars": 0, "truncated": False},
                "execution": {
                    "physical_attempts": [
                        {
                            "attempt": 1,
                            "request_started": True,
                            "stream_closed": True,
                            "physical_attempt_id": attempt_id,
                            "identity": f"openrouter:{model}",
                            "outcome": "failed",
                        }
                    ]
                },
            }
        )
    call["total_candidates"] = 5
    call.update(
        {
            "successful_proposers": 2,
            "usable_proposers": 2,
            "partial_proposers": 0,
            "strict_quorum_met": True,
            "execution_quorum_required": 2,
            "execution_quorum_met": True,
        }
    )
    receipt.update(
        {
            "strict_successful_proposers": 2,
            "usable_proposers": 2,
            "quorum_reached": True,
        }
    )

    reasons = module.ensemble_physical_call_reasons(
        call,
        expected_proposers=plan["proposer_models"],
        expected_aggregator="model-f",
        final_text="",
        require_output_binding=False,
    )
    assert "proposer_quorum_not_met" not in reasons
    assert "insufficient_actual_proposer_quorum" not in reasons

    one_success = deepcopy(call)
    one_success.update(
        {
            "successful_proposers": 1,
            "usable_proposers": 1,
            "partial_proposers": 0,
            "strict_quorum_met": False,
            "execution_quorum_required": 2,
            "execution_quorum_met": False,
        }
    )
    one_success["proposer_recovery"].update(
        {
            "strict_successful_proposers": 1,
            "usable_proposers": 1,
            "quorum_reached": False,
        }
    )
    failed_second = one_success["candidates"][1]
    failed_second.update(
        {
            "ok": False,
            "usable_for_aggregation": False,
            "completion_outcome": "failed",
            "provider": "",
            "model": "",
            "content": {"text": "", "chars": 0, "truncated": False},
        }
    )
    failed_second["execution"]["physical_attempts"][0]["outcome"] = "failed"
    one_success_reasons = module.ensemble_physical_call_reasons(
        one_success,
        expected_proposers=plan["proposer_models"],
        expected_aggregator="model-f",
        final_text="",
        require_output_binding=False,
    )
    assert "proposer_quorum_not_met" in one_success_reasons
    assert "insufficient_actual_proposer_quorum" in one_success_reasons


def test_provider_native_recovery_usage_is_bound_exactly_once(
    module,
) -> None:
    call, _ = _provider_native_recovery_call(module)

    def usage_unit(
        attempt_id: str,
        model: str,
    ) -> dict[str, object]:
        return {
            "role": "proposer",
            "provider": "openrouter",
            "requested_provider": "openrouter",
            "model": model,
            "requested_model": model,
            "request_count": 1,
            "physical_attempt_id": attempt_id,
            "input_tokens": 1,
            "output_tokens": 1,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "billed_cost": 0.0,
            "cost_source": "none",
            "provider_usage": {
                "physical_attempt_id": attempt_id,
            },
        }

    run = {
        "llm_request_count": 4,
        "ensemble_trace": call,
        "usage": {
            "model_usage_breakdown": [
                usage_unit("a" * 32, "model-a"),
                usage_unit("b" * 32, "model-a"),
                usage_unit("c" * 32, "model-b"),
                usage_unit("d" * 32, "model-c"),
            ]
        },
    }

    assert module.g1_thinking_physical_usage_binding_reasons(run) == []

    missing_recovery_usage = deepcopy(run)
    missing_recovery_usage["usage"]["model_usage_breakdown"].pop(1)
    assert (
        "g1_thinking_physical_usage_set_mismatch"
        in module.g1_thinking_physical_usage_binding_reasons(missing_recovery_usage)
    )

    duplicate_recovery_usage = deepcopy(run)
    duplicate_recovery_usage["usage"]["model_usage_breakdown"].append(
        usage_unit("b" * 32, "model-a")
    )
    assert (
        "g1_thinking_physical_usage_set_mismatch"
        in module.g1_thinking_physical_usage_binding_reasons(duplicate_recovery_usage)
    )


def test_provider_native_lifecycle_forbids_outer_generation_replay(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call, plan = _provider_native_recovery_call(module)

    def usage_unit(
        attempt_id: str,
        model: str,
    ) -> dict[str, object]:
        return {
            "role": "proposer",
            "provider": "openrouter",
            "requested_provider": "openrouter",
            "model": model,
            "requested_model": model,
            "request_count": 1,
            "physical_attempt_id": attempt_id,
            "input_tokens": 1,
            "output_tokens": 1,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "billed_cost": 0.0,
            "cost_source": "none",
            "provider_usage": {
                "physical_attempt_id": attempt_id,
            },
        }

    usage = {
        "model_usage_breakdown": [
            usage_unit("a" * 32, "model-a"),
            usage_unit("b" * 32, "model-a"),
            usage_unit("c" * 32, "model-b"),
            usage_unit("d" * 32, "model-c"),
        ]
    }
    run = {
        "final_text_sha256": "selected-answer",
        "llm_request_count": 4,
        "routing_trace": {"selection_plan": deepcopy(plan)},
        "ensemble_trace": deepcopy(call),
        "usage": deepcopy(usage),
    }
    attempt = {
        "attempt_id": "1" * 32,
        "attempt_kind": "generation",
        "attempt": 1,
        "retryable": False,
        "retry_reason": "",
        "retry_suppressed_reason": "",
        "will_retry": False,
        "retry_backoff_s": 0.0,
        "proposer_recovery_owner": "provider",
        "selection_plan": deepcopy(plan),
        "deterministic_proposer_failures": [],
        "excluded_proposer_identities": [],
        "run": run,
    }
    row = {
        "group": "G1",
        "task_id": "task-1",
        "final_text_sha256": "selected-answer",
        "usage": deepcopy(usage),
        "routing_trace": {"selection_plan": deepcopy(plan)},
        "ensemble_trace": deepcopy(call),
        "execution": {"generation_attempts": [attempt]},
    }

    def fake_plan_reasons(
        value,
        *,
        contract,
        allow_legacy_managed_v3=False,
        aggregator_recovery_policy=None,
        proposer_recovery_policy=None,
    ):
        del (
            contract,
            allow_legacy_managed_v3,
            aggregator_recovery_policy,
            proposer_recovery_policy,
        )
        return (
            [],
            tuple(value.get("proposer_models") or []),
            str(value.get("aggregator_model") or ""),
        )

    monkeypatch.setattr(
        module,
        "g1_registry_plan_reasons",
        fake_plan_reasons,
    )
    _, evidence, reasons = module._adaptive_g1_lifecycle_routing(
        row,
        registry={},
        physical_plans=(plan,),
        initial_reasons=(),
    )
    assert reasons == []
    assert evidence["validated_provider_native_receipt_count"] == 1

    replayed = deepcopy(row)
    first_attempt = replayed["execution"]["generation_attempts"][0]
    first_attempt["run"]["final_text_sha256"] = "discarded-answer"
    second_attempt = deepcopy(attempt)
    second_attempt["attempt_id"] = "2" * 32
    second_attempt["attempt"] = 2
    replayed["execution"]["generation_attempts"].append(second_attempt)
    _, _, replay_reasons = module._adaptive_g1_lifecycle_routing(
        replayed,
        registry={},
        physical_plans=(plan,),
        initial_reasons=(),
    )
    assert "g1_provider_native_outer_retry_forbidden" in replay_reasons


def _g1_routes() -> dict[str, str]:
    """Return a small formal pool with room for P, backup_P, and A fallbacks."""

    return {
        "deepseek/deepseek-v4-pro": "deepseek",
        "z-ai/glm-5.2": "z-ai",
        "qwen/qwen3.7-max": "alibaba",
        "moonshotai/kimi-k2.7-code": "moonshotai",
        "anthropic/claude-sonnet-5": "anthropic",
        "openai/gpt-5.5": "openai",
        "google/gemini-3.1-pro-preview": "google-ai-studio",
        "deepseek/deepseek-v4-flash": "deepseek",
    }


def _default_task_analyzer_policy() -> dict[str, object]:
    from opensquilla.provider.ranking_router import (
        ranking_config_resolution,
        task_analyzer_policy,
    )

    return dict(task_analyzer_policy(ranking_config_resolution()["effective_config"]))


def _contract(module, group: str, key_hash: str) -> dict[str, object]:
    fixed = {
        "B0": module.B0_MODEL,
        "B4": module.B4_MODEL,
    }
    blocked_domains = list(module.FORMAL_BLOCKED_DOMAINS)
    finalization_policy = dict(module.FORMAL_AGENT_FINALIZATION_POLICY)
    model_thinking_levels = dict(module.FORMAL_MODEL_THINKING_LEVELS)
    contract: dict[str, object] = {
        "schema": "opensquilla.draco.run-compatibility/v1",
        "benchmark": "DRACO",
        "group": group,
        "group_spec": (
            {"kind": "single", "model": fixed[group]}
            if group in fixed
            else {"kind": "router_single"}
            if group == "B1"
            else {
                "kind": "selection_mode",
                "selection_mode": ("static_openrouter_b5" if group == "B2" else "router_dynamic"),
            }
        ),
        "runner": {
            "mode": "agent_loop",
            "agent_max_iterations": 20,
            "finalization_policy": finalization_policy,
        },
        "tools": {
            "tool_mode": "local_web_tools",
            "tools_enabled": True,
            "tool_names": ["web_search", "web_fetch"],
            "local_web_tools": {
                "web_search": {
                    "excluded_domains": blocked_domains,
                    "max_results": 5,
                    "provider": "brave",
                    "api_key_env": "BRAVE_SEARCH_API_KEY",
                },
                "web_fetch": {
                    "blocked_domains": blocked_domains,
                    "max_content_tokens": 50_000,
                    "max_content_chars": 200_000,
                    "allow_firecrawl": False,
                },
            },
            "contamination_blocked_domains": blocked_domains,
            "contamination_controls": {
                "status": "enforced_by_local_web_tools",
                "web_search_field": "excluded_domains_query_and_result_filter",
                "web_fetch_field": "blocked_domains",
            },
        },
        "generation": {
            "policy": {
                "generation_thinking": "model_max",
                "temperature": 0.0,
                "thinking_enabled": True,
                "thinking_level": "model-specific",
                "default_thinking_level": "xhigh",
                "thinking_budget_tokens": 50_000,
                "max_thinking_budget_tokens": 50_000,
                "max_tokens": 16_384,
                "max_tokens_overridden": True,
                "model_thinking_levels": model_thinking_levels,
                "require_highest_thinking": True,
                "applies_to": "single baselines and ensemble members",
            },
            "max_attempts": 3,
            "retry_backoff_seconds": 2.0,
        },
        "judge": {
            "model": "google/gemini-3.1-pro-preview",
            "repeats": 3,
            "max_attempts": 3,
            "judge_candidates": False,
        },
        "timeouts": {
            "task_seconds": 10_800.0,
            "proposer_seconds": 907.5,
            "aggregator_seconds": 2662.5,
            "proposer_early_stop_success_count": 0,
            "proposer_early_stop_after_seconds": 0.0,
            "expand_to_task_timeout": False,
        },
        "resolved_llm_runtime": {
            "provider": "openrouter",
            "api_key_sha256": f"sha256:{key_hash}",
            "base_url": "https://openrouter.ai/api/v1",
            "base_url_from_env": False,
            "proxy": "",
            "provider_routing": {
                **dict(module.FORMAL_UPSTREAM_PINS),
                **(_g1_routes() if group == "G1" else {}),
            },
            "provider_routing_strict": True,
            "stream_error_frames": True,
            "router_metadata_required": True,
            "require_parameters": True,
            "response_cache_disabled": True,
            "key_exclusive": True,
            "cache_namespace_enabled": False,
            "cache_namespace_required": False,
            "cache_namespace_sha256": "",
            "trust_env": False,
            "ambient_proxies": {},
        },
        "cost_policy": {"require_openrouter_non_byok": True},
        "global_experiment_profile": {
            "schema_version": 1,
            "profile_id": "opensquilla_b2_quality_first_v2",
            "benchmark_input": {
                "sha256": module.FROZEN_DRACO_MINI_SHA256,
                "task_count": module.FROZEN_DRACO_MINI_TASK_COUNT,
                "enforce_reference_input": True,
            },
            "timeouts": {
                "task_seconds": 10_800.0,
                "proposer_seconds": 907.5,
                "aggregator_seconds": 2662.5,
                "task_margin_seconds": 30.0,
            },
            "runner": {
                "mode": "agent_loop",
                "agent_max_iterations": 20,
                **finalization_policy,
            },
            "generation": {
                "thinking_enabled": True,
                "thinking_budget_tokens": 50_000,
                "default_thinking_level": "xhigh",
                "model_thinking_levels": model_thinking_levels,
                "require_highest_thinking": True,
                "temperature": 0.0,
                "max_tokens": 16_384,
                "max_attempts": 3,
                "retry_backoff_seconds": 2.0,
            },
            "tools": {
                "mode": "local_web_tools",
                "sandbox_enabled": False,
                "contamination_blocked_domains": blocked_domains,
                "web_search": {
                    "provider": "brave",
                    "api_key_env": "BRAVE_SEARCH_API_KEY",
                    "max_results": 5,
                },
                "web_fetch": {"max_content_tokens": 50_000},
            },
            "judge": {
                "model": "google/gemini-3.1-pro-preview",
                "repeats": 3,
                "max_attempts": 3,
                "judge_candidates": False,
            },
        },
        "formal_runtime_freeze": {
            "source": "experiment_config",
            "sandbox_enabled": False,
            "sandbox_security_grading_enabled": False,
            **module.FORMAL_AGGREGATOR_RECOVERY_POLICY,
            "proposer_backup_count": 2,
            "proposer_recovery_max_additional_calls": 3,
            "proposer_max_tokens_cap": 65_536,
            "proposer_visible_answer_reserve_tokens": 4_096,
            "g1_user_profile_generation_enabled": False,
            "g1_user_profile_enabled": False,
        },
        "gateway_execution": {
            "llm_ensemble": dict(module.FORMAL_AGGREGATOR_RECOVERY_POLICY),
        },
        "dry_run": False,
    }
    if group == "B1":
        contract["gateway_execution"]["squilla_router"] = {
            "tiers": {
                "c0": {
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash",
                },
                "c1": {
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-pro",
                },
                "c2": {"provider": "openrouter", "model": "z-ai/glm-5.2"},
                "c3": {
                    "provider": "openrouter",
                    "model": "anthropic/claude-opus-4.8",
                },
                "image_model": {
                    "provider": "openrouter",
                    "model": "moonshotai/kimi-k2.6",
                },
            }
        }
    if group == "G1":
        routes = _g1_routes()
        contract["g1_registry_contract"] = {
            "profile_id": "test-g1",
            "selection_mode": "router_dynamic",
            "user_profile_enabled": False,
            "source_registry_snapshot_version": "test-registry-v1",
            "expected_routes": routes,
            "expected_routes_sha256": module.canonical_sha256(routes),
            "expected_candidate_count": len(routes),
            "expected_source_registry_snapshot_sha256": (
                module.FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256
            ),
            "expected_ranking_config_schema_version": (
                module.FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION
            ),
            "expected_ranking_config_version": (module.FORMAL_G1_RANKING_CONFIG_VERSION),
            "expected_ranking_config_sha256": (module.FORMAL_G1_RANKING_CONFIG_SHA256),
            "expected_proposer_count_max": (module.FORMAL_G1_PROPOSER_COUNT_MAX),
            "task_analyzer": _default_task_analyzer_policy(),
        }
    return contract


def _test_ranking_config(module) -> dict[str, object]:
    from opensquilla.provider.ranking_router import ranking_config_snapshot

    config = deepcopy(ranking_config_snapshot(base_version=module.FORMAL_G1_RANKING_CONFIG_VERSION))
    # Make this tiny three-model fixture select all three proposers while still
    # exercising the complete production ranking configuration.
    config["proposer_count"]["by_tier"]["3"] = {"min": 3, "max": 3}
    return config


def _runtime_override_ranking_contract(
    module,
    *,
    override: dict[str, object] | None,
) -> dict[str, object]:
    from opensquilla.provider.ranking_router import (
        ranking_config_resolution,
        task_analyzer_policy,
    )

    thinking_override = override.get("thinking_assignment") if isinstance(override, dict) else None
    resolution = ranking_config_resolution(
        thinking_assignment_enabled=(
            None
            if isinstance(thinking_override, dict) and "enabled" in thinking_override
            else False
        ),
        override=override,
        base_version=module.FORMAL_G1_RANKING_CONFIG_VERSION,
    )
    effective = resolution["effective_config"]
    proposer_count = effective["proposer_count"]
    effective_proposer_max = max(
        *(int(row["max"]) for row in proposer_count["by_tier"].values()),
        int(proposer_count["high_risk"]["max"]),
    )
    contract = _contract(module, "G1", "a" * 64)["g1_registry_contract"]
    contract.update(
        {
            "baseline_expected_ranking_config_schema_version": (
                module.FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION
            ),
            "baseline_expected_ranking_config_version": (module.FORMAL_G1_RANKING_CONFIG_VERSION),
            "baseline_expected_ranking_config_sha256": (module.FORMAL_G1_RANKING_CONFIG_SHA256),
            "baseline_expected_proposer_count_max": (module.FORMAL_G1_PROPOSER_COUNT_MAX),
            "expected_ranking_config_schema_version": effective["schema_version"],
            "expected_ranking_config_version": effective["config_version"],
            "expected_ranking_config_sha256": resolution["effective_sha256"],
            "expected_proposer_count_max": effective_proposer_max,
            "ranking_config_resolution": resolution,
            "task_analyzer": dict(task_analyzer_policy(effective)),
        }
    )
    return contract


def _thinking_registry_all_contract(module) -> dict[str, object]:
    contract = _runtime_override_ranking_contract(
        module,
        override={"thinking_assignment": {"enabled": True}},
    )
    routes = _frozen_registry_all_routes(module)
    contract.update(
        {
            "candidate_scope": "registry_all",
            "policy": "all_registry_models",
            "source_registry_snapshot_version": (module.FORMAL_G1_FULL_REGISTRY_SNAPSHOT_VERSION),
            "expected_routes": routes,
            "expected_routes_sha256": module.canonical_sha256(routes),
            "expected_candidate_count": len(routes),
            "expected_source_registry_snapshot_sha256": (
                module.FORMAL_G1_FULL_REGISTRY_SNAPSHOT_SHA256
            ),
        }
    )
    return contract


def test_thinking_v4_full_registry_contract_uses_frozen_source_identity(module) -> None:
    registry = _thinking_registry_all_contract(module)

    assert module.g1_registry_source_identity(registry) == (
        module.FORMAL_G1_FULL_REGISTRY_SNAPSHOT_VERSION,
        module.FORMAL_G1_FULL_REGISTRY_SNAPSHOT_SHA256,
    )
    assert module.authenticated_registry_all_routes(registry) == registry["expected_routes"]
    reasons, _, _ = module.g1_registry_plan_reasons({}, contract=registry)
    assert "invalid_g1_registry_contract" not in reasons

    contracts = {group: _contract(module, group, "a" * 64) for group in module.GROUPS}
    contracts["G1"]["g1_registry_contract"] = registry
    assert module.validate_formal_campaign_contracts(contracts).generation_max_attempts == 3


def test_thinking_v4_full_registry_contract_rejects_legacy_source_hash(module) -> None:
    registry = _thinking_registry_all_contract(module)
    registry["expected_source_registry_snapshot_sha256"] = (
        module.FORMAL_G1_LEGACY_SOURCE_REGISTRY_SNAPSHOT_SHA256
    )

    assert module.authenticated_registry_all_routes(registry) is None
    reasons, _, _ = module.g1_registry_plan_reasons({}, contract=registry)
    assert "invalid_g1_registry_contract" in reasons


def test_g1_ranking_proposer_max_uses_authenticated_effective_config(module) -> None:
    contract = _runtime_override_ranking_contract(
        module,
        override={
            "proposer_count": {
                "by_tier": {"4": {"max": 4}},
                "high_risk": {"max": 4},
            }
        },
    )

    assert contract["expected_proposer_count_max"] == 4
    assert module.g1_ranking_proposer_max(contract) == 4
    contract["expected_proposer_count_max"] = 5
    assert module.g1_ranking_proposer_max(contract) is None


def test_g1_ranking_config_identity_accepts_legacy_and_runtime_override(module) -> None:
    legacy = _contract(module, "G1", "a" * 64)["g1_registry_contract"]
    assert module.g1_ranking_config_identity(legacy) == (
        module.FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION,
        module.FORMAL_G1_RANKING_CONFIG_VERSION,
        module.FORMAL_G1_RANKING_CONFIG_SHA256,
    )

    resolved_baseline = _runtime_override_ranking_contract(
        module,
        override=None,
    )
    assert module.g1_ranking_config_identity(resolved_baseline) == (
        module.FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION,
        module.FORMAL_G1_RANKING_CONFIG_VERSION,
        module.FORMAL_G1_RANKING_CONFIG_SHA256,
    )

    contract = _runtime_override_ranking_contract(
        module,
        override={
            "penalties": {
                "task_cost_weights": {
                    "low": 0.17,
                    "medium": 0.08,
                    "high": 0.03,
                    "hard_limit": 0.24,
                }
            }
        },
    )
    resolution = contract["ranking_config_resolution"]
    assert module.g1_ranking_config_identity(contract) == (
        resolution["effective_config"]["schema_version"],
        resolution["effective_config"]["config_version"],
        resolution["effective_sha256"],
    )

    thinking_contract = _runtime_override_ranking_contract(
        module,
        override={"thinking_assignment": {"enabled": True}},
    )
    thinking_resolution = thinking_contract["ranking_config_resolution"]
    assert thinking_resolution["base_config"]["schema_version"] == (
        module.FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION
    )
    assert thinking_resolution["effective_config"]["schema_version"] == ("step2-ranking-config-v4")
    assert module.g1_ranking_config_identity(thinking_contract) == (
        thinking_resolution["effective_config"]["schema_version"],
        thinking_resolution["effective_config"]["config_version"],
        thinking_resolution["effective_sha256"],
    )


def test_g1_ranking_resolution_replay_never_loads_packaged_config(
    module,
    monkeypatch,
) -> None:
    import opensquilla.provider.ranking_router as ranking_router

    contract = _runtime_override_ranking_contract(
        module,
        override={"penalties": {"task_cost_weights": {"medium": 0.09}}},
    )
    expected = (
        contract["expected_ranking_config_schema_version"],
        contract["expected_ranking_config_version"],
        contract["expected_ranking_config_sha256"],
    )
    monkeypatch.setattr(
        ranking_router,
        "ranking_config_resolution",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("packaged config was loaded")),
    )
    monkeypatch.setattr(
        ranking_router,
        "task_analyzer_policy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("packaged analyzer policy was loaded")
        ),
    )

    assert module.g1_ranking_config_identity(contract) == expected
    assert module.g1_task_analyzer_execution_policy(contract) == contract["task_analyzer"]


def test_g1_historical_no_resolution_analyzer_uses_frozen_policy(
    module,
    monkeypatch,
) -> None:
    import opensquilla.provider.ranking_router as ranking_router

    contract = _contract(module, "G1", "a" * 64)["g1_registry_contract"]
    historical = next(iter(module.HISTORICAL_G1_RANKING_CONFIG_IDENTITIES))
    contract.update(
        {
            "expected_ranking_config_schema_version": historical[0],
            "expected_ranking_config_version": historical[1],
            "expected_ranking_config_sha256": historical[2],
        }
    )
    contract.pop("task_analyzer", None)
    monkeypatch.setattr(
        ranking_router,
        "ranking_config_resolution",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("packaged config was loaded")),
    )
    monkeypatch.setattr(
        ranking_router,
        "task_analyzer_policy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("packaged analyzer policy was loaded")
        ),
    )

    assert module.g1_task_analyzer_execution_policy(contract) == (
        module.FORMAL_TASK_ANALYZER_EXECUTION_POLICY
    )


def test_g1_ranking_config_identity_accepts_2026_07_22_historical_replay(
    module,
) -> None:
    contract = _contract(module, "G1", "a" * 64)["g1_registry_contract"]
    historical = (
        "step2-ranking-config-v3",
        "step2-ranking-2026-07-22.1",
        "a8addcdefa04349209c20e97ca5851ed0f5ca55646c9d0c5badc5d32dd7ef10c",
    )
    contract["expected_ranking_config_schema_version"] = historical[0]
    contract["expected_ranking_config_version"] = historical[1]
    contract["expected_ranking_config_sha256"] = historical[2]

    assert module.g1_ranking_config_identity(contract) == historical


def test_g1_ranking_config_identity_accepts_unicode_runtime_override(
    module,
    monkeypatch,
) -> None:
    from opensquilla.provider.ranking_router import ranking_config_resolution

    baseline = ranking_config_resolution(thinking_assignment_enabled=False)
    monkeypatch.setattr(
        module,
        "FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION",
        baseline["effective_config"]["schema_version"],
    )
    monkeypatch.setattr(
        module,
        "FORMAL_G1_RANKING_CONFIG_VERSION",
        baseline["effective_config"]["config_version"],
    )
    monkeypatch.setattr(
        module,
        "FORMAL_G1_RANKING_CONFIG_SHA256",
        baseline["effective_sha256"],
    )
    contract = _runtime_override_ranking_contract(
        module,
        override={"mock_user_profile": {"profile_source": "实验-中文"}},
    )
    resolution = contract["ranking_config_resolution"]

    assert resolution["override_sha256"] == module.canonical_sha256(resolution["override"])
    assert module.g1_ranking_config_identity(contract) == (
        resolution["effective_config"]["schema_version"],
        resolution["effective_config"]["config_version"],
        resolution["effective_sha256"],
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "baseline_hash",
        "base_hash",
        "override_hash",
        "effective_hash",
        "effective_version",
        "thinking_switch",
        "extra_resolution_field",
        "undeclared_effective_change",
        "override_not_applied",
    ],
)
def test_g1_ranking_config_identity_rejects_resolution_tampering(
    module,
    mutation: str,
) -> None:
    contract = _runtime_override_ranking_contract(
        module,
        override={"penalties": {"task_cost_weights": {"medium": 0.09}}},
    )
    resolution = contract["ranking_config_resolution"]
    if mutation == "baseline_hash":
        contract["baseline_expected_ranking_config_sha256"] = "0" * 64
    elif mutation == "base_hash":
        resolution["base_sha256"] = "0" * 64
    elif mutation == "override_hash":
        resolution["override_sha256"] = "0" * 64
    elif mutation == "effective_hash":
        resolution["effective_sha256"] = "0" * 64
    elif mutation == "effective_version":
        resolution["effective_config"]["config_version"] = "tampered"
    elif mutation == "thinking_switch":
        resolution["thinking_assignment_enabled"] = True
    elif mutation == "undeclared_effective_change":
        resolution["effective_config"]["penalties"]["default_cost_weight"] = 0.123
        resolution["effective_sha256"] = module.canonical_sha256(resolution["effective_config"])
        contract["expected_ranking_config_sha256"] = resolution["effective_sha256"]
    elif mutation == "override_not_applied":
        resolution["override"]["penalties"]["task_cost_weights"]["medium"] = 0.11
        resolution["override_sha256"] = module.canonical_sha256(resolution["override"])
        resolution["effective_config"]["config_version"] = (
            f"{resolution['base_config']['config_version']}+override."
            f"{resolution['override_sha256'][:12]}"
        )
        resolution["effective_sha256"] = module.canonical_sha256(resolution["effective_config"])
        contract["expected_ranking_config_version"] = resolution["effective_config"][
            "config_version"
        ]
        contract["expected_ranking_config_sha256"] = resolution["effective_sha256"]
    else:
        resolution["unexpected"] = True

    assert module.g1_ranking_config_identity(contract) is None


def test_g1_plan_is_bound_to_runtime_effective_ranking_identity(module) -> None:
    contract = _runtime_override_ranking_contract(
        module,
        override={"penalties": {"task_cost_weights": {"medium": 0.09}}},
    )
    resolution = contract["ranking_config_resolution"]
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    plan.update(
        {
            "ranking_config_schema_version": contract["expected_ranking_config_schema_version"],
            "ranking_config_version": contract["expected_ranking_config_version"],
            "ranking_config_hash": contract["expected_ranking_config_sha256"],
            "ranking_parameters": deepcopy(resolution["effective_config"]),
        }
    )
    reasons, _, _ = module.g1_registry_plan_reasons(plan, contract=contract)
    assert "invalid_g1_registry_contract" not in reasons
    assert "wrong_g1_ranking_config" not in reasons

    plan["ranking_config_hash"] = module.FORMAL_G1_RANKING_CONFIG_SHA256
    reasons, _, _ = module.g1_registry_plan_reasons(plan, contract=contract)
    assert "wrong_g1_ranking_config" in reasons


def _g1_registry_row(
    model_id: str,
    *,
    vendor: str,
    capability: float,
    aggregator_fit: float,
) -> dict[str, object]:
    return {
        "source": "test_registry",
        "runtime": {"thinking": "off"},
        "registry_facts": {
            "model_id": model_id,
            "version": "test-v1",
            "provider": "openrouter",
            "vendor": vendor,
            "family": model_id,
            "is_open_source": False,
            "is_chinese_model": True,
            "supports_reasoning": True,
            "supports_tools": True,
            "status": "enabled",
            "roles": ["proposer", "aggregator"],
            "context_window": 256_000,
            "effective_context_bucket": "extra_long",
            "modalities": ["text"],
            "tools": [],
            "price": {
                "input_per_million": 1.0,
                "output_per_million": 1.0,
            },
            "latency_p50_ms": 1_000,
            "latency_p95_ms": 2_000,
            "quota": "available",
            "rate_limit": "available",
            "health": "healthy",
            "credential_available": True,
        },
        "static_profile": {
            "capability_dist_prior": {
                "reasoning": capability,
                "code_generation": capability,
                "format_following": capability,
            },
            "domain_dist_prior": {
                "software_engineering": capability,
                "general": capability,
            },
            "tier_dist_prior": {
                "1": capability,
                "2": capability,
                "3": capability,
                "4": capability,
            },
            "role_fit_prior": {
                "proposer": capability,
                "aggregator": aggregator_fit,
            },
        },
        "online_profile": {
            "error_rates": {
                "hallucination": max(0.0, 1.0 - capability),
                "omission": max(0.0, 0.9 - capability),
            }
        },
    }


def _g1_plan(module, proposers: list[str], aggregator: str) -> dict[str, object]:
    from opensquilla.provider.ranking_router import (
        TaskAnalysisResult,
        build_request_context,
        fallback_task_profile,
        rank_models,
    )

    routes = _g1_routes()
    routes_hash = module.canonical_sha256(routes)
    identities = [f"openrouter:{model}" for model in routes]
    version = f"test-registry-v1+test-g1+{routes_hash[:12]}"
    ranking_config = _test_ranking_config(module)
    request_context = build_request_context(
        message="research this",
        turn_metadata={},
        attachments=[],
        candidate_output_tokens=2_000,
        aggregator_output_tokens=2_000,
        ranking_config=ranking_config,
    )
    task_profile = fallback_task_profile(
        routed_tier="c2",
        request_context=request_context,
        ranking_config=ranking_config,
    )
    preference_order = {model: len(proposers) - index for index, model in enumerate(proposers)}
    registry_snapshot = {
        "schema_version": "test-registry",
        "snapshot_version": version,
        "models": [
            _g1_registry_row(
                model,
                vendor=vendor,
                capability=0.82 + 0.04 * preference_order.get(model, 0),
                aggregator_fit=0.99 if model == aggregator else 0.70,
            )
            for model, vendor in routes.items()
        ],
    }
    decision = rank_models(
        task_analysis=TaskAnalysisResult(
            profile=task_profile,
            source="test",
            schema_valid=True,
            confidence=0.9,
            provider_id="openrouter",
            model_id=module.TASK_ANALYZER_MODEL,
        ),
        user_profile=None,
        request_context=request_context,
        registry_snapshot=registry_snapshot,
        routed_tier="c2",
        routing_confidence=0.9,
        ranking_config=ranking_config,
        decision_id="test-g1-decision",
        proposer_recovery_max_additional_calls=3,
        proposer_max_tokens_cap=65_536,
        proposer_visible_answer_reserve_tokens=4_096,
        proposer_recovery_quorum=2,
    )
    plan = deepcopy(decision.trace)
    selected_proposers = [str(identity).partition(":")[2] for identity in plan["selected_P"]]
    selected_aggregator = str(plan["selected_A"]).partition(":")[2]
    plan.update(
        {
            "selection_mode": "router_dynamic",
            "proposer_models": selected_proposers,
            "proposer_sample_count": len(selected_proposers),
            "configured_min_successful_proposers": 2,
            "effective_min_successful_proposers": 2,
            "legal_min_successful_proposers": 2,
            "aggregator_model": selected_aggregator,
            **module.FORMAL_AGGREGATOR_RECOVERY_POLICY,
            "candidate_allowlist": {
                "policy": "exact_openrouter_routes",
                "profile_id": "test-g1",
                "source_registry_snapshot_version": "test-registry-v1",
                "expected_source_registry_snapshot_sha256": (
                    module.FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256
                ),
                "filtered_registry_snapshot_version": version,
                "expected_routes_sha256": routes_hash,
                "expected_candidate_count": len(routes),
                "candidate_count": len(routes),
                "expected_identities": identities,
            },
        }
    )
    return plan


def _c0_proposer_bound_plan(
    *,
    quorum_required: object = None,
) -> dict[str, object]:
    from opensquilla.provider.ranking_router import ranking_config_snapshot

    plan: dict[str, object] = {
        "ranking_parameters": ranking_config_snapshot(),
        "task_profile": {
            "tier_dist": {"1": 1.0},
            "constraints": {
                "cost": "medium",
                "latency": "normal",
                "risk": "low",
            },
        },
    }
    if quorum_required is not None:
        plan["proposer_recovery_policy"] = {
            "quorum_required": quorum_required,
        }
    return plan


def test_g1_recomputed_proposer_bounds_apply_explicit_recovery_quorum(
    module,
) -> None:
    plan = _c0_proposer_bound_plan(quorum_required=2)

    assert module.g1_recomputed_proposer_bounds(plan) == (
        2,
        2,
        ["tier_1", "proposer_recovery_quorum"],
    )


@pytest.mark.parametrize(
    "quorum_required",
    [None, True, 0, "2"],
    ids=["missing", "bool", "zero", "string"],
)
def test_g1_recomputed_proposer_bounds_do_not_apply_invalid_recovery_quorum(
    module,
    quorum_required: object,
) -> None:
    plan = _c0_proposer_bound_plan(quorum_required=quorum_required)

    assert module.g1_recomputed_proposer_bounds(plan) == (
        1,
        1,
        ["tier_1"],
    )


def _formalize_provider_native_call(
    module,
    call: dict[str, object],
    plan: dict[str, object],
    *,
    scope_id: str,
) -> list[tuple[str, str, str]]:
    """Attach one exact provider-owned recovery and physical usage ledger."""

    from opensquilla.provider.protocol import (
        provider_retry_roster_fingerprint,
    )

    call["selection_plan"] = deepcopy(plan)
    selected, slot_reasons = module.expanded_proposer_slot_identities(plan)
    assert slot_reasons == []
    candidates = call["candidates"]
    assert isinstance(candidates, list)
    assert len(candidates) == len(selected)
    physical_units: list[tuple[str, str, str]] = []
    for index, (candidate, identity) in enumerate(zip(candidates, selected, strict=True)):
        assert isinstance(candidate, dict)
        provider, separator, model = identity.partition(":")
        assert separator and provider and model
        physical_id = _physical_attempt_id(f"{scope_id}:proposer:{index}:{identity}")
        candidate.update(
            {
                "index": index,
                "provider": provider,
                "model": model,
                "requested_provider": provider,
                "requested_model": model,
                "usage_reported": True,
                "usage_missing_count": 0,
                "usable_for_aggregation": candidate.get("ok") is True,
                "completion_outcome": ("complete" if candidate.get("ok") is True else "failed"),
                "execution": {
                    "actual_provider": provider,
                    "actual_model": model,
                    "requested_provider": provider,
                    "requested_model": model,
                    "physical_attempts": [
                        {
                            "attempt": 1,
                            "request_started": True,
                            "stream_closed": True,
                            "physical_attempt_id": physical_id,
                            "identity": identity,
                            "outcome": "succeeded",
                        }
                    ],
                },
            }
        )
        physical_units.append((physical_id, model, "proposer"))

    recovery_policy = plan.get("proposer_recovery_policy")
    assert isinstance(recovery_policy, dict)
    complete_proposers_only = plan.get("complete_proposers_only") is True
    quorum_required = int(recovery_policy["quorum_required"])
    max_additional_requests = int(recovery_policy["max_additional_physical_requests"])
    strict_successful = sum(
        module.successful_candidate(candidate)
        and (not complete_proposers_only or candidate.get("completion_outcome") == "complete")
        for candidate in candidates
    )
    usable = sum(module.usable_candidate(candidate) for candidate in candidates)
    call.update(
        {
            "successful_proposers": strict_successful,
            "usable_proposers": usable,
            "partial_proposers": usable - strict_successful,
            "strict_quorum_met": strict_successful >= quorum_required,
            "execution_quorum_required": quorum_required,
            "execution_quorum_met": (
                strict_successful >= quorum_required
                if complete_proposers_only
                else usable >= quorum_required
            ),
        }
    )

    call["proposer_recovery"] = {
        "schema": module.FORMAL_PROPOSER_RECOVERY_SCHEMA,
        "selection_plan_fingerprint": provider_retry_roster_fingerprint(plan),
        "scope": "run_turn",
        "scope_id": scope_id,
        "max_additional_physical_requests": max_additional_requests,
        "external_physical_requests_reserved": 0,
        "additional_physical_requests_started": 0,
        "remaining_additional_physical_requests": max_additional_requests,
        "quorum_required": quorum_required,
        "strict_successful_proposers": strict_successful,
        "usable_proposers": usable,
        "quorum_reached": (
            strict_successful >= quorum_required
            if complete_proposers_only
            else usable >= quorum_required
        ),
        "cumulative_excluded_identities": [],
        "visited_identities": [],
        "executed_proposer_roster_before": list(selected),
        "executed_proposer_roster_after": list(selected),
        "attempts": [],
    }

    recovery = call["aggregator_recovery"]
    assert isinstance(recovery, dict)
    attempts = recovery["attempts"]
    assert isinstance(attempts, list)
    for position, attempt in enumerate(attempts, start=1):
        assert isinstance(attempt, dict)
        if attempt.get("request_started") is not True:
            attempt.pop("physical_attempt_id", None)
            continue
        provider = str(attempt["requested_provider"])
        model = str(attempt["requested_model"])
        physical_id = _physical_attempt_id(f"{scope_id}:aggregator:{position}:{provider}:{model}")
        attempt.update(
            {
                "physical_request_count": 1,
                "physical_attempt_id": physical_id,
                "stream_closed": True,
            }
        )
        physical_units.append((physical_id, model, "aggregator"))

    selected_attempt = recovery["selected_attempt"]
    selected_rows = [
        attempt
        for attempt in attempts
        if isinstance(attempt, dict)
        and attempt.get("attempt") == selected_attempt
        and attempt.get("request_started") is True
        and attempt.get("outcome") == "succeeded"
    ]
    assert len(selected_rows) == 1
    selected_id = str(selected_rows[0]["physical_attempt_id"])
    final_request = call["final_request"]
    assert isinstance(final_request, dict)
    final_usage = final_request["usage"]
    assert isinstance(final_usage, dict)
    final_usage["physical_attempt_id"] = selected_id
    final_usage["provider_usage"] = {
        "physical_attempt_id": selected_id,
    }
    started_count = len(physical_units)
    call["llm_request_count"] = started_count
    call["physical_request_count"] = started_count
    return physical_units


def _row(
    module,
    *,
    group: str,
    task: dict[str, object],
    fingerprint: str,
    response_prefix: str,
    quality: float = 85.0,
    exact: bool = True,
) -> dict[str, object]:
    model = {
        "B0": module.B0_MODEL,
        "B1": "deepseek/deepseek-v4-pro",
        "B2": "z-ai/glm-5.2",
        "B4": module.B4_MODEL,
        "G1": "z-ai/glm-5.2",
    }[group]
    generation_receipt = _receipt(
        f"{response_prefix}-generation",
        model,
        exact=exact,
    )
    generation_units = [generation_receipt]
    if group == "G1":
        generation_units = []
    final_text = f"answer for {group}"
    routing_trace: dict[str, object]
    ensemble_trace: dict[str, object]
    if group in {"B0", "B4"}:
        routing_trace = {"kind": "single", "selection": "fixed", "model": model}
        ensemble_trace = {
            "mode": "agent_loop",
            "agent_iterations": 1,
        }
    elif group == "B1":
        routing_trace = {
            "kind": "router_single",
            "routing_applied": True,
            "routed_model": model,
            "applied_model": model,
        }
        ensemble_trace = {"mode": "agent_loop", "agent_iterations": 1}
    elif group == "B2":
        routing_trace = {
            "kind": "selection_mode",
            "selection_mode": "static_openrouter_b5",
            "selection_plan": {
                "proposer_models": list(module.B2_PROPOSERS),
                "aggregator_model": module.B2_AGGREGATOR,
                "aggregator_candidates": [f"openrouter:{module.B2_AGGREGATOR}"],
            },
        }
        ensemble_trace = _ensemble_trace(
            list(module.B2_PROPOSERS),
            module.B2_AGGREGATOR,
            final_text=final_text,
            selection_mode="static_openrouter_b5",
        )
    else:
        proposers = [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ]
        plan = _g1_plan(module, proposers, model)
        selected_proposers = [str(value) for value in plan["proposer_models"]]
        selected_aggregator = str(plan["aggregator_model"])
        routing_trace = {
            "kind": "selection_mode",
            "selection_mode": "router_dynamic",
            "selection_plan": plan,
        }
        ensemble_trace = _ensemble_trace(
            selected_proposers,
            selected_aggregator,
            final_text=final_text,
            selection_mode="router_dynamic",
        )
        call = ensemble_trace["calls"][0]
        assert isinstance(call, dict)
        call["selection_plan"] = deepcopy(plan)
        recovery = call["aggregator_recovery"]
        assert isinstance(recovery, dict)
        recovery["candidate_ids"] = list(plan["aggregator_candidates"])
        recovery["candidate_count"] = len(plan["aggregator_candidates"])
        physical_units = _formalize_provider_native_call(
            module,
            call,
            plan,
            scope_id=f"{response_prefix}-run-turn",
        )
        analyzer_id = _physical_attempt_id(f"{response_prefix}:task-analyzer")
        analyzer_receipt = _receipt(
            f"{response_prefix}-task-analyzer",
            module.TASK_ANALYZER_MODEL,
            exact=exact,
        )
        analyzer_receipt.update(
            {
                "role": "task_analyzer",
                "physical_attempt_id": analyzer_id,
            }
        )
        analyzer_provider_usage = analyzer_receipt["provider_usage"]
        assert isinstance(analyzer_provider_usage, dict)
        analyzer_provider_usage["physical_attempt_id"] = analyzer_id
        generation_units.append(analyzer_receipt)
        for index, (physical_id, physical_model, role) in enumerate(physical_units):
            receipt = _receipt(
                f"{response_prefix}-generation-{index}",
                physical_model,
                exact=exact,
            )
            receipt.update(
                {
                    "role": role,
                    "physical_attempt_id": physical_id,
                }
            )
            provider_usage = receipt["provider_usage"]
            assert isinstance(provider_usage, dict)
            provider_usage["physical_attempt_id"] = physical_id
            generation_units.append(receipt)
    generation_request_count = len(generation_units)
    generation_cost = sum(float(unit.get("billed_cost") or 0.0) for unit in generation_units)
    rubric = task["rubric"]
    criteria = rubric["sections"][0]["criteria"]
    judgments = []
    for repeat in range(module.JUDGE_REPEATS):
        for criterion_index, criterion in enumerate(criteria):
            judge_attempt_number = (
                1000 + module.GROUPS.index(group) * 100 + repeat * 10 + criterion_index
            )
            met = criterion_index == 0 or quality >= 100
            judge_receipt = _receipt(
                f"{response_prefix}-judge-{repeat}-{criterion_index}",
                module.JUDGE_MODEL,
                exact=exact,
            )
            met_verdict = "MET" if met else "UNMET"
            judge_run = {
                "error": "",
                "llm_request_count": 1,
                "usage": {
                    "model_usage_breakdown": [judge_receipt],
                },
            }
            judgments.append(
                {
                    **criterion,
                    "section_id": rubric["sections"][0]["id"],
                    "section_title": rubric["sections"][0]["title"],
                    "repeat_index": repeat,
                    "verdict": met_verdict,
                    "met": met,
                    "error": "",
                    "judge_run": deepcopy(judge_run),
                    "judge_attempt_evidence_schema": (module.JUDGE_ATTEMPT_EVIDENCE_SCHEMA),
                    "judge_attempt_budget_scope": (module.JUDGE_ATTEMPT_BUDGET_SCOPE),
                    "judge_attempt_budget_limit": 3,
                    "prior_judge_attempts_used": 0,
                    "judge_attempt_count": 1,
                    "judge_attempt_budget_used": 1,
                    "judge_attempt_budget_remaining": 2,
                    "judge_new_attempt_count": 1,
                    "judge_attempt_budget_exhausted": False,
                    "judge_attempts": [
                        {
                            "attempt_id": f"{judge_attempt_number:032x}",
                            "attempt": 1,
                            "verdict": met_verdict,
                            "met": met,
                            "retry_suppressed_reason": "",
                            "run": judge_run,
                        }
                    ],
                }
            )
    row = {
        "task_id": task["id"],
        "group": group,
        "domain": "test",
        "prompt": task["prompt"],
        "prompt_sha256": module.text_sha256(str(task["prompt"])),
        "task_input_sha256": module.canonical_sha256(task, prefix=True),
        "run_compatibility_fingerprint": fingerprint,
        "provider_spec": {"kind": "single", "model": model},
        "routing_trace": routing_trace,
        "runner_mode": "agent_loop",
        "tools_enabled": True,
        "tool_policy": {
            "tool_mode": "local_web_tools",
            "local_web_tools": {
                "web_search": {"provider": "brave"},
                "web_fetch": {"allow_firecrawl": False},
            },
        },
        "generation_policy": {"max_attempts": 3},
        "generation_config": {"max_tokens": 16384, "temperature": 0},
        "started_at": 1_000.0,
        "generation_completed_at": 1_005.0,
        "completed_at": 1_010.0,
        "llm_request_count": generation_request_count,
        "selected_generation_succeeded": True,
        "generation_attempt_count": 1,
        "generation_attempt_evidence_schema": (module.GENERATION_ATTEMPT_EVIDENCE_SCHEMA),
        "generation_attempt_budget_limit": 3,
        "generation_attempt_budget_used": 1,
        "generation_attempt_total_billed_cost": generation_cost,
        "actual_spend_metrics": {
            "generation_attempt_count": 1,
            "total_tool_call_count": 0,
        },
        "error": (None if exact else "openrouter_non_byok_metadata_incomplete"),
        "final_text": final_text,
        "final_text_chars": len(final_text),
        "final_text_sha256": module.text_sha256(final_text),
        "usage": {
            "model_usage_breakdown": generation_units,
            "billed_cost": generation_cost,
        },
        "execution": {
            "run_error": "",
            "prior_generation_attempts_used": 0,
            "generation_attempts": [
                {
                    "attempt_id": (f"{module.GROUPS.index(group) + 1:032x}"),
                    "attempt_kind": "generation",
                    "started_at": 1_000.0,
                    "completed_at": 1_005.0,
                    "attempt": 1,
                    "retry_reason": "",
                    "run": {
                        "error": "",
                        "final_text_sha256": module.text_sha256(final_text),
                        "llm_request_count": generation_request_count,
                        "usage": {
                            "model_usage_breakdown": generation_units,
                        },
                    },
                }
            ],
        },
        "ensemble_trace": ensemble_trace,
        "judge": {
            "mode": "draco_criterion_judgments",
            "rubric_id": rubric["id"],
            "judge_model": module.JUDGE_MODEL,
            "judge_repeats": module.JUDGE_REPEATS,
            "rubric_criteria_count": len(criteria),
            "score_status": "complete",
            "judge_error_count": 0,
            "normalized_score": quality,
            "pass_rate": 50.0,
            "valid_pass_rate": 50.0,
            "criteria_count": len(judgments),
            "valid_criteria_count": len(judgments),
            "invalid_criteria_count": 0,
            "criterion_judgments": judgments,
            "judge_attempt_evidence_schema": (module.JUDGE_ATTEMPT_EVIDENCE_SCHEMA),
            "judge_attempt_budget_scope": module.JUDGE_ATTEMPT_BUDGET_SCOPE,
            "judge_attempt_budget_limit_per_unit": 3,
            "judge_attempt_count": len(judgments),
            "judge_new_attempt_count": len(judgments),
            "judge_attempt_budget_exhausted_count": 0,
            "judge_attempt_budget_exhausted": False,
        },
        "quality_total": quality,
        "completion_status": {
            "generation_accepted": True,
            "judge_complete": True,
            "cost_metadata_complete": exact,
            "status": "complete" if exact else "incomplete",
        },
        "cost_accounting": {
            "selected_generation_attempt": {
                "recorded_cost_usd": generation_cost,
                "request_count": generation_request_count,
                "cost_complete": exact,
                "cost_exact": exact,
            },
            "actual_llm_cost_complete": exact,
            "actual_spend_cost_complete": exact,
        },
        "openrouter_non_byok_audit": {
            "pass": exact,
            "status": "exact" if exact else "metadata_incomplete",
            "policy_safe_to_continue": True,
        },
    }
    return module.seal_result_row(row)


def test_ensemble_gate_allows_only_empty_nonterminal_fallback(module) -> None:
    final_text = "final answer"
    trace = _ensemble_trace(
        list(module.B2_PROPOSERS),
        module.B2_AGGREGATOR,
        final_text=final_text,
        selection_mode="static_openrouter_b5",
    )
    terminal = deepcopy(trace["calls"][0])
    terminal["agent_call_index"] = 2
    fallback = _nonterminal_fallback_call(terminal, successful=2)
    row = {
        "final_text": final_text,
        "routing_trace": {
            "selection_plan": deepcopy(terminal["selection_plan"]),
        },
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 2,
            "untraced_agent_llm_call_count": 0,
            "calls": [fallback, terminal],
        },
    }

    assert (
        module.ensemble_gate(
            row,
            expected_proposers=module.B2_PROPOSERS,
            expected_aggregator=module.B2_AGGREGATOR,
        )
        == []
    )

    visible = deepcopy(row)
    visible["ensemble_trace"]["calls"][0]["final_request"]["output"] = {
        "text": "unsafe text",
        "chars": len("unsafe text"),
        "truncated": False,
    }
    assert "intermediate_fallback_visible_output" in module.ensemble_gate(
        visible,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )

    wrong_model = deepcopy(row)
    fallback_request = wrong_model["ensemble_trace"]["calls"][0]["final_request"]
    fallback_request["usage"]["model"] = "outside/model"
    fallback_request["usage"]["requested_model"] = "outside/model"
    assert "wrong_intermediate_fallback_model" in module.ensemble_gate(
        wrong_model,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )

    conflicting_execution = deepcopy(row)
    conflicting_execution["ensemble_trace"]["calls"][0]["final_request"]["execution"][
        "actual_model"
    ] = "outside/model"
    assert "wrong_intermediate_fallback_model" in module.ensemble_gate(
        conflicting_execution,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )

    boolean_chars = deepcopy(row)
    boolean_chars["ensemble_trace"]["calls"][0]["final_request"]["output"]["chars"] = False
    assert "intermediate_fallback_visible_output" in (
        module.admissible_empty_nonterminal_fallback_reasons(
            boolean_chars["ensemble_trace"]["calls"][0],
            expected_proposers=module.B2_PROPOSERS,
            executed_plan=terminal["selection_plan"],
        )
    )

    terminal_fallback = deepcopy(row)
    terminal_fallback["ensemble_trace"].update(
        {
            "agent_llm_call_count": 1,
            "calls": [deepcopy(fallback)],
        }
    )
    assert "missing_aggregator_recovery_evidence" in module.ensemble_gate(
        terminal_fallback,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )

    prefix = "preface "
    visible_intermediate = deepcopy(row)
    visible_intermediate["final_text"] = prefix + final_text
    first = deepcopy(terminal)
    first["agent_call_index"] = 1
    first["final_request"]["output"] = {
        "text": prefix,
        "chars": len(prefix),
        "truncated": False,
        "sha256": module.text_sha256(prefix),
    }
    first["assembled_output"] = deepcopy(first["final_request"]["output"])
    first["output_components"][0].update(
        {
            "assembled_end": len(prefix),
            "physical_output": deepcopy(first["final_request"]["output"]),
            "assembled_contribution": deepcopy(first["final_request"]["output"]),
            "assembled_prefix_sha256": module.text_sha256(prefix),
        }
    )
    visible_intermediate["ensemble_trace"]["calls"] = [first, terminal]
    assert (
        module.ensemble_gate(
            visible_intermediate,
            expected_proposers=module.B2_PROPOSERS,
            expected_aggregator=module.B2_AGGREGATOR,
        )
        == []
    )
    visible_intermediate["ensemble_trace"]["calls"][0]["assembled_output"]["text"] = "tampered"
    assert "wrong_agent_call_output_binding" in module.ensemble_gate(
        visible_intermediate,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )


def test_nonterminal_fallback_uses_the_executed_recovery_quorum(
    module,
) -> None:
    proposers = [f"model-{index}" for index in range(5)]
    terminal = _ensemble_trace(
        proposers,
        "model-aggregator",
        final_text="final answer",
        selection_mode="router_dynamic",
    )["calls"][0]
    fallback = _nonterminal_fallback_call(terminal, successful=2)
    legacy_plan = terminal["selection_plan"]

    assert (
        "invalid_intermediate_fallback_quorum"
        not in module.admissible_empty_nonterminal_fallback_reasons(
            fallback,
            expected_proposers=proposers,
            executed_plan=legacy_plan,
        )
    )

    formal_plan = deepcopy(legacy_plan)
    formal_plan["proposer_recovery_policy"] = deepcopy(module.FORMAL_PROPOSER_RECOVERY_POLICY)
    assert (
        "invalid_intermediate_fallback_quorum"
        in module.admissible_empty_nonterminal_fallback_reasons(
            fallback,
            expected_proposers=proposers,
            executed_plan=formal_plan,
        )
    )


@pytest.mark.parametrize("fallback_index", [1, 2])
def test_g1_ensemble_gate_accepts_only_frozen_ranked_aggregator_fallbacks(
    module,
    monkeypatch: pytest.MonkeyPatch,
    fallback_index: int,
) -> None:
    monkeypatch.setattr(
        module,
        "FORMAL_G1_RANKING_CONFIG_SHA256",
        module.canonical_sha256(_test_ranking_config(module)),
    )
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    final_text = "ranked fallback answer"
    call = _ranked_aggregator_fallback_call(
        module,
        plan,
        final_text=final_text,
        fallback_index=fallback_index,
    )
    row = {
        "final_text": final_text,
        "routing_trace": {"selection_plan": deepcopy(plan)},
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 1,
            "untraced_agent_llm_call_count": 0,
            "calls": [call],
        },
    }
    contract = _contract(module, "G1", "a" * 64)["g1_registry_contract"]

    reasons, _, primary_model = module.g1_registry_plan_reasons(plan, contract=contract)
    assert reasons == []
    assert primary_model == str(plan["selected_A"]).partition(":")[2]
    assert call["selection_plan"]["selected_A"] == plan["selected_A"]
    assert call["selection_plan"]["aggregator_model"] == plan["aggregator_model"]
    assert (
        module.ensemble_gate(
            row,
            expected_proposers=plan["proposer_models"],
            expected_aggregator=primary_model,
            allowed_models=set(_g1_routes()),
        )
        == []
    )


def test_g1_registry_plan_returns_expanded_proposer_samples_for_k_gt_one(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "FORMAL_G1_RANKING_CONFIG_SHA256",
        module.canonical_sha256(_test_ranking_config(module)),
    )
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    selected = [str(identity) for identity in plan["selected_P"]]
    expanded_models = [
        selected[0].partition(":")[2],
        selected[0].partition(":")[2],
        selected[1].partition(":")[2],
        selected[2].partition(":")[2],
    ]
    plan["proposer_models"] = expanded_models
    plan["proposer_sample_count"] = len(expanded_models)
    contract = _contract(module, "G1", "a" * 64)["g1_registry_contract"]

    reasons, proposer_samples, aggregator = module.g1_registry_plan_reasons(
        plan,
        contract=contract,
    )

    assert reasons == []
    assert proposer_samples == tuple(expanded_models)
    final_text = "expanded proposer samples"
    trace = _ensemble_trace(
        expanded_models,
        aggregator,
        final_text=final_text,
        selection_mode="router_dynamic",
    )
    calls = trace["calls"]
    assert isinstance(calls, list)
    assert isinstance(calls[0], dict)
    recovery = calls[0]["aggregator_recovery"]
    assert isinstance(recovery, dict)
    recovery["candidate_ids"] = list(plan["aggregator_candidates"])
    recovery["candidate_count"] = len(plan["aggregator_candidates"])
    _formalize_provider_native_call(
        module,
        calls[0],
        plan,
        scope_id="expanded-k-slots",
    )
    assert (
        module.ensemble_gate(
            {
                "final_text": final_text,
                "routing_trace": {"selection_plan": deepcopy(plan)},
                "ensemble_trace": trace,
            },
            expected_proposers=proposer_samples,
            expected_aggregator=aggregator,
            allowed_models=set(_g1_routes()),
        )
        == []
    )


@pytest.mark.parametrize(
    ("continuation_count", "expected_reason"),
    [(2, None), (0, "aggregator_continuation_fallback_count_mismatch")],
)
def test_formal_continuation_fallback_contract(
    module,
    continuation_count: int,
    expected_reason: str | None,
) -> None:
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    final_text = "assembled continuation fallback answer"
    call = _ranked_aggregator_fallback_call(
        module,
        plan,
        final_text=final_text,
        fallback_index=1,
        trigger="visible_length_continuations_exhausted",
    )
    recovery = call["aggregator_recovery"]
    recovery["selected_kind"] = "continuation_fallback"
    recovery["continuation_count"] = continuation_count
    recovery["attempts"][0]["visible_output_emitted"] = True
    recovery["attempts"][-1]["kind"] = "continuation_fallback"
    prefix = "assembled "
    remainder = final_text[len(prefix) :]

    def binding(text: str) -> dict[str, object]:
        return {
            "text": text,
            "chars": len(text),
            "truncated": False,
            "sha256": module.text_sha256(text),
        }

    final_request = call["final_request"]
    final_request["output"] = binding(remainder)
    selected_attempt = recovery["selected_attempt"]
    selected_fallback_index = recovery["fallback_index"]
    selected_identity = recovery["candidate_ids"][selected_fallback_index]
    selected_provider, _, selected_model = selected_identity.partition(":")
    call["output_components"] = [
        {
            "attempt": 1,
            "kind": "primary",
            "fallback_index": 0,
            "requested_provider": "openrouter",
            "requested_model": str(plan["aggregator_model"]),
            "assembled_start": 0,
            "assembled_end": len(prefix),
            "physical_output": binding(prefix),
            "assembled_contribution": binding(prefix),
            "assembled_prefix_sha256": module.text_sha256(prefix),
        },
        {
            "attempt": selected_attempt,
            "kind": "continuation_fallback",
            "fallback_index": selected_fallback_index,
            "requested_provider": selected_provider,
            "requested_model": selected_model,
            "assembled_start": len(prefix),
            "assembled_end": len(final_text),
            "physical_output": binding(remainder),
            "assembled_contribution": binding(remainder),
            "assembled_prefix_sha256": module.text_sha256(final_text),
        },
    ]
    row = {
        "final_text": final_text,
        "routing_trace": {"selection_plan": deepcopy(plan)},
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 1,
            "untraced_agent_llm_call_count": 0,
            "calls": [call],
        },
    }

    reasons = module.ensemble_gate(
        row,
        expected_proposers=plan["proposer_models"],
        expected_aggregator=str(plan["aggregator_model"]),
    )
    if expected_reason is None:
        assert reasons == []
    else:
        assert expected_reason in reasons


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("missing_schema", "missing_aggregator_output_binding_schema"),
        ("missing_components", "missing_aggregator_output_components"),
        ("component_gap", "noncontiguous_aggregator_output_components"),
        (
            "wrong_selected_physical",
            "wrong_selected_aggregator_physical_output_binding",
        ),
        (
            "wrong_prefix_hash",
            "wrong_aggregator_output_component_prefix_hash",
        ),
    ],
)
def test_formal_output_binding_fails_closed(
    module,
    mutation: str,
    expected_reason: str,
) -> None:
    final_text = "auditable assembled answer"
    trace = _ensemble_trace(
        list(module.B2_PROPOSERS),
        module.B2_AGGREGATOR,
        final_text=final_text,
        selection_mode="static_openrouter_b5",
    )
    call = trace["calls"][0]
    if mutation == "missing_schema":
        call.pop("output_binding_schema")
    elif mutation == "missing_components":
        call["output_components"] = []
    elif mutation == "component_gap":
        call["output_components"][0]["assembled_start"] = 1
    elif mutation == "wrong_selected_physical":
        call["output_components"][0]["physical_output"]["sha256"] = "f" * 64
    else:
        call["output_components"][0]["assembled_prefix_sha256"] = "f" * 64
    row = {
        "final_text": final_text,
        "routing_trace": {
            "selection_plan": deepcopy(call["selection_plan"]),
        },
        "ensemble_trace": trace,
    }

    reasons = module.ensemble_gate(
        row,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )
    assert expected_reason in reasons


def test_formal_recovery_request_conservation_allows_nested_physical_usage(module) -> None:
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    for field in (
        "backup_P",
        "configured_proposer_backup_count",
        "effective_proposer_backup_count",
        "proposer_recovery_policy",
    ):
        plan.pop(field, None)
    final_text = "nested provider answer"
    call = _ranked_aggregator_fallback_call(
        module,
        plan,
        final_text=final_text,
        fallback_index=1,
    )
    attempts = call["aggregator_recovery"]["attempts"]
    attempts[0]["physical_request_count"] = 2
    attempts[1]["physical_attempt_index"] = 3
    call["llm_request_count"] += 1
    call["physical_request_count"] += 1
    row = {
        "final_text": final_text,
        "routing_trace": {"selection_plan": deepcopy(plan)},
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 1,
            "untraced_agent_llm_call_count": 0,
            "calls": [call],
        },
    }

    assert (
        module.ensemble_gate(
            row,
            expected_proposers=plan["proposer_models"],
            expected_aggregator=str(plan["aggregator_model"]),
        )
        == []
    )


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("unranked_execution", "aggregator_executed_identity_mismatch"),
        ("final_request_identity", "wrong_final_aggregator_usage_requested_identity"),
        ("judge_trigger", "nonstructural_aggregator_fallback_trigger"),
        ("candidate_trace_chain", "aggregator_recovery_candidates_mismatch"),
        ("skip_top2", "aggregator_fallback_skipped_ranked_candidate"),
        ("fallback_single", "final_request_not_aggregator"),
    ],
)
def test_g1_ensemble_gate_rejects_forged_or_semantic_aggregator_fallback(
    module,
    mutation: str,
    expected_reason: str,
) -> None:
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    final_text = "ranked fallback answer"
    call = _ranked_aggregator_fallback_call(
        module,
        plan,
        final_text=final_text,
        fallback_index=2 if mutation == "skip_top2" else 1,
    )
    if mutation == "unranked_execution":
        call["executed_A"] = "openrouter:outside/unranked"
    elif mutation == "final_request_identity":
        call["final_request"]["usage"]["requested_model"] = "outside/unranked"
    elif mutation == "judge_trigger":
        call["fallback_reason"] = "judge_low_score"
        call["aggregator_recovery"]["fallback_reason"] = "judge_low_score"
        for attempt in call["aggregator_recovery"]["attempts"]:
            attempt["trigger"] = "judge_low_score"
    elif mutation == "candidate_trace_chain":
        call["aggregator_recovery"]["candidate_ids"] = list(
            reversed(call["aggregator_recovery"]["candidate_ids"])
        )
    elif mutation == "skip_top2":
        call["aggregator_recovery"]["attempts"] = [
            attempt
            for attempt in call["aggregator_recovery"]["attempts"]
            if attempt["fallback_index"] != 1
        ]
    else:
        call["final_request_role"] = "fallback_single"
        call["final_request"]["role"] = "fallback_single"
    row = {
        "final_text": final_text,
        "routing_trace": {"selection_plan": deepcopy(plan)},
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 1,
            "untraced_agent_llm_call_count": 0,
            "calls": [call],
        },
    }

    reasons = module.ensemble_gate(
        row,
        expected_proposers=plan["proposer_models"],
        expected_aggregator=str(plan["aggregator_model"]),
    )
    assert expected_reason in reasons


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("missing_all_candidates", "missing_aggregator_candidate_chain"),
        ("missing_recovery", "missing_aggregator_recovery_evidence"),
        ("wrong_run_outcome", "aggregator_run_outcome_selected_kind_mismatch"),
        ("duplicate_attempt", "invalid_aggregator_recovery_attempt_sequence"),
        ("selected_unstarted", "ambiguous_aggregator_recovery_selected_attempt"),
        ("proposer_attempt_kind", "invalid_aggregator_recovery_attempt_kind"),
        ("missing_physical_index", "invalid_aggregator_physical_attempt_index"),
        ("request_count_mismatch", "ensemble_request_count_mismatch"),
        ("hidden_proposer_request", "ensemble_physical_request_count_undercounted"),
    ],
)
def test_formal_ensemble_recovery_evidence_fails_closed(
    module,
    mutation: str,
    expected_reason: str,
) -> None:
    final_text = "formal answer"
    trace = _ensemble_trace(
        list(module.B2_PROPOSERS),
        module.B2_AGGREGATOR,
        final_text=final_text,
        selection_mode="static_openrouter_b5",
    )
    call = trace["calls"][0]
    recovery = call["aggregator_recovery"]
    selected_attempt = recovery["attempts"][0]
    if mutation == "missing_all_candidates":
        call["selection_plan"].pop("aggregator_candidates")
        recovery.pop("candidate_ids")
    elif mutation == "missing_recovery":
        call.pop("aggregator_recovery")
    elif mutation == "partial_salvage":
        recovery["selected_kind"] = "partial_salvage"
        recovery["degraded"] = True
        call["delivery_outcome"] = "partial_usable"
        call["run_outcome"] = "length_capped_usable"
    elif mutation == "degraded":
        recovery["degraded"] = True
    elif mutation == "partial_delivery":
        call["delivery_outcome"] = "partial_usable"
    elif mutation == "wrong_run_outcome":
        call["run_outcome"] = "aggregator_recovered"
    elif mutation == "duplicate_attempt":
        recovery["attempts"].append(deepcopy(selected_attempt))
    elif mutation == "selected_unstarted":
        selected_attempt["request_started"] = False
        selected_attempt["physical_attempt_index"] = None
    elif mutation == "proposer_attempt_kind":
        selected_attempt["kind"] = "proposer"
    elif mutation == "missing_physical_index":
        selected_attempt["physical_attempt_index"] = None
    elif mutation == "request_count_mismatch":
        call["physical_request_count"] -= 1
    else:
        call["candidates"][0]["physical_request_count"] += 1
    row = {
        "final_text": final_text,
        "routing_trace": {"selection_plan": deepcopy(call["selection_plan"])},
        "ensemble_trace": trace,
    }

    reasons = module.ensemble_gate(
        row,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )

    assert expected_reason in reasons


@pytest.mark.parametrize(
    "code",
    ["ensemble_aggregator_close_timeout", "incomplete_stream"],
)
def test_provider_shaped_degraded_aggregator_keeps_failed_physical_audit(
    module,
    code: str,
) -> None:
    final_text = "usable degraded answer"
    trace = _ensemble_trace(
        list(module.B2_PROPOSERS),
        module.B2_AGGREGATOR,
        final_text=final_text,
        selection_mode="static_openrouter_b5",
    )
    call = trace["calls"][0]
    recovery = call["aggregator_recovery"]
    recovery.update(
        {
            "selected_kind": "degraded_delivery",
            "degraded": True,
            "success": False,
            "delivery_success": True,
            "delivery_outcome": "degraded_success",
            "audit_outcome": "incomplete",
            "recovery_skipped": True,
            "tools_disabled_after_partial_output": True,
            "terminal_code": code,
            "run_outcome": code,
        }
    )
    recovery["attempts"][0].update(
        {
            "stream_closed": False,
            "outcome": "failed",
            "code": code,
            "delivery_selected": True,
            "visible_output_emitted": True,
        }
    )
    call.update(
        {
            "delivery_outcome": "degraded_success",
            "execution_outcome": "degraded_success",
            "run_outcome": code,
        }
    )
    row = {
        "final_text": final_text,
        "routing_trace": {"selection_plan": deepcopy(call["selection_plan"])},
        "ensemble_trace": trace,
    }

    reasons = module.ensemble_gate(
        row,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )
    blocking, warnings = module.partition_execution_and_audit_reasons(reasons)
    assert blocking == []
    assert "aggregator_recovery_selected_stream_not_closed" in warnings


def test_failed_aggregator_attempt_without_explicit_degraded_binding_is_hard(
    module,
) -> None:
    final_text = "usable but unauthenticated answer"
    trace = _ensemble_trace(
        list(module.B2_PROPOSERS),
        module.B2_AGGREGATOR,
        final_text=final_text,
        selection_mode="static_openrouter_b5",
    )
    call = trace["calls"][0]
    recovery = call["aggregator_recovery"]
    recovery["attempts"][0].update(
        {
            "outcome": "failed",
            "delivery_selected": True,
            "visible_output_emitted": True,
        }
    )
    recovery.update({"selected_kind": "degraded_delivery", "degraded": True})
    call.update(
        {
            "delivery_outcome": "degraded_success",
            "execution_outcome": "degraded_success",
        }
    )

    reasons = module.ensemble_gate(
        {
            "final_text": final_text,
            "routing_trace": {"selection_plan": deepcopy(call["selection_plan"])},
            "ensemble_trace": trace,
        },
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
    )

    assert "ambiguous_aggregator_recovery_selected_attempt" in reasons


def test_static_aggregator_chain_can_be_bound_by_execution_receipt(module) -> None:
    final_text = "formal static answer"
    trace = _ensemble_trace(
        list(module.B2_PROPOSERS),
        module.B2_AGGREGATOR,
        final_text=final_text,
        selection_mode="static_openrouter_b5",
    )
    call = trace["calls"][0]
    call["selection_plan"].pop("aggregator_candidates")
    row = {
        "final_text": final_text,
        "routing_trace": {"selection_plan": deepcopy(call["selection_plan"])},
        "ensemble_trace": trace,
    }

    assert (
        module.ensemble_gate(
            row,
            expected_proposers=module.B2_PROPOSERS,
            expected_aggregator=module.B2_AGGREGATOR,
        )
        == []
    )


def test_agent_call_sequence_hashes_full_segment_when_trace_text_is_clipped(
    module,
) -> None:
    first = "short"
    second = "long-" + ("x" * 9_000)
    calls = [
        {
            "output_binding_schema": module.ENSEMBLE_OUTPUT_BINDING_SCHEMA,
            "assembled_output": {
                "text": first,
                "chars": len(first),
                "truncated": False,
                "sha256": module.text_sha256(first),
            },
        },
        {
            "output_binding_schema": module.ENSEMBLE_OUTPUT_BINDING_SCHEMA,
            "assembled_output": {
                "text": second[:8_000],
                "chars": len(second),
                "truncated": True,
                "sha256": module.text_sha256(second),
            },
        },
    ]

    assert (
        module.agent_call_output_sequence_reasons(
            calls,
            final_text=first + second,
        )
        == []
    )
    calls[1]["assembled_output"]["sha256"] = module.text_sha256(second + "tampered")
    assert module.agent_call_output_sequence_reasons(
        calls,
        final_text=first + second,
    ) == ["wrong_agent_call_output_hash"]


def test_g1_candidate_chain_must_equal_unique_frozen_aggregator_top3(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "FORMAL_G1_RANKING_CONFIG_SHA256",
        module.canonical_sha256(_test_ranking_config(module)),
    )
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    contract = _contract(module, "G1", "a" * 64)["g1_registry_contract"]
    assert module.g1_registry_plan_reasons(plan, contract=contract)[0] == []

    reordered = deepcopy(plan)
    reordered["aggregator_candidates"][1:] = reversed(reordered["aggregator_candidates"][1:])
    reasons, _, _ = module.g1_registry_plan_reasons(reordered, contract=contract)
    assert "wrong_g1_aggregator_candidate_chain" in reasons

    duplicated = deepcopy(plan)
    duplicated["aggregator_candidates"][2] = duplicated["aggregator_candidates"][1]
    reasons, _, _ = module.g1_registry_plan_reasons(duplicated, contract=contract)
    assert "wrong_g1_aggregator_candidate_chain" in reasons

    missing = deepcopy(plan)
    missing.pop("aggregator_candidates")
    for field in module.FORMAL_AGGREGATOR_RECOVERY_POLICY:
        missing.pop(field)
    reasons, _, _ = module.g1_registry_plan_reasons(missing, contract=contract)
    assert "missing_g1_aggregator_recovery_policy" in reasons


def test_g1_plan_uses_authenticated_dynamic_proposer_recovery_policy(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "FORMAL_G1_RANKING_CONFIG_SHA256",
        module.canonical_sha256(_test_ranking_config(module)),
    )
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    contract = _contract(module, "G1", "a" * 64)["g1_registry_contract"]
    expected_policy = {
        **deepcopy(module.FORMAL_PROPOSER_RECOVERY_POLICY),
        "max_additional_physical_requests": 2,
    }

    reasons, _, _ = module.g1_registry_plan_reasons(
        plan,
        contract=contract,
        proposer_recovery_policy=expected_policy,
    )

    assert "wrong_g1_proposer_recovery_policy" in reasons


def test_g1_plan_accepts_effective_proposer_backup_shortfall(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "FORMAL_G1_RANKING_CONFIG_SHA256",
        module.canonical_sha256(_test_ranking_config(module)),
    )
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    plan["backup_P"] = plan["backup_P"][:1]
    plan["effective_proposer_backup_count"] = 1
    plan["proposer_recovery_policy"] = {
        **plan["proposer_recovery_policy"],
        "effective_backup_count": 1,
    }
    contract = _contract(module, "G1", "a" * 64)["g1_registry_contract"]

    reasons, _, _ = module.g1_registry_plan_reasons(plan, contract=contract)

    assert "wrong_g1_proposer_recovery_policy" not in reasons
    assert "wrong_g1_proposer_recovery_roster" not in reasons


def test_g1_route_gate_reuses_one_provider_lifecycle_plan_and_analyzer(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider import ranking_router

    monkeypatch.setattr(
        module,
        "FORMAL_G1_RANKING_CONFIG_SHA256",
        module.canonical_sha256(_test_ranking_config(module)),
    )
    legacy_replay_flags: list[bool] = []

    def replay_reasons(_plan, **kwargs):
        legacy_replay_flags.append(kwargs.get("allow_legacy_managed_v3") is True)
        return []

    monkeypatch.setattr(
        ranking_router,
        "ranking_trace_replay_reasons",
        replay_reasons,
    )
    task = {
        "id": "task-1",
        "prompt": "research this",
        "rubric": {
            "id": "rubric-1",
            "sections": [
                {
                    "id": "quality",
                    "title": "Quality",
                    "criteria": [{"id": "core", "weight": 100, "requirement": "core"}],
                }
            ],
        },
    }
    contract = _contract(module, "G1", "a" * 64)
    row = _row(
        module,
        group="G1",
        task=task,
        fingerprint="fingerprint",
        response_prefix="g1-lifecycle",
    )
    routing = deepcopy(row["routing_trace"])
    plan = deepcopy(routing["selection_plan"])
    original_attempt = row["execution"]["generation_attempts"][0]
    original_units = row["usage"]["model_usage_breakdown"]
    analyzer = deepcopy(original_units[0])
    generation = deepcopy(original_units[1])
    selected_attempt_id = "f" * 32
    row["routing_trace"] = {}
    row["usage"] = {
        "model_usage_breakdown": [generation],
        "billed_cost": generation["billed_cost"],
    }
    row["llm_request_count"] = 1
    row["execution"].update(
        {
            "selected_generation_attempt": 2,
            "generation_attempts": [
                {
                    **deepcopy(original_attempt),
                    "attempt_id": "e" * 32,
                    "attempt": 1,
                    "run": {
                        "error": "retry",
                        "final_text_sha256": module.text_sha256("earlier answer"),
                        "llm_request_count": 1,
                        "routing_trace": routing,
                        "usage": {"model_usage_breakdown": [analyzer]},
                    },
                },
                {
                    "attempt_id": selected_attempt_id,
                    "attempt": 2,
                    "run": {
                        "error": "",
                        "final_text_sha256": row["final_text_sha256"],
                        "llm_request_count": 1,
                        "routing_trace": {},
                        "usage": {"model_usage_breakdown": [generation]},
                    },
                },
            ],
        }
    )
    row["ensemble_trace"]["calls"][0]["selection_plan"] = deepcopy(plan)

    assert module.route_reasons(row, group="G1", contract=contract) == []
    assert legacy_replay_flags and not any(legacy_replay_flags)

    legacy_replay_flags.clear()
    authenticated_contract = deepcopy(contract)
    authenticated_contract["source_identity"] = deepcopy(module.LEGACY_MANAGED_V3_SOURCE_IDENTITY)
    assert (
        module.route_reasons(
            row,
            group="G1",
            contract=authenticated_contract,
        )
        == []
    )
    assert legacy_replay_flags and all(legacy_replay_flags)

    conflicting = deepcopy(row)
    conflicting["ensemble_trace"]["calls"][0]["selection_plan"]["decision_id"] = (
        "conflicting-decision"
    )
    assert "g1_lifecycle_plan_differs_from_physical_plan" in module.route_reasons(
        conflicting,
        group="G1",
        contract=contract,
    )

    missing_analyzer = deepcopy(row)
    missing_analyzer["execution"]["generation_attempts"][0]["run"]["usage"] = {
        "model_usage_breakdown": []
    }
    assert "missing_g1_task_analyzer_request" in (
        module.g1_provider_lifecycle_analyzer_reasons(
            missing_analyzer,
            allow_unknown_placeholder=True,
        )
    )


def test_adaptive_g1_lifecycle_audits_serialized_prior_attempt_trace(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider.ranking_router import (
        ROUTER_DYNAMIC_RETRY_ROUTING_SCHEMA,
        build_router_dynamic_task_analysis_reuse_binding,
    )

    failed_identity = "openrouter:openai/gpt-5.6-sol"
    task_profile = {"task_type": "analysis", "tier_dist": {"2": 1.0}}
    request_context = {
        "schema_version": "test-request-context/v1",
        "task_text": "test prompt",
    }
    request_context["snapshot_hash"] = module.canonical_sha256(request_context)
    analysis_fields = {
        "task_analyzer": {
            "source": "llm_provider",
            "schema_valid": True,
            "fallback_reason": "",
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "usage": {
                "physical_attempts": [
                    {
                        "attempt": 1,
                        "physical_attempt_id": "a" * 32,
                        "provider": "openrouter",
                        "model": "anthropic/claude-opus-4.8",
                        "requested_provider": "openrouter",
                        "requested_model": "anthropic/claude-opus-4.8",
                        "input_tokens": 10,
                        "output_tokens": 1,
                        "reasoning_tokens": 0,
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                        "billed_cost": 0.0,
                        "provider_usage": {
                            "provider": "openrouter",
                            "model": "anthropic/claude-opus-4.8",
                            "physical_attempt_id": "a" * 32,
                        },
                    }
                ],
                "attempt_count": 1,
                "input_tokens": 10,
                "output_tokens": 1,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "billed_cost": 0.0,
            },
        },
        "task_profile": task_profile,
        "task_profile_hash": module.canonical_sha256(task_profile),
        "request_context": request_context,
        "request_context_hash": request_context["snapshot_hash"],
        "routed_tier": "c2",
        "routing_confidence": 0.9,
        "user_profile_enabled": False,
        "user_profile_version": "",
        "user_profile_source": "",
    }
    plan_1 = {
        "decision_id": "decision-1",
        "selected_P": [failed_identity, "openrouter:model-b"],
        "proposer_models": ["openai/gpt-5.6-sol", "model-b"],
        "selected_A": "openrouter:model-a",
        "aggregator_model": "model-a",
    }
    plan_1.update(deepcopy(analysis_fields))
    plan_2 = {
        "decision_id": "decision-2",
        "selected_P": ["openrouter:model-b", "openrouter:model-c"],
        "proposer_models": ["model-b", "model-c"],
        "selected_A": "openrouter:model-a",
        "aggregator_model": "model-a",
        "retry_parent_decision_id": "decision-1",
        "retry_excluded_proposer_identities": [failed_identity],
        "task_analysis_reused": True,
    }
    plan_2.update(deepcopy(analysis_fields))
    plan_2.update(
        {
            "ranking_thinking_assignment_enabled": True,
            "aggregator_candidates": ["openrouter:model-a"],
            "executed_thinking_assignment": {
                "proposers": {identity: "high" for identity in plan_2["selected_P"]},
                "aggregator": "high",
                "thinking_policy_version": "test-thinking-policy/v1",
            },
            "thinking_assignment_details": {
                "proposers": [
                    {
                        "identity": identity,
                        "model_id": identity.partition(":")[2],
                        "role": "proposer",
                        "requested_level": "high",
                        "effective_level": "high",
                        "provider_level": "high",
                        "fallback_reason": "",
                        "reasons": [],
                        "provider_rejection_fallbacks": [
                            {
                                "unified_level": "medium",
                                "provider_level": "medium",
                                "reason": "provider_rejection_fallback",
                            }
                        ],
                    }
                    for identity in plan_2["selected_P"]
                ],
                "aggregator": {
                    "identity": "openrouter:model-a",
                    "model_id": "model-a",
                    "role": "aggregator",
                    "requested_level": "high",
                    "effective_level": "high",
                    "provider_level": "high",
                    "fallback_reason": "",
                    "reasons": [],
                    "provider_rejection_fallbacks": [],
                },
                "aggregator_candidates": [
                    {
                        "identity": "openrouter:model-a",
                        "model_id": "model-a",
                        "role": "aggregator",
                        "requested_level": "high",
                        "effective_level": "high",
                        "provider_level": "high",
                        "fallback_reason": "",
                        "reasons": [],
                        "provider_rejection_fallbacks": [],
                    }
                ],
            },
        }
    )
    plan_1.update(
        {
            "ranking_thinking_assignment_enabled": True,
            "aggregator_candidates": ["openrouter:model-a"],
            "executed_thinking_assignment": {
                "proposers": {identity: "high" for identity in plan_1["selected_P"]},
                "aggregator": "high",
                "thinking_policy_version": "test-thinking-policy/v1",
            },
            "thinking_assignment_details": {
                **deepcopy(plan_2["thinking_assignment_details"]),
                "proposers": [
                    {
                        **deepcopy(plan_2["thinking_assignment_details"]["proposers"][0]),
                        "identity": identity,
                        "model_id": identity.partition(":")[2],
                    }
                    for identity in plan_1["selected_P"]
                ],
            },
        }
    )
    task_analysis_binding = build_router_dynamic_task_analysis_reuse_binding(plan_1)
    plan_2.update(
        {
            "task_analysis_reuse": task_analysis_binding,
            "retry_routing": {
                "schema": ROUTER_DYNAMIC_RETRY_ROUTING_SCHEMA,
                "reason": "prior_attempt_reasoning_only_length",
                "parent_decision_id": "decision-1",
                "excluded_proposer_identities": [failed_identity],
                "task_analysis_reused": True,
                "task_analysis_source_decision_id": "decision-1",
                "task_analysis_reuse_sha256": task_analysis_binding["projection_sha256"],
            },
        }
    )
    failure_trace = {
        "candidates": [
            {
                "ok": False,
                "provider": "openrouter",
                "model": "openai/gpt-5.6-sol",
                "requested_provider": "openrouter",
                "requested_model": "openai/gpt-5.6-sol",
                "request_started": True,
                "physical_request_count": 1,
                "stop_reason": "length",
                "reasoning_tokens": 16_384,
                "effective_thinking_level": "high",
                "provider_thinking_level": "high",
                "execution": {
                    "role": "proposer",
                    "requested_provider": "openrouter",
                    "provider": "openrouter",
                    "requested_model": "openai/gpt-5.6-sol",
                    "model": "openai/gpt-5.6-sol",
                    "assigned_thinking_level": "high",
                    "effective_thinking_level": "high",
                    "provider_thinking_level": "high",
                    "thinking_override": "high",
                    "effective_thinking": True,
                    "effective_provider_thinking_level": "high",
                    "thinking_policy_managed": True,
                    "thinking_fallback_attempts": [],
                },
                "content": {"text": "", "chars": 0},
            }
        ]
    }
    failure_trace["selection_plan"] = deepcopy(plan_1)
    failure_record = module._g1_reasoning_only_length_failures({"ensemble_trace": failure_trace})[0]
    from opensquilla.provider.thinking_execution import (
        project_thinking_execution_history,
    )

    projected_plan_2, projection_audit, projection_reason = project_thinking_execution_history(
        [plan_1], plan_2
    )
    assert projection_reason == ""
    plan_2 = projected_plan_2
    row = {
        "group": "G1",
        "task_id": "task-1",
        "final_text_sha256": "selected-answer",
        "usage": {},
        "routing_trace": {"selection_plan": deepcopy(plan_2)},
        "ensemble_trace": {"selection_plan": deepcopy(plan_2)},
        "execution": {
            "generation_attempts": [
                {
                    "attempt_id": "1" * 32,
                    "attempt": 1,
                    "selection_plan": deepcopy(plan_1),
                    "excluded_proposer_identities": [],
                    "deterministic_proposer_failures": [failure_record],
                    "retry_selection_plan": deepcopy(plan_2),
                    "retry_excluded_proposer_identities": [failed_identity],
                    "thinking_execution_projection": projection_audit,
                    "will_retry": True,
                    "run": {
                        "final_text_sha256": "discarded-answer",
                        "llm_request_count": 1,
                        "setup_usage": [
                            {
                                "role": "task_analyzer",
                                "attempt": 1,
                                "physical_attempt_id": "a" * 32,
                                "provider": "openrouter",
                                "requested_provider": "openrouter",
                                "model": "anthropic/claude-opus-4.8",
                                "requested_model": "anthropic/claude-opus-4.8",
                                "input_tokens": 10,
                                "output_tokens": 1,
                                "reasoning_tokens": 0,
                                "cached_tokens": 0,
                                "cache_write_tokens": 0,
                                "billed_cost": 0.0,
                                "provider_usage": {
                                    "provider": "openrouter",
                                    "model": "anthropic/claude-opus-4.8",
                                    "physical_attempt_id": "a" * 32,
                                },
                                "request_count": 1,
                            }
                        ],
                        "usage": {
                            "model_usage_breakdown": [
                                {
                                    "role": "task_analyzer",
                                    "attempt": 1,
                                    "physical_attempt_id": "a" * 32,
                                    "provider": "openrouter",
                                    "requested_provider": "openrouter",
                                    "model": "anthropic/claude-opus-4.8",
                                    "requested_model": "anthropic/claude-opus-4.8",
                                    "input_tokens": 10,
                                    "output_tokens": 1,
                                    "reasoning_tokens": 0,
                                    "cached_tokens": 0,
                                    "cache_write_tokens": 0,
                                    "billed_cost": 0.0,
                                    "provider_usage": {
                                        "provider": "openrouter",
                                        "model": "anthropic/claude-opus-4.8",
                                        "physical_attempt_id": "a" * 32,
                                    },
                                    "request_count": 1,
                                }
                            ]
                        },
                        "routing_trace": {"selection_plan": deepcopy(plan_1)},
                        "ensemble_trace": failure_trace,
                    },
                },
                {
                    "attempt_id": "2" * 32,
                    "attempt": 2,
                    "selection_plan": deepcopy(plan_2),
                    "excluded_proposer_identities": [failed_identity],
                    "deterministic_proposer_failures": [],
                    "will_retry": False,
                    "run": {
                        "final_text_sha256": "selected-answer",
                        "llm_request_count": 1,
                        "usage": {},
                        "routing_trace": {"selection_plan": deepcopy(plan_2)},
                        "ensemble_trace": {
                            "selection_plan": deepcopy(plan_2),
                            "candidates": [],
                        },
                    },
                },
            ]
        },
    }
    serialized = json.loads(json.dumps(row))
    validated_decisions: list[str] = []

    def fake_plan_reasons(
        plan,
        *,
        contract,
        allow_legacy_managed_v3=False,
        aggregator_recovery_policy=None,
        proposer_recovery_policy=None,
    ):
        del (
            contract,
            allow_legacy_managed_v3,
            aggregator_recovery_policy,
            proposer_recovery_policy,
        )
        decision_id = str(plan.get("decision_id") or "")
        validated_decisions.append(decision_id)
        reasons = ["invalid_prior_g1_plan"] if decision_id == "invalid-prior" else []
        return (
            reasons,
            tuple(plan.get("proposer_models") or []),
            str(plan.get("aggregator_model") or ""),
        )

    monkeypatch.setattr(module, "g1_registry_plan_reasons", fake_plan_reasons)
    routing, evidence, reasons = module.effective_g1_lifecycle_routing(
        serialized,
        contract={"g1_registry_contract": {}},
    )

    assert reasons == []
    assert routing["selection_plan"] == plan_2
    assert evidence["selected_attempt"] == 2
    assert evidence["validated_attempt_plan_count"] == 2
    assert "decision-1" in validated_decisions
    assert module._g1_reasoning_only_length_failure_identities(
        serialized["execution"]["generation_attempts"][0]["run"]
    ) == {failed_identity}

    default_off_retry = deepcopy(serialized)

    def disable_managed_thinking(value):
        if isinstance(value, dict):
            if "decision_id" in value and "selected_P" in value:
                value["ranking_thinking_assignment_enabled"] = False
                for field in (
                    "executed_thinking_assignment",
                    "thinking_execution_fallbacks",
                    "thinking_assignment_details",
                ):
                    value.pop(field, None)
            for child in value.values():
                disable_managed_thinking(child)
        elif isinstance(value, list):
            for child in value:
                disable_managed_thinking(child)

    disable_managed_thinking(default_off_retry)
    default_off_retry["execution"]["generation_attempts"][0].pop("thinking_execution_projection")
    default_off_routing, _, default_off_reasons = module.effective_g1_lifecycle_routing(
        default_off_retry,
        contract={"g1_registry_contract": {}},
    )
    assert default_off_reasons == []
    assert default_off_routing["selection_plan"]["ranking_thinking_assignment_enabled"] is False

    managed_retry_downgrade = deepcopy(serialized)
    managed_retry_downgrade["execution"]["generation_attempts"][0]["retry_selection_plan"][
        "ranking_thinking_assignment_enabled"
    ] = False
    assert any(
        reason.startswith("invalid_g1_thinking_execution_projection:")
        for reason in module.effective_g1_lifecycle_routing(
            managed_retry_downgrade,
            contract={"g1_registry_contract": {}},
        )[2]
    )

    default_off_reuse_tamper = deepcopy(default_off_retry)
    default_off_reuse_tamper["execution"]["generation_attempts"][0]["retry_selection_plan"].pop(
        "task_analysis_reuse"
    )
    assert (
        "missing_g1_retry_task_analysis_binding"
        in module.effective_g1_lifecycle_routing(
            default_off_reuse_tamper,
            contract={"g1_registry_contract": {}},
        )[2]
    )

    default_off_exclusion_tamper = deepcopy(default_off_retry)
    default_off_exclusion_tamper["execution"]["generation_attempts"][1][
        "excluded_proposer_identities"
    ] = []
    assert (
        "wrong_g1_retry_exclusion_evolution"
        in module.effective_g1_lifecycle_routing(
            default_off_exclusion_tamper,
            contract={"g1_registry_contract": {}},
        )[2]
    )

    default_off_physical_plan_tamper = deepcopy(default_off_retry)
    default_off_physical_plan_tamper["execution"]["generation_attempts"][0]["run"][
        "ensemble_trace"
    ]["selection_plan"]["decision_id"] = "default-off-tampered-physical"
    assert (
        "g1_attempt_selection_plan_differs_from_physical_plan"
        in module.effective_g1_lifecycle_routing(
            default_off_physical_plan_tamper,
            contract={"g1_registry_contract": {}},
        )[2]
    )

    fallback_success = deepcopy(serialized)
    physical_fallback_plan = fallback_success["execution"]["generation_attempts"][1]["run"][
        "ensemble_trace"
    ]["selection_plan"]
    fallback_identity = plan_2["selected_P"][0]
    physical_fallback_plan["executed_thinking_assignment"]["proposers"][fallback_identity] = (
        "medium"
    )
    physical_fallback_plan["thinking_execution_fallbacks"] = [
        {
            "trigger_stage": "proposer_execution",
            "fallback_type": "thinking_level_neighbor",
            "reason": "provider_rejected_thinking_level",
            "identity": fallback_identity,
            "requested_thinking_level": "high",
            "rejected_unified_level": "high",
            "rejected_provider_level": "high",
            "effective_thinking_level": "medium",
            "effective_provider_level": "medium",
            "thinking_policy_version": "test-thinking-policy/v1",
            "fallback_result": "succeeded",
        }
    ]
    fallback_success["execution"]["generation_attempts"][1]["run"]["ensemble_trace"][
        "candidates"
    ] = [
        {
            "requested_provider": "openrouter",
            "provider": "openrouter",
            "requested_model": fallback_identity.partition(":")[2],
            "model": fallback_identity.partition(":")[2],
            "ok": True,
            "request_started": True,
            "physical_request_count": 2,
            "effective_thinking_level": "medium",
            "provider_thinking_level": "medium",
            "execution": {
                "role": "proposer",
                "requested_provider": "openrouter",
                "provider": "openrouter",
                "requested_model": fallback_identity.partition(":")[2],
                "model": fallback_identity.partition(":")[2],
                "assigned_thinking_level": "medium",
                "effective_thinking_level": "medium",
                "provider_thinking_level": "medium",
                "thinking_override": "medium",
                "effective_thinking": True,
                "effective_provider_thinking_level": "medium",
                "thinking_policy_managed": True,
                "thinking_fallback_attempts": deepcopy(
                    physical_fallback_plan["thinking_execution_fallbacks"]
                ),
            },
        }
    ]
    assert (
        module.effective_g1_lifecycle_routing(
            fallback_success,
            contract={"g1_registry_contract": {}},
        )[2]
        == []
    )

    cross_attempt_receipt = deepcopy(fallback_success)
    receipt_attempt = cross_attempt_receipt["execution"]["generation_attempts"][1]
    receipt_plan = deepcopy(receipt_attempt["run"]["ensemble_trace"]["selection_plan"])
    # consume_provider_setup() must snapshot this post-call receipt prefix.
    receipt_attempt["run"]["routing_trace"]["selection_plan"] = deepcopy(receipt_plan)
    receipt_attempt["run"]["final_text_sha256"] = "transient-attempt"
    receipt_attempt["run"]["error"] = "HTTP 429"
    receipt_attempt["will_retry"] = True
    final_attempt = deepcopy(receipt_attempt)
    final_attempt.update(
        {
            "attempt_id": "3" * 32,
            "attempt": 3,
            "selection_plan": deepcopy(receipt_plan),
            "deterministic_proposer_failures": [],
            "will_retry": False,
        }
    )
    final_attempt["run"]["error"] = ""
    final_attempt["run"]["final_text_sha256"] = "selected-answer"
    final_attempt["run"]["routing_trace"]["selection_plan"] = deepcopy(receipt_plan)
    final_candidate = final_attempt["run"]["ensemble_trace"]["candidates"][0]
    final_candidate["physical_request_count"] = 1
    final_candidate["execution"]["thinking_fallback_attempts"] = []
    cross_attempt_receipt["execution"]["generation_attempts"].append(final_attempt)
    cross_attempt_receipt["routing_trace"]["selection_plan"] = deepcopy(receipt_plan)
    cross_attempt_receipt["ensemble_trace"] = deepcopy(final_attempt["run"]["ensemble_trace"])
    assert (
        module.effective_g1_lifecycle_routing(
            cross_attempt_receipt,
            contract={"g1_registry_contract": {}},
        )[2]
        == []
    )

    stale_cross_attempt_routing = deepcopy(cross_attempt_receipt)
    stale_cross_attempt_routing["execution"]["generation_attempts"][2]["run"]["routing_trace"][
        "selection_plan"
    ] = deepcopy(plan_2)
    assert (
        "invalid_g1_thinking_execution_plan_mutation"
        in module.effective_g1_lifecycle_routing(
            stale_cross_attempt_routing,
            contract={"g1_registry_contract": {}},
        )[2]
    )

    fallback_tamper = deepcopy(fallback_success)
    fallback_tamper["execution"]["generation_attempts"][1]["run"]["ensemble_trace"][
        "selection_plan"
    ]["thinking_execution_fallbacks"][0]["effective_thinking_level"] = "low"
    assert (
        "invalid_g1_thinking_execution_plan_mutation"
        in module.effective_g1_lifecycle_routing(
            fallback_tamper,
            contract={"g1_registry_contract": {}},
        )[2]
    )

    missing_trace = deepcopy(serialized)
    missing_trace["execution"]["generation_attempts"][0]["run"].pop("ensemble_trace")
    assert (
        "wrong_g1_deterministic_proposer_failure_evidence"
        in module.effective_g1_lifecycle_routing(
            missing_trace,
            contract={"g1_registry_contract": {}},
        )[2]
    )

    invalid_prior = deepcopy(serialized)
    invalid_prior["execution"]["generation_attempts"][0]["selection_plan"]["decision_id"] = (
        "invalid-prior"
    )
    invalid_prior["execution"]["generation_attempts"][0]["run"]["routing_trace"]["selection_plan"][
        "decision_id"
    ] = "invalid-prior"
    assert (
        "invalid_prior_g1_plan"
        in module.effective_g1_lifecycle_routing(
            invalid_prior,
            contract={"g1_registry_contract": {}},
        )[2]
    )

    physical_plan_tamper = deepcopy(serialized)
    physical_plan_tamper["execution"]["generation_attempts"][0]["run"]["ensemble_trace"][
        "selection_plan"
    ]["decision_id"] = "tampered-physical"
    assert (
        "g1_attempt_selection_plan_differs_from_physical_plan"
        in module.effective_g1_lifecycle_routing(
            physical_plan_tamper,
            contract={"g1_registry_contract": {}},
        )[2]
    )

    retry_provenance_tamper = deepcopy(serialized)
    retry_provenance_tamper["execution"]["generation_attempts"][1]["selection_plan"][
        "retry_routing"
    ]["schema"] = "tampered"
    assert (
        "wrong_g1_retry_routing_schema"
        in module.effective_g1_lifecycle_routing(
            retry_provenance_tamper,
            contract={"g1_registry_contract": {}},
        )[2]
    )

    retry_schema_downgrade = deepcopy(serialized)
    retry_schema_downgrade["execution"]["generation_attempts"][1]["selection_plan"][
        "retry_routing"
    ]["schema"] = "opensquilla.router-dynamic-retry-routing/v1"
    retry_schema_downgrade["execution"]["generation_attempts"][1]["selection_plan"].pop(
        "task_analysis_reuse"
    )
    assert (
        "wrong_g1_retry_routing_schema"
        in module.effective_g1_lifecycle_routing(
            retry_schema_downgrade,
            contract={"g1_registry_contract": {}},
        )[2]
    )

    retry_link_tamper = deepcopy(serialized)
    retry_link_tamper["execution"]["generation_attempts"][0]["retry_selection_plan"][
        "decision_id"
    ] = "unlinked-decision"
    assert (
        "g1_retry_selection_plan_not_used_by_next_attempt"
        in module.effective_g1_lifecycle_routing(
            retry_link_tamper,
            contract={"g1_registry_contract": {}},
        )[2]
    )

    current_selected_excluded = deepcopy(serialized)
    current_selected_excluded["execution"]["generation_attempts"][1]["selection_plan"][
        "selected_P"
    ].append(failed_identity)
    assert (
        "g1_retry_selected_excluded_proposer"
        in module.effective_g1_lifecycle_routing(
            current_selected_excluded,
            contract={"g1_registry_contract": {}},
        )[2]
    )

    retry_selection_selected_excluded = deepcopy(serialized)
    retry_selection_selected_excluded["execution"]["generation_attempts"][0][
        "retry_selection_plan"
    ]["selected_P"].append(failed_identity)
    assert (
        "g1_retry_selection_selected_excluded_proposer"
        in module.effective_g1_lifecycle_routing(
            retry_selection_selected_excluded,
            contract={"g1_registry_contract": {}},
        )[2]
    )

    missing_retry_selection = deepcopy(serialized)
    missing_retry_selection["execution"]["generation_attempts"][0].pop("retry_selection_plan")
    missing_retry_selection["execution"]["generation_attempts"][0].pop(
        "retry_excluded_proposer_identities"
    )
    assert (
        "missing_g1_retry_selection_plan"
        in module.effective_g1_lifecycle_routing(
            missing_retry_selection,
            contract={"g1_registry_contract": {}},
        )[2]
    )

    immutable_payload = module.immutable_attempt_payload(
        serialized["execution"]["generation_attempts"][0]
    )
    tampered_attempt = deepcopy(serialized["execution"]["generation_attempts"][0])
    tampered_attempt["deterministic_proposer_failures"][0]["reasoning_tokens"] += 1
    assert module.immutable_attempt_payload(tampered_attempt) != immutable_payload

    physical_plan_tampered_attempt = deepcopy(serialized["execution"]["generation_attempts"][0])
    physical_plan_tampered_attempt["run"]["ensemble_trace"]["selection_plan"]["decision_id"] = (
        "cross-wave-physical-plan-tamper"
    )
    assert module.immutable_attempt_payload(physical_plan_tampered_attempt) != immutable_payload

    paid_attempt_contracts = {"G1": _contract(module, "G1", "a" * 64)}
    source_record = module.SourceRecord(
        path=Path("wave-1.jsonl"),
        source_index=0,
        line=1,
        row=serialized,
    )
    module.validate_g1_paid_attempt_plan_history(
        [source_record],
        contracts=paid_attempt_contracts,
    )

    dynamic_cap_history = deepcopy(serialized)

    def replace_aggregator_cap(value: object) -> None:
        if isinstance(value, dict):
            if "aggregator_max_tokens_cap" in value:
                value["aggregator_max_tokens_cap"] = 32_768
            for child in value.values():
                replace_aggregator_cap(child)
        elif isinstance(value, list):
            for child in value:
                replace_aggregator_cap(child)

    replace_aggregator_cap(dynamic_cap_history)
    dynamic_cap_contract = deepcopy(paid_attempt_contracts["G1"])
    dynamic_cap_contract["formal_runtime_freeze"]["aggregator_max_tokens_cap"] = 32_768
    dynamic_cap_contract["gateway_execution"]["llm_ensemble"]["aggregator_max_tokens_cap"] = 32_768
    module.validate_g1_paid_attempt_plan_history(
        [
            module.SourceRecord(
                path=Path("dynamic-cap-wave-1.jsonl"),
                source_index=0,
                line=1,
                row=dynamic_cap_history,
            )
        ],
        contracts={"G1": dynamic_cap_contract},
    )

    duplicate_cross_task_analyzer = deepcopy(serialized)
    duplicate_cross_task_analyzer["task_id"] = "task-2"
    with pytest.raises(
        module.FinalizationError,
        match="invalid paid G1 attempt",
    ):
        module.validate_g1_paid_attempt_plan_history(
            [
                source_record,
                module.SourceRecord(
                    path=Path("cross-task-duplicate-analyzer.jsonl"),
                    source_index=0,
                    line=2,
                    row=duplicate_cross_task_analyzer,
                ),
            ],
            contracts=paid_attempt_contracts,
        )

    pre_call_guard_then_success = deepcopy(serialized)
    guarded_attempt = pre_call_guard_then_success["execution"]["generation_attempts"][0]
    guarded_attempt["attempt_kind"] = "provider_build_after_paid_setup"
    guarded_attempt["run"]["trace_events"] = [
        {
            "kind": "error",
            "code": "g1_pre_call_guard_failed",
            "request_started": False,
            "physical_request_count": 0,
        }
    ]
    with pytest.raises(
        module.FinalizationError,
        match="g1_pre_call_guard_failed",
    ):
        module.validate_g1_paid_attempt_plan_history(
            [
                module.SourceRecord(
                    path=Path("pre-call-guard-then-success.jsonl"),
                    source_index=0,
                    line=1,
                    row=pre_call_guard_then_success,
                )
            ],
            contracts=paid_attempt_contracts,
        )

    analyzer_retry = deepcopy(serialized)
    analyzer_retry_run = analyzer_retry["execution"]["generation_attempts"][0]["run"]
    analyzer_retry_rows = [
        {
            "role": "task_analyzer",
            "attempt": attempt,
            "physical_attempt_id": str(attempt) * 32,
            "provider": "openrouter",
            "requested_provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "requested_model": "anthropic/claude-opus-4.8",
            "input_tokens": 10,
            "output_tokens": 1,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "billed_cost": 0.0,
            "provider_usage": {
                "provider": "openrouter",
                "model": "anthropic/claude-opus-4.8",
                "physical_attempt_id": str(attempt) * 32,
            },
            "request_count": 1,
        }
        for attempt in (1, 2)
    ]
    analyzer_retry_run["llm_request_count"] = 2
    analyzer_retry_run["setup_usage"] = deepcopy(analyzer_retry_rows)
    analyzer_retry_run["usage"] = {"model_usage_breakdown": deepcopy(analyzer_retry_rows)}

    def replace_analyzer_trace_usage(value: object) -> None:
        if isinstance(value, dict):
            analyzer_trace = value.get("task_analyzer")
            if isinstance(analyzer_trace, dict):
                analyzer_trace["usage"] = {
                    "physical_attempts": [
                        {
                            "attempt": row["attempt"],
                            "physical_attempt_id": row[
                                "physical_attempt_id"
                            ],
                            "requested_provider": row[
                                "requested_provider"
                            ],
                            "requested_model": row["requested_model"],
                            "provider": row["provider"],
                            "model": row["model"],
                            "input_tokens": row["input_tokens"],
                            "output_tokens": row["output_tokens"],
                            "reasoning_tokens": row["reasoning_tokens"],
                            "cached_tokens": row["cached_tokens"],
                            "cache_write_tokens": row["cache_write_tokens"],
                            "billed_cost": row["billed_cost"],
                            "provider_usage": deepcopy(row["provider_usage"]),
                        }
                        for row in analyzer_retry_rows
                    ],
                    "attempt_count": len(analyzer_retry_rows),
                }
            for child in value.values():
                replace_analyzer_trace_usage(child)
        elif isinstance(value, list):
            for child in value:
                replace_analyzer_trace_usage(child)

    replace_analyzer_trace_usage(analyzer_retry)
    analyzer_retry_attempts = analyzer_retry["execution"][
        "generation_attempts"
    ]
    analyzer_retry_source_plan = analyzer_retry_attempts[0][
        "selection_plan"
    ]
    analyzer_retry_target_plan = deepcopy(
        analyzer_retry_attempts[1]["selection_plan"]
    )
    analyzer_retry_binding = build_router_dynamic_task_analysis_reuse_binding(
        analyzer_retry_source_plan
    )
    analyzer_retry_target_plan["task_analysis_reuse"] = (
        analyzer_retry_binding
    )
    analyzer_retry_target_plan["retry_routing"].update(
        {
            "task_analysis_source_decision_id": analyzer_retry_source_plan[
                "decision_id"
            ],
            "task_analysis_reuse_sha256": analyzer_retry_binding[
                "projection_sha256"
            ],
        }
    )
    (
        analyzer_retry_target_plan,
        analyzer_retry_projection,
        analyzer_retry_projection_reason,
    ) = project_thinking_execution_history(
        [analyzer_retry_source_plan],
        analyzer_retry_target_plan,
    )
    assert analyzer_retry_projection_reason == ""
    analyzer_retry_attempts[0]["retry_selection_plan"] = deepcopy(
        analyzer_retry_target_plan
    )
    analyzer_retry_attempts[0]["thinking_execution_projection"] = deepcopy(
        analyzer_retry_projection
    )
    analyzer_retry_attempts[1]["selection_plan"] = deepcopy(
        analyzer_retry_target_plan
    )
    analyzer_retry_attempts[1]["run"]["routing_trace"][
        "selection_plan"
    ] = deepcopy(analyzer_retry_target_plan)
    analyzer_retry_attempts[1]["run"]["ensemble_trace"][
        "selection_plan"
    ] = deepcopy(analyzer_retry_target_plan)
    analyzer_retry["routing_trace"]["selection_plan"] = deepcopy(
        analyzer_retry_target_plan
    )
    analyzer_retry["ensemble_trace"]["selection_plan"] = deepcopy(
        analyzer_retry_target_plan
    )
    module.validate_g1_paid_attempt_plan_history(
        [
            module.SourceRecord(
                path=Path("analyzer-internal-retry.jsonl"),
                source_index=0,
                line=1,
                row=analyzer_retry,
            )
        ],
        contracts=paid_attempt_contracts,
    )
    reconciled_analyzers = module._canonical_task_analyzer_setup_units(
        analyzer_retry_run,
        identity_seed="legal-duplicate-mirror",
    )
    assert [row["physical_attempt_id"] for row in reconciled_analyzers] == ["1" * 32, "2" * 32]

    setup_one_usage_five = deepcopy(analyzer_retry_run)
    five_analyzers = [
        {
            **deepcopy(analyzer_retry_rows[0]),
            "attempt": attempt,
            "physical_attempt_id": f"{attempt:x}" * 32,
            "provider_usage": {
                **deepcopy(analyzer_retry_rows[0]["provider_usage"]),
                "physical_attempt_id": f"{attempt:x}" * 32,
            },
        }
        for attempt in range(1, 6)
    ]
    setup_one_usage_five["setup_usage"] = [deepcopy(five_analyzers[0])]
    setup_one_usage_five["usage"] = {"model_usage_breakdown": deepcopy(five_analyzers)}
    reconciled_five = module._canonical_task_analyzer_setup_units(
        setup_one_usage_five,
        identity_seed="setup-one-usage-five",
    )
    assert {row["physical_attempt_id"] for row in reconciled_five} == {
        f"{attempt:x}" * 32 for attempt in range(1, 6)
    }

    usage_only_extra = deepcopy(analyzer_retry_run)
    usage_only_extra["setup_usage"] = [deepcopy(analyzer_retry_rows[0])]
    usage_only_extra["usage"] = {
        "model_usage_breakdown": [
            deepcopy(analyzer_retry_rows[0]),
            deepcopy(analyzer_retry_rows[1]),
        ]
    }
    reconciled_usage_extra = module._canonical_task_analyzer_setup_units(
        usage_only_extra,
        identity_seed="usage-only-extra",
    )
    assert [row["physical_attempt_id"] for row in reconciled_usage_extra] == ["1" * 32, "2" * 32]

    analyzer_attempt = analyzer_retry["execution"]["generation_attempts"][0]
    analyzer_immutable = module.immutable_attempt_payload(analyzer_attempt)
    changed_analyzer_identity = deepcopy(analyzer_attempt)
    changed_analyzer_run = changed_analyzer_identity["run"]
    changed_analyzer_run["setup_usage"][0]["physical_attempt_id"] = "f" * 32
    changed_analyzer_run["setup_usage"][0]["provider_usage"][
        "physical_attempt_id"
    ] = "f" * 32
    changed_analyzer_run["usage"]["model_usage_breakdown"][0]["physical_attempt_id"] = "f" * 32
    changed_analyzer_run["usage"]["model_usage_breakdown"][0]["provider_usage"][
        "physical_attempt_id"
    ] = "f" * 32
    assert module.immutable_attempt_payload(changed_analyzer_identity) != analyzer_immutable

    wave_1 = deepcopy(serialized)
    wave_1["final_text_sha256"] = "wave-1-not-selected"
    wave_1["execution"]["generation_attempts"] = [
        deepcopy(serialized["execution"]["generation_attempts"][0])
    ]
    wave_2 = deepcopy(serialized)
    wave_2["execution"]["generation_attempts"] = [
        deepcopy(serialized["execution"]["generation_attempts"][1])
    ]
    cross_wave_records = [
        module.SourceRecord(
            path=Path("wave-1.jsonl"),
            source_index=0,
            line=1,
            row=wave_1,
        ),
        module.SourceRecord(
            path=Path("wave-2.jsonl"),
            source_index=1,
            line=1,
            row=wave_2,
        ),
    ]
    module.validate_g1_paid_attempt_plan_history(
        cross_wave_records,
        contracts=paid_attempt_contracts,
    )

    duplicate_analyzer_wave_2 = deepcopy(wave_2)
    duplicate_analyzer_run = duplicate_analyzer_wave_2["execution"]["generation_attempts"][0]["run"]
    duplicate_analyzer_run["setup_usage"] = [
        {
            "role": "task_analyzer",
            "attempt": 1,
            "physical_attempt_id": "a" * 32,
            "provider": "openrouter",
            "requested_provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "requested_model": "anthropic/claude-opus-4.8",
            "request_count": 1,
        }
    ]
    duplicate_analyzer_run["usage"] = {
        "model_usage_breakdown": deepcopy(duplicate_analyzer_run["setup_usage"])
    }
    with pytest.raises(
        module.FinalizationError,
        match="invalid paid G1 attempt",
    ):
        module.validate_g1_paid_attempt_plan_history(
            [
                cross_wave_records[0],
                module.SourceRecord(
                    path=Path("wave-2-duplicate-analyzer.jsonl"),
                    source_index=1,
                    line=1,
                    row=duplicate_analyzer_wave_2,
                ),
            ],
            contracts=paid_attempt_contracts,
        )

    reset_wave_2 = deepcopy(wave_2)
    reset_attempt = reset_wave_2["execution"]["generation_attempts"][0]
    reset_plan = deepcopy(plan_1)
    reset_plan["decision_id"] = "fresh-cross-wave-reset"
    reset_attempt["selection_plan"] = deepcopy(reset_plan)
    reset_attempt["excluded_proposer_identities"] = []
    reset_attempt["run"]["routing_trace"]["selection_plan"] = deepcopy(reset_plan)
    reset_attempt["run"]["ensemble_trace"]["selection_plan"] = deepcopy(reset_plan)
    with pytest.raises(
        module.FinalizationError,
        match="invalid paid G1 attempt",
    ):
        module.validate_g1_paid_attempt_plan_history(
            [
                cross_wave_records[0],
                module.SourceRecord(
                    path=Path("wave-2-reset.jsonl"),
                    source_index=1,
                    line=1,
                    row=reset_wave_2,
                ),
            ],
            contracts=paid_attempt_contracts,
        )

    paid_then_zero_request = deepcopy(serialized)
    paid_then_zero_request["final_text_sha256"] = "not-selected-from-this-wave"
    first_run = paid_then_zero_request["execution"]["generation_attempts"][0]["run"]
    first_run.pop("llm_request_count")
    first_run["usage"] = {
        "model_usage_breakdown": [
            {
                "role": "proposer",
                "provider": "openrouter",
                "model": "openai/gpt-5.6-sol",
            }
        ]
    }
    zero_request_run = paid_then_zero_request["execution"]["generation_attempts"][1]["run"]
    zero_request_run.update(
        {
            "llm_request_count": 0,
            "request_started": False,
            "final_text_sha256": "",
        }
    )
    zero_request_run.pop("ensemble_trace")
    module.validate_g1_paid_attempt_plan_history(
        [
            module.SourceRecord(
                path=Path("paid-then-zero-wave.jsonl"),
                source_index=0,
                line=1,
                row=paid_then_zero_request,
            )
        ],
        contracts=paid_attempt_contracts,
    )

    analyzer_only_run = {
        "llm_request_count": 1,
        "setup_usage": [
            {
                "role": "task_analyzer",
                "provider": "openrouter",
                "model": "anthropic/claude-opus-4.8",
                "request_count": 1,
            }
        ],
        "usage": {
            "model_usage_breakdown": [
                {
                    "role": "task_analyzer",
                    "provider": "openrouter",
                    "model": "anthropic/claude-opus-4.8",
                    "request_count": 1,
                }
            ]
        },
    }
    assert module.run_expected_ensemble_request_count(analyzer_only_run) == 0

    unknown_analyzer = {
        "role": "unknown_request",
        "label": "task_analyzer",
        "attempt": 1,
        "physical_attempt_id": "a" * 32,
        "provider": "",
        "model": "",
        "requested_provider": "openrouter",
        "requested_model": "anthropic/claude-opus-4.8",
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "unavailable",
        "request_count": 1,
        "provider_usage": {
            "usage_unknown": True,
            "physical_attempt_id": "a" * 32,
        },
    }
    unknown_analyzer_run = {
        "llm_request_count": 1,
        "usage_unknown_count": 1,
        "setup_usage": [deepcopy(unknown_analyzer)],
        "usage": {"model_usage_breakdown": [deepcopy(unknown_analyzer)]},
    }
    assert module.run_expected_ensemble_request_count(unknown_analyzer_run) == 0
    unknown_then_generation = deepcopy(unknown_analyzer_run)
    unknown_then_generation["llm_request_count"] = 2
    unknown_then_generation["usage"]["model_usage_breakdown"].append(
        {
            "role": "proposer",
            "provider": "openrouter",
            "model": "openai/gpt-5.6-sol",
            "requested_provider": "openrouter",
            "requested_model": "openai/gpt-5.6-sol",
            "request_count": 1,
        }
    )
    assert module.run_expected_ensemble_request_count(unknown_then_generation) == 1

    paid_history_tamper = deepcopy(serialized)
    paid_history_tamper["execution"]["generation_attempts"][0]["run"]["ensemble_trace"][
        "selection_plan"
    ]["decision_id"] = "tampered-paid-history"
    with pytest.raises(
        module.FinalizationError,
        match="invalid paid G1 attempt",
    ):
        module.validate_g1_paid_attempt_plan_history(
            [
                module.SourceRecord(
                    path=Path("failed-wave.jsonl"),
                    source_index=0,
                    line=1,
                    row=paid_history_tamper,
                )
            ],
            contracts=paid_attempt_contracts,
        )

    adaptive_downgrade = deepcopy(serialized)
    for attempt in adaptive_downgrade["execution"]["generation_attempts"]:
        for field in (
            "selection_plan",
            "excluded_proposer_identities",
            "deterministic_proposer_failures",
            "retry_selection_plan",
            "retry_excluded_proposer_identities",
        ):
            attempt.pop(field, None)
    with pytest.raises(
        module.FinalizationError,
        match="invalid paid G1 attempt",
    ):
        module.validate_g1_paid_attempt_plan_history(
            [
                module.SourceRecord(
                    path=Path("adaptive-downgrade.jsonl"),
                    source_index=0,
                    line=1,
                    row=adaptive_downgrade,
                )
            ],
            contracts=paid_attempt_contracts,
        )

    v3_downgrade = deepcopy(serialized)
    for attempt in v3_downgrade["execution"]["generation_attempts"]:
        for field in (
            "selection_plan",
            "excluded_proposer_identities",
            "deterministic_proposer_failures",
            "retry_selection_plan",
            "retry_excluded_proposer_identities",
        ):
            attempt.pop(field, None)
        run = attempt["run"]
        runtime_plans = [
            run["routing_trace"]["selection_plan"],
            *[
                call["selection_plan"]
                for call in module.ensemble_call_trace_sequence(run["ensemble_trace"])[0]
            ],
        ]
        for runtime_plan in runtime_plans:
            runtime_plan["ranking_version"] = module.LEGACY_THINKING_RANKING_VERSION
            runtime_plan.pop(
                "thinking_physical_evidence_schema",
                None,
            )
    with pytest.raises(
        module.FinalizationError,
        match="invalid paid G1 attempt",
    ):
        module.validate_g1_paid_attempt_plan_history(
            [
                module.SourceRecord(
                    path=Path("unauthenticated-v3-downgrade.jsonl"),
                    source_index=0,
                    line=1,
                    row=v3_downgrade,
                )
            ],
            contracts=paid_attempt_contracts,
        )

    authenticated_contract = deepcopy(paid_attempt_contracts["G1"])
    authenticated_contract["source_identity"] = deepcopy(module.LEGACY_MANAGED_V3_SOURCE_IDENTITY)
    module.validate_g1_paid_attempt_plan_history(
        [
            module.SourceRecord(
                path=Path("authenticated-legacy-v3-managed-history.jsonl"),
                source_index=0,
                line=1,
                row=v3_downgrade,
            )
        ],
        contracts={"G1": authenticated_contract},
    )

    forged_contract = deepcopy(authenticated_contract)
    forged_contract["source_identity"]["source_tree_sha256"] = "0" * 64
    with pytest.raises(
        module.FinalizationError,
        match="invalid paid G1 attempt",
    ):
        module.validate_g1_paid_attempt_plan_history(
            [
                module.SourceRecord(
                    path=Path("forged-v3-source.jsonl"),
                    source_index=0,
                    line=1,
                    row=v3_downgrade,
                )
            ],
            contracts={"G1": forged_contract},
        )

    paid_setup_build_failure = deepcopy(serialized)
    paid_setup_attempt = paid_setup_build_failure["execution"]["generation_attempts"][0]
    paid_setup_attempt["attempt_kind"] = "provider_build_after_paid_setup"
    for field in (
        "selection_plan",
        "excluded_proposer_identities",
        "deterministic_proposer_failures",
        "retry_selection_plan",
        "retry_excluded_proposer_identities",
    ):
        paid_setup_attempt.pop(field, None)
    paid_setup_attempt["run"].pop("routing_trace", None)
    paid_setup_attempt["run"].pop("ensemble_trace", None)
    paid_setup_build_failure["execution"]["generation_attempts"] = [paid_setup_attempt]
    with pytest.raises(
        module.FinalizationError,
        match="invalid paid G1 attempt",
    ):
        module.validate_g1_paid_attempt_plan_history(
            [
                module.SourceRecord(
                    path=Path("paid-setup-build-failure.jsonl"),
                    source_index=0,
                    line=1,
                    row=paid_setup_build_failure,
                )
            ],
            contracts=paid_attempt_contracts,
        )

    early_best = deepcopy(serialized)
    early_best["final_text_sha256"] = "discarded-answer"
    early_best["usage"] = deepcopy(
        early_best["execution"]["generation_attempts"][0]["run"]["usage"]
    )
    early_best["routing_trace"]["selection_plan"] = deepcopy(plan_1)
    early_best["ensemble_trace"]["selection_plan"] = deepcopy(plan_1)
    early_best["execution"]["generation_attempts"].append(
        {
            "attempt_id": "3" * 32,
            "attempt": 3,
            "selection_plan": deepcopy(plan_2),
            "excluded_proposer_identities": [failed_identity],
            "deterministic_proposer_failures": [],
            "will_retry": False,
            "run": {
                "final_text_sha256": "last-failed-answer",
                "llm_request_count": 1,
                "usage": {},
                "routing_trace": {"selection_plan": deepcopy(plan_2)},
                "ensemble_trace": {
                    "selection_plan": deepcopy(plan_2),
                    "candidates": [],
                },
            },
        }
    )
    _, early_evidence, early_reasons = module.effective_g1_lifecycle_routing(
        early_best,
        contract={"g1_registry_contract": {}},
    )
    assert early_reasons == []
    assert early_evidence["selected_attempt"] == 1
    assert early_evidence["validated_attempt_plan_count"] == 3

    tampered_later_attempt = deepcopy(early_best)
    tampered_later_attempt["execution"]["generation_attempts"][2][
        "excluded_proposer_identities"
    ] = []
    assert (
        "wrong_g1_retry_exclusion_evolution"
        in module.effective_g1_lifecycle_routing(
            tampered_later_attempt,
            contract={"g1_registry_contract": {}},
        )[2]
    )


@pytest.mark.parametrize(
    "tamper_field",
    [
        "task_analyzer",
        "task_profile",
        "task_profile_hash",
        "request_context",
        "request_context_hash",
        "routed_tier",
        "routing_confidence",
        "user_profile_enabled",
        "user_profile_version",
        "user_profile_source",
        "binding_hash",
        "source_decision",
    ],
)
def test_finalizer_v2_retry_replay_rejects_task_analysis_tamper(
    module,
    tamper_field: str,
) -> None:
    from opensquilla.provider.ranking_router import (
        ROUTER_DYNAMIC_RETRY_ROUTING_SCHEMA,
        build_router_dynamic_task_analysis_reuse_binding,
    )

    failed_identity = "openrouter:openai/gpt-5.6-sol"
    task_profile = {"task_type": "analysis", "tier_dist": {"2": 1.0}}
    request_context = {
        "schema_version": "test-request-context/v1",
        "task_text": "test prompt",
    }
    request_context["snapshot_hash"] = module.canonical_sha256(request_context)
    source_plan = {
        "decision_id": "decision-1",
        "task_analyzer": {
            "source": "test_analyzer",
            "schema_valid": True,
        },
        "task_profile": task_profile,
        "task_profile_hash": module.canonical_sha256(task_profile),
        "request_context": request_context,
        "request_context_hash": request_context["snapshot_hash"],
        "routed_tier": "c2",
        "routing_confidence": 0.9,
        "user_profile_enabled": False,
        "user_profile_version": "",
        "user_profile_source": "",
    }
    retry_plan = deepcopy(source_plan)
    retry_plan["decision_id"] = "decision-2"
    binding = build_router_dynamic_task_analysis_reuse_binding(source_plan)
    retry_plan.update(
        {
            "retry_parent_decision_id": "decision-1",
            "retry_excluded_proposer_identities": [failed_identity],
            "task_analysis_reused": True,
            "task_analysis_reuse": binding,
            "retry_routing": {
                "schema": ROUTER_DYNAMIC_RETRY_ROUTING_SCHEMA,
                "reason": "prior_attempt_reasoning_only_length",
                "parent_decision_id": "decision-1",
                "excluded_proposer_identities": [failed_identity],
                "task_analysis_reused": True,
                "task_analysis_source_decision_id": "decision-1",
                "task_analysis_reuse_sha256": binding["projection_sha256"],
            },
        }
    )
    assert (
        module._g1_retry_plan_provenance_reasons(
            retry_plan,
            initial_plan=source_plan,
            initial_decision_id="decision-1",
            exclusions={failed_identity},
        )
        == []
    )

    tampered = deepcopy(retry_plan)
    if tamper_field == "task_analyzer":
        tampered["task_analyzer"]["source"] = "tampered"
    elif tamper_field == "task_profile":
        tampered["task_profile"]["task_type"] = "tampered"
    elif tamper_field == "request_context":
        tampered["request_context"]["task_text"] = "tampered"
    elif tamper_field == "binding_hash":
        tampered["task_analysis_reuse"]["projection_sha256"] = "0" * 64
        tampered["retry_routing"]["task_analysis_reuse_sha256"] = "0" * 64
    elif tamper_field == "source_decision":
        tampered["task_analysis_reuse"]["source_decision_id"] = "tampered"
    elif tamper_field == "user_profile_enabled":
        tampered[tamper_field] = True
    elif tamper_field == "routing_confidence":
        tampered[tamper_field] = 0.1
    else:
        tampered[tamper_field] = "tampered"

    assert module._g1_retry_plan_provenance_reasons(
        tampered,
        initial_plan=source_plan,
        initial_decision_id="decision-1",
        exclusions={failed_identity},
    )


def test_legacy_b2_attempt_error_requires_terminal_reclassification_proof(
    module,
    tmp_path: Path,
) -> None:
    task = {
        "id": "task-1",
        "prompt": "research this",
        "rubric": {
            "id": "rubric-1",
            "sections": [
                {
                    "id": "quality",
                    "title": "Quality",
                    "criteria": [{"id": "core", "weight": 100, "requirement": "core"}],
                }
            ],
        },
    }
    row = _row(
        module,
        group="B2",
        task=task,
        fingerprint="fingerprint",
        response_prefix="b2-reclassified",
    )
    terminal = deepcopy(row["ensemble_trace"]["calls"][0])
    terminal["agent_call_index"] = 2
    fallback = _nonterminal_fallback_call(terminal, successful=2)
    row["ensemble_trace"].update(
        {
            "agent_llm_call_count": 2,
            "calls": [fallback, terminal],
        }
    )
    attempt = row["execution"]["generation_attempts"][0]
    attempt["run"]["error"] = module.LEGACY_TERMINAL_POLICY_ERROR
    attempt["retry_reason"] = module.LEGACY_TERMINAL_POLICY_ERROR
    row["error"] = None
    row["selected_generation_succeeded"] = True
    row["execution"]["run_error"] = ""
    row["execution"]["selected_generation_attempt"] = 1
    row["execution"]["generation_terminal_reclassification"] = {
        "schema": module.GENERATION_TERMINAL_RECLASSIFICATION_SCHEMA,
        "policy": "terminal_aggregator_with_empty_intermediate_fallback/v1",
        "original_error": module.LEGACY_TERMINAL_POLICY_ERROR,
        "selected_attempt_id": attempt["attempt_id"],
        "selected_attempt": attempt["attempt"],
        "terminal_call_index": 2,
        "intermediate_fallback_call_indexes": [1],
    }
    record = module.SourceRecord(
        path=tmp_path / "wave.jsonl",
        source_index=0,
        line=1,
        row=row,
    )

    assert module.bind_selected_generation_attempts([record], [record]) == {
        "B2/task-1": attempt["attempt_id"]
    }

    invalid = deepcopy(row)
    invalid["execution"]["generation_terminal_reclassification"]["schema"] = "wrong"
    invalid_record = module.SourceRecord(
        path=tmp_path / "wave.jsonl",
        source_index=0,
        line=1,
        row=invalid,
    )
    with pytest.raises(
        module.FinalizationError,
        match="not bound to exactly one successful physical generation attempt",
    ):
        module.bind_selected_generation_attempts([invalid_record], [invalid_record])


def _stub_selected_attempt_binding_helpers(
    module,
    monkeypatch,
    *,
    identity_calls: dict[str, int] | None = None,
) -> None:
    def fake_generation_identity(row):
        if identity_calls is not None:
            identity_calls["count"] += 1
        return str(row["binding_identity"])

    monkeypatch.setattr(module, "generation_identity", fake_generation_identity)
    monkeypatch.setattr(
        module,
        "usage_generation_identity_contract",
        lambda value: value,
    )
    monkeypatch.setattr(
        module,
        "_canonicalized_run",
        lambda run, **_kwargs: run,
    )
    monkeypatch.setattr(
        module,
        "run_expected_request_count",
        lambda run: int(run["request_count"]),
    )
    monkeypatch.setattr(
        module,
        "canonical_run_usage_units",
        lambda run, **_kwargs: list(run["units"]),
    )


def _selected_attempt_binding_record(
    module,
    tmp_path: Path,
    *,
    group: str,
    task_id: str,
    identity: str,
    final_sha: str,
    usage_marker: str,
    attempt_ids: list[str],
    line: int,
):
    usage = {"marker": usage_marker}
    return module.SourceRecord(
        path=tmp_path / "binding-wave.jsonl",
        source_index=0,
        line=line,
        row={
            "group": group,
            "task_id": task_id,
            "binding_identity": identity,
            "final_text_sha256": final_sha,
            "usage": usage,
            "execution": {
                "generation_attempts": [
                    {
                        "attempt_id": attempt_id,
                        "run": {
                            "final_text_sha256": final_sha,
                            "usage": usage,
                            "request_count": 1,
                            "units": [{"attempt_id": attempt_id}],
                            "error": "",
                        },
                    }
                    for attempt_id in attempt_ids
                ]
            },
        },
    )


def _legacy_bind_selected_generation_attempts_reference(
    module,
    records,
    selected,
):
    bindings: dict[str, str] = {}
    for selected_record in selected:
        selected_identity = module.generation_identity(selected_record.row)
        selected_final_sha = str(selected_record.row.get("final_text_sha256") or "")
        selected_usage = module.usage_generation_identity_contract(
            selected_record.row.get("usage")
        )
        matching_ids: set[str] = set()
        for candidate in records:
            if (
                candidate.key != selected_record.key
                or module.generation_identity(candidate.row) != selected_identity
            ):
                continue
            execution = candidate.row.get("execution")
            attempts = (
                execution.get("generation_attempts")
                if isinstance(execution, module.Mapping)
                and isinstance(execution.get("generation_attempts"), list)
                else []
            )
            for attempt in attempts:
                if not isinstance(attempt, module.Mapping):
                    continue
                run = attempt.get("run")
                attempt_id = str(attempt.get("attempt_id") or "")
                canonical_run = (
                    module._canonicalized_run(
                        run,
                        identity_seed=f"generation-attempt:{attempt_id}",
                    )
                    if isinstance(run, module.Mapping)
                    else None
                )
                if (
                    canonical_run is None
                    or str(canonical_run.get("final_text_sha256") or "")
                    != selected_final_sha
                    or module.usage_generation_identity_contract(
                        canonical_run.get("usage")
                    )
                    != selected_usage
                    or module.run_expected_request_count(canonical_run) <= 0
                    or len(
                        module.canonical_run_usage_units(
                            canonical_run,
                            identity_seed=f"generation-attempt:{attempt_id}",
                        )
                    )
                    != module.run_expected_request_count(canonical_run)
                ):
                    continue
                run_error = str(canonical_run.get("error") or "")
                if (
                    run_error
                    and not module.audit_only_error_text(
                        run_error,
                        degraded_success=module.explicit_degraded_success(
                            selected_record.row
                        ),
                    )
                    and not module.selected_legacy_attempt_error_is_reclassified(
                        selected_record.row,
                        attempt,
                    )
                ):
                    continue
                if module.HEX32.fullmatch(attempt_id):
                    matching_ids.add(attempt_id)
        pair = f"{selected_record.key[0]}/{selected_record.key[1]}"
        if len(matching_ids) != 1:
            raise module.FinalizationError(
                f"{pair} selected final answer is not bound to exactly one "
                f"successful physical generation attempt: {sorted(matching_ids)}"
            )
        bindings[pair] = next(iter(matching_ids))
    if len(bindings) != len(selected):
        raise module.FinalizationError(
            "selected generation attempt binding is incomplete"
        )
    return bindings


@pytest.mark.parametrize(
    "scenario",
    ["valid", "missing", "ambiguous", "duplicate_selected", "empty"],
)
def test_selected_attempt_binding_single_pass_matches_legacy_semantics(
    module,
    monkeypatch,
    tmp_path: Path,
    scenario: str,
) -> None:
    _stub_selected_attempt_binding_helpers(module, monkeypatch)
    selected_a = _selected_attempt_binding_record(
        module,
        tmp_path,
        group="B0",
        task_id="task-a",
        identity="identity-a",
        final_sha="final-a",
        usage_marker="usage-a",
        attempt_ids=["d" * 32],
        line=100,
    )
    selected_b = _selected_attempt_binding_record(
        module,
        tmp_path,
        group="B1",
        task_id="task-b",
        identity="identity-b",
        final_sha="final-b",
        usage_marker="usage-b",
        attempt_ids=["e" * 32],
        line=101,
    )
    candidate_a = _selected_attempt_binding_record(
        module,
        tmp_path,
        group="B0",
        task_id="task-a",
        identity="identity-a",
        final_sha="final-a",
        usage_marker="usage-a",
        attempt_ids=["a" * 32],
        line=1,
    )
    candidate_b = _selected_attempt_binding_record(
        module,
        tmp_path,
        group="B1",
        task_id="task-b",
        identity="identity-b",
        final_sha="final-b",
        usage_marker="usage-b",
        attempt_ids=["b" * 32],
        line=2,
    )
    unrelated = _selected_attempt_binding_record(
        module,
        tmp_path,
        group="B4",
        task_id="task-c",
        identity="identity-c",
        final_sha="final-c",
        usage_marker="usage-c",
        attempt_ids=["f" * 32],
        line=3,
    )
    if scenario == "valid":
        records = [candidate_a, unrelated, candidate_b, candidate_a]
        selected = [selected_a, selected_b]
    elif scenario == "missing":
        records = [unrelated, candidate_b]
        selected = [selected_a, selected_b]
    elif scenario == "ambiguous":
        ambiguous_a = _selected_attempt_binding_record(
            module,
            tmp_path,
            group="B0",
            task_id="task-a",
            identity="identity-a",
            final_sha="final-a",
            usage_marker="usage-a",
            attempt_ids=["c" * 32],
            line=4,
        )
        records = [candidate_a, candidate_b, ambiguous_a]
        selected = [selected_a, selected_b]
    elif scenario == "duplicate_selected":
        records = [candidate_a]
        selected = [selected_a, selected_a]
    else:
        records = [unrelated]
        selected = []

    def outcome(callable_):
        try:
            return "return", callable_()
        except Exception as exc:  # noqa: BLE001 - differential captures exact legacy behavior
            return "error", type(exc), str(exc)

    legacy = outcome(
        lambda: _legacy_bind_selected_generation_attempts_reference(
            module,
            records,
            selected,
        )
    )
    single_pass = outcome(
        lambda: module.bind_selected_generation_attempts(records, selected)
    )
    assert single_pass == legacy


def test_selected_attempt_binding_iterates_history_once(
    module,
    monkeypatch,
    tmp_path: Path,
) -> None:
    identity_calls = {"count": 0}
    _stub_selected_attempt_binding_helpers(
        module,
        monkeypatch,
        identity_calls=identity_calls,
    )
    selected_a = _selected_attempt_binding_record(
        module,
        tmp_path,
        group="B0",
        task_id="task-a",
        identity="identity-a",
        final_sha="final-a",
        usage_marker="usage-a",
        attempt_ids=[],
        line=100,
    )
    selected_b = _selected_attempt_binding_record(
        module,
        tmp_path,
        group="B1",
        task_id="task-b",
        identity="identity-b",
        final_sha="final-b",
        usage_marker="usage-b",
        attempt_ids=[],
        line=101,
    )
    candidates = [
        _selected_attempt_binding_record(
            module,
            tmp_path,
            group="B0",
            task_id="task-a",
            identity="identity-a",
            final_sha="final-a",
            usage_marker="usage-a",
            attempt_ids=["a" * 32],
            line=1,
        ),
        _selected_attempt_binding_record(
            module,
            tmp_path,
            group="B4",
            task_id="task-c",
            identity="identity-c",
            final_sha="final-c",
            usage_marker="usage-c",
            attempt_ids=["c" * 32],
            line=2,
        ),
        _selected_attempt_binding_record(
            module,
            tmp_path,
            group="B0",
            task_id="task-a",
            identity="identity-a",
            final_sha="final-a",
            usage_marker="usage-a",
            attempt_ids=["a" * 32],
            line=3,
        ),
        _selected_attempt_binding_record(
            module,
            tmp_path,
            group="B1",
            task_id="task-b",
            identity="identity-b",
            final_sha="final-b",
            usage_marker="usage-b",
            attempt_ids=["b" * 32],
            line=4,
        ),
    ]

    class CountingSequence:
        def __init__(self, values):
            self.values = values
            self.iteration_count = 0

        def __iter__(self):
            self.iteration_count += 1
            if self.iteration_count > 1:
                raise AssertionError("source history was iterated more than once")
            return iter(self.values)

    history = CountingSequence(candidates)
    assert module.bind_selected_generation_attempts(
        history,
        [selected_a, selected_b],
    ) == {
        "B0/task-a": "a" * 32,
        "B1/task-b": "b" * 32,
    }
    assert history.iteration_count == 1
    relevant_candidate_count = sum(
        candidate.key in {selected_a.key, selected_b.key}
        for candidate in candidates
    )
    assert identity_calls["count"] == 2 + relevant_candidate_count


def _campaign(
    module,
    tmp_path: Path,
    *,
    exact: bool = True,
    with_repair: bool = True,
) -> tuple[argparse.Namespace, list[dict[str, object]], int]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    task = {
        "id": "task-1",
        "prompt": "research this",
        "rubric": {
            "id": "rubric-1",
            "sections": [
                {
                    "id": "quality",
                    "title": "Quality",
                    "criteria": [
                        {"id": "core", "weight": 85, "requirement": "core"},
                        {"id": "extra", "weight": 15, "requirement": "extra"},
                    ],
                }
            ],
        },
    }
    input_path = tmp_path / "mini.jsonl"
    input_path.write_text(json.dumps(task) + "\n")
    input_path.chmod(0o600)
    module.FROZEN_DRACO_MINI_TASK_COUNT = 1
    module.FROZEN_DRACO_MINI_SHA256 = module.file_sha256(input_path)
    module.FORMAL_G1_RANKING_CONFIG_SHA256 = module.canonical_sha256(_test_ranking_config(module))
    key_hash = "a" * 64
    contracts = {group: _contract(module, group, key_hash) for group in module.GROUPS}
    fingerprints = {
        group: module.canonical_sha256(contract, prefix=True)
        for group, contract in contracts.items()
    }
    rows = [
        _row(
            module,
            group=group,
            task=task,
            fingerprint=fingerprints[group],
            response_prefix=group.lower(),
            exact=exact,
        )
        for group in module.GROUPS
    ]
    wave1 = tmp_path / "wave1.jsonl"
    wave1.write_text("".join(json.dumps(row) + "\n" for row in rows))
    wave1.chmod(0o600)
    result_paths = [wave1]
    manifest_paths = []

    def write_source_manifest(
        path: Path, result_path: Path, source_rows: list[dict[str, object]]
    ) -> None:
        repair_shard = bool(source_rows) and all(
            isinstance(row.get("execution"), dict)
            and row["execution"].get("generation_reused") is True
            for row in source_rows
        )
        _owner_json(
            path,
            {
                "benchmark": "DRACO",
                "status": "complete",
                "started_at": 1_000.0,
                "finished_at": 1_030.0,
                "groups": list(module.GROUPS),
                "task_ids": [task["id"]],
                "rows_written": len(source_rows),
                "artifacts": {"results_jsonl": str(result_path.resolve())},
                "command": {
                    "parsed_args": {
                        "groups": "B0,B1,B2,B4,G1",
                        "max_tasks": module.FROZEN_DRACO_MINI_TASK_COUNT,
                        "concurrency": 5,
                        "judge_concurrency": 6,
                        "require_clean_source": True,
                        "dry_run": False,
                    }
                },
                "tool_policy": {
                    "local_web_tools": {
                        "preflight": {
                            "status": ("skipped_not_required" if repair_shard else "passed"),
                            "preflight_calls": {
                                "web_search": 0 if repair_shard else 1,
                                "web_fetch": 0 if repair_shard else 1,
                            },
                        },
                    },
                },
                **(
                    {
                        "resume_selection": {
                            "model_regenerate_pair_count": 0,
                        }
                    }
                    if repair_shard
                    else {}
                ),
                "run_compatibility": {
                    "fingerprints": fingerprints,
                    "contracts": contracts,
                },
            },
        )

    wave1_manifest = tmp_path / "wave1.manifest.json"
    write_source_manifest(wave1_manifest, wave1, rows)
    manifest_paths.append(wave1_manifest)
    if with_repair:
        repaired = []
        for row in rows:
            value = deepcopy(row)
            value["completed_at"] = 1_020.0
            value["execution"]["prior_generation_attempts_used"] = 1
            value["execution"]["resume_action"] = "metadata_only"
            value["execution"]["generation_reused"] = True
            value["execution"]["metadata_repaired"] = True
            value["execution"]["judge_reran"] = False
            value["resume_completion"] = {
                "action": "metadata_only",
                "generation_reused": True,
                "metadata_repaired": True,
                "judge_reran": False,
                "post_repair_action": "complete",
                "status": "complete",
                "incomplete_reasons": [],
            }
            for judgment in value["judge"]["criterion_judgments"]:
                judgment["prior_judge_attempts_used"] = 1
                judgment["judge_new_attempt_count"] = 0
            value["judge"]["judge_new_attempt_count"] = 0
            repaired.append(module.seal_result_row(value))
        wave2 = tmp_path / "wave2.jsonl"
        wave2.write_text("".join(json.dumps(row) + "\n" for row in repaired))
        wave2.chmod(0o600)
        result_paths.append(wave2)
        wave2_manifest = tmp_path / "wave2.manifest.json"
        write_source_manifest(wave2_manifest, wave2, repaired)
        manifest_paths.append(wave2_manifest)

    runtime_path = tmp_path / "runtime.json"
    environment = {"python": {"version": "test"}, "packages": []}
    runtime_sha = module.canonical_sha256(environment)
    _owner_json(
        runtime_path,
        {
            "schema": module.RUNTIME_SCHEMA,
            "captured_at": "1970-01-01T00:00:00+00:00",
            "environment_sha256": runtime_sha,
            "environment": environment,
        },
    )
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    _owner_json(
        before,
        {
            "captured_at": "1970-01-01T00:16:30+00:00",
            "api_key_sha256": key_hash,
            "benchmark_environment_key_verified": True,
            "usage": "10",
            "byok_usage": "0",
            "is_free_tier": False,
        },
    )
    _owner_json(
        after,
        {
            "captured_at": "1970-01-01T00:20:30+00:00",
            "api_key_sha256": key_hash,
            "benchmark_environment_key_verified": True,
            "usage": "13.9",
            "byok_usage": "0",
            "is_free_tier": False,
        },
    )
    lock_path = tmp_path / "account.lock"
    lock_path.write_text("")
    lock_path.chmod(0o600)
    lock_fd = os.open(lock_path, os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    reconciliation = tmp_path / "reconciliation.json"
    stable_offsets = (0, 60, 120, 180, 195, 210)
    stable = [
        {
            "captured_at": (f"1970-01-01T00:{17 + offset // 60:02d}:{offset % 60:02d}+00:00"),
            "usage": "13.9",
            "byok_usage": "0",
        }
        for offset in stable_offsets
    ]
    _owner_json(
        reconciliation,
        {
            "schema": module.RECONCILIATION_SCHEMA,
            "created_at": "1970-01-01T00:20:30+00:00",
            "settlement_status": "stable",
            "api_key_sha256": key_hash,
            "usage_before_usd": "10",
            "usage_after_usd": "13.9",
            "usage_delta_usd": "3.9",
            "byok_usage_before_usd": "0",
            "byok_usage_after_usd": "0",
            "byok_usage_delta_usd": "0",
            "is_free_tier": False,
            "stable_poll_count": 6,
            "required_stable_poll_count": 6,
            "poll_observation_count": 6,
            "stable_tail_start_index": 0,
            "poll_interval_seconds": 15,
            "minimum_settlement_seconds": 180,
            "minimum_stable_tail_seconds": 75,
            "observation_span_seconds": 210.0,
            "stable_tail_span_seconds": 210.0,
            "stable_observations": stable,
            "runtime_environment_sha256": runtime_sha,
            "runtime_environment_file_sha256": module.file_sha256(runtime_path),
            "lock_file": str(lock_path.resolve()),
            "lock_inode": lock_path.stat().st_ino,
        },
    )
    args = argparse.Namespace(
        input=input_path,
        result=result_paths,
        manifest=manifest_paths,
        account_before=before,
        account_after=after,
        account_reconciliation=reconciliation,
        runtime_environment=runtime_path,
        lock_file=lock_path,
        lock_fd=lock_fd,
        output_dir=tmp_path / "final",
        groups="B0,B1,B2,B4,G1",
        max_generation_attempts=3,
    )
    return args, rows, lock_fd


def _g1_only_campaign(
    module,
    tmp_path: Path,
) -> tuple[argparse.Namespace, list[dict[str, object]], int]:
    args, rows, lock_fd = _campaign(module, tmp_path)
    active_groups = ("G1",)
    for result_path in args.result:
        source_rows = [
            row
            for row in (json.loads(line) for line in result_path.read_text().splitlines())
            if row["group"] == "G1"
        ]
        result_path.write_text("".join(json.dumps(row) + "\n" for row in source_rows))
        result_path.chmod(0o600)
    for manifest_path in args.manifest:
        manifest = json.loads(manifest_path.read_text())
        manifest["groups"] = list(active_groups)
        manifest["rows_written"] = 1
        manifest["command"]["parsed_args"]["groups"] = "G1"
        compatibility = manifest["run_compatibility"]
        compatibility["fingerprints"] = {
            "G1": compatibility["fingerprints"]["G1"],
        }
        compatibility["contracts"] = {
            "G1": compatibility["contracts"]["G1"],
        }
        _owner_json(manifest_path, manifest)

    usage_after = "11.1"
    after = json.loads(args.account_after.read_text())
    after["usage"] = usage_after
    _owner_json(args.account_after, after)
    reconciliation = json.loads(args.account_reconciliation.read_text())
    reconciliation["usage_after_usd"] = usage_after
    reconciliation["usage_delta_usd"] = "1.1"
    for observation in reconciliation["stable_observations"]:
        observation["usage"] = usage_after
    _owner_json(args.account_reconciliation, reconciliation)
    args.groups = "G1"
    return args, [row for row in rows if row["group"] == "G1"], lock_fd


def _prior_account_window(
    module,
    args: argparse.Namespace,
    tmp_path: Path,
    *,
    before_usage: str = "5.401561244",
    suffix: str = "prior-aborted",
) -> Path:
    window = tmp_path / "archive" / "account" / suffix
    window.mkdir(parents=True)
    runtime = window / "runtime-environment.json"
    runtime.write_bytes(args.runtime_environment.read_bytes())
    runtime.chmod(0o600)
    key_hash = json.loads(args.account_before.read_text())["api_key_sha256"]
    before = window / "openrouter-account-before.json"
    after = window / "openrouter-account-after.json"
    _owner_json(
        before,
        {
            "captured_at": "1970-01-01T00:10:00+00:00",
            "api_key_sha256": key_hash,
            "benchmark_environment_key_verified": True,
            "usage": before_usage,
            "byok_usage": "0",
            "is_free_tier": False,
        },
    )
    _owner_json(
        after,
        {
            "captured_at": "1970-01-01T00:15:00+00:00",
            "api_key_sha256": key_hash,
            "benchmark_environment_key_verified": True,
            "usage": "10",
            "byok_usage": "0",
            "is_free_tier": False,
        },
    )
    runtime_payload = json.loads(runtime.read_text())
    usage_delta = str(module.Decimal("10") - module.Decimal(before_usage))
    observations = [
        {
            "captured_at": value,
            "usage": "10",
            "byok_usage": "0",
        }
        for value in (
            "1970-01-01T00:11:00+00:00",
            "1970-01-01T00:12:00+00:00",
            "1970-01-01T00:13:00+00:00",
            "1970-01-01T00:14:00+00:00",
            "1970-01-01T00:14:45+00:00",
            "1970-01-01T00:15:00+00:00",
        )
    ]
    _owner_json(
        window / "openrouter-account-reconciliation.json",
        {
            "schema": module.RECONCILIATION_SCHEMA,
            "created_at": "1970-01-01T00:15:00+00:00",
            "settlement_status": "stable",
            "api_key_sha256": key_hash,
            "usage_before_usd": before_usage,
            "usage_after_usd": "10",
            "usage_delta_usd": usage_delta,
            "byok_usage_before_usd": "0",
            "byok_usage_after_usd": "0",
            "byok_usage_delta_usd": "0",
            "is_free_tier": False,
            "stable_poll_count": 6,
            "required_stable_poll_count": 6,
            "poll_observation_count": 6,
            "stable_tail_start_index": 0,
            "poll_interval_seconds": 15,
            "minimum_settlement_seconds": 180,
            "minimum_stable_tail_seconds": 75,
            "observation_span_seconds": 240.0,
            "stable_tail_span_seconds": 240.0,
            "stable_observations": observations,
            "runtime_environment_sha256": runtime_payload["environment_sha256"],
            "runtime_environment_file_sha256": module.file_sha256(runtime),
        },
    )
    return window


def _bind_prior_campaign_window(
    module,
    args: argparse.Namespace,
    tmp_path: Path,
) -> Path:
    prior = _prior_account_window(
        module,
        args,
        tmp_path,
        before_usage="6.1",
        suffix="prior-campaign",
    )
    prior_reconciliation_path = prior / "openrouter-account-reconciliation.json"
    prior_reconciliation = json.loads(prior_reconciliation_path.read_text())
    prior_reconciliation.update(
        {
            "lock_file": str(args.lock_file.resolve()),
            "lock_inode": args.lock_file.stat().st_ino,
        }
    )
    _owner_json(prior_reconciliation_path, prior_reconciliation)

    for source_index, result_path in enumerate(args.result):
        rows = [json.loads(line) for line in result_path.read_text().splitlines()]
        for row_index, row in enumerate(rows):
            # Both waves retain the immutable timing of the generation that
            # physically ran in wave 1.  Only the repair row completion belongs
            # to the current window.
            row["started_at"] = 660.0
            row["generation_completed_at"] = 665.0
            if source_index == 0:
                row["completed_at"] = 870.0
            execution = row["execution"]
            assert isinstance(execution, dict)
            attempts = execution["generation_attempts"]
            assert isinstance(attempts, list)
            for attempt in attempts:
                assert isinstance(attempt, dict)
                attempt["started_at"] = 660.0
                attempt["completed_at"] = 665.0
            rows[row_index] = module.seal_result_row(row)
        result_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        result_path.chmod(0o600)

    wave1_manifest = json.loads(args.manifest[0].read_text())
    wave1_manifest.update(
        {
            "started_at": 600.0,
            "finished_at": 900.0,
        }
    )
    _owner_json(args.manifest[0], wave1_manifest)

    account_after = json.loads(args.account_after.read_text())
    account_after["usage"] = "10"
    _owner_json(args.account_after, account_after)
    current_reconciliation = json.loads(args.account_reconciliation.read_text())
    current_reconciliation.update(
        {
            "usage_before_usd": "10",
            "usage_after_usd": "10",
            "usage_delta_usd": "0",
        }
    )
    for observation in current_reconciliation["stable_observations"]:
        observation["usage"] = "10"
    _owner_json(args.account_reconciliation, current_reconciliation)
    args.campaign_account_window_dir = [prior]
    return prior


def _prior_campaign_window_reconciliation(
    proof: dict[str, object],
) -> dict[str, dict[str, object]]:
    cost_scope = proof["cost_scope"]
    assert isinstance(cost_scope, dict)
    reconciliations = cost_scope["ledger_window_reconciliation"]
    assert isinstance(reconciliations, list)
    return {
        str(value["account_window_kind"]): value
        for value in reconciliations
        if isinstance(value, dict)
    }


def test_prior_campaign_window_reconciles_physical_first_sources(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    prior = _bind_prior_campaign_window(module, args, tmp_path)
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)

    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    assert [window["kind"] for window in proof["account_windows"]] == [
        "prior_campaign",
        "current",
    ]
    assert proof["result_row_account_window_scope"] == "campaign_windows"
    assert proof["account"]["campaign_usage_delta_usd"] == "3.9"
    assert proof["account"]["campaign_window_count"] == 2
    scope = proof["cost_scope"]
    assert scope["campaign_bound_account_window_total_usd"] == "3.9"
    assert scope["campaign_attributable_exact"] is True
    assert scope["campaign_attributable_cost_usd"] == "3.9"

    coverage = proof["window"]["source_window_coverage"]
    assert [
        (
            value["source_index"],
            value["account_window_kind"],
            value["physical_first_generation_attempt_count"],
        )
        for value in coverage
    ] == [
        (0, "prior_campaign", 5),
        (1, "current", 0),
    ]
    assert coverage[0]["account_window_path"] == str(prior.resolve())
    assert coverage[1]["account_window_path"] == str(tmp_path.resolve())

    reconciliations = _prior_campaign_window_reconciliation(proof)
    assert reconciliations["prior_campaign"] == {
        "account_window_path": str(prior.resolve()),
        "account_window_kind": "prior_campaign",
        "source_indexes": [0],
        "physical_request_count": 39,
        "ledger_recorded_cost_usd": "3.900000000",
        "ledger_exact_cost_usd": "3.900000000",
        "unknown_cost_request_count": 0,
        "non_exact_cost_request_count": 0,
        "account_usage_delta_usd": "3.9",
        "reconciliation_gap_usd": "0E-9",
        "reconciliation_status": "exact",
    }
    assert reconciliations["current"] == {
        "account_window_path": str(tmp_path.resolve()),
        "account_window_kind": "current",
        "source_indexes": [1],
        "physical_request_count": 0,
        "ledger_recorded_cost_usd": "0",
        "ledger_exact_cost_usd": "0",
        "unknown_cost_request_count": 0,
        "non_exact_cost_request_count": 0,
        "account_usage_delta_usd": "0",
        "reconciliation_gap_usd": "0",
        "reconciliation_status": "exact",
    }

    ledger = [
        json.loads(line)
        for line in (args.output_dir / "actual-spend-ledger.jsonl").read_text().splitlines()
    ]
    assert len(ledger) == 39
    assert {
        (
            row["physical_source"]["source_index"],
            row["physical_source"]["path"],
            tuple(row["receipt_source_indexes"]),
        )
        for row in ledger
    } == {
        (0, str(args.result[0]), (1,)),
    }

    report = (args.output_dir / "EXPERIMENT_RESULTS.md").read_text()
    assert "Generation disposition 成本：selected=$0.900000000" in report
    assert "账本已记录成本：$3.900000000" in report
    assert "Campaign-bound OpenRouter account delta：$3.9" in report
    assert "prior_campaign=39 requests/$3.9/exact" in report
    assert "current=0 requests/$0/exact" in report
    assert manifest["cost_attribution"]["campaign_bound_account_window_total_usd"] == "3.9"


@pytest.mark.parametrize(
    ("outside_evidence", "warning_pattern"),
    [
        (
            "physical_attempt",
            "physical-first generation attempt is outside its source manifest",
        ),
        (
            "row_completion",
            "source result row completion is outside its manifest execution window",
        ),
    ],
)
def test_prior_campaign_rejects_source_timing_outside_manifest(
    module,
    tmp_path: Path,
    outside_evidence: str,
    warning_pattern: str,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    _bind_prior_campaign_window(module, args, tmp_path)
    result_paths = args.result if outside_evidence == "physical_attempt" else args.result[:1]
    for result_path in result_paths:
        rows = [json.loads(line) for line in result_path.read_text().splitlines()]
        if outside_evidence == "physical_attempt":
            rows[0]["execution"]["generation_attempts"][0]["started_at"] = 599.0
        else:
            rows[0]["completed_at"] = 901.0
        rows[0] = module.seal_result_row(rows[0])
        result_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        result_path.chmod(0o600)
    try:
        with pytest.raises(module.FinalizationError, match=warning_pattern):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize("binding", ["lock", "runtime"])
def test_prior_campaign_requires_same_runtime_and_lock(
    module,
    tmp_path: Path,
    binding: str,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    prior = _bind_prior_campaign_window(module, args, tmp_path)
    reconciliation_path = prior / "openrouter-account-reconciliation.json"
    reconciliation = json.loads(reconciliation_path.read_text())
    if binding == "lock":
        reconciliation["lock_inode"] = args.lock_file.stat().st_ino + 1
    else:
        runtime_path = prior / "runtime-environment.json"
        runtime = json.loads(runtime_path.read_text())
        runtime["environment"]["python"]["version"] = "different"
        runtime["environment_sha256"] = module.canonical_sha256(runtime["environment"])
        _owner_json(runtime_path, runtime)
        reconciliation["runtime_environment_sha256"] = runtime["environment_sha256"]
        reconciliation["runtime_environment_file_sha256"] = module.file_sha256(runtime_path)
    _owner_json(reconciliation_path, reconciliation)
    try:
        with pytest.raises(
            module.FinalizationError,
            match="not bound to the same runtime/lock",
        ):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_full_offline_finalization_is_atomic_and_preserves_contracts(
    module, tmp_path: Path
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)
    assert manifest["status"] == "complete"
    assert manifest["result_count"] == 5
    output = args.output_dir
    assert sorted(path.name for path in output.iterdir()) == [
        "EXPERIMENT_RESULTS.md",
        "actual-spend-ledger.jsonl",
        "audit.json",
        "manifest.json",
        "openrouter-non-byok-campaign-proof.json",
        "results.jsonl",
        "trace.jsonl",
    ]
    final_rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
    assert [row["group"] for row in final_rows] == list(module.GROUPS)
    assert all(module.verify_result_row_evidence(row) for row in final_rows)
    traces = [json.loads(line) for line in (output / "trace.jsonl").read_text().splitlines()]
    assert traces == [canonical_trace_row_from_result(row) for row in final_rows]
    selected_source_rows = [json.loads(line) for line in args.result[-1].read_text().splitlines()]
    by_group = {row["group"]: row for row in selected_source_rows}
    for final in final_rows:
        original = by_group[final["group"]]
        assert final["final_text"] == original["final_text"]
        assert final["usage"] == original["usage"]
        assert final["execution"] == original["execution"]
        assert final["cost_accounting"] == original["cost_accounting"]
        assert final["quality_total"] == 85.0
        assert final["openrouter_non_byok_resolution"]["campaign_proof_pass"] is True
    ledger = [
        json.loads(line) for line in (output / "actual-spend-ledger.jsonl").read_text().splitlines()
    ]
    # Nine physical generation requests (G1 includes its analyzer) plus thirty
    # (2 criteria x 3 repeats)
    # physical Judge requests.
    # The repair wave repeats both but must not double-count them.
    assert len(ledger) == 39
    generation_rows = [row for row in ledger if "generation" in row["scopes"]]
    assert {row["generation_disposition"] for row in generation_rows} == {"selected"}
    audit = json.loads((output / "audit.json").read_text())
    assert audit["pass"] is True
    assert audit["result_count"] == 5
    assert (
        audit["selected_generation_cost"]["attribution_precision"] == "campaign-attributable-exact"
    )
    assert audit["external_tool_cost"]["task_generation_tool_call_count"] == 0
    assert audit["external_tool_cost"]["live_preflight_tool_call_count"] == 2
    assert audit["judge_attempt_evidence"]["unique_physical_judge_attempt_count"] == 30
    assert audit["selected_generation_cost_reconciliation"]["pair_count"] == 5
    report = (output / "EXPERIMENT_RESULTS.md").read_text()
    assert "Brave" in report
    assert "单任务超时 10800 秒，最多 20 轮；任务并发 5" in report
    assert "temperature=0，max_tokens=16384" in report
    assert "`google/gemini-3.1-pro-preview`，3 repeats，Judge 并发 6" in report
    assert "最多 3 次 attempt" in report
    assert "`research.perplexity.ai`" in report
    assert "只允许 non-BYOK" in report
    assert "response cache 关闭" in report
    assert "Selected generation 费用按固定优先级统计" in report
    assert "cache-read/cache-write 分桶补算" in report
    assert "cache-aware 结果" in report
    assert "cache-blind 上界" in report
    assert "ignored 只表示未纳入小计，不是 $0" in report
    assert "Rubric 分项平均" in report
    assert "修复动作明细" in report
    proof = json.loads((output / "openrouter-non-byok-campaign-proof.json").read_text())
    assert proof["cost_scope"]["campaign_attributable_exact"] is True
    assert proof["cost_scope"]["campaign_attributable_cost_usd"] == "3.9"
    assert proof["cost_scope"]["attribution_precision"] == "campaign-attributable-exact"
    assert [window["kind"] for window in proof["account_windows"]] == ["current"]
    assert proof["account_window_total_usd"] == "3.9"
    assert proof["unallocated_aborted_window_usd"] == "0"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("B0,B1,B2,B4,G1", ("B0", "B1", "B2", "B4", "G1")),
        ("G1", ("G1",)),
        ("B0,B2,G1", ("B0", "B2", "G1")),
    ],
)
def test_normalize_groups_accepts_canonical_nonempty_subsets(
    module,
    raw: str,
    expected: tuple[str, ...],
) -> None:
    assert module.normalize_groups(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        ",",
        "G1,G1",
        "G1,B0",
        "B0,G2",
        "B0,,G1",
        " B0, G1,",
    ],
)
def test_normalize_groups_rejects_invalid_subsets(module, raw: str) -> None:
    with pytest.raises(module.FinalizationError, match="canonical-order subset"):
        module.normalize_groups(raw)


def test_g1_only_finalization_scopes_all_formal_outputs(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _g1_only_campaign(module, tmp_path)
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)

    output = args.output_dir
    rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
    ledger = [
        json.loads(line) for line in (output / "actual-spend-ledger.jsonl").read_text().splitlines()
    ]
    audit = json.loads((output / "audit.json").read_text())
    proof = json.loads((output / "openrouter-non-byok-campaign-proof.json").read_text())
    report = (output / "EXPERIMENT_RESULTS.md").read_text()

    assert manifest["groups"] == ["G1"]
    assert manifest["result_count"] == 1
    assert set(manifest["run_compatibility_fingerprints"]) == {"G1"}
    assert [metric["group"] for metric in manifest["group_metrics"]] == ["G1"]
    assert manifest["paired_quality_comparisons"] == []
    assert manifest["openrouter_non_byok_campaign_proof_sha256"] == proof["proof_sha256"]
    assert [row["group"] for row in rows] == ["G1"]
    assert len(ledger) == 11
    assert {
        reference["group"] for ledger_row in ledger for reference in ledger_row["source_references"]
    } == {"G1"}
    assert audit["pass"] is True
    assert audit["groups"] == ["G1"]
    assert audit["expected_result_count"] == 1
    assert audit["result_count"] == 1
    assert set(audit["pair_selection"]) == {"G1/task-1"}
    assert [metric["group"] for metric in audit["selected_generation_cost"]["groups"]] == ["G1"]
    assert audit["paired_quality_comparisons"] == []
    assert proof["pass"] is True
    assert proof["account"]["campaign_byok_usage_delta_usd"] == "0"
    assert (
        "| Group | Rows | Done | AvgQ | AvgPass | JudgeErr | Avg Gen$† | "
        "Total Gen$† | Gen exact | Avg Input | Avg Output | Avg Reason | "
        "Avg Cache | Avg Visible | Avg Tokens | Avg Tools | Tool% | Avg Steps | "
        "Avg LLMReq | p50 ms | p95 ms |"
    ) in report
    g1_metric_row = next(
        line
        for line in report.splitlines()
        if line.startswith("| G1 |") and len(line.split("|")) == 23
    )
    assert len(g1_metric_row.split("|")) == 23
    assert "† 上表成本只统计最终选中的成功 generation" in report
    assert proof["cost_scope"]["ledger_window_reconciliation"] == [
        {
            "account_window_path": str(tmp_path.resolve()),
            "account_window_kind": "current",
            "source_indexes": [0, 1],
            "physical_request_count": 11,
            "ledger_recorded_cost_usd": "1.100000000",
            "ledger_exact_cost_usd": "1.100000000",
            "unknown_cost_request_count": 0,
            "non_exact_cost_request_count": 0,
            "account_usage_delta_usd": "1.1",
            "reconciliation_gap_usd": "0E-9",
            "reconciliation_status": "exact",
        }
    ]
    assert "# DRACO Mini G1 实验结果" in report
    assert "- 实验组：G1" in report
    assert "- 严格完成 1/1 个 group×task" in report
    assert "| G1 | selection_mode | frozen-registry `router_dynamic` |" in report
    assert "| B0 | single |" not in report
    assert "本次活动组不包含 B0/B1 基线，无法进行同题配对比较" in report


@pytest.mark.parametrize("tamper", ["manifest_groups", "contract_scope"])
def test_g1_only_manifest_rejects_extra_group_scope(
    module,
    tmp_path: Path,
    tamper: str,
) -> None:
    args, _, lock_fd = _g1_only_campaign(module, tmp_path)
    manifest = json.loads(args.manifest[0].read_text())
    if tamper == "manifest_groups":
        manifest["groups"] = ["B0", "G1"]
        error_pattern = "groups/task_ids do not cover"
    else:
        b0_contract = _contract(module, "B0", "a" * 64)
        compatibility = manifest["run_compatibility"]
        compatibility["contracts"]["B0"] = b0_contract
        compatibility["fingerprints"]["B0"] = module.canonical_sha256(
            b0_contract,
            prefix=True,
        )
        error_pattern = "compatibility scope differs from active groups"
    _owner_json(args.manifest[0], manifest)
    try:
        with pytest.raises(
            module.FinalizationError,
            match=error_pattern,
        ):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_metadata_only_wave_may_replay_identical_judge_snapshot_declarations(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    rows = [json.loads(line) for line in args.result[1].read_text().splitlines()]
    judge = rows[0]["judge"]
    declared_new = 0
    for judgment in judge["criterion_judgments"]:
        attempts = judgment["judge_attempts"]
        judgment["prior_judge_attempts_used"] = 0
        judgment["judge_new_attempt_count"] = len(attempts)
        declared_new += len(attempts)
    judge["judge_new_attempt_count"] = declared_new
    rows[0] = module.seal_result_row(rows[0])
    args.result[1].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)

    assert manifest["status"] == "complete"


def test_prior_aborted_account_window_is_preserved_without_fake_ledger_rows(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.provider import ranking_router

    monkeypatch.setattr(
        ranking_router,
        "ranking_trace_replay_reasons",
        lambda _plan, **_kwargs: [],
        raising=False,
    )
    args, _, lock_fd = _campaign(module, tmp_path)
    prior = _prior_account_window(module, args, tmp_path)
    args.prior_account_window_dir = [prior]
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)
    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    scope = proof["cost_scope"]
    assert [window["kind"] for window in scope["account_windows"]] == [
        "prior_aborted",
        "current",
    ]
    assert scope["account_window_total_usd"] == "8.498438756"
    assert scope["unallocated_aborted_window_usd"] == "4.598438756"
    assert (
        scope["attribution_precision"] == "multi-window-counter-exact-campaign-attribution-unproven"
    )
    assert scope["campaign_attributable_exact"] is False
    assert scope["campaign_attributable_cost_usd"] is None
    assert proof["result_row_account_window_scope"] == "current_window_only"
    ledger = [
        json.loads(line)
        for line in (args.output_dir / "actual-spend-ledger.jsonl").read_text().splitlines()
    ]
    assert len(ledger) == 39
    assert manifest["cost_attribution"]["account_windows"] == scope["account_windows"]
    assert manifest["cost_attribution"]["account_window_total_usd"] == "8.498438756"
    audit = json.loads((args.output_dir / "audit.json").read_text())
    assert audit["selected_generation_cost"]["unallocated_aborted_window_usd"] == ("4.598438756")
    report = (args.output_dir / "EXPERIMENT_RESULTS.md").read_text()
    assert "multi-window-counter-exact-campaign-attribution-unproven" in report
    assert "4.598438756" in report
    assert "当前正式窗口的物理回执与账户 counter 已精确对账" in report
    assert "当前正式窗口存在 non-exact/unknown 物理请求" not in report
    assert proof["account_windows"][0]["admission_basis"] == (
        "operator_supplied_unallocated_window"
    )


def test_current_reconciliation_runtime_file_hash_mismatch_is_rejected(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.provider import ranking_router

    monkeypatch.setattr(
        ranking_router,
        "ranking_trace_replay_reasons",
        lambda _plan, **_kwargs: [],
        raising=False,
    )
    args, _, lock_fd = _campaign(module, tmp_path)
    reconciliation = json.loads(args.account_reconciliation.read_text())
    reconciliation["runtime_environment_file_sha256"] = "b" * 64
    _owner_json(args.account_reconciliation, reconciliation)
    try:
        with pytest.raises(module.FinalizationError, match="runtime file hash differs"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_prior_account_window_overlap_is_rejected(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.provider import ranking_router

    monkeypatch.setattr(
        ranking_router,
        "ranking_trace_replay_reasons",
        lambda _plan, **_kwargs: [],
        raising=False,
    )
    args, _, lock_fd = _campaign(module, tmp_path)
    prior = _prior_account_window(module, args, tmp_path)
    after = json.loads((prior / "openrouter-account-after.json").read_text())
    after["captured_at"] = "1970-01-01T00:17:00+00:00"
    _owner_json(prior / "openrouter-account-after.json", after)
    reconciliation = json.loads((prior / "openrouter-account-reconciliation.json").read_text())
    reconciliation["stable_observations"][-1]["captured_at"] = "1970-01-01T00:17:00+00:00"
    reconciliation["observation_span_seconds"] = 360.0
    reconciliation["stable_tail_span_seconds"] = 360.0
    _owner_json(prior / "openrouter-account-reconciliation.json", reconciliation)
    args.prior_account_window_dir = [prior]
    try:
        with pytest.raises(module.FinalizationError, match="overlap"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_prior_account_before_must_precede_first_stable_observation(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.provider import ranking_router

    monkeypatch.setattr(
        ranking_router,
        "ranking_trace_replay_reasons",
        lambda _plan, **_kwargs: [],
        raising=False,
    )
    args, _, lock_fd = _campaign(module, tmp_path)
    prior = _prior_account_window(module, args, tmp_path)
    before = json.loads((prior / "openrouter-account-before.json").read_text())
    before["captured_at"] = "1970-01-01T00:11:30+00:00"
    _owner_json(prior / "openrouter-account-before.json", before)
    args.prior_account_window_dir = [prior]
    try:
        with pytest.raises(
            module.FinalizationError,
            match="precede settlement observations",
        ):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_prior_windows_require_monotonic_cumulative_byok_counter(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.provider import ranking_router

    monkeypatch.setattr(
        ranking_router,
        "ranking_trace_replay_reasons",
        lambda _plan, **_kwargs: [],
        raising=False,
    )
    args, _, lock_fd = _campaign(module, tmp_path)
    for path in (args.account_before, args.account_after):
        payload = json.loads(path.read_text())
        payload["byok_usage"] = "2"
        _owner_json(path, payload)
    current_reconciliation = json.loads(args.account_reconciliation.read_text())
    for field in (
        "byok_usage_before_usd",
        "byok_usage_after_usd",
    ):
        current_reconciliation[field] = "2"
    for observation in current_reconciliation["stable_observations"]:
        observation["byok_usage"] = "2"
    _owner_json(args.account_reconciliation, current_reconciliation)

    earlier = _prior_account_window(
        module,
        args,
        tmp_path,
        before_usage="1",
        suffix="prior-earlier",
    )
    earlier_before = json.loads((earlier / "openrouter-account-before.json").read_text())
    earlier_before.update(
        captured_at="1970-01-01T00:03:00+00:00",
        byok_usage="2",
    )
    _owner_json(earlier / "openrouter-account-before.json", earlier_before)
    earlier_after = json.loads((earlier / "openrouter-account-after.json").read_text())
    earlier_after.update(
        captured_at="1970-01-01T00:09:00+00:00",
        usage="5",
        byok_usage="2",
    )
    _owner_json(earlier / "openrouter-account-after.json", earlier_after)
    earlier_reconciliation = json.loads(
        (earlier / "openrouter-account-reconciliation.json").read_text()
    )
    earlier_reconciliation.update(
        usage_before_usd="1",
        usage_after_usd="5",
        usage_delta_usd="4",
        byok_usage_before_usd="2",
        byok_usage_after_usd="2",
        byok_usage_delta_usd="0",
    )
    earlier_times = (
        "1970-01-01T00:05:00+00:00",
        "1970-01-01T00:06:00+00:00",
        "1970-01-01T00:07:00+00:00",
        "1970-01-01T00:08:00+00:00",
        "1970-01-01T00:08:45+00:00",
        "1970-01-01T00:09:00+00:00",
    )
    for observation, captured_at in zip(
        earlier_reconciliation["stable_observations"],
        earlier_times,
        strict=True,
    ):
        observation.update(captured_at=captured_at, usage="5", byok_usage="2")
    _owner_json(
        earlier / "openrouter-account-reconciliation.json",
        earlier_reconciliation,
    )

    later = _prior_account_window(
        module,
        args,
        tmp_path,
        before_usage="6",
        suffix="prior-later",
    )
    for name in (
        "openrouter-account-before.json",
        "openrouter-account-after.json",
    ):
        payload = json.loads((later / name).read_text())
        payload["byok_usage"] = "1"
        _owner_json(later / name, payload)
    later_reconciliation = json.loads(
        (later / "openrouter-account-reconciliation.json").read_text()
    )
    later_reconciliation.update(
        byok_usage_before_usd="1",
        byok_usage_after_usd="1",
        byok_usage_delta_usd="0",
    )
    for observation in later_reconciliation["stable_observations"]:
        observation["byok_usage"] = "1"
    _owner_json(later / "openrouter-account-reconciliation.json", later_reconciliation)
    args.prior_account_window_dir = [earlier, later]
    try:
        with pytest.raises(module.FinalizationError, match="BYOK usage decreased"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_prior_account_window_delta_is_not_hard_coded(
    module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opensquilla.provider import ranking_router

    monkeypatch.setattr(
        ranking_router,
        "ranking_trace_replay_reasons",
        lambda _plan, **_kwargs: [],
        raising=False,
    )
    args, _, lock_fd = _campaign(module, tmp_path)
    args.prior_account_window_dir = [
        _prior_account_window(module, args, tmp_path, before_usage="6")
    ]
    try:
        module.run_finalization(args)
    finally:
        os.close(lock_fd)
    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    assert proof["unallocated_aborted_window_usd"] == "4"
    assert proof["account_window_total_usd"] == "7.9"
    assert proof["cost_scope"]["campaign_attributable_exact"] is False


def test_unverified_receipt_is_resolved_only_by_campaign_proof(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, exact=False)
    try:
        module.run_finalization(args)
    finally:
        os.close(lock_fd)
    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    assert proof["pass"] is True
    assert proof["local_physical_request_evidence"]["unverified_request_count"] == 39
    assert (
        proof["local_physical_request_evidence"]["campaign_covered_unverified_request_count"] == 39
    )
    assert proof["cost_scope"]["campaign_attributable_exact"] is False
    assert proof["cost_scope"]["campaign_attributable_cost_usd"] is None
    assert (
        proof["cost_scope"]["attribution_precision"]
        == "account_window_only_external-use-not-provable"
    )
    assert proof["window"]["exclusive_lock_scope"] == ("local_host_filesystem_only")
    assert proof["window"]["cross_host_exclusivity_proven"] is False
    report = (args.output_dir / "EXPERIMENT_RESULTS.md").read_text()
    assert "account_window_only_external-use-not-provable" in report


def test_unknown_failed_analyzer_attempt_is_preserved_without_rerun(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(
        module,
        tmp_path,
        with_repair=False,
    )
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    g1 = next(row for row in rows if row["group"] == "G1")
    physical_attempt_id = "a" * 32
    unknown_analyzer = {
        "role": "unknown_request",
        "label": "task_analyzer",
        "provider": "",
        "model": "",
        "requested_provider": "openrouter",
        "requested_model": module.TASK_ANALYZER_MODEL,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
        "attempt": 1,
        "physical_attempt_id": physical_attempt_id,
        "provider_usage": {
            "usage_unknown": True,
            "physical_attempt_id": physical_attempt_id,
        },
    }
    top_units = g1["usage"]["model_usage_breakdown"]
    run = g1["execution"]["generation_attempts"][0]["run"]
    run_units = run["usage"]["model_usage_breakdown"]
    top_units.insert(0, deepcopy(unknown_analyzer))
    run_units.insert(0, deepcopy(unknown_analyzer))
    g1["llm_request_count"] = 6
    run["llm_request_count"] = 6
    g1["error"] = "openrouter_non_byok_metadata_incomplete"
    g1["completion_status"].update(
        {
            "cost_metadata_complete": False,
            "status": "incomplete",
        }
    )
    g1["cost_accounting"]["selected_generation_attempt"].update(
        {
            "request_count": 6,
            "cost_complete": False,
            "cost_exact": False,
        }
    )
    g1["cost_accounting"]["actual_llm_cost_complete"] = False
    g1["cost_accounting"]["actual_spend_cost_complete"] = False
    g1["openrouter_non_byok_audit"].update(
        {
            "pass": False,
            "status": "metadata_incomplete",
        }
    )
    rows[rows.index(g1)] = module.seal_result_row(g1)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)

    assert manifest["status"] == "complete"
    ledger = [
        json.loads(line)
        for line in (args.output_dir / "actual-spend-ledger.jsonl").read_text().splitlines()
    ]
    assert len(ledger) == 40
    unknown = [
        row
        for row in ledger
        if row["cost_precision"] == "unknown"
        and {"group": "G1", "task_id": "task-1"} in row["group_task_pairs"]
    ]
    assert len(unknown) == 1
    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    assert proof["cost_scope"]["campaign_attributable_exact"] is False
    assert proof["cost_scope"]["campaign_attributable_cost_usd"] is None
    assert (
        proof["cost_scope"]["attribution_precision"]
        == "account_window_only_external-use-not-provable"
    )


def test_five_counter_only_failed_judge_attempts_finalize_without_rerun(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    row = rows[0]
    judgments = row["judge"]["criterion_judgments"]

    for index, judgment in enumerate(judgments[:5]):
        successful = judgment["judge_attempts"][0]
        successful["attempt"] = 2
        failed = {
            "attempt_id": f"{0x5030 + index:032x}",
            "attempt": 1,
            "verdict": "",
            "met": None,
            "retry_suppressed_reason": "",
            "run": {
                "error": (
                    "OpenRouter chat request failed (HTTP 503): "
                    "Cloudflare Worker exceeded resource limits"
                ),
                "llm_request_count": 1,
                "usage_unknown_count": 1,
                "usage": {},
                "trace_events": [
                    {
                        "kind": "error",
                        "code": "503",
                        "request_started": None,
                        "physical_request_count": None,
                    }
                ],
            },
        }
        judgment["judge_attempts"] = [failed, successful]
        judgment.update(
            {
                "judge_attempt_count": 2,
                "judge_attempt_budget_used": 2,
                "judge_attempt_budget_remaining": 1,
                "judge_new_attempt_count": 2,
            }
        )

    row["judge"]["judge_attempt_count"] += 5
    row["judge"]["judge_new_attempt_count"] += 5
    row["error"] = "openrouter_non_byok_metadata_incomplete"
    row["completion_status"].update(
        {
            "cost_metadata_complete": False,
            "status": "incomplete",
        }
    )
    row["cost_accounting"]["actual_llm_cost_complete"] = False
    row["cost_accounting"]["actual_spend_cost_complete"] = False
    row["openrouter_non_byok_audit"].update(
        {
            "pass": False,
            "status": "metadata_incomplete",
        }
    )
    rows[0] = module.seal_result_row(row)
    path.write_text("".join(json.dumps(item) + "\n" for item in rows))
    path.chmod(0o600)

    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)

    assert manifest["status"] == "complete"
    ledger = [
        json.loads(line)
        for line in (args.output_dir / "actual-spend-ledger.jsonl").read_text().splitlines()
    ]
    unknown_judge = [
        item
        for item in ledger
        if item["cost_precision"] == "unknown"
        and item["scopes"] == ["judge"]
        and item["group_task_pairs"] == [{"group": row["group"], "task_id": row["task_id"]}]
    ]
    assert len(ledger) == 44
    assert len(unknown_judge) == 5
    assert all(item["recorded_cost_usd"] is None for item in unknown_judge)


def test_cleanup_failure_setup_attempt_blocks_unreconstructed_later_success(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=True)
    wave1 = [json.loads(line) for line in args.result[0].read_text().splitlines()]
    wave2 = [json.loads(line) for line in args.result[1].read_text().splitlines()]
    first = next(row for row in wave1 if row["group"] == "G1")
    second = next(row for row in wave2 if row["group"] == "G1")
    physical_attempt_id = "b" * 32
    unknown_analyzer = {
        "role": "unknown_request",
        "label": "task_analyzer",
        "provider": "",
        "model": "",
        "requested_provider": "openrouter",
        "requested_model": module.TASK_ANALYZER_MODEL,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
        "attempt": 1,
        "physical_attempt_id": physical_attempt_id,
        "provider_usage": {
            "usage_unknown": True,
            "physical_attempt_id": physical_attempt_id,
        },
    }
    failed_run = {
        "error": "TaskAnalyzerStreamCleanupError",
        "final_text_sha256": module.text_sha256(""),
        "llm_request_count": 1,
        "usage_unknown_count": 1,
        "usage": {"model_usage_breakdown": [deepcopy(unknown_analyzer)]},
        "trace_events": [
            {
                "kind": "error",
                "code": "provider_build_failed_after_setup",
                "request_started": False,
                "physical_request_count": 0,
            }
        ],
    }
    failed_attempt = {
        "attempt_id": "f" * 32,
        "attempt_kind": "provider_build_after_paid_setup",
        "attempt": 1,
        "started_at": 1_000.0,
        "completed_at": 1_002.0,
        "retryable": True,
        "retry_reason": "TaskAnalyzerStreamCleanupError",
        "retry_suppressed_reason": "",
        "will_retry": False,
        "retry_backoff_s": 0.0,
        "run": failed_run,
    }
    first.update(
        {
            "final_text": "",
            "final_text_chars": 0,
            "final_text_sha256": module.text_sha256(""),
            "selected_generation_succeeded": False,
            "generation_attempt_count": 1,
            "generation_attempt_budget_used": 1,
            "generation_attempt_total_billed_cost": 0.0,
            "llm_request_count": 1,
            "error": "TaskAnalyzerStreamCleanupError",
            "usage": {
                "model_usage_breakdown": [deepcopy(unknown_analyzer)],
                "billed_cost": 0.0,
            },
        }
    )
    first["actual_spend_metrics"]["generation_attempt_count"] = 1
    first["execution"]["generation_attempts"] = [failed_attempt]
    first["execution"]["run_error"] = "TaskAnalyzerStreamCleanupError"
    first["completion_status"].update({"generation_accepted": False, "status": "incomplete"})

    success = second["execution"]["generation_attempts"][0]
    success["attempt"] = 2
    second["execution"]["generation_attempts"] = [success]
    second["execution"]["prior_generation_attempts_used"] = 1
    second["execution"]["generation_reused"] = False
    second["execution"]["resume_action"] = "model_regenerate"
    second["generation_attempt_count"] = 1
    second["generation_attempt_budget_used"] = 2
    second["actual_spend_metrics"]["generation_attempt_count"] = 1
    wave1[wave1.index(first)] = module.seal_result_row(first)
    wave2[wave2.index(second)] = module.seal_result_row(second)
    args.result[0].write_text("".join(json.dumps(row) + "\n" for row in wave1))
    args.result[1].write_text("".join(json.dumps(row) + "\n" for row in wave2))
    wave2_manifest = json.loads(args.manifest[1].read_text())
    wave2_manifest["tool_policy"]["local_web_tools"]["preflight"].update(
        {
            "status": "passed",
            "preflight_calls": {"web_search": 1, "web_fetch": 1},
        }
    )
    wave2_manifest["resume_selection"]["model_regenerate_pair_count"] = 1
    _owner_json(args.manifest[1], wave2_manifest)
    try:
        with pytest.raises(
            module.FinalizationError,
            match="invalid paid G1 attempt",
        ):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)
    assert not args.output_dir.exists()


def test_explicit_byok_fails_policy_but_preserves_execution(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    row = rows[0]
    units = row["execution"]["generation_attempts"][0]["run"]["usage"]["model_usage_breakdown"]
    units[0]["provider_usage"]["is_byok"] = True
    units[0]["provider_usage"]["router_metadata"]["is_byok"] = True
    row["usage"]["model_usage_breakdown"][0]["provider_usage"]["is_byok"] = True
    row["usage"]["model_usage_breakdown"][0]["provider_usage"]["router_metadata"]["is_byok"] = True
    rows[0] = module.seal_result_row(row)
    path.write_text("".join(json.dumps(value) + "\n" for value in rows))
    path.chmod(0o600)
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)
    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    audit = json.loads((args.output_dir / "audit.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["execution_pass"] is True
    assert manifest["policy_pass"] is False
    assert proof["pass"] is False
    assert proof["publication_eligible"] is True
    assert proof["policy_pass"] is False
    assert proof["local_physical_request_evidence"]["explicit_byok_request_count"] == 1
    assert audit["execution_pass"] is True
    assert audit["policy_pass"] is False
    assert audit["pass"] is False


def test_generation_attempt_budget_over_three_is_fatal(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    first_attempt = rows[0]["execution"]["generation_attempts"][0]
    attempts = []
    for index in range(1, 5):
        attempt = deepcopy(first_attempt)
        attempt["attempt"] = index
        attempt["attempt_id"] = f"{100 + index:032x}"
        attempts.append(attempt)
    rows[0]["execution"]["generation_attempts"] = attempts
    rows[0]["generation_attempt_count"] = 4
    rows[0]["actual_spend_metrics"]["generation_attempt_count"] = 4
    rows[0]["generation_attempt_budget_used"] = 4
    rows[0] = module.seal_result_row(rows[0])
    path.write_text("".join(json.dumps(value) + "\n" for value in rows))
    path.chmod(0o600)
    try:
        with pytest.raises(
            module.FinalizationError,
            match="ordinal is invalid|exceeds 3|between 0 and 3",
        ):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)
    assert not args.output_dir.exists()


def test_b2_declared_quorum_mismatch_is_audit_only_when_candidates_prove_quorum(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    b2 = next(index for index, row in enumerate(rows) if row["group"] == "B2")
    rows[b2]["ensemble_trace"]["calls"][0]["successful_proposers"] = 2
    rows[b2] = module.seal_result_row(rows[b2])
    path.write_text("".join(json.dumps(value) + "\n" for value in rows))
    path.chmod(0o600)
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)
    assert manifest["status"] == "complete"
    assert manifest["execution_pass"] is True
    assert any("successful_proposer_count_mismatch" in warning for warning in manifest["warnings"])


def _b2_physical_call(
    module,
    *,
    proven_successes: int,
    declared_successes: int | None = None,
) -> dict[str, object]:
    trace = _ensemble_trace(
        list(module.B2_PROPOSERS),
        module.B2_AGGREGATOR,
        final_text="bound final answer",
        selection_mode="fixed",
        successful=(proven_successes if declared_successes is None else declared_successes),
    )
    call = trace["calls"][0]
    assert isinstance(call, dict)
    candidates = call["candidates"]
    assert isinstance(candidates, list)
    for candidate in candidates[proven_successes:]:
        assert isinstance(candidate, dict)
        candidate.update(
            {
                "ok": False,
                "error": "test proposer failure",
                "provider": "",
                "model": "",
                "content": {"text": "", "chars": 0, "truncated": False},
            }
        )
    return call


def _b2_physical_call_reasons(module, call: dict[str, object]) -> list[str]:
    return module.ensemble_physical_call_reasons(
        call,
        expected_proposers=module.B2_PROPOSERS,
        expected_aggregator=module.B2_AGGREGATOR,
        final_text="bound final answer",
        require_output_binding=True,
    )


def _analyzer_failure_fallback_physical_call(
    module,
    *,
    outcomes: tuple[str, str, str, str],
) -> tuple[dict[str, object], dict[str, object], list[str], str]:
    proposers = [
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.7-code",
        "qwen/qwen3.8-max",
        "deepseek/deepseek-v4-pro",
    ]
    aggregator = "z-ai/glm-5.2"
    selected_p = [f"openrouter:{model}" for model in proposers]
    selected_a = f"openrouter:{aggregator}"
    plan: dict[str, object] = {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "analyzer_failure_fallback": True,
        "analyzer_failure_fallback_schema": (
            "opensquilla.router-dynamic-analyzer-failure-fallback/v1"
        ),
        "selected_P": selected_p,
        "backup_P": [],
        "selected_A": selected_a,
        "aggregator_candidates": [selected_a],
        "proposer_count": 4,
        "proposer_sample_count": 4,
        "proposer_models": proposers,
        "aggregator_model": aggregator,
        "configured_proposer_backup_count": 0,
        "configured_min_successful_proposers": 1,
        "effective_min_successful_proposers": 1,
        "N_min": 1,
        "N_max": 4,
        "complete_proposers_only": True,
        "aggregator_max_recovery_actions": 1,
        "all_failed_policy": "error",
        "proposer_recovery_policy": (module.analyzer_failure_fallback_proposer_recovery_policy()),
        **deepcopy(module.FORMAL_AGGREGATOR_RECOVERY_POLICY),
    }
    trace = _ensemble_trace(
        proposers,
        aggregator,
        final_text="bound final answer",
        selection_mode="router_dynamic",
    )
    call = trace["calls"][0]
    assert isinstance(call, dict)
    recovery = call["aggregator_recovery"]
    assert isinstance(recovery, dict)
    recovery["max_recovery_actions"] = 1
    _formalize_provider_native_call(
        module,
        call,
        plan,
        scope_id="analyzer-failure-fixed-fallback",
    )

    candidates = call["candidates"]
    assert isinstance(candidates, list)
    for candidate, outcome in zip(candidates, outcomes, strict=True):
        assert isinstance(candidate, dict)
        execution = candidate["execution"]
        assert isinstance(execution, dict)
        attempts = execution["physical_attempts"]
        assert isinstance(attempts, list)
        physical_attempt = attempts[-1]
        assert isinstance(physical_attempt, dict)
        if outcome == "complete":
            continue
        if outcome == "partial_usable":
            text = str(candidate["content"]["text"])
            candidate.update(
                {
                    "ok": False,
                    "error": "length",
                    "error_code": "proposer_length_limit",
                    "usable_for_aggregation": True,
                    "completion_outcome": "partial_usable",
                    "stop_reason": "length",
                    "content": {
                        "text": text,
                        "chars": len(text),
                        "truncated": False,
                    },
                }
            )
            physical_attempt["outcome"] = "failed"
        else:
            candidate.update(
                {
                    "ok": False,
                    "error": "test proposer failure",
                    "error_code": "provider_error",
                    "usable_for_aggregation": False,
                    "completion_outcome": "failed",
                    "stop_reason": "error",
                    "content": {"text": "", "chars": 0, "truncated": False},
                }
            )
            physical_attempt["outcome"] = "failed"

    strict_successful = sum(
        module.successful_candidate(candidate) and candidate.get("completion_outcome") == "complete"
        for candidate in candidates
    )
    usable = sum(module.usable_candidate(candidate) for candidate in candidates)
    partial = sum(module.partial_usable_candidate(candidate) for candidate in candidates)
    call.update(
        {
            "successful_proposers": strict_successful,
            "usable_proposers": usable,
            "partial_proposers": partial,
            "strict_quorum_met": strict_successful >= 1,
            "complete_proposers_only": True,
            "execution_quorum_required": 1,
            "execution_quorum_met": strict_successful >= 1,
        }
    )
    proposer_recovery = call["proposer_recovery"]
    assert isinstance(proposer_recovery, dict)
    proposer_recovery.update(
        {
            "complete_proposers_only": True,
            "strict_successful_proposers": strict_successful,
            "usable_proposers": usable,
            "quorum_reached": strict_successful >= 1,
        }
    )
    return call, plan, proposers, aggregator


def _analyzer_failure_fallback_call_reasons(
    module,
    call: dict[str, object],
    proposers: list[str],
    aggregator: str,
) -> list[str]:
    return module.ensemble_physical_call_reasons(
        call,
        expected_proposers=proposers,
        expected_aggregator=aggregator,
        final_text="bound final answer",
        require_output_binding=True,
    )


def test_analyzer_failure_fallback_accepts_one_complete_and_one_length_partial(
    module,
) -> None:
    call, _, proposers, aggregator = _analyzer_failure_fallback_physical_call(
        module,
        outcomes=("complete", "partial_usable", "failed", "failed"),
    )

    reasons = _analyzer_failure_fallback_call_reasons(
        module,
        call,
        proposers,
        aggregator,
    )

    assert reasons == []


def test_analyzer_failure_fallback_rejects_partials_only(
    module,
) -> None:
    call, _, proposers, aggregator = _analyzer_failure_fallback_physical_call(
        module,
        outcomes=(
            "partial_usable",
            "partial_usable",
            "failed",
            "failed",
        ),
    )

    reasons = _analyzer_failure_fallback_call_reasons(
        module,
        call,
        proposers,
        aggregator,
    )

    assert "proposer_quorum_not_met" in reasons
    assert "insufficient_actual_proposer_quorum" in reasons


def test_analyzer_failure_fallback_accepts_zero_proposer_recovery_receipt(
    module,
) -> None:
    call, plan, _, _ = _analyzer_failure_fallback_physical_call(
        module,
        outcomes=("complete", "partial_usable", "failed", "failed"),
    )

    _, _, reasons = module.proposer_recovery_execution_reasons(
        call,
        executed_plan=plan,
        expected_policy=(module.analyzer_failure_fallback_proposer_recovery_policy()),
    )

    assert call["proposer_recovery"]["max_additional_physical_requests"] == 0
    assert call["proposer_recovery"]["attempts"] == []
    assert reasons == []


def test_g1_analyzer_failure_fallback_plan_requires_zero_recovery_policy(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ranking_parameters = _test_ranking_config(module)
    ranking_hash = module.canonical_sha256(ranking_parameters)
    monkeypatch.setattr(
        module,
        "FORMAL_G1_RANKING_CONFIG_SHA256",
        ranking_hash,
    )
    contract = _contract(module, "G1", "a" * 64)["g1_registry_contract"]
    fallback_routes = [
        ("z-ai/glm-5.2", "z-ai"),
        ("moonshotai/kimi-k2.7-code", "moonshotai"),
        ("qwen/qwen3.8-max", "alibaba"),
        ("deepseek/deepseek-v4-pro", "deepseek"),
    ]
    contract["analyzer_failure_fallback_ensemble"] = {
        "proposers": [
            {
                "provider": "openrouter",
                "model": model,
                "upstream_provider": upstream,
                "max_attempts": 1,
            }
            for model, upstream in fallback_routes
        ],
        "aggregator": {
            "provider": "openrouter",
            "model": "z-ai/glm-5.2",
            "upstream_provider": "z-ai",
            "max_attempts": 1,
        },
        "min_successful_proposers": 1,
        "complete_proposers_only": True,
        "aggregator_max_recovery_actions": 1,
    }
    _, plan, _, _ = _analyzer_failure_fallback_physical_call(
        module,
        outcomes=("complete", "partial_usable", "failed", "failed"),
    )
    plan.update(
        {
            "task_analyzer": {
                "schema_valid": False,
                "chain": {"exhausted": True},
            },
            "ranking_config_schema_version": contract["expected_ranking_config_schema_version"],
            "ranking_config_version": contract["expected_ranking_config_version"],
            "ranking_config_hash": ranking_hash,
            "ranking_parameters": ranking_parameters,
        }
    )

    reasons, _, _ = module.g1_registry_plan_reasons(
        plan,
        contract=contract,
    )
    ordinary_policy = deepcopy(plan)
    ordinary_policy["proposer_recovery_policy"] = deepcopy(module.FORMAL_PROPOSER_RECOVERY_POLICY)
    wrong_reasons, _, _ = module.g1_registry_plan_reasons(
        ordinary_policy,
        contract=contract,
    )

    assert reasons == []
    assert "wrong_g1_proposer_recovery_policy" in wrong_reasons


def test_analyzer_failure_fallback_rejects_aggregator_recovery_over_budget(
    module,
) -> None:
    call, _, proposers, aggregator = _analyzer_failure_fallback_physical_call(
        module,
        outcomes=("complete", "partial_usable", "failed", "failed"),
    )
    recovery = call["aggregator_recovery"]
    assert isinstance(recovery, dict)
    attempts = recovery["attempts"]
    assert isinstance(attempts, list)
    for attempt_number in (2, 3):
        attempts.append(
            {
                "attempt": attempt_number,
                "physical_attempt_index": attempt_number,
                "physical_request_count": 1,
                "physical_attempt_id": _physical_attempt_id(
                    f"analyzer-fallback-aggregator-overrun-{attempt_number}"
                ),
                "kind": "same_model_recovery",
                "fallback_index": 0,
                "trigger": "ensemble_aggregator_error",
                "request_started": True,
                "visible_output_emitted": False,
                "stream_closed": True,
                "outcome": "failed",
                "stop_reason": "error",
                "requested_provider": "openrouter",
                "requested_model": aggregator,
                "actual_provider": "openrouter",
                "actual_model": aggregator,
            }
        )
    recovery["same_model_recovery_count"] = 2
    call["llm_request_count"] += 2
    call["physical_request_count"] += 2

    reasons = _analyzer_failure_fallback_call_reasons(
        module,
        call,
        proposers,
        aggregator,
    )

    assert "aggregator_recovery_action_budget_exceeded" in reasons


def test_analyzer_failure_fallback_one_complete_proves_execution_evidence(
    module,
) -> None:
    final_text = "bound final answer"
    call, _, _, _ = _analyzer_failure_fallback_physical_call(
        module,
        outcomes=("complete", "partial_usable", "failed", "failed"),
    )
    row = {
        "final_text": final_text,
        "final_text_chars": len(final_text),
        "final_text_sha256": module.text_sha256(final_text),
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 1,
            "untraced_agent_llm_call_count": 0,
            "calls": [call],
        },
    }

    assert module.row_has_bound_answer_and_proposer_quorum(row) is True


@pytest.mark.parametrize(
    ("physical_counts", "selected_index", "exhausted", "expected_models"),
    [
        ([0, 1], 1, False, ["openai/gpt-5.6-sol"]),
        ([0, 0, 0], None, True, []),
    ],
)
def test_analyzer_chain_trace_derives_only_physical_request_routes(
    module,
    physical_counts: list[int],
    selected_index: int | None,
    exhausted: bool,
    expected_models: list[str],
) -> None:
    chain = [
        {
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "upstream_provider": "anthropic",
            "max_attempts": 1,
        },
        {
            "provider": "openrouter",
            "model": "openai/gpt-5.6-sol",
            "upstream_provider": "azure",
            "max_attempts": 1,
        },
        {
            "provider": "openrouter",
            "model": "google/gemini-3.1-pro-preview",
            "upstream_provider": "google-ai-studio",
            "max_attempts": 1,
        },
    ]
    outcomes = []
    for index, physical_count in enumerate(physical_counts):
        route = chain[index]
        outcomes.append(
            {
                "candidate_index": index,
                "provider": route["provider"],
                "model": route["model"],
                "upstream_provider": route["upstream_provider"],
                "outcome": ("success" if selected_index == index else "failed"),
                "reason": "" if selected_index == index else "provider_unavailable",
                "physical_request_count": physical_count,
            }
        )
    plan = {
        "task_analyzer": {
            "source": "llm_provider" if selected_index is not None else "router_fallback",
            "schema_valid": selected_index is not None,
            "provider": chain[
                selected_index if selected_index is not None else len(outcomes) - 1
            ]["provider"],
            "model": chain[
                selected_index if selected_index is not None else len(outcomes) - 1
            ]["model"],
            "fallback_reason": "" if selected_index is not None else "provider_unavailable",
            "chain": {
                "protocol": "opensquilla.task-analyzer-fallback-chain/v1",
                "configured_routes": [
                    {key: route[key] for key in ("provider", "model", "upstream_provider")}
                    for route in chain
                ],
                "attempt_outcomes": outcomes,
                "selected_index": selected_index,
                "exhausted": exhausted,
            },
            "usage": {
                "attempt_count": sum(physical_counts),
                "physical_attempts": [
                    {
                        "attempt": ordinal,
                        "physical_attempt_id": f"{ordinal:032x}",
                        "provider": chain[index]["provider"],
                        "model": chain[index]["model"],
                        "requested_provider": chain[index]["provider"],
                        "requested_model": chain[index]["model"],
                        "input_tokens": 10,
                        "output_tokens": 1,
                        "reasoning_tokens": 0,
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                        "billed_cost": 0.0,
                        "provider_usage": {
                            "provider": chain[index]["provider"],
                            "model": chain[index]["model"],
                            "physical_attempt_id": f"{ordinal:032x}",
                        },
                    }
                    for ordinal, index in enumerate(
                        (
                            index
                            for index, count in enumerate(physical_counts)
                            for _ in range(count)
                        ),
                        start=1,
                    )
                ],
            },
        }
    }
    from opensquilla.eval.draco_task_analyzer_execution import (
        build_task_analyzer_execution_contract,
    )

    execution_contract = build_task_analyzer_execution_contract(
        routes=chain,
        schema_repair_max_retries=0,
        total_timeout_seconds=60.0,
        route_source="test.live_task_analyzer_chain",
        source_payload={"live_task_analyzer_chain": chain},
    )

    routes, reasons = module.g1_task_analyzer_physical_routes_from_trace(
        plan,
        execution_contract,
    )

    assert reasons == []
    assert [route["model"] for route in routes] == expected_models


def test_analyzer_chain_trace_rejects_boolean_physical_request_count(
    module,
) -> None:
    chain = [
        {
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "upstream_provider": "anthropic",
            "max_attempts": 1,
        }
    ]
    route = {
        key: chain[0][key]
        for key in ("provider", "model", "upstream_provider")
    }
    plan = {
        "task_analyzer": {
            "source": "router_fallback",
            "schema_valid": False,
            "provider": route["provider"],
            "model": route["model"],
            "fallback_reason": "provider_unavailable",
            "chain": {
                "protocol": "opensquilla.task-analyzer-fallback-chain/v1",
                "configured_routes": [route],
                "attempt_outcomes": [
                    {
                        "candidate_index": 0,
                        **route,
                        "outcome": "failed",
                        "reason": "provider_unavailable",
                        "physical_request_count": False,
                    }
                ],
                "selected_index": None,
                "exhausted": True,
            },
            "usage": {"attempt_count": 0, "physical_attempts": []},
        }
    }
    from opensquilla.eval.draco_task_analyzer_execution import (
        build_task_analyzer_execution_contract,
    )

    execution_contract = build_task_analyzer_execution_contract(
        routes=chain,
        schema_repair_max_retries=0,
        total_timeout_seconds=60.0,
        route_source="test.live_task_analyzer_chain",
        source_payload={"live_task_analyzer_chain": chain},
    )

    routes, reasons = module.g1_task_analyzer_physical_routes_from_trace(
        plan,
        execution_contract,
    )

    assert routes == []
    assert reasons == ["invalid_g1_task_analyzer_chain_trace"]


def test_b2_physical_call_accepts_failed_candidate_without_actual_route(
    module,
) -> None:
    call = _b2_physical_call(module, proven_successes=3)

    assert _b2_physical_call_reasons(module, call) == []


def test_b2_physical_call_rejects_wrong_failed_candidate_requested_route(
    module,
) -> None:
    call = _b2_physical_call(module, proven_successes=3)
    candidate = call["candidates"][-1]
    assert isinstance(candidate, dict)
    candidate["requested_provider"] = "direct"
    candidate["requested_model"] = "vendor/wrong-model"

    reasons = _b2_physical_call_reasons(module, call)
    assert "wrong_actual_proposer_provider" in reasons
    assert "wrong_actual_proposer_model" in reasons


def test_b2_physical_call_rejects_failed_candidate_conflicting_actual_route(
    module,
) -> None:
    call = _b2_physical_call(module, proven_successes=3)
    candidate = call["candidates"][-1]
    assert isinstance(candidate, dict)
    candidate["provider"] = "direct"
    candidate["model"] = "vendor/wrong-model"

    reasons = _b2_physical_call_reasons(module, call)
    assert "wrong_actual_proposer_provider" in reasons
    assert "wrong_actual_proposer_model" in reasons


def test_b2_physical_call_rejects_successful_candidate_without_actual_route(
    module,
) -> None:
    call = _b2_physical_call(module, proven_successes=3)
    candidate = call["candidates"][0]
    assert isinstance(candidate, dict)
    candidate["provider"] = ""
    candidate["model"] = ""

    reasons = _b2_physical_call_reasons(module, call)
    assert "wrong_actual_proposer_provider" in reasons
    assert "wrong_actual_proposer_model" in reasons


def test_b2_physical_call_rejects_declared_success_count_above_proven_count(
    module,
) -> None:
    call = _b2_physical_call(
        module,
        proven_successes=3,
        declared_successes=4,
    )

    assert "successful_proposer_count_mismatch" in _b2_physical_call_reasons(
        module,
        call,
    )


def test_b2_physical_call_accepts_two_of_four_usable_quorum(module) -> None:
    call = _b2_physical_call(module, proven_successes=2)

    assert _b2_physical_call_reasons(module, call) == []


def test_static_b2_does_not_replace_strict_success_with_partial_quorum(module) -> None:
    call = _b2_physical_call(
        module,
        proven_successes=1,
        declared_successes=2,
    )
    candidate = call["candidates"][1]
    model = module.B2_PROPOSERS[1]
    candidate.update(
        {
            "usable_for_aggregation": True,
            "completion_outcome": "partial_usable",
            "provider": "openrouter",
            "model": model,
            "content": {
                "text": "",
                "chars": 12,
                "truncated": True,
                "sha256": "a" * 64,
            },
        }
    )

    assert module.successful_candidate(candidate) is False
    assert module.partial_usable_candidate(candidate) is True
    reasons = _b2_physical_call_reasons(module, call)
    assert "successful_proposer_count_mismatch" in reasons
    assert "insufficient_actual_proposer_quorum" in reasons


def test_degraded_generation_requires_closed_explicit_evidence(module) -> None:
    selected_attempt = {
        "attempt": 1,
        "kind": "primary",
        "outcome": "failed",
        "code": "ensemble_aggregator_close_timeout",
        "physical_request_count": 1,
        "delivery_selected": True,
        "visible_output_emitted": True,
        "request_started": True,
    }
    row = {
        "execution_status": {
            "status": "degraded_success",
            "success": True,
        },
        "ensemble_trace": {
            "execution_outcome": "degraded_success",
            "delivery_outcome": "degraded_success",
            "run_outcome": "ensemble_aggregator_close_timeout",
            "assembled_output": {
                "text": "usable answer",
                "chars": 13,
                "truncated": False,
            },
            "aggregator_recovery": {
                "degraded": True,
                "success": False,
                "delivery_success": True,
                "delivery_outcome": "degraded_success",
                "audit_outcome": "incomplete",
                "recovery_skipped": True,
                "tools_disabled_after_partial_output": True,
                "selected_kind": "degraded_delivery",
                "run_outcome": "ensemble_aggregator_close_timeout",
                "terminal_code": "ensemble_aggregator_close_timeout",
                "selected_attempt": 1,
                "attempts": [selected_attempt],
            },
        },
    }

    assert module.explicit_degraded_success(row) is True
    assert (
        module.audit_only_error_text(
            "ensemble_aggregator_close_timeout",
            degraded_success=True,
        )
        is True
    )

    row["ensemble_trace"]["aggregator_recovery"]["attempts"][0]["delivery_selected"] = False
    assert module.explicit_degraded_success(row) is False
    assert module.audit_only_error_text("stream_protocol_identity_conflict") is False
    assert module.audit_only_error_text("usage_receipt_tampered") is False


def test_unaccepted_generation_is_blocking_without_degraded_receipt(module) -> None:
    blocking, warnings = module.partition_execution_and_audit_reasons(
        ["selected_generation_not_successful", "generation_not_accepted"]
    )

    assert blocking == [
        "selected_generation_not_successful",
        "generation_not_accepted",
    ]
    assert warnings == []


def test_bound_quorum_demotes_identity_and_recovery_evidence(module) -> None:
    final_text = "A complete, judgeable ensemble answer."
    trace = _ensemble_trace(
        ["model-a", "model-b", "model-c"],
        "aggregator-a",
        final_text=final_text,
        selection_mode="router_dynamic",
    )
    row = {
        "final_text": final_text,
        "final_text_chars": len(final_text),
        "final_text_sha256": module.text_sha256(final_text),
        "ensemble_trace": trace,
    }

    assert module.row_has_bound_answer_and_proposer_quorum(row) is True
    blocking, warnings = module.partition_execution_and_audit_reasons(
        [
            "wrong_requested_proposer_identity",
            "g1_attempt_plan_provenance_invalid",
            "proposer_recovery_physical_attempt_set_mismatch",
        ],
        evidence_proven=True,
    )

    assert blocking == []
    assert warnings == [
        "wrong_requested_proposer_identity",
        "g1_attempt_plan_provenance_invalid",
        "proposer_recovery_physical_attempt_set_mismatch",
    ]


def test_actual_proposer_quorum_remains_blocking(module) -> None:
    final_text = "A non-empty answer cannot substitute for ensemble quorum."
    trace = _ensemble_trace(
        ["model-a", "model-b", "model-c"],
        "aggregator-a",
        final_text=final_text,
        selection_mode="router_dynamic",
    )
    candidates = trace["calls"][0]["candidates"]
    for candidate in candidates[1:]:
        candidate.update(
            {
                "ok": False,
                "error": "failed",
                "content": {"text": "", "chars": 0, "truncated": False},
            }
        )
    row = {
        "final_text": final_text,
        "final_text_chars": len(final_text),
        "final_text_sha256": module.text_sha256(final_text),
        "ensemble_trace": trace,
    }

    assert module.row_has_bound_answer_and_proposer_quorum(row) is False
    blocking, warnings = module.partition_execution_and_audit_reasons(
        [
            "wrong_requested_proposer_identity",
            "insufficient_actual_proposer_quorum",
        ],
        evidence_proven=True,
    )

    assert blocking == ["insufficient_actual_proposer_quorum"]
    assert warnings == ["wrong_requested_proposer_identity"]


def test_judge_audit_demotion_is_closed_to_missing_usage(module) -> None:
    assert (
        module.judge_evidence_error_is_audit_only(
            module.FinalizationError(
                "Judge attempt abc does not represent every physical request in usage"
            )
        )
        is True
    )
    for message in (
        "Judge attempt id abc is reused by another unit",
        "Judge attempt budget exceeds 3",
        "Judge attempt abc receipt repair conflicts on exact cost",
        "Judge attempt abc violates the frozen route",
    ):
        assert module.judge_evidence_error_is_audit_only(module.FinalizationError(message)) is False


def test_nonzero_decimal_byok_delta_fails_policy_not_execution(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    after = json.loads(args.account_after.read_text())
    after["byok_usage"] = "0.000000001"
    _owner_json(args.account_after, after)
    reconciliation = json.loads(args.account_reconciliation.read_text())
    reconciliation["byok_usage_after_usd"] = "0.000000001"
    reconciliation["byok_usage_delta_usd"] = "0.000000001"
    for observation in reconciliation["stable_observations"]:
        observation["byok_usage"] = "0.000000001"
    _owner_json(args.account_reconciliation, reconciliation)
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)
    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["execution_pass"] is True
    assert manifest["policy_pass"] is False
    assert proof["pass"] is False
    assert proof["account"]["campaign_byok_usage_delta_usd"] == "1E-9"


def test_incomplete_settlement_publishes_explicit_account_audit_conflict(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    reconciliation = json.loads(args.account_reconciliation.read_text())
    reconciliation["settlement_status"] = "pending"
    _owner_json(args.account_reconciliation, reconciliation)
    try:
        manifest = _assert_account_audit_conflict(
            module,
            args,
            warning="account reconciliation is not stable",
        )
    finally:
        os.close(lock_fd)

    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    assert manifest["account_windows"] == []
    assert manifest["cost_attribution"]["account_windows"] == []
    assert proof["account_windows"] == []


def test_prior_campaign_byok_delta_preserves_actual_account_cost(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    prior = _bind_prior_campaign_window(module, args, tmp_path)
    prior_after_path = prior / "openrouter-account-after.json"
    prior_after = json.loads(prior_after_path.read_text())
    prior_after["byok_usage"] = "0.05"
    _owner_json(prior_after_path, prior_after)
    prior_reconciliation_path = prior / "openrouter-account-reconciliation.json"
    prior_reconciliation = json.loads(prior_reconciliation_path.read_text())
    prior_reconciliation.update(
        {
            "byok_usage_after_usd": "0.05",
            "byok_usage_delta_usd": "0.05",
        }
    )
    for observation in prior_reconciliation["stable_observations"]:
        observation["byok_usage"] = "0.05"
    _owner_json(prior_reconciliation_path, prior_reconciliation)

    for path in (args.account_before, args.account_after):
        payload = json.loads(path.read_text())
        payload["byok_usage"] = "0.05"
        _owner_json(path, payload)
    reconciliation = json.loads(args.account_reconciliation.read_text())
    reconciliation.update(
        {
            "byok_usage_before_usd": "0.05",
            "byok_usage_after_usd": "0.05",
            "byok_usage_delta_usd": "0",
        }
    )
    for observation in reconciliation["stable_observations"]:
        observation["byok_usage"] = "0.05"
    _owner_json(args.account_reconciliation, reconciliation)

    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)

    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["execution_pass"] is True
    assert manifest["policy_pass"] is False
    assert proof["account"]["campaign_usage_delta_usd"] == "3.9"
    assert proof["account"]["campaign_byok_usage_delta_usd"] == "0.05"
    assert proof["cost_scope"]["campaign_bound_account_window_total_usd"] == "3.9"


def test_unexplained_account_to_exact_ledger_delta_is_a_reconciliation_warning(
    module, tmp_path: Path
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    after = json.loads(args.account_after.read_text())
    after["usage"] = "14"
    _owner_json(args.account_after, after)
    reconciliation = json.loads(args.account_reconciliation.read_text())
    reconciliation["usage_after_usd"] = "14"
    reconciliation["usage_delta_usd"] = "4"
    for observation in reconciliation["stable_observations"]:
        observation["usage"] = "14"
    _owner_json(args.account_reconciliation, reconciliation)
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)
    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["execution_pass"] is True
    assert manifest["policy_pass"] is True
    assert manifest["reconciliation"]["pass"] is False
    assert proof["pass"] is True
    assert proof["reconciliation"]["status"] == "conflict"
    assert any("unexplained OpenRouter" in value for value in proof["warnings"])


def test_invalid_settlement_or_lock_fails_audit_without_erasing_execution(
    module, tmp_path: Path
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    reconciliation = json.loads(args.account_reconciliation.read_text())
    reconciliation["stable_poll_count"] = 2
    _owner_json(args.account_reconciliation, reconciliation)
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)
    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["execution_pass"] is True
    assert manifest["policy_pass"] is False
    assert proof["pass"] is False
    assert "stable_poll_count" in proof["warnings"][0]

    other = tmp_path / "other"
    args2, _, lock_fd2 = _campaign(module, other)
    fcntl.flock(lock_fd2, fcntl.LOCK_UN)
    try:
        with pytest.raises(module.FinalizationError, match="not already held"):
            module.run_finalization(args2)
    finally:
        os.close(lock_fd2)
    assert not (args2.output_dir / "openrouter-non-byok-campaign-proof.json").exists()


def test_source_hash_recheck_detects_mutation(module, tmp_path: Path) -> None:
    source = tmp_path / "rows.jsonl"
    source.write_text("{}\n")
    source.chmod(0o600)
    snapshots = {str(source.resolve()): module.file_sha256(source)}
    source.write_text('{"changed":true}\n')
    with pytest.raises(module.FinalizationError, match="changed during finalization"):
        module.verify_source_snapshots(snapshots)


def test_source_shard_index_hashes_and_parses_in_one_file_open(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "rows.jsonl"
    rows = [
        module.seal_result_row(
            {"group": "B0", "task_id": "task-1", "text": "before\u2028after"}
        ),
        module.seal_result_row(
            {"group": "B1", "task_id": "task-2", "text": "第二行"}
        ),
    ]
    payload = (
        json.dumps(rows[0], ensure_ascii=False)
        + "\r\n\r\n"
        + json.dumps(rows[1], ensure_ascii=False)
        + "\n"
    ).encode()
    source.write_bytes(payload)
    source.chmod(0o600)
    resolved_source = source.resolve()
    original_open = Path.open
    source_opens: list[tuple[object, ...]] = []

    def tracking_open(path: Path, *args: object, **kwargs: object):
        if path.resolve() == resolved_source:
            source_opens.append(args)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracking_open)
    indexes = module.read_source_shard_indexes([source])

    assert len(source_opens) == 1
    assert indexes[0].sha256 == hashlib.sha256(payload).hexdigest()
    assert [record.line for record in indexes[0].records] == [1, 3]
    assert [record.row["text"] for record in indexes[0].records] == [
        "before\u2028after",
        "第二行",
    ]


def test_source_shard_index_rejects_bare_carriage_return_boundaries(
    module,
    tmp_path: Path,
) -> None:
    source = tmp_path / "rows.jsonl"
    rows = [
        module.seal_result_row({"group": "B0", "task_id": "task-1"}),
        module.seal_result_row({"group": "B1", "task_id": "task-2"}),
    ]
    source.write_text("\r".join(json.dumps(row) for row in rows))
    source.chmod(0o600)

    with pytest.raises(module.FinalizationError, match="invalid result JSONL"):
        module.read_source_shard_indexes([source])


def test_manifest_contracts_reuse_preloaded_result_index(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    try:
        indexes = module.read_source_shard_indexes(args.result)
        result_path = args.result[0].resolve()
        original_hash = module.file_sha256
        original_load = module.load_jsonl_rows

        def guarded_hash(path: Path) -> str:
            if path.resolve() == result_path:
                raise AssertionError("preloaded result shard was hashed again")
            return original_hash(path)

        def guarded_load(
            path: Path,
            *,
            owner_only: bool,
            source_label: str,
        ):
            if path.resolve() == result_path:
                raise AssertionError("preloaded result shard was parsed again")
            return original_load(
                path,
                owner_only=owner_only,
                source_label=source_label,
            )

        monkeypatch.setattr(module, "file_sha256", guarded_hash)
        monkeypatch.setattr(module, "load_jsonl_rows", guarded_load)
        _, _, _, sources = module.load_manifest_contracts(
            args.manifest,
            result_paths=args.result,
            groups=module.GROUPS,
            result_indexes=indexes,
        )

        assert sources[0]["result_sha256"] == indexes[0].sha256
        assert sources[0]["rows_written"] == len(indexes[0].records)
    finally:
        os.close(lock_fd)


def test_manifest_contracts_reject_preloaded_index_with_wrong_shard_ordinal(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    try:
        indexes = module.read_source_shard_indexes(args.result)
        first = indexes[0]
        forged = module.SourceShardIndex(
            path=first.path,
            source_index=1,
            sha256=first.sha256,
            records=tuple(
                module.SourceRecord(
                    path=record.path,
                    source_index=1,
                    line=record.line,
                    row=record.row,
                )
                for record in first.records
            ),
        )

        with pytest.raises(module.FinalizationError, match="not bound"):
            module.load_manifest_contracts(
                args.manifest,
                result_paths=args.result,
                groups=module.GROUPS,
                result_indexes=(forged, *indexes[1:]),
            )
    finally:
        os.close(lock_fd)


def test_finalization_rechecks_result_after_index_reuse(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    original_load = module.load_manifest_contracts

    def mutate_after_manifest_validation(*call_args: object, **call_kwargs: object):
        result = original_load(*call_args, **call_kwargs)
        source = args.result[0]
        source.write_bytes(source.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(
        module,
        "load_manifest_contracts",
        mutate_after_manifest_validation,
    )
    try:
        with pytest.raises(module.FinalizationError, match="changed during finalization"):
            module.run_finalization(args)
        assert not args.output_dir.exists()
    finally:
        os.close(lock_fd)


def _compact_selection_plan_values(
    value: object,
    *,
    appender: SelectionPlanPackAppender,
    field_name: str | None = None,
    embedded_plan_marker: tuple[str, object] | None = None,
) -> object:
    if isinstance(value, dict):
        if field_name == "selection_plan":
            selection_plan = deepcopy(value)
            if embedded_plan_marker is not None:
                marker_field, marker_value = embedded_plan_marker
                selection_plan[marker_field] = deepcopy(marker_value)
            return appender.store_selection_plan(selection_plan)
        return {
            key: _compact_selection_plan_values(
                item,
                appender=appender,
                field_name=key,
                embedded_plan_marker=embedded_plan_marker,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _compact_selection_plan_values(
                item,
                appender=appender,
                embedded_plan_marker=embedded_plan_marker,
            )
            for item in value
        ]
    return value


def _without_selection_plan_values(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_selection_plan_values(item)
            for key, item in value.items()
            if key != "selection_plan"
        }
    if isinstance(value, list):
        return [_without_selection_plan_values(item) for item in value]
    return value


def _compact_source_fixture(
    module,
    tmp_path: Path,
    *,
    nested_row_marker: bool = False,
    embedded_plan_marker: bool = False,
    header_only: bool = False,
) -> tuple[argparse.Namespace, Path, int]:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    legacy_rows = [
        json.loads(line)
        for line in args.result[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    stamp = "20260811-010101"
    results_path = tmp_path / f"draco_ensemble_{stamp}.jsonl"
    trace_path = tmp_path / f"draco_run_{stamp}.trace.jsonl"
    checkpoint_path = tmp_path / f"draco_run_{stamp}.checkpoint.json"
    manifest_path = tmp_path / f"draco_run_{stamp}.manifest.json"
    pack_path = tmp_path / f"draco_run_{stamp}.selection-plan.pack.jsonl"
    capability = module.selection_plan_evidence_capability_contract()
    durability = module.durable_artifact_capability_contract()
    manifest = json.loads(args.manifest[0].read_text(encoding="utf-8"))
    compatibility = deepcopy(manifest["run_compatibility"])
    for group in module.GROUPS:
        contract = deepcopy(compatibility["contracts"][group])
        contract["durable_artifact_capability"] = deepcopy(durability)
        compatibility["contracts"][group] = contract
        compatibility["fingerprints"][group] = module.canonical_sha256(
            contract,
            prefix=True,
        )

    compact_rows: list[dict[str, object]] = []
    with SelectionPlanPackAppender(pack_path) as appender:
        for legacy_row in legacy_rows:
            unsealed = deepcopy(legacy_row)
            unsealed["run_compatibility_fingerprint"] = compatibility[
                "fingerprints"
            ][str(unsealed["group"])]
            unsealed[module.SELECTION_PLAN_EVIDENCE_ROW_FIELD] = deepcopy(
                capability
            )
            if nested_row_marker:
                unsealed["nested_capability_probe"] = {
                    module.SELECTION_PLAN_EVIDENCE_ROW_FIELD: deepcopy(
                        capability
                    )
                }
            unsealed[module.DRACO_DURABLE_RESULT_ROW_FIELD] = deepcopy(durability)
            if header_only:
                without_plans = _without_selection_plan_values(unsealed)
                assert isinstance(without_plans, dict)
                unsealed = without_plans
            compact = _compact_selection_plan_values(
                unsealed,
                appender=appender,
                embedded_plan_marker=(
                    (
                        module.SELECTION_PLAN_EVIDENCE_ROW_FIELD,
                        capability,
                    )
                    if embedded_plan_marker
                    else None
                ),
            )
            assert isinstance(compact, dict)
            compact_rows.append(module.seal_result_row(compact))
        pack_sha256 = appender.index.pack_sha256
        pack_bytes = appender.index.pack_bytes
        pack_object_count = appender.index.object_count

    with DurableDracoArtifactWriter(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
        create=True,
    ) as writer:
        for row in compact_rows:
            assert writer.append(row, canonical_trace_row_from_result(row))
    artifact_verification = module.verify_durable_draco_artifacts(
        results_path=results_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    )

    manifest.update(
        {
            "schema": module.DRACO_RUN_MANIFEST_SCHEMA_V2,
            "stamp": stamp,
            "rows_written": len(compact_rows),
            "durable_artifact_capability": deepcopy(durability),
            "run_compatibility": compatibility,
            module.SELECTION_PLAN_EVIDENCE_ROW_FIELD: deepcopy(capability),
            module.SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD: {
                "schema": module.SELECTION_PLAN_EVIDENCE_MANIFEST_SCHEMA,
                "capability": deepcopy(capability),
                "result_evidence_schema": module.RESULT_EVIDENCE_SCHEMA,
                "durable_artifact_capability": deepcopy(durability),
                "results_sha256": artifact_verification["results_sha256"],
                "trace_sha256": artifact_verification["trace_sha256"],
                "checkpoint_sha256": artifact_verification[
                    "checkpoint_sha256"
                ],
                "pack_sha256": pack_sha256,
                "pack_bytes": pack_bytes,
                "pack_object_count": pack_object_count,
                "compact_row_count": sum(
                    module.selection_plan_reference_signal(row)
                    for row in compact_rows
                ),
            },
            "artifacts": {
                "results_jsonl": str(results_path),
                "trace_jsonl": str(trace_path),
                "checkpoint_json": str(checkpoint_path),
                "manifest_json": str(manifest_path),
                module.SELECTION_PLAN_PACK_ARTIFACT_FIELD: str(pack_path),
            },
        }
    )
    _owner_json(manifest_path, manifest)
    args.result = [results_path]
    args.manifest = [manifest_path]
    return args, pack_path, lock_fd


def test_compact_finalizer_accepts_header_only_zero_ref_rows_and_reseals_output(
    module,
    tmp_path: Path,
) -> None:
    args, pack_path, lock_fd = _compact_source_fixture(
        module,
        tmp_path,
        header_only=True,
    )
    try:
        source_manifest = json.loads(args.manifest[0].read_text())
        binding = source_manifest[module.SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD]
        assert binding["pack_object_count"] == 0
        assert binding["compact_row_count"] == 0
        with module.SelectionPlanPackReader(pack_path, owner_only=True) as reader:
            assert reader.index.object_count == 0

        final_manifest = module.run_finalization(args)
        assert final_manifest["status"] == "complete"
        output_rows = [
            json.loads(line)
            for line in (args.output_dir / "results.jsonl").read_text().splitlines()
        ]
        assert output_rows
        assert all(
            module.SELECTION_PLAN_EVIDENCE_ROW_FIELD not in row
            for row in output_rows
        )
        assert all(module.verify_result_row_evidence(row) for row in output_rows)
    finally:
        os.close(lock_fd)


def _open_compact_finalizer_source_entry(module, args: argparse.Namespace):
    raw_indexes = module.read_source_shard_indexes(args.result)
    _, _, _, manifest_sources = module.load_manifest_contracts(
        args.manifest,
        result_paths=args.result,
        groups=module.GROUPS,
        result_indexes=raw_indexes,
    )
    views = module.open_source_selection_plan_views(
        raw_indexes,
        manifest_sources,
    )
    return raw_indexes, manifest_sources, views


def test_compact_finalizer_source_entry_binds_and_expands_after_raw_evidence(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _compact_source_fixture(module, tmp_path)
    views = ()
    try:
        raw_indexes, manifest_sources, views = _open_compact_finalizer_source_entry(
            module,
            args,
        )
        raw_record = raw_indexes[0].records[-1]
        expanded_record = views[0].records[-1]
        assert module.selection_plan_reference_signal(raw_record.row)
        assert not module.selection_plan_reference_signal(expanded_record.row)
        assert (
            module.SELECTION_PLAN_EVIDENCE_ROW_FIELD not in expanded_record.row
        )
        assert expanded_record.path == raw_record.path
        assert expanded_record.source_index == raw_record.source_index
        assert expanded_record.line == raw_record.line
        assert views[0].raw_index is raw_indexes[0]
        assert manifest_sources[0]["result_sha256"] == raw_indexes[0].sha256
        assert (
            expanded_record.row["routing_trace"]["selection_plan"]["decision_id"]
            == raw_record.row["routing_trace"]["selection_plan"]["summary"][
                "decision_id"
            ]
        )
        module.verify_and_close_source_selection_plan_views(views)
        views = ()
        final_manifest = module.run_finalization(args)
        assert final_manifest["status"] == "complete"
        final_rows = [
            json.loads(line)
            for line in (args.output_dir / "results.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert all(
            not module.selection_plan_reference_signal(row) for row in final_rows
        )
        assert all(
            not module.selection_plan_row_capability_signal(row)
            for row in final_rows
        )
        assert all(module.verify_result_row_evidence(row) for row in final_rows)
    finally:
        for view in views:
            view.close()
        os.close(lock_fd)


@pytest.mark.parametrize(
    ("fixture_kwargs", "error_match"),
    [
        (
            {"nested_row_marker": True},
            "row capability was stripped or downgraded",
        ),
        (
            {"embedded_plan_marker": True},
            "retains a reserved capability marker",
        ),
    ],
)
def test_compact_finalizer_rejects_non_root_reserved_capability_markers(
    module,
    tmp_path: Path,
    fixture_kwargs: dict[str, bool],
    error_match: str,
) -> None:
    args, _, lock_fd = _compact_source_fixture(
        module,
        tmp_path,
        **fixture_kwargs,
    )
    try:
        with pytest.raises(module.FinalizationError, match=error_match):
            module.run_finalization(args)
        assert not args.output_dir.exists()
    finally:
        os.close(lock_fd)


def test_legacy_inline_finalizer_source_view_is_identity_and_byte_preserving(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    source_bytes = args.result[0].read_bytes()
    views = ()
    try:
        indexes = module.read_source_shard_indexes(args.result)
        _, _, _, manifest_sources = module.load_manifest_contracts(
            args.manifest,
            result_paths=args.result,
            groups=module.GROUPS,
            result_indexes=indexes,
        )
        views = module.open_source_selection_plan_views(indexes, manifest_sources)
        assert views[0].raw_index is indexes[0]
        assert views[0].records[0] is indexes[0].records[0]
        assert args.result[0].read_bytes() == source_bytes
        module.verify_and_close_source_selection_plan_views(views)
        views = ()
    finally:
        for view in views:
            view.close()
        os.close(lock_fd)


def test_finalizer_closes_bound_pack_reader_after_business_exception(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _, lock_fd = _compact_source_fixture(module, tmp_path)
    opened_archives: list[object] = []
    original_archive = module.FinalizerSourceArchive

    class TrackedArchive(original_archive):
        def __init__(self, *call_args: object, **call_kwargs: object) -> None:
            super().__init__(*call_args, **call_kwargs)
            opened_archives.append(self)

    def fail_after_open(_records: object) -> object:
        raise RuntimeError("business validation failed after pack open")

    monkeypatch.setattr(module, "FinalizerSourceArchive", TrackedArchive)
    monkeypatch.setattr(module, "validate_source_policy_history", fail_after_open)
    try:
        with pytest.raises(RuntimeError, match="business validation failed"):
            module.run_finalization(args)
        assert opened_archives
        assert all(archive.closed for archive in opened_archives)
    finally:
        os.close(lock_fd)


def test_finalizer_streams_history_by_locator_without_legacy_full_row_views(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    archives: list[object] = []
    original_archive = module.FinalizerSourceArchive

    class TrackedArchive(original_archive):
        def __init__(self, *call_args: object, **call_kwargs: object) -> None:
            super().__init__(*call_args, **call_kwargs)
            archives.append(self)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("legacy full-row finalizer path was called")

    monkeypatch.setattr(module, "FinalizerSourceArchive", TrackedArchive)
    monkeypatch.setattr(module, "read_source_shard_indexes", forbidden)
    monkeypatch.setattr(module, "open_source_selection_plan_views", forbidden)
    try:
        manifest = module.run_finalization(args)
        assert manifest["status"] == "complete"
        assert len(archives) == 1
        archive = archives[0]
        assert archive.materialized_row_count == 10
        assert archive.peeked_row_count == 5
        assert archive.closed is True
    finally:
        os.close(lock_fd)


def test_compact_finalizer_streaming_archive_supports_forced_spool(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _, lock_fd = _compact_source_fixture(module, tmp_path)
    archives: list[object] = []
    original_archive = module.FinalizerSourceArchive

    class ForcedSpoolArchive(original_archive):
        def __init__(self, paths: object) -> None:
            super().__init__(paths, force_spool=True)
            archives.append(self)

    monkeypatch.setattr(module, "FinalizerSourceArchive", ForcedSpoolArchive)
    try:
        manifest = module.run_finalization(args)
        assert manifest["status"] == "complete"
        assert len(archives) == 1
        archive = archives[0]
        assert archive.backing == "spool"
        assert archive.materialized_row_count == 5
        assert archive.peeked_row_count == 5
        assert archive.closed is True
    finally:
        os.close(lock_fd)


def test_streaming_compact_manifest_reuses_bound_durable_verification(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.eval import draco_resume_source_index

    args, _, lock_fd = _compact_source_fixture(module, tmp_path)
    index_verification_calls = 0
    original_index_verifier = (
        draco_resume_source_index.verify_durable_draco_artifacts
    )

    def tracked_index_verifier(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal index_verification_calls
        index_verification_calls += 1
        return original_index_verifier(*args, **kwargs)

    def duplicate_finalizer_verifier(*_args: object, **_kwargs: object) -> object:
        pytest.fail("finalizer rescanned a compact durable result bundle")

    monkeypatch.setattr(
        draco_resume_source_index,
        "verify_durable_draco_artifacts",
        tracked_index_verifier,
    )
    monkeypatch.setattr(
        module,
        "verify_durable_draco_artifacts",
        duplicate_finalizer_verifier,
    )
    archive = module.FinalizerSourceArchive(args.result)
    try:
        indexes = archive.scan()
        contracts = module.load_manifest_contracts(
            args.manifest,
            result_paths=args.result,
            groups=module.GROUPS,
            result_indexes=indexes,
        )
        assert index_verification_calls == 1
        assert contracts[3][0]["durable_artifacts"] == (
            indexes[0].durable_artifact_verification
        )
    finally:
        archive.close(verify=False)
        os.close(lock_fd)


def test_streaming_pair_rejects_expansion_beyond_materialized_byte_cap(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _, lock_fd = _compact_source_fixture(module, tmp_path)
    monkeypatch.setattr(module, "FINALIZER_MAX_PAIR_MATERIALIZED_BYTES", 1)
    archive = module.FinalizerSourceArchive(args.result)
    try:
        archive.scan()
        key = next(iter(sorted(archive.keys)))
        with pytest.raises(
            module.FinalizationError,
            match="per-pair materialized byte cap",
        ):
            archive.materialize_pair(key)
    finally:
        archive.close(verify=False)
        os.close(lock_fd)


@pytest.mark.parametrize("artifact", ["result", "pack"])
def test_streaming_finalizer_rechecks_same_bytes_inode_before_publication(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
) -> None:
    if artifact == "pack":
        args, pack_path, lock_fd = _compact_source_fixture(module, tmp_path)
        target = pack_path
    else:
        args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
        target = args.result[0]
    original_publish = module.publish_atomically
    replaced = False

    def replace_at_final_gate(**kwargs: object) -> dict[str, object]:
        original_verifier = kwargs.get("pre_publish_verifier")
        assert callable(original_verifier)

        def replace_then_verify() -> None:
            nonlocal replaced
            replacement = target.with_name(f".{target.name}.replacement")
            replacement.write_bytes(target.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, target)
            replaced = True
            original_verifier()

        kwargs["pre_publish_verifier"] = replace_then_verify
        return original_publish(**kwargs)

    monkeypatch.setattr(module, "publish_atomically", replace_at_final_gate)
    try:
        with pytest.raises(module.FinalizationError, match="changed during finalization"):
            module.run_finalization(args)
        assert replaced is True
        assert not args.output_dir.exists()
    finally:
        os.close(lock_fd)


def test_streaming_history_analysis_matches_legacy_in_memory_pipeline(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    groups = module.normalize_groups(args.groups)
    tasks = module.read_tasks(args.input)
    raw_indexes = module.read_source_shard_indexes(args.result)
    legacy_contracts = module.load_manifest_contracts(
        args.manifest,
        result_paths=args.result,
        groups=groups,
        result_indexes=raw_indexes,
    )
    views = module.open_source_selection_plan_views(
        raw_indexes,
        legacy_contracts[3],
    )
    archive = module.FinalizerSourceArchive(args.result)
    try:
        records = [record for view in views for record in view.records]
        fingerprints, contracts, _, manifest_sources = legacy_contracts
        policy = module.validate_formal_campaign_contracts(contracts, groups=groups)
        max_attempts = module.authenticated_generation_attempt_limit(
            args.max_generation_attempts,
            policy,
        )
        old_generation = module.validate_generation_attempt_evidence(
            records,
            max_attempts=max_attempts,
        )
        old_judge = module.validate_judge_attempt_evidence(
            records,
            judge_model=policy.judge_model,
            judge_max_attempts=policy.judge_max_attempts,
            judge_provider_pin=policy.judge_provider_pin,
        )
        old_selected, old_pair_audit = module.select_results(
            records,
            tasks=tasks,
            groups=groups,
            fingerprints=fingerprints,
            contracts=contracts,
            max_attempts=max_attempts,
            experiment_policy=policy,
            manifest_sources=manifest_sources,
        )
        old_bindings = module.bind_selected_generation_attempts(records, old_selected)
        old_ledger, old_ledger_summary = module.build_actual_spend_ledger(
            records,
            selected=old_selected,
            selected_attempt_bindings=old_bindings,
            judge_model=policy.judge_model,
        )
        old_external = module.build_external_tool_cost_summary(
            records,
            manifest_sources=manifest_sources,
        )

        streaming_indexes = archive.scan()
        streaming_contracts = module.load_manifest_contracts(
            args.manifest,
            result_paths=args.result,
            groups=groups,
            result_indexes=streaming_indexes,
        )
        assert streaming_contracts == legacy_contracts
        streamed = module.analyze_streaming_source_history(
            archive,
            tasks=tasks,
            groups=groups,
            fingerprints=fingerprints,
            contracts=contracts,
            max_attempts=max_attempts,
            experiment_policy=policy,
            manifest_sources=manifest_sources,
        )
        assert streamed.generation_attempt_evidence == old_generation
        assert streamed.judge_attempt_evidence == old_judge
        assert streamed.pair_audit == old_pair_audit
        assert streamed.selected_attempt_bindings == old_bindings
        assert streamed.ledger_rows == old_ledger
        assert streamed.ledger_summary == old_ledger_summary
        assert streamed.external_tool_cost == old_external
        assert archive.materialized_row_count == len(records)
    finally:
        archive.close(verify=False)
        module.verify_and_close_source_selection_plan_views(views)
        os.close(lock_fd)


def test_run_finalization_cleanup_preserves_primary_and_closes_every_view(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class View:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)
            raise RuntimeError(f"close-{self.name}")

    primary = module.FinalizationError("primary-finalization-error")

    def fail_with_views(_args, holder):
        holder.extend((View("first"), View("second")))
        raise primary

    monkeypatch.setattr(
        module,
        "_run_finalization_with_selection_plan_views",
        fail_with_views,
    )
    with pytest.raises(module.FinalizationError, match="primary-finalization-error") as caught:
        module.run_finalization(argparse.Namespace())

    assert caught.value is primary
    assert closed == ["second", "first"]
    assert getattr(primary, "__notes__", []) == [
        "finalizer cleanup also failed: builtins.RuntimeError",
        "finalizer cleanup also failed: builtins.RuntimeError",
    ]


def test_run_finalization_cleanup_reports_first_close_error_after_success(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class View:
        def __init__(self, name: str, error: BaseException | None) -> None:
            self.name = name
            self.error = error

        def close(self) -> None:
            closed.append(self.name)
            if self.error is not None:
                raise self.error

    expected = ValueError("second-close-failed")

    def succeed_with_views(_args, holder):
        holder.extend(
            (
                View("first", RuntimeError("first-close-failed")),
                View("second", expected),
                View("third", None),
            )
        )
        return {"status": "complete"}

    monkeypatch.setattr(
        module,
        "_run_finalization_with_selection_plan_views",
        succeed_with_views,
    )
    with pytest.raises(ValueError, match="second-close-failed") as caught:
        module.run_finalization(argparse.Namespace())

    assert caught.value is expected
    assert closed == ["third", "second", "first"]


def test_compact_finalizer_rejects_raw_ref_tamper_before_manifest_or_pack_use(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _compact_source_fixture(module, tmp_path)
    try:
        rows = [
            json.loads(line)
            for line in args.result[0].read_text(encoding="utf-8").splitlines()
        ]
        ref = rows[-1]["routing_trace"]["selection_plan"]
        ref["expanded_bytes"] += 1
        args.result[0].write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        with pytest.raises(module.FinalizationError, match="not sealed or was mutated"):
            module.read_source_shard_indexes(args.result)
    finally:
        os.close(lock_fd)


def test_compact_finalizer_rejects_missing_pack_and_wrong_stamp_path(
    module,
    tmp_path: Path,
) -> None:
    missing_args, missing_pack, missing_lock_fd = _compact_source_fixture(
        module,
        tmp_path / "missing",
    )
    try:
        missing_pack.unlink()
        raw_indexes = module.read_source_shard_indexes(missing_args.result)
        _, _, _, sources = module.load_manifest_contracts(
            missing_args.manifest,
            result_paths=missing_args.result,
            groups=module.GROUPS,
            result_indexes=raw_indexes,
        )
        with pytest.raises(module.FinalizationError, match="pack binding failed"):
            module.open_source_selection_plan_views(raw_indexes, sources)
    finally:
        os.close(missing_lock_fd)

    wrong_args, _, wrong_lock_fd = _compact_source_fixture(
        module,
        tmp_path / "wrong-path",
    )
    try:
        manifest = json.loads(wrong_args.manifest[0].read_text(encoding="utf-8"))
        manifest["artifacts"][module.SELECTION_PLAN_PACK_ARTIFACT_FIELD] = str(
            (tmp_path / "wrong-path" / "other.pack.jsonl").resolve()
        )
        _owner_json(wrong_args.manifest[0], manifest)
        raw_indexes = module.read_source_shard_indexes(wrong_args.result)
        with pytest.raises(module.FinalizationError, match="pack path is not stamp-bound"):
            module.load_manifest_contracts(
                wrong_args.manifest,
                result_paths=wrong_args.result,
                groups=module.GROUPS,
                result_indexes=raw_indexes,
            )
    finally:
        os.close(wrong_lock_fd)


@pytest.mark.parametrize("field", ["pack_sha256", "pack_object_count"])
def test_compact_finalizer_rejects_pack_digest_or_count_binding(
    module,
    tmp_path: Path,
    field: str,
) -> None:
    args, _, lock_fd = _compact_source_fixture(module, tmp_path)
    try:
        manifest = json.loads(args.manifest[0].read_text(encoding="utf-8"))
        binding = manifest[module.SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD]
        if field == "pack_sha256":
            binding[field] = "sha256:" + "0" * 64
        else:
            binding[field] += 1
        _owner_json(args.manifest[0], manifest)
        raw_indexes = module.read_source_shard_indexes(args.result)
        _, _, _, sources = module.load_manifest_contracts(
            args.manifest,
            result_paths=args.result,
            groups=module.GROUPS,
            result_indexes=raw_indexes,
        )
        with pytest.raises(module.FinalizationError, match="digest/count differs"):
            module.open_source_selection_plan_views(raw_indexes, sources)
    finally:
        os.close(lock_fd)


def test_compact_finalizer_binds_durable_results_to_raw_shard_hash(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _compact_source_fixture(module, tmp_path)
    try:
        raw_index = module.read_source_shard_indexes(args.result)[0]
        forged_index = module.SourceShardIndex(
            path=raw_index.path,
            source_index=raw_index.source_index,
            sha256="0" * 64,
            records=raw_index.records,
        )
        with pytest.raises(module.FinalizationError, match="raw shard index"):
            module.load_manifest_contracts(
                args.manifest,
                result_paths=args.result,
                groups=module.GROUPS,
                result_indexes=(forged_index,),
            )
    finally:
        os.close(lock_fd)


def test_compact_finalizer_rejects_pack_replacement_at_single_reader_open(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, pack_path, lock_fd = _compact_source_fixture(module, tmp_path)
    try:
        raw_indexes = module.read_source_shard_indexes(args.result)
        _, _, _, sources = module.load_manifest_contracts(
            args.manifest,
            result_paths=args.result,
            groups=module.GROUPS,
            result_indexes=raw_indexes,
        )
        replacement = tmp_path / "replacement-selection-plan.pack.jsonl"
        with SelectionPlanPackAppender(replacement):
            pass
        original_reader = module.SelectionPlanPackReader
        original_require = module.require_regular_file

        def replace_then_open(path: Path, *call_args: object, **call_kwargs: object):
            moved = tmp_path / "pre-open-selection-plan.pack.jsonl"
            pack_path.rename(moved)
            replacement.rename(pack_path)
            return original_reader(path, *call_args, **call_kwargs)

        def no_pack_prelookup(
            path: Path,
            *,
            owner_only: bool = True,
        ) -> Path:
            if Path(path) == pack_path:
                raise AssertionError("pack path was looked up before reader open")
            return original_require(path, owner_only=owner_only)

        monkeypatch.setattr(module, "SelectionPlanPackReader", replace_then_open)
        monkeypatch.setattr(module, "require_regular_file", no_pack_prelookup)
        with pytest.raises(module.FinalizationError, match="digest/count differs"):
            module.open_source_selection_plan_views(raw_indexes, sources)
    finally:
        os.close(lock_fd)


def test_compact_finalizer_rejects_capability_downgrade_and_path_replacement(
    module,
    tmp_path: Path,
) -> None:
    downgrade_args, _, downgrade_lock_fd = _compact_source_fixture(
        module,
        tmp_path / "downgrade",
    )
    try:
        manifest = json.loads(
            downgrade_args.manifest[0].read_text(encoding="utf-8")
        )
        manifest.pop(module.SELECTION_PLAN_EVIDENCE_ROW_FIELD)
        _owner_json(downgrade_args.manifest[0], manifest)
        raw_indexes = module.read_source_shard_indexes(downgrade_args.result)
        with pytest.raises(module.FinalizationError, match="capability or durable binding"):
            module.load_manifest_contracts(
                downgrade_args.manifest,
                result_paths=downgrade_args.result,
                groups=module.GROUPS,
                result_indexes=raw_indexes,
            )
    finally:
        os.close(downgrade_lock_fd)

    replace_args, pack_path, replace_lock_fd = _compact_source_fixture(
        module,
        tmp_path / "replace",
    )
    views = ()
    try:
        _, _, views = _open_compact_finalizer_source_entry(module, replace_args)
        moved = pack_path.with_name("original-selection-plan.pack.jsonl")
        pack_path.rename(moved)
        with SelectionPlanPackAppender(pack_path):
            pass
        with pytest.raises(module.FinalizationError, match="changed during finalization"):
            module.verify_and_close_source_selection_plan_views(views)
        views = ()
    finally:
        for view in views:
            view.close()
        os.close(replace_lock_fd)


def test_attempt_id_payload_conflict_and_legacy_mix_are_fatal(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    try:
        wave2 = args.result[-1]
        rows = [json.loads(line) for line in wave2.read_text().splitlines()]
        rows[0]["execution"]["generation_attempts"][0]["run"]["usage"]["model_usage_breakdown"][0][
            "input_tokens"
        ] = 999
        rows[0] = module.seal_result_row(rows[0])
        wave2.write_text("".join(json.dumps(row) + "\n" for row in rows))
        wave2.chmod(0o600)
        with pytest.raises(module.FinalizationError, match="conflicting payloads"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)

    legacy_dir = tmp_path / "legacy"
    args2, _, lock_fd2 = _campaign(module, legacy_dir, with_repair=False)
    try:
        path = args2.result[0]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0].pop("generation_attempt_evidence_schema")
        rows[0] = module.seal_result_row(rows[0])
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        path.chmod(0o600)
        with pytest.raises(module.FinalizationError, match="legacy or missing"):
            module.run_finalization(args2)
    finally:
        os.close(lock_fd2)


def test_attempt_budget_must_be_monotonic_across_repair_wave(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    wave2 = args.result[-1]
    rows = [json.loads(line) for line in wave2.read_text().splitlines()]
    rows[0]["execution"]["prior_generation_attempts_used"] = 0
    rows[0] = module.seal_result_row(rows[0])
    wave2.write_text("".join(json.dumps(row) + "\n" for row in rows))
    wave2.chmod(0o600)
    try:
        with pytest.raises(module.FinalizationError, match="non-monotonic"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize(
    "field",
    [
        "row_count",
        "actual_spend_count",
        "budget_limit",
        "budget_used",
        "prior_used",
        "attempt_ordinal",
    ],
)
@pytest.mark.parametrize("invalid_kind", ["string", "float", "boolean"])
def test_generation_attempt_v1_integer_fields_reject_nonintegers(
    module,
    tmp_path: Path,
    field: str,
    invalid_kind: str,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    try:
        path = args.result[0]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        row = rows[0]
        expected = {
            "row_count": 1,
            "actual_spend_count": 1,
            "budget_limit": 3,
            "budget_used": 1,
            "prior_used": 0,
            "attempt_ordinal": 1,
        }[field]
        invalid_value: object
        if invalid_kind == "string":
            invalid_value = str(expected)
        elif invalid_kind == "float":
            invalid_value = float(expected) + 0.5
        else:
            invalid_value = True
        if field == "row_count":
            row["generation_attempt_count"] = invalid_value
        elif field == "actual_spend_count":
            row["actual_spend_metrics"]["generation_attempt_count"] = invalid_value
        elif field == "budget_limit":
            row["generation_attempt_budget_limit"] = invalid_value
        elif field == "budget_used":
            row["generation_attempt_budget_used"] = invalid_value
        elif field == "prior_used":
            row["execution"]["prior_generation_attempts_used"] = invalid_value
        else:
            row["execution"]["generation_attempts"][0]["attempt"] = invalid_value
        rows[0] = module.seal_result_row(row)
        path.write_text("".join(json.dumps(item) + "\n" for item in rows))
        path.chmod(0o600)
        records, _ = module.read_source_rows([path])

        with pytest.raises(module.FinalizationError, match="must be an integer"):
            module.validate_generation_attempt_evidence(records, max_attempts=3)
    finally:
        os.close(lock_fd)


def test_actual_spend_ledger_keeps_failed_replaced_attempt_once(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    try:
        path = args.result[0]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        first = rows[0]
        success = first["execution"]["generation_attempts"][0]
        failed = deepcopy(success)
        failed["attempt"] = 1
        failed["attempt_id"] = "f" * 32
        failed["run"]["error"] = "provider_timeout"
        failed["run"]["usage"]["model_usage_breakdown"][0]["provider_usage"]["response_ids"] = [
            "b0-failed-generation"
        ]
        success["attempt"] = 2
        first["execution"]["generation_attempts"] = [failed, success]
        first["generation_attempt_count"] = 2
        first["actual_spend_metrics"]["generation_attempt_count"] = 2
        first["generation_attempt_budget_used"] = 2
        rows[0] = module.seal_result_row(first)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        path.chmod(0o600)
        records, _ = module.read_source_rows([path])
        attempt_audit = module.validate_generation_attempt_evidence(records, max_attempts=3)
        ledger, summary = module.build_actual_spend_ledger(records)
        assert attempt_audit["B0/task-1"]["unique_attempt_count"] == 2
        assert summary["distinct_generation_attempt_count"] == 6
        assert len(ledger) == 40
        ids = {item for row in ledger for item in row["response_id_sha256"]}
        assert len(ids) == 40
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize(
    "collision",
    ["generation_generation", "generation_judge", "judge_judge"],
)
def test_actual_spend_ledger_rejects_response_id_reuse_across_physical_units(
    module,
    tmp_path: Path,
    collision: str,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    try:
        path = args.result[0]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        shared = "shared-provider-response"
        generation_a = rows[0]["execution"]["generation_attempts"][0]["run"]["usage"][
            "model_usage_breakdown"
        ][0]
        judge_a = rows[0]["judge"]["criterion_judgments"][0]["judge_attempts"][0]["run"]["usage"][
            "model_usage_breakdown"
        ][0]
        if collision == "generation_generation":
            generation_b = rows[1]["execution"]["generation_attempts"][0]["run"]["usage"][
                "model_usage_breakdown"
            ][0]
            units = [generation_a, generation_b]
        elif collision == "generation_judge":
            units = [generation_a, judge_a]
        else:
            judge_b = rows[0]["judge"]["criterion_judgments"][1]["judge_attempts"][0]["run"][
                "usage"
            ]["model_usage_breakdown"][0]
            units = [judge_a, judge_b]
        for unit in units:
            unit["provider_usage"]["response_ids"] = [shared]
        rows = [module.seal_result_row(row) for row in rows]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        path.chmod(0o600)
        records, _ = module.read_source_rows([path])
        with pytest.raises(module.FinalizationError, match="response_id is reused"):
            module.build_actual_spend_ledger(records)
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize(
    ("estimated_cost", "expected_cost", "expected_precision"),
    [(None, None, "unknown"), (0.42, "0.42", "estimated")],
)
def test_none_cost_source_does_not_turn_billed_default_into_recorded_zero(
    module,
    tmp_path: Path,
    estimated_cost: float | None,
    expected_cost: str | None,
    expected_precision: str,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    try:
        path = args.result[0]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        unit = rows[0]["execution"]["generation_attempts"][0]["run"]["usage"][
            "model_usage_breakdown"
        ][0]
        unit["provider_usage"].pop("provider_reported_cost")
        unit["cost_source"] = "none"
        unit["billed_cost"] = 0.0
        if estimated_cost is not None:
            unit["estimated_cost_usd"] = estimated_cost
        rows[0] = module.seal_result_row(rows[0])
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        path.chmod(0o600)
        records, _ = module.read_source_rows([path])
        ledger, _ = module.build_actual_spend_ledger(records)
        generation = next(
            row
            for row in ledger
            if "generation" in row["scopes"]
            and {"group": "B0", "task_id": "task-1"} in row["group_task_pairs"]
        )
        assert generation["provider"] == "openrouter"
        assert generation["model"] == module.B0_MODEL
        assert generation["input_tokens"] == 10
        assert generation["recorded_cost_usd"] == expected_cost
        assert generation["cost_precision"] == expected_precision
    finally:
        os.close(lock_fd)


def test_static_estimate_takes_precedence_over_normalized_billed_zero(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    try:
        path = args.result[0]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        unit = rows[0]["execution"]["generation_attempts"][0]["run"]["usage"][
            "model_usage_breakdown"
        ][0]
        unit["provider_usage"].pop("provider_reported_cost")
        unit.update(
            {
                "billed_cost": 0.0,
                "cost_source": "opensquilla_static_estimate",
                "estimated_cost_usd": 0.42,
                "cost_usd": 0.42,
            }
        )
        rows[0] = module.seal_result_row(rows[0])
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        path.chmod(0o600)
        records, _ = module.read_source_rows([path])

        ledger, _ = module.build_actual_spend_ledger(records)
        generation = next(
            row
            for row in ledger
            if "generation" in row["scopes"]
            and {"group": "B0", "task_id": "task-1"} in row["group_task_pairs"]
        )

        assert generation["recorded_cost_usd"] == "0.42"
        assert generation["cost_precision"] == "estimated"
    finally:
        os.close(lock_fd)


def test_estimated_generation_cost_is_in_total_but_not_exact_total(module) -> None:
    pair = {"group": "B0", "task_id": "task-1"}
    rows = [{**pair, "usage": {}}]
    ledger = [
        {
            "generation_disposition": "selected",
            "group_task_pairs": [pair],
            "recorded_cost_usd": "0.10",
            "cost_precision": "exact",
        },
        {
            "generation_disposition": "selected",
            "group_task_pairs": [pair],
            "recorded_cost_usd": "0.25",
            "cost_precision": "estimated",
        },
    ]

    summaries, audit = module.selected_generation_costs_from_ledger(rows, ledger)
    summary = summaries["B0/task-1"]
    assert summary["value"] == module.Decimal("0.35")
    assert summary["recorded_cost_usd"] == "0.35"
    assert summary["exact_cost_usd"] == "0.10"
    assert summary["estimated_cost_usd"] == "0.25"
    assert summary["exact"] is False
    assert summary["complete"] is True
    assert audit["exact_pair_count"] == 0
    assert audit["ignored_cost_request_count"] == 0
    assert audit["reporting_policy"]["priority"] == [
        "recorded_dollar_cost",
        "frozen_cache_aware_token_estimate",
        "exclude_unpriced_request",
    ]


def test_unknown_selected_request_is_ignored_without_erasing_known_subtotal(module) -> None:
    pair = {"group": "B0", "task_id": "task-1"}
    rows = [
        {
            **pair,
            "quality_total": 80,
            "usage": {},
            "judge": {
                "criterion_judgments": [{"met": True, "weight": 1}],
                "judge_error_count": 0,
            },
        }
    ]
    ledger = [
        {
            "generation_disposition": "selected",
            "group_task_pairs": [pair],
            "recorded_cost_usd": "0.10",
            "cost_precision": "exact",
        },
        {
            "generation_disposition": "selected",
            "group_task_pairs": [pair],
            "recorded_cost_usd": "0.25",
            "cost_precision": "estimated",
        },
        {
            "generation_disposition": "selected",
            "group_task_pairs": [pair],
            "recorded_cost_usd": None,
            "cost_precision": "unknown",
        },
    ]

    summaries, audit = module.selected_generation_costs_from_ledger(rows, ledger)
    summary = summaries["B0/task-1"]
    assert summary["value"] == module.Decimal("0.35")
    assert summary["recorded_cost_usd"] is None
    assert summary["counted_cost_usd"] == "0.35"
    assert summary["exact_cost_usd"] == "0.10"
    assert summary["estimated_cost_usd"] == "0.25"
    assert summary["ignored_cost_request_count"] == 1
    assert summary["ignored_cost_requests_are_zero"] is False
    assert summary["precision"] == "partial_excluding_unknown"
    assert summary["complete"] is False
    assert summary["exact"] is False
    assert audit["counted_cost_usd"] == "0.35"
    assert audit["known_cost_request_count"] == 2
    assert audit["estimated_cost_request_count"] == 1
    assert audit["ignored_cost_request_count"] == 1
    assert audit["unknown_is_zero"] is False

    metric = module.group_metrics(
        rows,
        selected_costs_by_pair=summaries,
        groups=("B0",),
    )[0]
    assert metric["avg_selected_generation_cost_usd"] == "0.35"
    assert metric["selected_generation_cost_usd"] == "0.35"
    assert metric["selected_generation_cost_complete"] is False
    assert metric["selected_generation_cost_covered_task_count"] == 1
    assert metric["selected_generation_cost_exact_task_count"] == 0
    assert metric["selected_generation_cost_ignored_request_count"] == 1
    assert metric["selected_generation_cost_precision"] == "partial_excluding_unknown"


def test_all_unknown_selected_requests_have_no_subtotal_and_are_not_zero_cost(module) -> None:
    pair = {"group": "B0", "task_id": "task-1"}
    rows = [
        {
            **pair,
            "quality_total": 80,
            "usage": {},
            "judge": {
                "criterion_judgments": [{"met": True, "weight": 1}],
                "judge_error_count": 0,
            },
        }
    ]
    ledger = [
        {
            "generation_disposition": "selected",
            "group_task_pairs": [pair],
            "recorded_cost_usd": None,
            "cost_precision": "unknown",
        }
    ]

    summaries, audit = module.selected_generation_costs_from_ledger(rows, ledger)
    summary = summaries["B0/task-1"]
    assert summary["value"] is None
    assert summary["recorded_cost_usd"] is None
    assert summary["counted_cost_usd"] is None
    assert summary["ignored_cost_request_count"] == 1
    assert summary["ignored_cost_requests_are_zero"] is False
    assert summary["complete"] is False
    assert audit["counted_pair_count"] == 0
    assert audit["counted_cost_usd"] is None
    assert audit["known_cost_request_count"] == 0
    assert audit["ignored_cost_request_count"] == 1
    assert audit["unknown_is_zero"] is False

    metric = module.group_metrics(
        rows,
        selected_costs_by_pair=summaries,
        groups=("B0",),
    )[0]
    assert metric["avg_selected_generation_cost_usd"] is None
    assert metric["selected_generation_cost_usd"] is None
    assert metric["selected_generation_cost_counted_usd"] is None
    assert metric["selected_generation_cost_covered_task_count"] == 0
    assert metric["selected_generation_cost_complete"] is False
    assert metric["selected_generation_cost_ignored_requests_are_zero"] is False
    assert metric["selected_generation_cost_precision"] == "partial_excluding_unknown"


def test_g1_plan_must_be_bound_to_frozen_registry(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    index = next(index for index, row in enumerate(rows) if row["group"] == "G1")
    forged = {
        "selection_mode": "router_dynamic",
        "proposer_models": ["z-ai/glm-5.2"],
        "proposer_sample_count": 1,
        "aggregator_model": "z-ai/glm-5.2",
    }
    rows[index]["routing_trace"]["selection_plan"] = forged
    call = rows[index]["ensemble_trace"]["calls"][0]
    call["selection_plan"] = deepcopy(forged)
    call["total_candidates"] = 1
    call["successful_proposers"] = 1
    call["candidates"] = call["candidates"][:1]
    rows[index] = module.seal_result_row(rows[index])
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    try:
        with pytest.raises(module.FinalizationError, match="no valid generation"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_g1_source_and_runtime_registry_hashes_have_distinct_semantics(
    module,
) -> None:
    module.FORMAL_G1_RANKING_CONFIG_SHA256 = module.canonical_sha256(_test_ranking_config(module))
    contract = _contract(module, "G1", "1" * 64)["g1_registry_contract"]
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "z-ai/glm-5.2",
            "qwen/qwen3.7-max",
        ],
        "z-ai/glm-5.2",
    )
    assert plan["registry_snapshot_hash"] != contract["expected_source_registry_snapshot_sha256"]
    reasons, _, _ = module.g1_registry_plan_reasons(
        plan,
        contract=contract,
    )
    assert reasons == []

    wrong_source = deepcopy(plan)
    wrong_source["candidate_allowlist"]["expected_source_registry_snapshot_sha256"] = "e" * 64
    reasons, _, _ = module.g1_registry_plan_reasons(
        wrong_source,
        contract=contract,
    )
    assert ("wrong_g1_candidate_allowlist_expected_source_registry_snapshot_sha256") in reasons

    invalid_runtime = deepcopy(plan)
    invalid_runtime["registry_snapshot_hash"] = "not-a-sha256"
    reasons, _, _ = module.g1_registry_plan_reasons(
        invalid_runtime,
        contract=contract,
    )
    assert "wrong_g1_registry_snapshot_hash" in reasons


@pytest.mark.parametrize("selection_field", ["selected_P", "selected_A"])
def test_g1_frozen_replay_rejects_valid_pool_selection_swap(
    module,
    selection_field: str,
) -> None:
    module.FORMAL_G1_RANKING_CONFIG_SHA256 = module.canonical_sha256(_test_ranking_config(module))
    contract = _contract(module, "G1", "1" * 64)["g1_registry_contract"]
    plan = _g1_plan(
        module,
        [
            "deepseek/deepseek-v4-pro",
            "qwen/qwen3.7-max",
            "z-ai/glm-5.2",
        ],
        "z-ai/glm-5.2",
    )
    forged = deepcopy(plan)
    if selection_field == "selected_P":
        forged["selected_P"] = list(reversed(forged["selected_P"]))
        forged["proposer_models"] = [
            identity.partition(":")[2] for identity in forged["selected_P"]
        ]
        for index, step in enumerate(forged["selection_steps"]):
            step["selected"] = forged["selected_P"][index]
    else:
        replacement = next(
            row["identity"]
            for row in forged["candidate_pool"]
            if row["identity"] != forged["selected_A"]
        )
        forged["selected_A"] = replacement
        forged["aggregator_model"] = replacement.partition(":")[2]

    reasons, _, _ = module.g1_registry_plan_reasons(
        forged,
        contract=contract,
    )
    assert f"g1_frozen_ranker_replay_mismatch_{selection_field}" in reasons


def test_g1_runtime_registry_hash_must_match_every_physical_plan(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    index = next(index for index, row in enumerate(rows) if row["group"] == "G1")
    rows[index]["ensemble_trace"]["calls"][0]["selection_plan"]["registry_snapshot_hash"] = "e" * 64
    rows[index] = module.seal_result_row(rows[index])
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    try:
        with pytest.raises(module.FinalizationError, match="no valid generation"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_same_attempt_metadata_repair_replaces_estimate_with_exact_receipt(
    module, tmp_path: Path
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, exact=False)
    try:
        wave1_rows = [json.loads(line) for line in args.result[0].read_text().splitlines()]
        wave2_rows = [json.loads(line) for line in args.result[1].read_text().splitlines()]
        old_unit = wave1_rows[0]["execution"]["generation_attempts"][0]["run"]["usage"][
            "model_usage_breakdown"
        ][0]
        old_unit["provider_usage"] = {}
        old_unit["billed_cost"] = 0.2
        old_unit["cost_source"] = "opensquilla_static_estimate"
        new_unit = _receipt(
            "b0-repaired-exact",
            module.B0_MODEL,
            cost=0.1,
            exact=True,
        )
        wave2_rows[0]["execution"]["generation_attempts"][0]["run"]["usage"][
            "model_usage_breakdown"
        ][0] = new_unit
        wave1_rows[0] = module.seal_result_row(wave1_rows[0])
        wave2_rows[0] = module.seal_result_row(wave2_rows[0])
        args.result[0].write_text("".join(json.dumps(row) + "\n" for row in wave1_rows))
        args.result[1].write_text("".join(json.dumps(row) + "\n" for row in wave2_rows))
        records, _ = module.read_source_rows(args.result)
        module.validate_generation_attempt_evidence(records, max_attempts=3)
        selected = [record for record in records if record.source_index == 1]
        ledger, _ = module.build_actual_spend_ledger(
            records,
            selected=selected,
        )
        b0_generation = next(
            row
            for row in ledger
            if "generation" in row["scopes"]
            and {"group": "B0", "task_id": "task-1"} in row["group_task_pairs"]
        )
        assert b0_generation["recorded_cost_usd"] == "0.100000000"
        assert b0_generation["cost_precision"] == "exact"
        assert b0_generation["generation_disposition"] == "selected"
        assert len(b0_generation["response_id_sha256"]) == 1
    finally:
        os.close(lock_fd)


def test_non_exact_estimates_may_exceed_exact_account_delta(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(
        module,
        tmp_path,
        exact=False,
        with_repair=False,
    )
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        generation_units = row["execution"]["generation_attempts"][0]["run"]["usage"][
            "model_usage_breakdown"
        ]
        judge_units = [
            attempt["run"]["usage"]["model_usage_breakdown"][0]
            for judgment in row["judge"]["criterion_judgments"]
            for attempt in judgment["judge_attempts"]
        ]
        for unit in [*generation_units, *judge_units]:
            unit["provider_usage"].pop("provider_reported_cost", None)
            unit["billed_cost"] = 0.0
            unit["estimated_cost_usd"] = 0.2
            unit["cost_usd"] = 0.2
            unit["cost_source"] = "opensquilla_static_estimate"
        selected_cost = 0.2 * len(generation_units)
        row["cost_accounting"]["selected_generation_attempt"]["recorded_cost_usd"] = selected_cost
        row["cost_accounting"]["selected_generation_attempt"]["cost_exact"] = False
        row["cost_accounting"]["selected_generation_attempt"]["cost_complete"] = True
        row["generation_attempt_total_billed_cost"] = selected_cost
    rows = [module.seal_result_row(row) for row in rows]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)
    assert manifest["status"] == "complete"
    proof = json.loads((args.output_dir / "openrouter-non-byok-campaign-proof.json").read_text())
    assert proof["account"]["usage_delta_usd"] == "3.9"
    assert proof["cost_scope"]["ledger_recorded_cost_usd"] == "7.8"
    assert proof["cost_scope"]["reconciliation_status"] == "account_exact_per_request_incomplete"
    assert proof["cost_scope"]["campaign_attributable_exact"] is False
    assert proof["cost_scope"]["campaign_attributable_cost_usd"] is None
    assert (
        proof["cost_scope"]["attribution_precision"]
        == "account_window_only_external-use-not-provable"
    )


def test_frozen_draco_input_hash_and_count_are_fail_closed(module, tmp_path: Path) -> None:
    assert module.FROZEN_DRACO_MINI_TASK_COUNT == 10
    assert (
        module.FROZEN_DRACO_MINI_SHA256
        == "1eb4e618c8df8e7f68bded3d2b6f77a541744aa1072eb338835b776183188a8d"
    )
    path = tmp_path / "wrong.jsonl"
    path.write_text('{"id":"one","prompt":"one"}\n')
    path.chmod(0o600)
    tasks = module.read_tasks(path)
    with pytest.raises(module.FinalizationError, match="SHA256 differs"):
        module.validate_frozen_draco_input(path, tasks)
    module.FROZEN_DRACO_MINI_SHA256 = module.file_sha256(path)
    with pytest.raises(module.FinalizationError, match="exactly 10"):
        module.validate_frozen_draco_input(path, tasks)


def test_live_manifest_preflight_is_mandatory(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    manifest = json.loads(args.manifest[0].read_text())
    manifest.pop("tool_policy")
    _owner_json(args.manifest[0], manifest)
    try:
        with pytest.raises(module.FinalizationError, match="Web preflight"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_skipped_preflight_requires_a_true_no_generation_repair(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    manifest = json.loads(args.manifest[1].read_text())
    manifest["resume_selection"]["model_regenerate_pair_count"] = 1
    _owner_json(args.manifest[1], manifest)
    try:
        with pytest.raises(module.FinalizationError, match="no-generation repair shard"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize(
    "mutation",
    [None, "not_blocked", "model_started", "missing_terminal", "wrong_terminal_attempt"],
)
def test_skipped_preflight_accepts_only_blocked_regenerate_without_a_model_call(
    module,
    tmp_path: Path,
    mutation: str | None,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    try:
        rows = [json.loads(line) for line in args.result[1].read_text().splitlines()]
        for row in rows:
            attempt_id = row["execution"]["generation_attempts"][-1]["attempt_id"]
            row["execution"].update(
                {
                    "resume_action": "regenerate",
                    "generation_reused": True,
                    "generation_auto_retry_blocked": mutation != "not_blocked",
                    "generation_model_started": mutation == "model_started",
                    "judge_reran": False,
                    "metadata_repaired": False,
                    "provider_native_proposer_recovery_terminal": {
                        "schema": module.PROVIDER_NATIVE_PROPOSER_RECOVERY_TERMINAL_SCHEMA,
                        "status": "receipt_invalid",
                        "attempt_id": (
                            "f" * 32 if mutation == "wrong_terminal_attempt" else attempt_id
                        ),
                        "automatic_generation_retry_allowed": False,
                    },
                }
            )
            if mutation == "missing_terminal":
                row["execution"].pop("provider_native_proposer_recovery_terminal")
            row["resume_completion"] = {
                "action": "regenerate",
                "generation_reused": True,
                "metadata_repaired": False,
                "judge_reran": False,
                "post_repair_action": "regenerate",
                "status": "incomplete",
                "incomplete_reasons": ["generation_auto_retry_blocked"],
            }
            row["completion_status"] = {
                "generation_accepted": False,
                "cost_metadata_complete": False,
                "judge_complete": False,
                "status": "incomplete",
                "incomplete_reasons": ["generation_auto_retry_blocked"],
            }
        rows = [module.seal_result_row(row) for row in rows]
        args.result[1].write_text("".join(json.dumps(row) + "\n" for row in rows))
        args.result[1].chmod(0o600)

        manifest = json.loads(args.manifest[1].read_text())
        manifest["resume_selection"]["model_regenerate_pair_count"] = 0
        manifest["resume_selection"]["generation_auto_retry_blocked_pair_count"] = len(rows)
        _owner_json(args.manifest[1], manifest)

        if mutation is None:
            final_manifest = module.run_finalization(args)
            assert final_manifest["status"] == "complete"
        else:
            with pytest.raises(
                module.FinalizationError,
                match="no-generation repair shard",
            ):
                module.load_manifest_contracts(
                    args.manifest,
                    result_paths=args.result,
                    groups=module.GROUPS,
                )
    finally:
        os.close(lock_fd)


def _set_nested_value(target: dict[str, object], path: tuple[str, ...], value: object) -> None:
    current = target
    for field_name in path[:-1]:
        child = current[field_name]
        assert isinstance(child, dict)
        current = child
    current[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("runner", "mode"), "provider"),
        (("runner", "agent_max_iterations"), 13),
        (
            ("runner", "finalization_policy", "retrieval_loop_finalization_threshold"),
            1,
        ),
        (("timeouts", "task_seconds"), 10_799),
        (("generation", "policy", "temperature"), 0.1),
        (("generation", "policy", "max_tokens"), 4_096),
        (("tools", "local_web_tools", "web_search", "provider"), "duckduckgo"),
        (("judge", "repeats"), 1),
        (("cost_policy", "require_openrouter_non_byok"), False),
        (("resolved_llm_runtime", "response_cache_disabled"), False),
        (("global_experiment_profile", "generation", "max_tokens"), 4_096),
        (("formal_runtime_freeze", "sandbox_enabled"), True),
        (("formal_runtime_freeze", "aggregator_recovery_mode"), "serving"),
        (("formal_runtime_freeze", "aggregator_recovery_top_k"), 2),
        (("formal_runtime_freeze", "aggregator_max_tokens_cap"), 16_384),
        (
            ("formal_runtime_freeze", "aggregator_visible_answer_reserve_tokens"),
            4_096,
        ),
    ],
)
def test_formal_execution_contract_tampering_fails_closed(
    module,
    path: tuple[str, ...],
    value: object,
) -> None:
    key_hash = "a" * 64
    contracts = {group: _contract(module, group, key_hash) for group in module.GROUPS}
    b2 = contracts["B2"]
    assert isinstance(b2, dict)
    _set_nested_value(b2, path, value)

    with pytest.raises(module.FinalizationError) as error:
        module.validate_formal_campaign_contracts(contracts)

    if path[0] == "global_experiment_profile":
        assert "B2 global_experiment_profile differs across active groups" in str(error.value)
    elif path[:2] == ("formal_runtime_freeze", "aggregator_recovery_mode") or (
        path[0] == "formal_runtime_freeze" and path[1] in module.FORMAL_AGGREGATOR_RECOVERY_POLICY
    ):
        assert "B2 recovery policy differs across active groups" in str(error.value)
    else:
        assert f"B2 formal execution contract.{'.'.join(path)}" in str(error.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("aggregator_recovery_mode", "serving"),
        ("aggregator_recovery_top_k", 2),
        ("aggregator_max_tokens_cap", 16_384),
        ("aggregator_visible_answer_reserve_tokens", 4_096),
    ],
)
def test_formal_gateway_aggregator_recovery_tampering_fails_closed(
    module,
    field: str,
    value: object,
) -> None:
    contracts = {group: _contract(module, group, "a" * 64) for group in module.GROUPS}
    contracts["G1"]["gateway_execution"]["llm_ensemble"][field] = value

    with pytest.raises(module.FinalizationError) as error:
        module.validate_formal_campaign_contracts(contracts)

    assert f"G1 gateway llm_ensemble.{field}" in str(error.value)


@pytest.mark.parametrize(
    "model",
    [
        "openai/gpt-5.6-sol",
        "google/gemini-3.1-pro-preview",
        "qwen/qwen3.8-max",
    ],
)
def test_formal_g1_analyzer_and_fallback_route_pins_are_authenticated(
    module,
    model: str,
) -> None:
    contracts = {group: _contract(module, group, "a" * 64) for group in module.GROUPS}
    g1 = contracts["G1"]
    registry = g1["g1_registry_contract"]
    registry["live_task_analyzer_chain"] = [
        {
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "upstream_provider": "anthropic",
            "max_attempts": 1,
        },
        {
            "provider": "openrouter",
            "model": "openai/gpt-5.6-sol",
            "upstream_provider": "azure",
            "max_attempts": 1,
        },
        {
            "provider": "openrouter",
            "model": "google/gemini-3.1-pro-preview",
            "upstream_provider": "google-ai-studio",
            "max_attempts": 1,
        },
    ]
    registry["analyzer_failure_fallback_ensemble"] = {
        "proposers": [
            {
                "provider": "openrouter",
                "model": fallback_model,
                "upstream_provider": upstream,
                "max_attempts": 1,
            }
            for fallback_model, upstream in [
                ("z-ai/glm-5.2", "z-ai"),
                ("moonshotai/kimi-k2.7-code", "moonshotai"),
                ("qwen/qwen3.8-max", "alibaba"),
                ("deepseek/deepseek-v4-pro", "deepseek"),
            ]
        ],
        "aggregator": {
            "provider": "openrouter",
            "model": "z-ai/glm-5.2",
            "upstream_provider": "z-ai",
            "max_attempts": 1,
        },
        "min_successful_proposers": 1,
        "complete_proposers_only": True,
        "aggregator_max_recovery_actions": 1,
    }
    g1["resolved_llm_runtime"]["provider_routing"].update(
        {
            "openai/gpt-5.6-sol": "azure",
            "google/gemini-3.1-pro-preview": "google-ai-studio",
            "qwen/qwen3.8-max": "alibaba",
        }
    )
    g1["resolved_llm_runtime"]["provider_routing"][model] = "wrong-provider"

    with pytest.raises(
        module.FinalizationError,
        match="upstream provider pin differs",
    ):
        module.validate_formal_campaign_contracts(contracts)


def _apply_dynamic_experiment_policy(contract: dict[str, object]) -> None:
    profile = contract["global_experiment_profile"]
    profile["timeouts"].update(
        {
            "task_seconds": 7_200.0,
            "proposer_seconds": 600.0,
            "aggregator_seconds": 1_200.0,
            "task_margin_seconds": 45.0,
        }
    )
    profile["runner"].update(
        {
            "agent_max_iterations": 17,
            "deadline_wrapup_margin_seconds": 240,
        }
    )
    profile["generation"].update(
        {
            "max_tokens": 12_288,
            "max_attempts": 2,
            "retry_backoff_seconds": 1.5,
        }
    )
    profile["judge"].update({"repeats": 2, "max_attempts": 2})
    profile["tools"]["web_search"]["max_results"] = 7
    profile["tools"]["web_fetch"]["max_content_tokens"] = 12_000

    contract["timeouts"].update(
        {
            "task_seconds": 7_200.0,
            "proposer_seconds": 600.0,
            "aggregator_seconds": 1_200.0,
        }
    )
    contract["runner"].update(
        {
            "agent_max_iterations": 17,
            "finalization_policy": {
                **contract["runner"]["finalization_policy"],
                "deadline_wrapup_margin_seconds": 240,
            },
        }
    )
    contract["generation"]["policy"]["max_tokens"] = 12_288
    contract["generation"].update({"max_attempts": 2, "retry_backoff_seconds": 1.5})
    contract["judge"].update({"repeats": 2, "max_attempts": 2})
    local_tools = contract["tools"]["local_web_tools"]
    local_tools["web_search"]["max_results"] = 7
    local_tools["web_fetch"].update({"max_content_tokens": 12_000, "max_content_chars": 48_000})
    freeze = contract["formal_runtime_freeze"]
    freeze.update(
        {
            "aggregator_recovery_top_k": 2,
            "aggregator_max_tokens_cap": 32_768,
            "aggregator_visible_answer_reserve_tokens": 4_096,
            "proposer_recovery_max_additional_calls": 2,
            "proposer_max_tokens_cap": 32_768,
            "proposer_visible_answer_reserve_tokens": 2_048,
        }
    )
    contract["gateway_execution"]["llm_ensemble"].update(
        {
            "aggregator_recovery_top_k": 2,
            "aggregator_max_tokens_cap": 32_768,
            "aggregator_visible_answer_reserve_tokens": 4_096,
        }
    )


def test_formal_contract_derives_coherent_dynamic_experiment_policy(module) -> None:
    contracts = {group: _contract(module, group, "a" * 64) for group in module.GROUPS}
    for contract in contracts.values():
        _apply_dynamic_experiment_policy(contract)

    policy = module.validate_formal_campaign_contracts(contracts)

    assert policy.timeouts["task_seconds"] == Decimal("7200.0")
    assert policy.runner["agent_max_iterations"] == 17
    assert policy.generation_max_attempts == 2
    assert policy.judge_repeats == 2
    assert policy.judge_max_attempts == 2
    assert policy.tools["web_search"]["max_results"] == 7
    assert policy.tools["web_fetch"]["max_content_tokens"] == 12_000
    assert policy.aggregator_recovery["aggregator_recovery_top_k"] == 2
    assert policy.proposer_recovery["max_additional_physical_requests"] == 2


def test_max_generation_attempts_cli_is_only_an_authenticated_policy_assertion(
    module,
) -> None:
    parser = module.build_parser()
    action = next(action for action in parser._actions if action.dest == "max_generation_attempts")
    assert action.default is None

    policy = module.FinalizerExperimentPolicy(
        profile={},
        timeouts={},
        runner={},
        generation={"max_attempts": 2},
        judge={},
        tools={},
        aggregator_recovery={},
        proposer_recovery={},
        judge_provider_pin="",
    )
    assert module.authenticated_generation_attempt_limit(None, policy) == 2
    assert module.authenticated_generation_attempt_limit(2, policy) == 2
    for invalid in (0, 3, True, "2"):
        with pytest.raises(module.FinalizationError):
            module.authenticated_generation_attempt_limit(invalid, policy)


def test_formal_contract_accepts_lowercase_api_key_env_name(module) -> None:
    contracts = {group: _contract(module, group, "a" * 64) for group in module.GROUPS}
    for contract in contracts.values():
        contract["global_experiment_profile"]["tools"]["web_search"]["api_key_env"] = (
            "brave_search_key"
        )
        contract["tools"]["local_web_tools"]["web_search"]["api_key_env"] = "brave_search_key"

    policy = module.validate_formal_campaign_contracts(contracts)

    assert policy.tools["web_search"]["api_key_env"] == "brave_search_key"


def test_formal_contract_accepts_keyless_duckduckgo_search(module) -> None:
    contracts = {group: _contract(module, group, "a" * 64) for group in module.GROUPS}
    for contract in contracts.values():
        contract["global_experiment_profile"]["tools"]["web_search"].update(
            provider="duckduckgo",
            api_key_env="",
        )
        contract["tools"]["local_web_tools"]["web_search"].update(
            provider="duckduckgo",
            api_key_env="",
        )

    policy = module.validate_formal_campaign_contracts(contracts)

    assert policy.tools["web_search"] == {
        "provider": "duckduckgo",
        "api_key_env": "",
        "max_results": 5,
    }


def test_report_values_use_authenticated_dynamic_experiment_policy(module) -> None:
    contracts = {group: _contract(module, group, "a" * 64) for group in module.GROUPS}
    for contract in contracts.values():
        _apply_dynamic_experiment_policy(contract)
    policy = module.validate_formal_campaign_contracts(contracts)

    rendered = module.experiment_policy_report_values(
        policy,
        task_concurrency=4,
        judge_concurrency=5,
    )

    assert rendered == {
        **rendered,
        "runner_label": "Agent Loop",
        "task_timeout": "7200",
        "max_iterations": 17,
        "task_concurrency": 4,
        "generation_temperature": "0",
        "generation_max_tokens": 12_288,
        "generation_max_attempts": 2,
        "judge_model": module.JUDGE_MODEL,
        "judge_repeats": 2,
        "judge_concurrency": 5,
        "judge_max_attempts": 2,
        "web_search_provider": "brave",
        "web_search_max_results": 7,
        "web_fetch_max_content_tokens": 12_000,
    }


def test_formal_contract_rejects_cross_group_dynamic_profile_drift(module) -> None:
    contracts = {group: _contract(module, group, "a" * 64) for group in module.GROUPS}
    for contract in contracts.values():
        _apply_dynamic_experiment_policy(contract)
    contracts["B2"]["global_experiment_profile"]["generation"]["max_tokens"] = 8_192

    with pytest.raises(module.FinalizationError, match="differs across active groups"):
        module.validate_formal_campaign_contracts(contracts)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("tools", "sandbox_enabled"), True, "sandbox_enabled=false"),
        (("tools", "contamination_blocked_domains"), ["example.com"], "blocked domains"),
        (("tools", "web_search", "api_key_env"), "bad-name", "api_key_env"),
        (("judge", "model"), "openai/gpt-5.5", "Judge policy"),
    ],
)
def test_formal_contract_keeps_security_and_judge_model_hard_constraints(
    module,
    path: tuple[str, ...],
    value: object,
    message: str,
) -> None:
    contracts = {group: _contract(module, group, "a" * 64) for group in module.GROUPS}
    for contract in contracts.values():
        _set_nested_value(contract["global_experiment_profile"], path, value)

    with pytest.raises(module.FinalizationError, match=message):
        module.validate_formal_campaign_contracts(contracts)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("concurrency", 4),
        ("judge_concurrency", 5),
    ],
)
def test_manifest_scheduling_tampering_fails_closed(
    module,
    tmp_path: Path,
    field: str,
    value: int,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    manifest = json.loads(args.manifest[0].read_text())
    manifest["command"]["parsed_args"][field] = value
    _owner_json(args.manifest[0], manifest)
    try:
        with pytest.raises(module.FinalizationError) as error:
            module.run_finalization(args)
    finally:
        os.close(lock_fd)

    assert f"command.parsed_args.{field} differs from the formal value" in str(error.value)


def test_manifest_scheduling_accepts_explicit_task_concurrency(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    manifest = json.loads(args.manifest[0].read_text())
    manifest["command"]["parsed_args"]["concurrency"] = 7
    _owner_json(args.manifest[0], manifest)
    args.expected_task_concurrency = 7
    try:
        payload = module.run_finalization(args)
    finally:
        os.close(lock_fd)

    assert payload["source_manifests"][0]["execution_scheduling"] == {
        "task_concurrency": 7,
        "judge_concurrency": 6,
    }


def test_finalizer_rejects_nonpositive_expected_task_concurrency(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    args.expected_task_concurrency = 0
    try:
        with pytest.raises(
            module.FinalizationError,
            match="expected task concurrency must be a positive integer",
        ):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_b1_frozen_tier_map_cannot_drift(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    manifest = json.loads(args.manifest[0].read_text())
    contract = manifest["run_compatibility"]["contracts"]["B1"]
    contract["gateway_execution"]["squilla_router"]["tiers"]["c0"]["model"] = "evil/model"
    manifest["run_compatibility"]["fingerprints"]["B1"] = module.canonical_sha256(
        contract, prefix=True
    )
    _owner_json(args.manifest[0], manifest)
    try:
        with pytest.raises(module.FinalizationError, match="B1 c0"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_router_receipt_model_must_match_formal_route(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    path = args.result[0]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    receipt = rows[0]["execution"]["generation_attempts"][0]["run"]["usage"][
        "model_usage_breakdown"
    ][0]
    receipt["provider_usage"]["router_metadata"]["attempts"][0]["model"] = "evil/model"
    rows[0] = module.seal_result_row(rows[0])
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    try:
        with pytest.raises(
            module.FinalizationError,
            match="physical generation route",
        ):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_b2_router_receipt_provider_must_match_frozen_pin(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    rows = [json.loads(line) for line in args.result[0].read_text().splitlines()]
    b2 = next(row for row in rows if row["group"] == "B2")
    receipt = b2["execution"]["generation_attempts"][0]["run"]["usage"]["model_usage_breakdown"][0]
    receipt["provider_usage"]["router_metadata"]["attempts"][0]["provider"] = "evil-upstream"
    rows[rows.index(b2)] = module.seal_result_row(b2)
    args.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="physical generation route"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("required_stable_poll_count", 5),
        ("poll_interval_seconds", 14),
        ("minimum_settlement_seconds", 179),
        ("minimum_stable_tail_seconds", 74),
    ],
)
def test_formal_account_settlement_thresholds_fail_closed(
    module, tmp_path: Path, field: str, value: int
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    reconciliation = json.loads(args.account_reconciliation.read_text())
    reconciliation[field] = value
    _owner_json(args.account_reconciliation, reconciliation)
    try:
        with pytest.raises(module.FinalizationError, match="formal value"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_account_observation_spans_and_monotonicity_are_recomputed(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path / "span")
    reconciliation = json.loads(args.account_reconciliation.read_text())
    reconciliation["observation_span_seconds"] = 999
    _owner_json(args.account_reconciliation, reconciliation)
    try:
        with pytest.raises(module.FinalizationError, match="was not recomputed"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)

    args2, _, lock_fd2 = _campaign(module, tmp_path / "monotonic")
    reconciliation2 = json.loads(args2.account_reconciliation.read_text())
    reconciliation2["stable_observations"][1]["usage"] = "13.5"
    _owner_json(args2.account_reconciliation, reconciliation2)
    try:
        with pytest.raises(module.FinalizationError, match="not monotonic"):
            module.run_finalization(args2)
    finally:
        os.close(lock_fd2)


def test_missing_router_receipt_is_fatal_but_monotonic_backfill_is_allowed(
    module, tmp_path: Path
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path / "fatal", with_repair=False)
    rows = [json.loads(line) for line in args.result[0].read_text().splitlines()]
    rows[0]["execution"]["generation_attempts"][0]["run"]["usage"]["model_usage_breakdown"][0][
        "provider_usage"
    ].pop("router_metadata")
    rows[0] = module.seal_result_row(rows[0])
    args.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="physical generation route"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)

    args2, _, lock_fd2 = _campaign(module, tmp_path / "backfill")
    first_rows = [json.loads(line) for line in args2.result[0].read_text().splitlines()]
    first_rows[0]["execution"]["generation_attempts"][0]["run"]["usage"]["model_usage_breakdown"][
        0
    ]["provider_usage"].pop("router_metadata")
    first_rows[0] = module.seal_result_row(first_rows[0])
    args2.result[0].write_text("".join(json.dumps(row) + "\n" for row in first_rows))
    try:
        assert module.run_finalization(args2)["status"] == "complete"
    finally:
        os.close(lock_fd2)


def test_generation_receipt_conflict_across_repair_is_fatal(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    rows = [json.loads(line) for line in args.result[1].read_text().splitlines()]
    unit = rows[0]["execution"]["generation_attempts"][0]["run"]["usage"]["model_usage_breakdown"][
        0
    ]
    unit["provider_usage"]["response_ids"] = ["conflicting-response"]
    unit["provider_usage"]["provider_reported_cost"] = 0.2
    unit["billed_cost"] = 0.2
    rows[0] = module.seal_result_row(rows[0])
    args.result[1].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="receipt repair conflicts"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_judge_receipt_conflict_and_missing_route_are_fatal(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path / "conflict")
    rows = [json.loads(line) for line in args.result[1].read_text().splitlines()]
    unit = rows[0]["judge"]["criterion_judgments"][0]["judge_attempts"][0]["run"]["usage"][
        "model_usage_breakdown"
    ][0]
    unit["provider_usage"]["response_ids"] = ["conflicting-judge-response"]
    rows[0] = module.seal_result_row(rows[0])
    args.result[1].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="receipt repair conflicts"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)

    args2, _, lock_fd2 = _campaign(module, tmp_path / "missing", with_repair=False)
    rows2 = [json.loads(line) for line in args2.result[0].read_text().splitlines()]
    judge_unit = rows2[0]["judge"]["criterion_judgments"][0]["judge_attempts"][0]["run"]["usage"][
        "model_usage_breakdown"
    ][0]
    judge_unit["provider_usage"].pop("router_metadata")
    rows2[0] = module.seal_result_row(rows2[0])
    args2.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows2))
    try:
        with pytest.raises(
            module.FinalizationError,
            match="Judge attempt|latest repair lacks a complete Judge",
        ):
            module.run_finalization(args2)
    finally:
        os.close(lock_fd2)


def test_selected_answer_must_bind_to_unique_successful_attempt(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    rows = [json.loads(line) for line in args.result[0].read_text().splitlines()]
    rows[0]["execution"]["generation_attempts"][0]["run"]["final_text_sha256"] = module.text_sha256(
        "different answer"
    )
    rows[0] = module.seal_result_row(rows[0])
    args.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="not bound to exactly one"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_judge_result_must_bind_to_final_successful_attempt(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    rows = [json.loads(line) for line in args.result[0].read_text().splitlines()]
    judgment = rows[0]["judge"]["criterion_judgments"][0]
    judgment["met"] = False
    judgment["verdict"] = "UNMET"
    rows[0] = module.seal_result_row(rows[0])
    args.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="not bound to its final"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_successful_judge_unit_cannot_spend_again_in_repair_wave(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    rows = [json.loads(line) for line in args.result[1].read_text().splitlines()]
    judgment = rows[0]["judge"]["criterion_judgments"][0]
    extra = deepcopy(judgment["judge_attempts"][0])
    extra["attempt_id"] = "e" * 32
    extra["attempt"] = 2
    judgment["judge_attempts"].append(extra)
    judgment["judge_attempt_count"] = 2
    judgment["judge_attempt_budget_used"] = 2
    judgment["judge_attempt_budget_remaining"] = 1
    judgment["judge_new_attempt_count"] = 1
    rows[0]["judge"]["judge_attempt_count"] += 1
    rows[0]["judge"]["judge_new_attempt_count"] = 1
    rows[0] = module.seal_result_row(rows[0])
    args.result[1].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="terminal state"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)


def test_pass_rate_uses_negative_criterion_runner_semantics(module) -> None:
    row = {
        "judge": {
            "criterion_judgments": [
                {"weight": 10, "met": True},
                {"weight": -5, "met": False},
            ]
        }
    }
    assert module.row_pass_rate(row) == 1


def test_b2_rejects_analyzer_and_g1_requires_it(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path / "b2", with_repair=False)
    rows = [json.loads(line) for line in args.result[0].read_text().splitlines()]
    b2 = next(row for row in rows if row["group"] == "B2")
    analyzer = _receipt("evil-b2-analyzer", module.TASK_ANALYZER_MODEL)
    analyzer["role"] = "task_analyzer"
    attempt_run = b2["execution"]["generation_attempts"][0]["run"]
    attempt_run["usage"]["model_usage_breakdown"].append(analyzer)
    attempt_run["llm_request_count"] = 2
    b2_index = rows.index(b2)
    rows[b2_index] = module.seal_result_row(b2)
    args.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        with pytest.raises(module.FinalizationError, match="physical generation route"):
            module.run_finalization(args)
    finally:
        os.close(lock_fd)

    args2, _, lock_fd2 = _campaign(module, tmp_path / "g1", with_repair=False)
    rows2 = [json.loads(line) for line in args2.result[0].read_text().splitlines()]
    g1 = next(row for row in rows2 if row["group"] == "G1")
    attempt_run2 = g1["execution"]["generation_attempts"][0]["run"]
    attempt_run2["usage"]["model_usage_breakdown"] = attempt_run2["usage"]["model_usage_breakdown"][
        1:
    ]
    attempt_run2["llm_request_count"] = 4
    g1_index = rows2.index(g1)
    rows2[g1_index] = module.seal_result_row(g1)
    args2.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows2))
    try:
        with pytest.raises(module.FinalizationError, match="selected final answer"):
            module.run_finalization(args2)
    finally:
        os.close(lock_fd2)


def test_g1_proposer_count_above_frozen_ranking_bound_is_rejected(module) -> None:
    ranking = _test_ranking_config(module)
    module.FORMAL_G1_RANKING_CONFIG_SHA256 = module.canonical_sha256(ranking)
    routes = {f"vendor/model-{index}": "vendor" for index in range(6)}
    routes_hash = module.canonical_sha256(routes)
    contract = {
        "profile_id": "test-g1",
        "selection_mode": "router_dynamic",
        "user_profile_enabled": False,
        "source_registry_snapshot_version": "test-registry-v1",
        "expected_routes": routes,
        "expected_routes_sha256": routes_hash,
        "expected_candidate_count": 6,
        "expected_source_registry_snapshot_sha256": (
            module.FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256
        ),
        "expected_ranking_config_schema_version": (module.FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION),
        "expected_ranking_config_version": (module.FORMAL_G1_RANKING_CONFIG_VERSION),
        "expected_ranking_config_sha256": (module.FORMAL_G1_RANKING_CONFIG_SHA256),
        "expected_proposer_count_max": 5,
        "task_analyzer": _default_task_analyzer_policy(),
    }
    identities = [f"openrouter:{model}" for model in routes]
    version = f"test-registry-v1+test-g1+{routes_hash[:12]}"
    plan = {
        "candidate_allowlist": {
            "policy": "exact_openrouter_routes",
            "profile_id": "test-g1",
            "source_registry_snapshot_version": "test-registry-v1",
            "expected_source_registry_snapshot_sha256": (
                module.FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256
            ),
            "filtered_registry_snapshot_version": version,
            "expected_routes_sha256": routes_hash,
            "expected_candidate_count": 6,
            "candidate_count": 6,
            "expected_identities": identities,
        },
        "candidate_pool_size": 6,
        "candidate_pool": [{"identity": value} for value in identities],
        "registry_snapshot_version": version,
        "registry_snapshot_hash": TEST_G1_RUNTIME_REGISTRY_HASH,
        "ranking_config_schema_version": (module.FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION),
        "ranking_config_version": module.FORMAL_G1_RANKING_CONFIG_VERSION,
        "ranking_config_hash": module.FORMAL_G1_RANKING_CONFIG_SHA256,
        "ranking_parameters": ranking,
        "task_profile": {
            "tier_dist": {"4": 1.0},
            "constraints": {
                "cost": "medium",
                "latency": "normal",
                "risk": "low",
            },
        },
        "N_min": 3,
        "N_max": 5,
        "bound_reasons": ["tier_4"],
        "selected_P": identities,
        "selected_A": identities[0],
        "proposer_models": list(routes),
        "proposer_sample_count": 6,
        "proposer_count": 6,
        "aggregator_model": next(iter(routes)),
        "selection_steps": [
            {"step": index, "selected": identity}
            for index, identity in enumerate(identities, start=1)
        ],
    }
    reasons, _, _ = module.g1_registry_plan_reasons(plan, contract=contract)
    assert "g1_selected_proposer_count_outside_bounds" in reasons


def test_selected_generation_cost_is_reconciled_to_physical_ledger(module, tmp_path: Path) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    rows = [json.loads(line) for line in args.result[0].read_text().splitlines()]
    rows[0]["cost_accounting"]["selected_generation_attempt"]["recorded_cost_usd"] = 9.9
    rows[0] = module.seal_result_row(rows[0])
    args.result[0].write_text("".join(json.dumps(row) + "\n" for row in rows))
    try:
        manifest = module.run_finalization(args)
    finally:
        os.close(lock_fd)
    assert manifest["status"] == "complete"
    assert manifest["execution_pass"] is True
    assert any("cost conflicts with ledger" in warning for warning in manifest["warnings"])


def _g1_retrospective_fixture(module, tmp_path: Path):
    task = {
        "id": "g1-retrospective-task",
        "prompt": "research this",
        "rubric": {
            "id": "rubric-1",
            "sections": [
                {
                    "id": "quality",
                    "title": "Quality",
                    "criteria": [
                        {"id": "core", "weight": 85, "requirement": "core"},
                        {"id": "extra", "weight": 15, "requirement": "extra"},
                    ],
                }
            ],
        },
    }
    module.FORMAL_G1_RANKING_CONFIG_SHA256 = module.canonical_sha256(_test_ranking_config(module))
    contract = _contract(module, "G1", "a" * 64)
    fingerprint = module.canonical_sha256(contract, prefix=True)
    pre = deepcopy(
        _row(
            module,
            group="G1",
            task=task,
            fingerprint=fingerprint,
            response_prefix="g1-retrospective",
        )
    )
    restored_routing = deepcopy(pre["routing_trace"])
    original_attempt = pre["execution"]["generation_attempts"][0]
    analyzer = deepcopy(pre["usage"]["model_usage_breakdown"][0])
    selected_unit = deepcopy(pre["usage"]["model_usage_breakdown"][1])
    selected_attempt_id = "2" * 32
    post_attempt_id = "3" * 32
    attempt_1 = {
        **deepcopy(original_attempt),
        "attempt_id": "1" * 32,
        "attempt": 1,
        "retry_reason": "provider_lifecycle_setup_retry",
        "will_retry": True,
        "run": {
            "error": "provider_lifecycle_setup_retry",
            "final_text_sha256": module.text_sha256("discarded setup answer"),
            "llm_request_count": 1,
            "routing_trace": deepcopy(restored_routing),
            "usage": {"model_usage_breakdown": [analyzer]},
        },
    }
    attempt_2 = {
        **deepcopy(original_attempt),
        "attempt_id": selected_attempt_id,
        "attempt": 2,
        "started_at": 1_006.0,
        "completed_at": 1_007.0,
        "retry_reason": "",
        "will_retry": False,
        "run": {
            "error": "",
            "final_text_sha256": pre["final_text_sha256"],
            "llm_request_count": 1,
            "routing_trace": {},
            "usage": {"model_usage_breakdown": [selected_unit]},
        },
    }
    pre.update(
        {
            "routing_trace": {},
            "llm_request_count": 1,
            "usage": {
                "model_usage_breakdown": [selected_unit],
                "billed_cost": 0.1,
            },
            "generation_attempt_count": 2,
            "generation_attempt_budget_used": 2,
            "generation_attempt_total_billed_cost": 0.2,
            "generation_max_attempts": 3,
        }
    )
    pre["actual_spend_metrics"]["generation_attempt_count"] = 2
    pre["cost_accounting"]["selected_generation_attempt"].update(
        {
            "recorded_cost_usd": 0.1,
            "request_count": 1,
        }
    )
    pre["execution"].update(
        {
            "prior_generation_attempts_used": 0,
            "selected_generation_attempt": 2,
            "generation_max_attempts": 3,
            "generation_attempts": [attempt_1, attempt_2],
        }
    )
    recovered_routing, recovery_evidence, recovery_reasons = module.effective_g1_lifecycle_routing(
        pre, contract=contract
    )
    assert recovery_reasons == []
    assert recovered_routing == restored_routing
    assert (
        module.generation_reasons(
            module.SourceRecord(tmp_path / "pre.jsonl", 0, 1, pre),
            task=task,
            expected_fingerprint=fingerprint,
            contract=contract,
        )
        == []
    )

    post = deepcopy(pre)
    post_attempt = {
        "attempt_id": post_attempt_id,
        "attempt_kind": "generation",
        "attempt": 3,
        "started_at": 1_020.0,
        "completed_at": 1_021.0,
        "retry_reason": module.LEGACY_TERMINAL_POLICY_ERROR,
        "will_retry": False,
        "run": {
            "error": module.LEGACY_TERMINAL_POLICY_ERROR,
            "final_text_sha256": module.text_sha256("failed attempt three"),
            "llm_request_count": 1,
            "routing_trace": {},
            "usage": {
                "model_usage_breakdown": [
                    _receipt(
                        "g1-retrospective-attempt-3",
                        "z-ai/glm-5.2",
                        cost=0.3,
                    )
                ]
            },
        },
    }
    post.update(
        {
            "selected_generation_succeeded": False,
            "error": module.LEGACY_TERMINAL_POLICY_ERROR,
            "generation_attempt_count": 1,
            "generation_attempt_budget_used": 3,
            "generation_attempt_total_billed_cost": 0.3,
            "completed_at": 1_022.0,
        }
    )
    post["completion_status"]["generation_accepted"] = False
    post["actual_spend_metrics"]["generation_attempt_count"] = 1
    post["execution"].update(
        {
            "run_error": module.LEGACY_TERMINAL_POLICY_ERROR,
            "prior_generation_attempts_used": 2,
            "selected_generation_attempt": 3,
            "generation_attempts": [post_attempt],
        }
    )

    repair = deepcopy(pre)
    repair["routing_trace"] = recovered_routing
    repair["completed_at"] = 1_030.0
    repair["generation_attempt_budget_used"] = 3
    repair["execution"].update(
        {
            "prior_generation_attempts_used": 3,
            "resume_action": "metadata_only",
            "generation_reused": True,
            "metadata_repair_attempted": True,
            "metadata_repaired": True,
            "judge_reran": False,
            "g1_provider_lifecycle_routing_recovery": recovery_evidence,
        }
    )
    repair["resume_completion"] = {
        "action": "metadata_only",
        "generation_reused": True,
        "metadata_repaired": True,
        "judge_reran": False,
        "post_repair_action": "complete",
        "status": "complete",
        "incomplete_reasons": [],
    }
    records = [
        module.SourceRecord(tmp_path / "pre.jsonl", 0, 1, module.seal_result_row(pre)),
        module.SourceRecord(tmp_path / "post.jsonl", 1, 1, module.seal_result_row(post)),
        module.SourceRecord(tmp_path / "repair.jsonl", 2, 1, module.seal_result_row(repair)),
    ]
    manifest_sources = [
        {
            "resume_schedule_contract_verified": False,
            "resume_scheduled_pairs": [],
        },
        {
            "resume_schedule_contract_verified": True,
            "resume_scheduled_pairs": [
                {
                    "group": "G1",
                    "task_id": task["id"],
                    "action": "regenerate",
                }
            ],
        },
        {
            "resume_schedule_contract_verified": True,
            "resume_scheduled_pairs": [
                {
                    "group": "G1",
                    "task_id": task["id"],
                    "action": "metadata_only",
                }
            ],
        },
    ]
    return (
        task,
        contract,
        fingerprint,
        records,
        manifest_sources,
        selected_attempt_id,
        post_attempt_id,
        restored_routing,
    )


def _select_g1_retrospective(
    module,
    task,
    contract,
    fingerprint,
    records,
    manifest_sources,
):
    return module.select_results(
        records,
        tasks=[task],
        groups=["G1"],
        fingerprints={"G1": fingerprint},
        contracts={"G1": contract},
        max_attempts=3,
        manifest_sources=manifest_sources,
    )


def test_select_results_validates_each_g1_wave_against_only_its_cumulative_prefix(
    module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attempt_a = {
        "attempt_id": "a" * 32,
        "attempt": 1,
        "attempt_kind": "generation",
    }
    attempt_b = {
        "attempt_id": "b" * 32,
        "attempt": 2,
        "attempt_kind": "generation",
    }
    rows = [
        {
            "group": "G1",
            "task_id": "task-1",
            "final_text_sha256": "answer-a",
            "generation_attempt_budget_used": 1,
            "execution": {"generation_attempts": [attempt_a]},
        },
        {
            "group": "G1",
            "task_id": "task-1",
            "final_text_sha256": "answer-b",
            "generation_attempt_budget_used": 2,
            "execution": {"generation_attempts": [attempt_b]},
        },
    ]
    records = [
        module.SourceRecord(tmp_path / f"wave-{index}.jsonl", index, 1, row)
        for index, row in enumerate(rows, start=1)
    ]
    observed_prefixes: list[list[str]] = []

    def fake_generation_reasons(record, **_kwargs):
        attempt_ids = [
            attempt["attempt_id"] for attempt in record.row["execution"]["generation_attempts"]
        ]
        observed_prefixes.append(attempt_ids)
        return [] if attempt_ids == ["a" * 32, "b" * 32] else ["deferred"]

    monkeypatch.setattr(module, "generation_reasons", fake_generation_reasons)
    monkeypatch.setattr(
        module,
        "generation_identity",
        lambda row: str(row["final_text_sha256"]),
    )
    monkeypatch.setattr(
        module,
        "generation_attempt_count",
        lambda row: len(row["execution"]["generation_attempts"]),
    )
    monkeypatch.setattr(module, "judge_reasons", lambda *_args, **_kwargs: [])

    selected, _ = module.select_results(
        records,
        tasks=[{"id": "task-1"}],
        groups=["G1"],
        fingerprints={"G1": "fingerprint"},
        contracts={"G1": {}},
        max_attempts=3,
    )

    assert observed_prefixes == [
        ["a" * 32],
        ["a" * 32, "b" * 32],
    ]
    assert selected == [records[1]]


def test_g1_retrospective_selects_attempt_two_and_keeps_attempt_three_spend(
    module,
    tmp_path: Path,
) -> None:
    (
        task,
        contract,
        fingerprint,
        records,
        manifest_sources,
        selected_attempt_id,
        post_attempt_id,
        _,
    ) = _g1_retrospective_fixture(module, tmp_path)

    selected, pair_audit = _select_g1_retrospective(
        module,
        task,
        contract,
        fingerprint,
        records,
        manifest_sources,
    )
    assert selected == [records[2]]
    recovery = pair_audit[f"G1/{task['id']}"]["retrospective_reclassification_recovery"]
    assert recovery["selected_attempt"] == 2
    assert recovery["selected_attempt_id"] == selected_attempt_id
    assert recovery["invalid_post_accept_attempt_ids"] == [post_attempt_id]

    bindings = module.bind_selected_generation_attempts(records, selected)
    assert bindings == {f"G1/{task['id']}": selected_attempt_id}
    ledger, _ = module.build_actual_spend_ledger(
        records,
        selected=selected,
        selected_attempt_bindings=bindings,
    )
    by_attempt = {
        str(reference["attempt_id"]): row
        for row in ledger
        for reference in row["source_references"]
        if reference.get("phase") == "generation"
    }
    assert by_attempt[selected_attempt_id]["generation_disposition"] == "selected"
    assert by_attempt[selected_attempt_id]["physical_source"]["source_index"] == 0
    assert by_attempt[selected_attempt_id]["receipt_source_indexes"] == [2]
    assert by_attempt[post_attempt_id]["generation_disposition"] == "failed"
    assert by_attempt[post_attempt_id]["physical_source"]["source_index"] == 1
    assert by_attempt[post_attempt_id]["receipt_source_indexes"] == [1]
    assert by_attempt[post_attempt_id]["recorded_cost_usd"] == "0.300000000"


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_manifest_action",
        "wrong_post_error",
        "post_will_retry",
        "extra_post_attempt",
        "pre_top_routing_present",
    ],
)
def test_g1_retrospective_exception_fails_closed(
    module,
    tmp_path: Path,
    mutation: str,
) -> None:
    (
        task,
        contract,
        fingerprint,
        records,
        manifest_sources,
        _,
        _,
        restored_routing,
    ) = _g1_retrospective_fixture(module, tmp_path)
    if mutation == "wrong_manifest_action":
        manifest_sources[1]["resume_scheduled_pairs"][0]["action"] = "metadata_only"
    elif mutation == "wrong_post_error":
        records[1].row["error"] = "different_error"
    elif mutation == "post_will_retry":
        records[1].row["execution"]["generation_attempts"][0]["will_retry"] = True
    elif mutation == "extra_post_attempt":
        extra = deepcopy(records[1].row["execution"]["generation_attempts"][0])
        extra["attempt_id"] = "4" * 32
        records[1].row["execution"]["generation_attempts"].append(extra)
    else:
        records[0].row["routing_trace"] = deepcopy(restored_routing)

    with pytest.raises(module.FinalizationError):
        _select_g1_retrospective(
            module,
            task,
            contract,
            fingerprint,
            records,
            manifest_sources,
        )


def _install_complete_metadata_schedule(module, args: argparse.Namespace) -> dict:
    manifest = json.loads(args.manifest[1].read_text())
    rows = [json.loads(line) for line in args.result[1].read_text().splitlines()]
    scheduled_pairs = [
        {
            "group": str(row["group"]),
            "task_id": str(row["task_id"]),
            "action": "metadata_only",
        }
        for row in rows
    ]
    pair_count = len(scheduled_pairs)
    manifest["resume_selection"] = {
        "selected_pair_count": pair_count,
        "scheduled_pair_count": pair_count,
        "regenerate_pair_count": 0,
        "model_regenerate_pair_count": 0,
        "generation_budget_exhausted_pair_count": 0,
        "generation_auto_retry_blocked_pair_count": 0,
        "judge_only_pair_count": 0,
        "metadata_only_pair_count": pair_count,
        "audit_only_pair_count": 0,
        "policy_violation_pair_count": 0,
        "best_pair_count": pair_count,
        "strict_valid_pair_count": 0,
        "scheduled_pairs": scheduled_pairs,
        "resume_action_counts": {
            "regenerate": 0,
            "judge_only": 0,
            "metadata_only": pair_count,
            "audit_only": 0,
            "complete": 0,
        },
    }
    _owner_json(args.manifest[1], manifest)
    return manifest


def test_manifest_resume_schedule_accepts_legacy_missing_reserved_zero(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    try:
        _install_complete_metadata_schedule(module, args)
        module.load_manifest_contracts(
            args.manifest,
            result_paths=args.result,
            groups=module.GROUPS,
        )
    finally:
        os.close(lock_fd)


def _install_durable_artifact_binding(
    module,
    result_path: Path,
    manifest_path: Path,
    *,
    durable_v2: bool = False,
):
    rows = [
        json.loads(line)
        for line in result_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if durable_v2:
        stamp = "20260811-010203"
        durable_result_path = result_path.parent / f"draco_ensemble_{stamp}.jsonl"
        durable_manifest_path = manifest_path.parent / (
            f"draco_run_{stamp}.manifest.json"
        )
        result_path.rename(durable_result_path)
        manifest_path.rename(durable_manifest_path)
        result_path = durable_result_path
        manifest_path = durable_manifest_path
        capability = module.durable_artifact_capability_contract()
        manifest["schema"] = module.DRACO_RUN_MANIFEST_SCHEMA_V2
        manifest["stamp"] = stamp
        manifest["durable_artifact_capability"] = capability
        for group, contract in manifest["run_compatibility"]["contracts"].items():
            contract["durable_artifact_capability"] = capability
            manifest["run_compatibility"]["fingerprints"][group] = (
                module.canonical_sha256(contract, prefix=True)
            )
        rows = [
            module.seal_result_row(
                {
                    **row,
                    "run_compatibility_fingerprint": manifest[
                        "run_compatibility"
                    ]["fingerprints"][str(row["group"])],
                    module.DRACO_DURABLE_RESULT_ROW_FIELD: capability,
                }
            )
            for row in rows
        ]
    result_path.unlink()
    trace_path = (
        result_path.parent / f"draco_run_{stamp}.trace.jsonl"
        if durable_v2
        else result_path.with_suffix(".trace.jsonl")
    )
    checkpoint_path = (
        result_path.parent / f"draco_run_{stamp}.checkpoint.json"
        if durable_v2
        else result_path.with_suffix(".checkpoint.json")
    )
    with DurableDracoArtifactWriter(
        results_path=result_path,
        trace_path=trace_path,
        checkpoint_path=checkpoint_path,
    ) as writer:
        for row in rows:
            assert writer.append(row, canonical_trace_row_from_result(row)) is True
    for path in (result_path, trace_path, checkpoint_path):
        path.chmod(0o600)
    manifest["artifacts"].update(
        {
            "results_jsonl": str(result_path.resolve()),
            "trace_jsonl": str(trace_path.resolve()),
            "checkpoint_json": str(checkpoint_path.resolve()),
            "manifest_json": str(manifest_path.resolve()),
        }
    )
    _owner_json(manifest_path, manifest)
    return result_path, manifest_path, trace_path, checkpoint_path


def test_manifest_checkpoint_binding_accepts_new_artifacts_and_preserves_legacy(
    module,
    tmp_path: Path,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    try:
        legacy_records, _ = module.read_source_rows(args.result)
        module.load_manifest_contracts(
            args.manifest,
            result_paths=args.result,
            groups=module.GROUPS,
        )
        (
            args.result[0],
            args.manifest[0],
            trace_path,
            checkpoint_path,
        ) = _install_durable_artifact_binding(
            module,
            args.result[0],
            args.manifest[0],
            durable_v2=True,
        )
        _, _, _, sources = module.load_manifest_contracts(
            args.manifest,
            result_paths=args.result,
            groups=module.GROUPS,
        )
        durable = sources[0]["durable_artifacts"]
        assert durable["rows_written"] == len(legacy_records)
        assert durable["trace_sha256"] == module.file_sha256(trace_path)
        assert durable["checkpoint_sha256"] == module.file_sha256(checkpoint_path)
        prior_manifest = json.loads(args.manifest[0].read_text(encoding="utf-8"))
        prior_manifest["status"] = "running"
        _owner_json(args.manifest[0], prior_manifest)
        prior_manifest_path = args.manifest[0].with_name(
            f"{args.manifest[0].name}.pre-recovery"
        )
        prior_manifest_path.write_bytes(args.manifest[0].read_bytes())
        prior_manifest_path.chmod(0o600)
        manifest = json.loads(args.manifest[0].read_text(encoding="utf-8"))
        manifest["status"] = "result_incomplete"
        manifest["artifacts"]["pre_recovery_manifest_json"] = str(
            prior_manifest_path.resolve()
        )
        manifest["artifact_recovery"] = {
            "schema": "opensquilla.draco-artifact-recovery/v1",
            "status": "sealed_after_unclean_exit",
            "prior_status": "running",
            "prior_manifest_sha256": module.file_sha256(prior_manifest_path),
            "prior_manifest_path": str(prior_manifest_path.resolve()),
            "rows_written": durable["rows_written"],
            "results_sha256": durable["results_sha256"],
            "trace_sha256": durable["trace_sha256"],
            "checkpoint_sha256": durable["checkpoint_sha256"],
            "ambiguous_pair_count": 0,
            "ambiguous_pairs": [],
            "durable_scheduled_pairs": [],
            "physical_request_policy": "no_model_or_provider_calls",
            "automatic_rerun_allowed": False,
        }
        _owner_json(args.manifest[0], manifest)
        _, _, _, recovered_sources = module.load_manifest_contracts(
            args.manifest,
            result_paths=args.result,
            groups=module.GROUPS,
        )
        assert recovered_sources[0]["artifact_recovery"]["prior_status"] == "running"
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize(
    "downgrade",
    ["missing_checkpoint", "missing_top_capability", "missing_contract_capability", "row_only"],
)
def test_manifest_v2_durable_capability_cannot_be_downgraded(
    module,
    tmp_path: Path,
    downgrade: str,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    try:
        (
            args.result[0],
            args.manifest[0],
            _,
            _,
        ) = _install_durable_artifact_binding(
            module,
            args.result[0],
            args.manifest[0],
            durable_v2=True,
        )
        manifest = json.loads(args.manifest[0].read_text(encoding="utf-8"))
        if downgrade == "missing_checkpoint":
            manifest["artifacts"].pop("checkpoint_json")
        elif downgrade == "missing_top_capability":
            manifest.pop("durable_artifact_capability")
        elif downgrade == "missing_contract_capability":
            contract = manifest["run_compatibility"]["contracts"][module.GROUPS[0]]
            contract.pop("durable_artifact_capability")
            manifest["run_compatibility"]["fingerprints"][module.GROUPS[0]] = (
                module.canonical_sha256(contract, prefix=True)
            )
        else:
            manifest.pop("schema")
            manifest.pop("durable_artifact_capability")
            for group, contract in manifest["run_compatibility"]["contracts"].items():
                contract.pop("durable_artifact_capability")
                manifest["run_compatibility"]["fingerprints"][group] = (
                    module.canonical_sha256(contract, prefix=True)
                )
        _owner_json(args.manifest[0], manifest)

        with pytest.raises(
            module.FinalizationError,
            match="durable capability|stripped from sealed rows",
        ):
            module.load_manifest_contracts(
                args.manifest,
                result_paths=args.result,
                groups=module.GROUPS,
            )
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize(
    "tamper",
    ["trace", "checkpoint_count", "recovery_hash", "recovery_policy", "ambiguity"],
)
def test_manifest_checkpoint_or_recovery_mismatch_fails_closed(
    module,
    tmp_path: Path,
    tamper: str,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path, with_repair=False)
    try:
        (
            args.result[0],
            args.manifest[0],
            trace_path,
            checkpoint_path,
        ) = _install_durable_artifact_binding(
            module,
            args.result[0],
            args.manifest[0],
            durable_v2=True,
        )
        manifest = json.loads(args.manifest[0].read_text(encoding="utf-8"))
        verification = module.verify_durable_draco_artifacts(
            results_path=args.result[0],
            trace_path=trace_path,
            checkpoint_path=checkpoint_path,
        )
        if tamper == "trace":
            trace_rows = trace_path.read_text(encoding="utf-8").splitlines()
            value = json.loads(trace_rows[0])
            value["error"] = "forged"
            trace_rows[0] = json.dumps(value)
            trace_path.write_text("\n".join(trace_rows) + "\n", encoding="utf-8")
        elif tamper == "checkpoint_count":
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["rows_written"] -= 1
            _owner_json(checkpoint_path, checkpoint)
        else:
            prior_manifest = json.loads(args.manifest[0].read_text(encoding="utf-8"))
            prior_manifest["status"] = "running"
            _owner_json(args.manifest[0], prior_manifest)
            prior_manifest_path = args.manifest[0].with_name(
                f"{args.manifest[0].name}.pre-recovery"
            )
            prior_manifest_path.write_bytes(args.manifest[0].read_bytes())
            prior_manifest_path.chmod(0o600)
            manifest["status"] = "result_incomplete"
            manifest["artifacts"]["pre_recovery_manifest_json"] = str(
                prior_manifest_path.resolve()
            )
            manifest["artifact_recovery"] = {
                "schema": "opensquilla.draco-artifact-recovery/v1",
                "status": "sealed_after_unclean_exit",
                "prior_status": "running",
                "prior_manifest_sha256": module.file_sha256(prior_manifest_path),
                "prior_manifest_path": str(prior_manifest_path.resolve()),
                "rows_written": verification["rows_written"],
                "results_sha256": verification["results_sha256"],
                "trace_sha256": "0" * 64,
                "checkpoint_sha256": verification["checkpoint_sha256"],
                "ambiguous_pair_count": 0,
                "ambiguous_pairs": [],
                "durable_scheduled_pairs": [],
                "physical_request_policy": "no_model_or_provider_calls",
                "automatic_rerun_allowed": False,
            }
            if tamper == "recovery_policy":
                manifest["artifact_recovery"]["automatic_rerun_allowed"] = True
                manifest["artifact_recovery"]["trace_sha256"] = verification[
                    "trace_sha256"
                ]
            elif tamper == "ambiguity":
                manifest["artifact_recovery"]["ambiguous_pair_count"] = 1
                manifest["artifact_recovery"]["ambiguous_pairs"] = [
                    {"group": "B0", "task_id": "forged"}
                ]
                manifest["artifact_recovery"]["trace_sha256"] = verification[
                    "trace_sha256"
                ]
            _owner_json(args.manifest[0], manifest)
        with pytest.raises(module.FinalizationError, match="durable|recovery"):
            module.load_manifest_contracts(
                args.manifest,
                result_paths=args.result,
                groups=module.GROUPS,
            )
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize("bad_value", [1, True])
def test_manifest_resume_schedule_rejects_nonzero_or_boolean_policy_counter(
    module,
    tmp_path: Path,
    bad_value,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    try:
        manifest = _install_complete_metadata_schedule(module, args)
        manifest["resume_selection"]["resume_action_counts"]["policy_violation"] = bad_value
        _owner_json(args.manifest[1], manifest)
        with pytest.raises(
            module.FinalizationError,
            match="resume schedule counters differ from its result shard",
        ):
            module.load_manifest_contracts(
                args.manifest,
                result_paths=args.result,
                groups=module.GROUPS,
            )
    finally:
        os.close(lock_fd)


def _install_audit_only_schedule(module, args: argparse.Namespace) -> None:
    manifest = _install_complete_metadata_schedule(module, args)
    rows = [json.loads(line) for line in args.result[1].read_text().splitlines()]
    for row in rows:
        execution = dict(row["execution"])
        execution.update(
            {
                "resume_action": "audit_only",
                "generation_reused": True,
                "judge_reran": False,
                "audit_only_recorded": True,
                "audit_only_summary": {
                    "status": "recorded",
                    "generation_called": False,
                    "judge_called": False,
                },
            }
        )
        row["execution"] = execution
        completion = dict(row.get("resume_completion") or {})
        completion.update({"action": "audit_only", "generation_reused": True})
        row["resume_completion"] = completion
    rows = [module.seal_result_row(row) for row in rows]
    args.result[1].write_text("".join(json.dumps(row) + "\n" for row in rows))
    args.result[1].chmod(0o600)

    resume = manifest["resume_selection"]
    for scheduled in resume["scheduled_pairs"]:
        scheduled["action"] = "audit_only"
    pair_count = len(resume["scheduled_pairs"])
    resume["metadata_only_pair_count"] = 0
    resume["audit_only_pair_count"] = pair_count
    resume["resume_action_counts"]["metadata_only"] = 0
    resume["resume_action_counts"]["audit_only"] = pair_count
    _owner_json(args.manifest[1], manifest)


@pytest.mark.parametrize("tamper", [None, "missing_marker", "judge_reran"])
def test_audit_only_no_generation_wave_contract(
    module,
    tmp_path: Path,
    tamper: str | None,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    try:
        _install_audit_only_schedule(module, args)
        if tamper is not None:
            rows = [json.loads(line) for line in args.result[1].read_text().splitlines()]
            if tamper == "missing_marker":
                rows[0]["execution"].pop("audit_only_recorded")
            else:
                rows[0]["execution"]["judge_reran"] = True
            rows[0] = module.seal_result_row(rows[0])
            args.result[1].write_text("".join(json.dumps(row) + "\n" for row in rows))
            args.result[1].chmod(0o600)

        if tamper is None:
            module.load_manifest_contracts(
                args.manifest,
                result_paths=args.result,
                groups=module.GROUPS,
            )
        else:
            with pytest.raises(
                module.FinalizationError,
                match="skipped Web preflight is not bound",
            ):
                module.load_manifest_contracts(
                    args.manifest,
                    result_paths=args.result,
                    groups=module.GROUPS,
                )
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (None, True),
        ("missing_marker", False),
        ("judge_reran", False),
        ("generation_called", False),
    ],
)
def test_audit_only_repair_evidence_is_explicit(
    module,
    mutation: str | None,
    expected: bool,
) -> None:
    execution = {
        "resume_action": "audit_only",
        "generation_reused": True,
        "judge_reran": False,
        "audit_only_recorded": True,
        "audit_only_summary": {
            "status": "recorded",
            "generation_called": False,
            "judge_called": False,
        },
    }
    if mutation == "missing_marker":
        execution.pop("audit_only_recorded")
    elif mutation == "judge_reran":
        execution["judge_reran"] = True
    elif mutation == "generation_called":
        execution["audit_only_summary"]["generation_called"] = True

    row = {
        "resume_completion": {
            "action": "audit_only",
            "generation_reused": True,
        }
    }
    assert module.repair_evidence(row, execution) is expected


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "not_blocked",
        "missing_completion",
        "complete_status",
        "wrong_post_action",
        "judge_reran",
        "metadata_repaired",
        "missing_reason",
        "model_started",
        "missing_terminal",
        "wrong_terminal_schema",
        "wrong_terminal_attempt",
        "automatic_retry_allowed",
    ],
)
def test_blocked_regenerate_repair_evidence_is_explicit(
    module,
    mutation: str | None,
) -> None:
    execution = {
        "resume_action": "regenerate",
        "generation_reused": True,
        "generation_auto_retry_blocked": True,
        "generation_model_started": False,
        "generation_attempts": [{"attempt_id": "a" * 32}],
        "provider_native_proposer_recovery_terminal": {
            "schema": module.PROVIDER_NATIVE_PROPOSER_RECOVERY_TERMINAL_SCHEMA,
            "status": "receipt_invalid",
            "attempt_id": "a" * 32,
            "automatic_generation_retry_allowed": False,
        },
    }
    row = {
        "resume_completion": {
            "action": "regenerate",
            "generation_reused": True,
            "status": "incomplete",
            "post_repair_action": "regenerate",
            "judge_reran": False,
            "metadata_repaired": False,
            "incomplete_reasons": ["generation_auto_retry_blocked"],
        }
    }
    if mutation == "not_blocked":
        execution["generation_auto_retry_blocked"] = False
    elif mutation == "missing_completion":
        row.pop("resume_completion")
    elif mutation == "complete_status":
        row["resume_completion"]["status"] = "complete"
    elif mutation == "wrong_post_action":
        row["resume_completion"]["post_repair_action"] = "judge_only"
    elif mutation == "judge_reran":
        row["resume_completion"]["judge_reran"] = True
    elif mutation == "metadata_repaired":
        row["resume_completion"]["metadata_repaired"] = True
    elif mutation == "missing_reason":
        row["resume_completion"]["incomplete_reasons"] = []
    elif mutation == "model_started":
        execution["generation_model_started"] = True
    elif mutation == "missing_terminal":
        execution.pop("provider_native_proposer_recovery_terminal")
    elif mutation == "wrong_terminal_schema":
        execution["provider_native_proposer_recovery_terminal"]["schema"] = "wrong"
    elif mutation == "wrong_terminal_attempt":
        execution["provider_native_proposer_recovery_terminal"]["attempt_id"] = "b" * 32
    elif mutation == "automatic_retry_allowed":
        execution["provider_native_proposer_recovery_terminal"][
            "automatic_generation_retry_allowed"
        ] = True

    assert module.repair_evidence(row, execution) is (mutation is None)


@pytest.mark.parametrize(
    ("field_name", "schema_name"),
    [
        (
            "generation_postprocessing_terminal",
            "GENERATION_POSTPROCESSING_TERMINAL_SCHEMA",
        ),
        (
            "provider_native_proposer_recovery_terminal",
            "PROVIDER_NATIVE_PROPOSER_RECOVERY_TERMINAL_SCHEMA",
        ),
    ],
)
def test_blocked_regenerate_accepts_both_producer_terminal_schemas(
    module,
    field_name: str,
    schema_name: str,
) -> None:
    attempt_id = "a" * 32
    execution = {
        "generation_model_started": False,
        "generation_attempts": [{"attempt_id": attempt_id}],
        field_name: {
            "schema": getattr(module, schema_name),
            "attempt_id": attempt_id,
            "automatic_generation_retry_allowed": False,
        },
    }

    assert module.blocked_regenerate_terminal_evidence(execution) is True


def test_fixed_fallback_non_proposer_terminal_is_valid_repair_evidence(
    module,
) -> None:
    attempt_id = "a" * 32
    execution = {
        "resume_action": "regenerate",
        "generation_reused": True,
        "generation_auto_retry_blocked": True,
        "generation_model_started": False,
        "generation_attempts": [{"attempt_id": attempt_id}],
        "provider_native_proposer_recovery_terminal": {
            "schema": module.PROVIDER_NATIVE_PROPOSER_RECOVERY_TERMINAL_SCHEMA,
            "status": "receipt_invalid",
            "attempt_id": attempt_id,
            "automatic_generation_retry_allowed": False,
        },
    }
    row = {
        "resume_completion": {
            "action": "regenerate",
            "generation_reused": True,
            "status": "incomplete",
            "post_repair_action": "regenerate",
            "judge_reran": False,
            "metadata_repaired": False,
            "incomplete_reasons": ["generation_auto_retry_blocked"],
        }
    }

    assert module.blocked_regenerate_terminal_evidence(execution) is True
    assert module.repair_evidence(row, execution) is True


@pytest.mark.parametrize("tamper", ["counter", "result_pair_set"])
def test_manifest_resume_schedule_contract_rejects_counter_or_set_tamper(
    module,
    tmp_path: Path,
    tamper: str,
) -> None:
    args, _, lock_fd = _campaign(module, tmp_path)
    try:
        manifest = _install_complete_metadata_schedule(module, args)
        resume = manifest["resume_selection"]
        if tamper == "counter":
            resume["scheduled_pair_count"] -= 1
        else:
            resume["scheduled_pairs"].pop()
            resume["selected_pair_count"] -= 1
            resume["scheduled_pair_count"] -= 1
            resume["metadata_only_pair_count"] -= 1
            resume["resume_action_counts"]["metadata_only"] -= 1
        _owner_json(args.manifest[1], manifest)

        with pytest.raises(
            module.FinalizationError,
            match="resume schedule counters differ from its result shard",
        ):
            module.load_manifest_contracts(
                args.manifest,
                result_paths=args.result,
                groups=module.GROUPS,
            )
    finally:
        os.close(lock_fd)
