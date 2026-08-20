from __future__ import annotations

import ast
import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

import opensquilla.provider.ranking_router as ranking_router
from opensquilla.eval.draco_experiment_config import (
    DracoFrozenTaskAnalysisExecutionConfig,
    DracoFrozenTaskAnalysisExecutionV2Config,
    DracoFrozenTaskAnalysisExecutionV3Config,
)
from opensquilla.eval.draco_task_analyzer_execution import (
    build_task_analyzer_execution_contract,
)
from opensquilla.provider.ranking_router import (
    FROZEN_TASK_ANALYSIS_MODE,
    FROZEN_TASK_ANALYSIS_ROUTER_FALLBACK,
    FROZEN_TASK_ANALYSIS_SCHEMA,
    FROZEN_TASK_ANALYSIS_SCHEMA_V2,
    FROZEN_TASK_ANALYSIS_SCHEMA_V3,
    TASK_ANALYZER_VERSION,
    DynamicRankingError,
    _assert_public_ranking_trace_payload,
    build_request_context,
    canonical_json_sha256,
    fallback_task_profile,
    frozen_task_analysis_contract_reasons,
    frozen_task_analysis_plan_reasons,
    frozen_task_analysis_result,
    ranking_config_resolution,
)

ROOT = Path(__file__).resolve().parents[1]
FINALIZER = ROOT / "scripts" / "experiments" / "finalize_draco_campaign.py"
RUNNERS = (
    ROOT / "scripts" / "run_draco_routing_experiment.py",
    ROOT / "scripts" / "run_draco_routing_experiment_resume.py",
)


def _load_finalizer():
    spec = importlib.util.spec_from_file_location(
        "finalize_draco_campaign_frozen_replay_test",
        FINALIZER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture() -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    str,
    str,
    str,
]:
    ranking_config = copy.deepcopy(
        ranking_config_resolution()["effective_config"]
    )
    request_context = build_request_context(
        message="Frozen Analyzer replay test",
        turn_metadata={},
        attachments=[],
        candidate_output_tokens=8192,
        aggregator_output_tokens=8192,
        ranking_config=ranking_config,
    )
    profile = fallback_task_profile(
        routed_tier="c1",
        request_context=request_context,
        ranking_config=ranking_config,
    )
    profile_sha256 = canonical_json_sha256(profile)
    source_analyzer_config = copy.deepcopy(ranking_config["task_analyzer"])
    analyzer = {
        "source": "frozen_replay",
        "schema_valid": True,
        "confidence": 0.75,
        "analyzer_version": TASK_ANALYZER_VERSION,
        "provider": source_analyzer_config["provider"],
        "model": source_analyzer_config["model"],
        "fallback_reason": "",
        "usage": {},
        "normalization_warnings": [],
    }
    entries: dict[str, Any] = {}
    for ordinal in range(10):
        task_id = f"task-{ordinal:02d}"
        entries[task_id] = {
            "task_input_sha256": f"sha256:{ordinal + 1:064x}",
            "prompt_sha256": f"{ordinal + 101:064x}",
            "task_profile_pre_escalation": copy.deepcopy(profile),
            "task_profile_pre_escalation_sha256": profile_sha256,
            "task_analyzer": copy.deepcopy(analyzer),
        }
    contract = {
        "schema": FROZEN_TASK_ANALYSIS_SCHEMA,
        "mode": FROZEN_TASK_ANALYSIS_MODE,
        "source_experiment": "draco-mini-common-e0",
        "source_manifest_sha256": "a" * 64,
        "source_results_sha256": "b" * 64,
        "source_task_analyzer_config": source_analyzer_config,
        "source_task_analyzer_config_sha256": canonical_json_sha256(
            source_analyzer_config
        ),
        "entries": entries,
        "entries_sha256": canonical_json_sha256(entries),
    }
    task_id = "task-00"
    entry = entries[task_id]
    result = frozen_task_analysis_result(
        contract,
        task_id=task_id,
        task_input_sha256=entry["task_input_sha256"],
        prompt_sha256=entry["prompt_sha256"],
        routed_tier="c1",
        request_context=request_context,
        ranking_config=ranking_config,
    )
    plan = {
        "task_analyzer": result.trace(ranking_config),
        "task_profile_pre_escalation": copy.deepcopy(profile),
        "ranking_parameters": {
            "task_analyzer": copy.deepcopy(source_analyzer_config),
        },
    }
    return (
        contract,
        plan,
        request_context,
        task_id,
        entry["task_input_sha256"],
        entry["prompt_sha256"],
    )


def _fallback_fixture() -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    str,
    str,
    str,
]:
    contract, _, request_context, task_id, input_sha, prompt_sha = _fixture()
    fallback_contract = copy.deepcopy(contract)
    fallback_contract["schema"] = FROZEN_TASK_ANALYSIS_SCHEMA_V2
    for entry in fallback_contract["entries"].values():
        entry["origin_outcome"] = FROZEN_TASK_ANALYSIS_ROUTER_FALLBACK
        entry["task_analyzer"]["schema_valid"] = False
        entry["task_analyzer"]["fallback_reason"] = "TimeoutError"
    fallback_contract["entries_sha256"] = canonical_json_sha256(
        fallback_contract["entries"]
    )
    entry = fallback_contract["entries"][task_id]
    result = frozen_task_analysis_result(
        fallback_contract,
        task_id=task_id,
        task_input_sha256=input_sha,
        prompt_sha256=prompt_sha,
        routed_tier="c1",
        request_context=request_context,
        ranking_config=ranking_config_resolution()["effective_config"],
    )
    plan = {
        "task_analyzer": result.trace(ranking_config_resolution()["effective_config"]),
        "task_profile_pre_escalation": copy.deepcopy(
            entry["task_profile_pre_escalation"]
        ),
        "ranking_parameters": {
            "task_analyzer": copy.deepcopy(
                fallback_contract["source_task_analyzer_config"]
            ),
        },
    }
    return fallback_contract, plan, request_context, task_id, input_sha, prompt_sha


def test_replay_contract_materializes_bound_profile_with_zero_usage() -> None:
    contract, plan, _, task_id, task_input_sha256, prompt_sha256 = _fixture()

    assert frozen_task_analysis_contract_reasons(
        contract,
        expected_task_ids=list(contract["entries"]),
    ) == []
    validated_contract = DracoFrozenTaskAnalysisExecutionConfig.model_validate(contract)
    dumped_contract = validated_contract.model_dump(mode="json")
    assert dumped_contract["schema"] == FROZEN_TASK_ANALYSIS_SCHEMA
    assert "schema_id" not in dumped_contract
    assert frozen_task_analysis_plan_reasons(
        plan,
        contract,
        expected_task_id=task_id,
        expected_task_input_sha256=task_input_sha256,
        expected_prompt_sha256=prompt_sha256,
    ) == []
    analyzer = plan["task_analyzer"]
    assert analyzer["source"] == "frozen_replay"
    assert analyzer["usage"] == {}
    assert analyzer["replay"]["physical_request_count"] == 0
    assert (
        analyzer["replay"]["task_profile_pre_escalation_sha256"]
        == contract["entries"][task_id][
            "task_profile_pre_escalation_sha256"
        ]
    )


@pytest.mark.parametrize("cache_continuity_available", [False, True])
def test_frozen_replay_forwards_cache_continuity_without_manufacturing_intent(
    monkeypatch: pytest.MonkeyPatch,
    cache_continuity_available: bool,
) -> None:
    contract, _, request_context, task_id, input_sha, prompt_sha = _fixture()
    ranking_config = ranking_config_resolution()["effective_config"]
    observed: list[bool] = []
    original_normalize = ranking_router.normalize_task_profile

    def capture_continuity(*args: Any, **kwargs: Any):
        observed.append(kwargs.get("cache_continuity_available"))
        return original_normalize(*args, **kwargs)

    monkeypatch.setattr(
        ranking_router,
        "normalize_task_profile",
        capture_continuity,
    )
    result = frozen_task_analysis_result(
        contract,
        task_id=task_id,
        task_input_sha256=input_sha,
        prompt_sha256=prompt_sha,
        routed_tier="c1",
        request_context=request_context,
        ranking_config=ranking_config,
        cache_continuity_available=cache_continuity_available,
    )

    assert observed == [cache_continuity_available]
    assert result.profile["session_intent"]["type"] == "new_task"


def test_replay_accepts_selected_fallback_route_with_zero_usage() -> None:
    contract, _, request_context, task_id, input_sha, prompt_sha = _fixture()
    selected = contract["source_task_analyzer_config"]["fallback_chain"][0]
    entry = contract["entries"][task_id]
    entry["task_analyzer"]["provider"] = selected["provider"]
    entry["task_analyzer"]["model"] = selected["model"]
    contract["entries_sha256"] = canonical_json_sha256(contract["entries"])

    assert frozen_task_analysis_contract_reasons(contract) == []
    result = frozen_task_analysis_result(
        contract,
        task_id=task_id,
        task_input_sha256=input_sha,
        prompt_sha256=prompt_sha,
        routed_tier="c1",
        request_context=request_context,
        ranking_config=ranking_config_resolution()["effective_config"],
    )

    assert result.provider_id == selected["provider"]
    assert result.model_id == selected["model"]
    assert result.usage == {}
    assert result.replay["physical_request_count"] == 0


def test_v2_replay_preserves_deterministic_router_fallback_outcome() -> None:
    contract, plan, _, task_id, input_sha, prompt_sha = _fallback_fixture()

    assert frozen_task_analysis_contract_reasons(contract) == []
    validated = DracoFrozenTaskAnalysisExecutionV2Config.model_validate(contract)
    assert validated.model_dump(mode="json")["schema"] == FROZEN_TASK_ANALYSIS_SCHEMA_V2
    assert frozen_task_analysis_plan_reasons(
        plan,
        contract,
        expected_task_id=task_id,
        expected_task_input_sha256=input_sha,
        expected_prompt_sha256=prompt_sha,
    ) == []
    analyzer = plan["task_analyzer"]
    assert analyzer["source"] == "frozen_replay"
    assert analyzer["schema_valid"] is False
    assert analyzer["fallback_reason"] == "TimeoutError"
    assert analyzer["usage"] == {}
    assert analyzer["replay"]["physical_request_count"] == 0
    assert (
        analyzer["replay"]["origin_outcome"]
        == FROZEN_TASK_ANALYSIS_ROUTER_FALLBACK
    )


@pytest.mark.parametrize("selected_index", [1, 2], ids=["gpt", "gemini"])
def test_v3_replay_authorizes_live_chain_fallback_with_zero_requests(
    selected_index: int,
) -> None:
    contract, _, request_context, task_id, input_sha, prompt_sha = _fixture()
    source_config = copy.deepcopy(contract["source_task_analyzer_config"])
    source_config.pop("fallback_chain", None)
    source_config.pop("schema_repair_max_retries", None)
    source_config.pop("total_timeout_seconds", None)
    source_config.pop("payload_max_chars", None)
    source_config.pop("payload_max_bytes", None)
    source_config.pop("payload_max_estimated_tokens", None)
    routes = [
        {
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "upstream_provider": "deepseek",
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
    execution_contract = build_task_analyzer_execution_contract(
        routes=routes,
        schema_repair_max_retries=0,
        total_timeout_seconds=float(source_config["timeout_seconds"]) * len(routes),
        route_source="g1_registry_contract.live_task_analyzer_chain",
        source_payload={
            "task_analyzer": copy.deepcopy(source_config),
            "live_task_analyzer_chain": copy.deepcopy(routes),
        },
    )
    contract.update(
        {
            "schema": FROZEN_TASK_ANALYSIS_SCHEMA_V3,
            "source_task_analyzer_config": source_config,
            "source_task_analyzer_config_sha256": canonical_json_sha256(source_config),
            "source_task_analyzer_execution_contract": execution_contract,
            "source_task_analyzer_execution_contract_sha256": canonical_json_sha256(
                execution_contract
            ),
        }
    )
    selected = routes[selected_index]
    for entry in contract["entries"].values():
        entry["origin_outcome"] = "live_success"
        entry["task_analyzer"]["provider"] = selected["provider"]
        entry["task_analyzer"]["model"] = selected["model"]
    contract["entries_sha256"] = canonical_json_sha256(contract["entries"])
    historical_ranking = copy.deepcopy(ranking_config_resolution()["effective_config"])
    historical_ranking["task_analyzer"] = copy.deepcopy(source_config)

    assert frozen_task_analysis_contract_reasons(contract) == []
    validated = DracoFrozenTaskAnalysisExecutionV3Config.model_validate(contract)
    assert validated.model_dump(mode="json")["schema"] == FROZEN_TASK_ANALYSIS_SCHEMA_V3
    result = frozen_task_analysis_result(
        contract,
        task_id=task_id,
        task_input_sha256=input_sha,
        prompt_sha256=prompt_sha,
        routed_tier="c1",
        request_context=request_context,
        ranking_config=historical_ranking,
    )
    plan = {
        "task_analyzer": result.trace(historical_ranking),
        "task_profile_pre_escalation": copy.deepcopy(
            contract["entries"][task_id]["task_profile_pre_escalation"]
        ),
        "ranking_parameters": {"task_analyzer": copy.deepcopy(source_config)},
    }

    assert frozen_task_analysis_plan_reasons(
        plan,
        contract,
        expected_task_id=task_id,
        expected_task_input_sha256=input_sha,
        expected_prompt_sha256=prompt_sha,
    ) == []
    assert result.model_id == selected["model"]
    assert result.usage == {}
    assert result.replay["physical_request_count"] == 0
    assert (
        result.replay["source_task_analyzer_execution_contract_sha256"]
        == contract["source_task_analyzer_execution_contract_sha256"]
    )

    forged = copy.deepcopy(contract)
    forged_execution = forged["source_task_analyzer_execution_contract"]
    forged_execution["source_sha256"] = "0" * 64
    unsigned = dict(forged_execution)
    unsigned.pop("contract_sha256")
    forged_execution["contract_sha256"] = canonical_json_sha256(unsigned)
    forged["source_task_analyzer_execution_contract_sha256"] = canonical_json_sha256(
        forged_execution
    )
    assert "invalid_frozen_task_analyzer_execution_contract" in (
        frozen_task_analysis_contract_reasons(forged)
    )


def test_v2_replay_rejects_provenance_laundering_and_nondeterministic_profile() -> None:
    contract, _, request_context, task_id, input_sha, prompt_sha = _fallback_fixture()
    for field, value in (("schema_valid", True), ("fallback_reason", "")):
        tampered = copy.deepcopy(contract)
        tampered["entries"][task_id]["task_analyzer"][field] = value
        tampered["entries_sha256"] = canonical_json_sha256(tampered["entries"])
        assert "invalid_frozen_task_analyzer_trace" in (
            frozen_task_analysis_contract_reasons(tampered)
        )

    nondeterministic = copy.deepcopy(contract)
    profile = nondeterministic["entries"][task_id]["task_profile_pre_escalation"]
    profile["constraints"]["risk"] = (
        "high" if profile["constraints"]["risk"] != "high" else "low"
    )
    nondeterministic["entries"][task_id][
        "task_profile_pre_escalation_sha256"
    ] = canonical_json_sha256(profile)
    nondeterministic["entries_sha256"] = canonical_json_sha256(
        nondeterministic["entries"]
    )
    with pytest.raises(DynamicRankingError, match="not deterministic"):
        frozen_task_analysis_result(
            nondeterministic,
            task_id=task_id,
            task_input_sha256=input_sha,
            prompt_sha256=prompt_sha,
            routed_tier="c1",
            request_context=request_context,
            ranking_config=ranking_config_resolution()["effective_config"],
        )


def test_public_trace_secret_scan_allows_schema_but_rejects_real_key() -> None:
    _assert_public_ranking_trace_payload(
        {"schema": FROZEN_TASK_ANALYSIS_SCHEMA},
        label="frozen_task_analysis",
    )
    with pytest.raises(DynamicRankingError, match="secret-like value"):
        _assert_public_ranking_trace_payload(
            {"value": "sk-" + "a" * 32},
            label="frozen_task_analysis",
        )


@pytest.mark.parametrize(
    ("field", "replacement", "reason"),
    [
        (
            "expected_task_input_sha256",
            "sha256:" + "f" * 64,
            "wrong_frozen_task_analysis_task_input_sha256",
        ),
        (
            "expected_prompt_sha256",
            "f" * 64,
            "wrong_frozen_task_analysis_prompt_sha256",
        ),
    ],
)
def test_plan_rejects_row_binding_tampering(
    field: str,
    replacement: str,
    reason: str,
) -> None:
    contract, plan, _, task_id, task_input_sha256, prompt_sha256 = _fixture()
    expected = {
        "expected_task_id": task_id,
        "expected_task_input_sha256": task_input_sha256,
        "expected_prompt_sha256": prompt_sha256,
    }
    expected[field] = replacement

    assert reason in frozen_task_analysis_plan_reasons(
        plan,
        contract,
        **expected,
    )


def test_contract_and_plan_reject_profile_or_identity_tampering() -> None:
    contract, plan, _, task_id, task_input_sha256, prompt_sha256 = _fixture()
    tampered_contract = copy.deepcopy(contract)
    tampered_contract["entries"][task_id]["task_profile_pre_escalation"][
        "constraints"
    ]["risk"] = "high"
    assert "invalid_frozen_task_profile_hash" in (
        frozen_task_analysis_contract_reasons(tampered_contract)
    )

    tampered_identity = copy.deepcopy(contract)
    tampered_identity["entries"][task_id]["task_analyzer"]["model"] = (
        "test-vendor/not-the-frozen-analyzer"
    )
    tampered_identity["entries_sha256"] = canonical_json_sha256(
        tampered_identity["entries"]
    )
    assert "wrong_frozen_task_analyzer_identity" in (
        frozen_task_analysis_contract_reasons(tampered_identity)
    )

    duplicated_route = copy.deepcopy(contract)
    duplicated_route["source_task_analyzer_config"]["fallback_chain"].append(
        {
            "provider": duplicated_route["source_task_analyzer_config"]["provider"],
            "model": duplicated_route["source_task_analyzer_config"]["model"],
            "upstream_provider": duplicated_route["source_task_analyzer_config"][
                "upstream_provider"
            ],
        }
    )
    duplicated_route["source_task_analyzer_config_sha256"] = canonical_json_sha256(
        duplicated_route["source_task_analyzer_config"]
    )
    assert "invalid_frozen_task_analysis_source_analyzer_identity" in (
        frozen_task_analysis_contract_reasons(duplicated_route)
    )

    tampered_plan = copy.deepcopy(plan)
    tampered_plan["task_profile_pre_escalation"]["constraints"]["risk"] = "high"
    assert "wrong_frozen_task_profile" in frozen_task_analysis_plan_reasons(
        tampered_plan,
        contract,
        expected_task_id=task_id,
        expected_task_input_sha256=task_input_sha256,
        expected_prompt_sha256=prompt_sha256,
    )


def test_finalizer_requires_zero_replay_analyzer_requests_but_keeps_live_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_finalizer()
    contract, plan, _, task_id, task_input_sha256, prompt_sha256 = _fixture()
    row = {
        "group": "G1",
        "task_id": task_id,
        "task_input_sha256": task_input_sha256,
        "prompt_sha256": prompt_sha256,
        "execution": {
            "generation_attempts": [
                {
                    "run": {
                        "llm_request_count": 1,
                        "routing_trace": {"selection_plan": plan},
                    }
                }
            ]
        },
    }

    monkeypatch.setattr(
        module,
        "_canonical_task_analyzer_setup_units",
        lambda *args, **kwargs: [],
    )
    assert module.g1_provider_lifecycle_analyzer_reasons(
        row,
        replay_contract=contract,
    ) == []
    assert "missing_g1_task_analyzer_request" in (
        module.g1_provider_lifecycle_analyzer_reasons(
            row,
            replay_contract=None,
        )
    )

    monkeypatch.setattr(
        module,
        "_canonical_task_analyzer_setup_units",
        lambda *args, **kwargs: [{"role": "setup", "label": "task_analyzer"}],
    )
    assert "unexpected_g1_task_analyzer_request_in_frozen_replay" in (
        module.g1_provider_lifecycle_analyzer_reasons(
            row,
            replay_contract=contract,
        )
    )


def test_finalizer_authenticates_v2_fallback_trace_without_laundering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_finalizer()
    contract, plan, _, task_id, input_sha, prompt_sha = _fallback_fixture()
    row = {
        "group": "G1",
        "task_id": task_id,
        "task_input_sha256": input_sha,
        "prompt_sha256": prompt_sha,
        "execution": {
            "generation_attempts": [
                {
                    "attempt_id": "attempt-1",
                    "run": {
                        "llm_request_count": 1,
                        "routing_trace": {"selection_plan": plan},
                    },
                }
            ]
        },
    }
    monkeypatch.setattr(
        module,
        "_canonical_task_analyzer_setup_units",
        lambda *args, **kwargs: [],
    )
    assert module.g1_provider_lifecycle_analyzer_reasons(
        row,
        replay_contract=contract,
    ) == []

    laundered = copy.deepcopy(row)
    laundered["execution"]["generation_attempts"][0]["run"]["routing_trace"][
        "selection_plan"
    ]["task_analyzer"]["schema_valid"] = True
    assert "wrong_frozen_task_analyzer_provenance" in (
        module.g1_provider_lifecycle_analyzer_reasons(
            laundered,
            replay_contract=contract,
        )
    )


def test_main_and_resume_bind_raw_task_hashes_in_live_and_dry_replay() -> None:
    for runner in RUNNERS:
        tree = ast.parse(runner.read_text(encoding="utf-8"))
        build = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "build_experiment_provider"
        )
        argument_names = {argument.arg for argument in build.args.kwonlyargs}
        assert {
            "task_id",
            "task_input_sha256",
            "prompt_sha256",
        } <= argument_names

        replay_calls = [
            node
            for node in ast.walk(build)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "frozen_task_analysis_result"
        ]
        assert len(replay_calls) == 2
        for call in replay_calls:
            assert {
                keyword.arg for keyword in call.keywords
            } >= {"task_id", "task_input_sha256", "prompt_sha256"}

        run_one = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_one"
        )
        build_call = next(
            node
            for node in ast.walk(run_one)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_experiment_provider"
        )
        keywords = {keyword.arg: keyword.value for keyword in build_call.keywords}
        assert ast.unparse(keywords["task_input_sha256"]) == (
            "canonical_json_sha256(task)"
        )
        assert ast.unparse(keywords["prompt_sha256"]) == (
            "text_sha256(str(task['prompt']))"
        )

        zero_usage_assignments = [
            node
            for node in ast.walk(build)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "analyzer_usage_rows"
                for target in node.targets
            )
            and isinstance(node.value, ast.List)
            and not node.value.elts
        ]
        assert zero_usage_assignments
