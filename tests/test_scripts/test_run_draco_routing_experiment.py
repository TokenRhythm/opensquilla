from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import inspect
import json
import random
import sys
from collections.abc import Iterator, Mapping
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.engine.types import DoneEvent as AgentDoneEvent
from opensquilla.engine.types import ThinkingLevel
from opensquilla.eval import draco_artifact_index
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider import ensemble as ensemble_provider
from opensquilla.provider.ensemble import (
    _member_chat_config,
    build_ensemble_provider_from_config,
)
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    ProviderBillingReceipt,
)
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.sandbox.run_context import PublicNetworkGrant, RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.tool_boundary import ToolCall
from opensquilla.tools.types import current_tool_context

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_draco_routing_experiment.py"
RESUME_SCRIPT_PATH = SCRIPT_PATH.with_name("run_draco_routing_experiment_resume.py")
FINALIZER_SCRIPT_PATH = SCRIPT_PATH.parent / "experiments" / "finalize_draco_campaign.py"
ROOT = SCRIPT_PATH.parent.parent


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_draco_routing_experiment_under_test",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _frozen_router_fallback_replay_payload(
    task_ids: list[str],
) -> dict[str, object]:
    from opensquilla.provider.ranking_router import (
        TASK_ANALYZER_VERSION,
        ranking_config_resolution,
    )

    profile = {"task_type": "analysis"}
    source_config = dict(
        ranking_config_resolution()["effective_config"]["task_analyzer"]
    )
    entries = {
        task_id: {
            "task_input_sha256": f"sha256:{'a' * 64}",
            "prompt_sha256": "b" * 64,
            "task_profile_pre_escalation": profile,
            "task_profile_pre_escalation_sha256": _canonical_digest(profile),
            "origin_outcome": "deterministic_router_fallback",
            "task_analyzer": {
                "source": "frozen_replay",
                "schema_valid": False,
                "confidence": 0.0,
                "analyzer_version": TASK_ANALYZER_VERSION,
                "provider": source_config["provider"],
                "model": source_config["model"],
                "fallback_reason": "task_analysis_not_supplied",
                "usage": {},
                "normalization_warnings": [],
            },
        }
        for task_id in task_ids
    }
    return {
        "schema": "opensquilla.draco.frozen-task-analysis/v2",
        "mode": "frozen_replay",
        "source_experiment": "legacy-e0",
        "source_manifest_sha256": "c" * 64,
        "source_results_sha256": "d" * 64,
        "source_task_analyzer_config": source_config,
        "source_task_analyzer_config_sha256": _canonical_digest(source_config),
        "entries": entries,
        "entries_sha256": _canonical_digest(entries),
    }


def _experiment_with_inactive_live_pair_and_frozen_replay(
    module,
):
    experiment = _experiment_with_current_g1_contract(
        module,
        thinking_assignment_enabled=True,
    )
    payload = experiment.model_dump(mode="json")
    payload["g1_routing"]["task_analysis_execution"] = (
        _frozen_router_fallback_replay_payload(
            payload["benchmark_input"]["task_ids"]
        )
    )
    return type(experiment).model_validate(payload)


def _test_proposer_execution(
    identity: str,
    *,
    level: str = "high",
    fallback_attempts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    provider, _, model = identity.partition(":")
    return {
        "role": "proposer",
        "requested_provider": provider,
        "provider": provider,
        "requested_model": model,
        "model": model,
        "assigned_thinking_level": level,
        "effective_thinking_level": level,
        "provider_thinking_level": level,
        "thinking_override": level,
        "effective_thinking": True,
        "effective_provider_thinking_level": level,
        "thinking_policy_managed": True,
        "thinking_fallback_attempts": deepcopy(fallback_attempts or []),
    }


def _with_g1_task_analysis_invariants(
    plan: dict[str, object],
) -> dict[str, object]:
    task_profile = {"task_type": "analysis", "tier_dist": {"2": 1.0}}
    request_context: dict[str, object] = {
        "schema_version": "test-request-context/v1",
        "task_text": "test prompt",
    }
    request_context["snapshot_hash"] = _canonical_digest(request_context)
    selected_p = [str(identity) for identity in plan.get("selected_P", [])]
    selected_a = str(plan.get("selected_A") or "openrouter:model-a")
    thinking_details = {
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
                "provider_rejection_fallbacks": [],
            }
            for identity in selected_p
        ],
        "aggregator": {
            "identity": selected_a,
            "model_id": selected_a.partition(":")[2],
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
                "identity": selected_a,
                "model_id": selected_a.partition(":")[2],
                "role": "aggregator",
                "requested_level": "high",
                "effective_level": "high",
                "provider_level": "high",
                "fallback_reason": "",
                "reasons": [],
                "provider_rejection_fallbacks": [],
            }
        ],
    }
    return {
        **plan,
        "selected_A": selected_a,
        "aggregator_candidates": [selected_a],
        "ranking_thinking_assignment_enabled": True,
        "executed_thinking_assignment": {
            "proposers": dict.fromkeys(selected_p, "high"),
            "aggregator": "high",
            "thinking_policy_version": "test-thinking-policy/v1",
        },
        "thinking_assignment_details": thinking_details,
        "task_analyzer": {
            "source": "llm_provider",
            "schema_valid": True,
            "confidence": 0.9,
            "analyzer_version": "test-v1",
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "fallback_reason": "",
            "usage": _task_analyzer_usage([_task_analyzer_unit()]),
            "normalization_warnings": [],
        },
        "task_profile": task_profile,
        "task_profile_hash": _canonical_digest(task_profile),
        "request_context": request_context,
        "request_context_hash": request_context["snapshot_hash"],
        "routed_tier": "c2",
        "routing_confidence": 0.9,
        "user_profile_enabled": False,
        "user_profile_version": "",
        "user_profile_source": "",
    }


def _bind_g1_retry_plan(
    initial_plan: dict[str, object],
    retry_plan: dict[str, object],
    *,
    exclusions: list[str],
) -> dict[str, object]:
    from opensquilla.provider.ranking_router import (
        ROUTER_DYNAMIC_RETRY_ROUTING_SCHEMA,
        build_router_dynamic_task_analysis_reuse_binding,
    )

    retry_plan.update(
        {
            field: deepcopy(initial_plan[field])
            for field in (
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
            )
        }
    )
    if initial_plan.get("ranking_thinking_assignment_enabled") is True:
        selected_p = [str(identity) for identity in retry_plan.get("selected_P", [])]
        selected_a = str(
            retry_plan.get("selected_A") or initial_plan.get("selected_A") or "openrouter:model-a"
        )
        initial_details = initial_plan.get("thinking_assignment_details")
        initial_details = initial_details if isinstance(initial_details, dict) else {}
        initial_proposer_details = {
            str(detail.get("identity") or ""): deepcopy(detail)
            for detail in initial_details.get("proposers", [])
            if isinstance(detail, dict) and str(detail.get("identity") or "")
        }
        initial_aggregator_details = {
            str(detail.get("identity") or ""): deepcopy(detail)
            for detail in initial_details.get("aggregator_candidates", [])
            if isinstance(detail, dict) and str(detail.get("identity") or "")
        }
        initial_primary_aggregator = initial_details.get("aggregator")
        if isinstance(initial_primary_aggregator, dict) and str(
            initial_primary_aggregator.get("identity") or ""
        ):
            initial_aggregator_details.setdefault(
                str(initial_primary_aggregator["identity"]),
                deepcopy(initial_primary_aggregator),
            )

        def retry_detail(identity: str, *, role: str) -> dict[str, object]:
            inherited = (
                initial_proposer_details.get(identity)
                if role == "proposer"
                else initial_aggregator_details.get(identity)
            )
            if inherited is not None:
                return deepcopy(inherited)
            return {
                "identity": identity,
                "model_id": identity.partition(":")[2],
                "role": role,
                "requested_level": "high",
                "effective_level": "high",
                "provider_level": "high",
                "fallback_reason": "",
                "reasons": [],
                "provider_rejection_fallbacks": [],
            }

        proposer_details = [retry_detail(identity, role="proposer") for identity in selected_p]
        aggregator_detail = retry_detail(selected_a, role="aggregator")
        initial_assignment = initial_plan.get("executed_thinking_assignment")
        policy_version = (
            str(initial_assignment.get("thinking_policy_version") or "")
            if isinstance(initial_assignment, dict)
            else ""
        ) or "test-thinking-policy/v1"
        retry_plan.update(
            {
                "selected_A": selected_a,
                "aggregator_candidates": [selected_a],
                "ranking_thinking_assignment_enabled": True,
                "executed_thinking_assignment": {
                    "proposers": {
                        str(detail["identity"]): str(detail["effective_level"])
                        for detail in proposer_details
                    },
                    "aggregator": str(aggregator_detail["effective_level"]),
                    "thinking_policy_version": policy_version,
                },
                "thinking_execution_fallbacks": [],
                "thinking_assignment_details": {
                    "proposers": proposer_details,
                    "aggregator": aggregator_detail,
                    "aggregator_candidates": [deepcopy(aggregator_detail)],
                },
            }
        )
    binding = build_router_dynamic_task_analysis_reuse_binding(initial_plan)
    parent_decision_id = str(initial_plan["decision_id"])
    retry_plan.update(
        {
            "retry_parent_decision_id": parent_decision_id,
            "retry_excluded_proposer_identities": exclusions,
            "task_analysis_reused": True,
            "task_analysis_reuse": binding,
            "retry_routing": {
                "schema": ROUTER_DYNAMIC_RETRY_ROUTING_SCHEMA,
                "reason": "prior_attempt_reasoning_only_length",
                "parent_decision_id": parent_decision_id,
                "excluded_proposer_identities": exclusions,
                "task_analysis_reused": True,
                "task_analysis_source_decision_id": parent_decision_id,
                "task_analysis_reuse_sha256": binding["projection_sha256"],
            },
        }
    )
    return retry_plan


def _openrouter_exact_evidence(
    cost: float,
    response_id: str,
    *,
    requested_model: str = "deepseek/deepseek-v4-pro",
    serving_provider: str = "DeepSeek",
    serving_model: str = "deepseek/deepseek-v4-pro-20260423",
) -> dict[str, object]:
    return {
        "is_byok": False,
        "provider_reported_cost": cost,
        "response_ids": [response_id],
        "router_metadata": {
            "requested": requested_model,
            "is_byok": False,
            "endpoints": {
                "available": [
                    {
                        "provider": serving_provider,
                        "model": serving_model,
                        "selected": True,
                    }
                ]
            },
            "attempts": [
                {
                    "provider": serving_provider,
                    "model": serving_model,
                    "status": 200,
                }
            ],
        },
    }


def _complete_legacy_judge(
    response_id: str,
    *,
    score: float = 4.0,
) -> dict[str, object]:
    cost = 0.002
    return {
        "mode": "legacy_dimension_score",
        "judge_model": "judge-model",
        "scores": {
            "accuracy": score,
            "completeness": score,
            "objectivity": score,
            "citation": score,
        },
        "score_status": "complete",
        "judge_error_count": 0,
        "total": score / 5.0 * 100.0,
        "judge_attempt_count": 1,
        "judge_new_attempt_count": 1,
        "judge_attempts": [
            {
                "attempt": 1,
                "run": {
                    "llm_request_count": 1,
                    "usage": {
                        "provider": "openrouter",
                        "model": "judge-model",
                        "input_tokens": 5,
                        "output_tokens": 2,
                        "billed_cost": cost,
                        "cost_source": "provider_billed",
                        "provider_usage": _openrouter_exact_evidence(
                            cost,
                            response_id,
                        ),
                    },
                },
            }
        ],
    }


def _valid_ensemble_trace(
    *,
    selection_mode: str,
    llm_request_count: int,
    total_candidates: int = 4,
    successful_proposers: int = 3,
    final_text: str = "answer",
) -> dict[str, object]:
    candidates = []
    for index in range(total_candidates):
        ok = index < successful_proposers
        text = f"candidate-{index}" if ok else ""
        candidates.append(
            {
                "index": index,
                "sample_index": 0,
                "provider": "openrouter" if ok else "",
                "requested_provider": "openrouter",
                "model": f"model-{index}" if ok else "",
                "requested_model": f"model-{index}",
                "ok": ok,
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": ok,
                "stop_reason": "stop" if ok else "",
                "content": {
                    "text": text,
                    "chars": len(text),
                    "truncated": False,
                },
                **(
                    {}
                    if ok
                    else {
                        "error": "candidate failed",
                        "error_code": "test_failure",
                    }
                ),
            }
        )
    return {
        "llm_request_count": llm_request_count,
        "selection_strategy": selection_mode,
        "fallback_used": False,
        "final_request_role": "aggregator",
        "total_candidates": total_candidates,
        "successful_proposers": successful_proposers,
        "candidates": candidates,
        "final_request": {
            "role": "aggregator",
            "request_started": True,
            "usage": {
                "provider": "openrouter",
                "model": "aggregator",
                "requested_provider": "openrouter",
                "requested_model": "aggregator",
                "stop_reason": "stop",
            },
            "output": {
                "text": final_text,
                "chars": len(final_text),
                "truncated": False,
            },
        },
    }


def _k21_router_dynamic_plan() -> dict[str, object]:
    return {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "profile": "router_dynamic/c2",
        "proposer_models": ["p0", "p0", "p1"],
        "proposer_count": 2,
        "proposer_sample_count": 3,
        "selected_P": ["openrouter:p0", "openrouter:p1"],
        "backup_P": ["openrouter:b0", "openrouter:b1"],
        "aggregator_model": "agg",
        "selected_A": "openrouter:agg",
        "aggregator_candidates": ["openrouter:agg"],
        "effective_min_successful_proposers": 2,
        "proposer_recovery_policy": deepcopy(runner.FORMAL_PROPOSER_RECOVERY_POLICY),
    }


def _k21_ensemble_trace(
    plan: dict[str, object],
    *,
    include_requested_identity: bool = True,
) -> dict[str, object]:
    trace = _valid_ensemble_trace(
        selection_mode="router_dynamic",
        llm_request_count=4,
        total_candidates=3,
        successful_proposers=3,
    )
    expanded = ("openrouter:p0", "openrouter:p0", "openrouter:p1")
    for index, (candidate, identity) in enumerate(zip(trace["candidates"], expanded, strict=True)):
        assert isinstance(candidate, dict)
        provider, model = identity.split(":", 1)
        candidate["sample_index"] = 1 if index == 1 else 0
        candidate["provider"] = provider
        candidate["model"] = model
        if include_requested_identity:
            candidate["requested_provider"] = provider
            candidate["requested_model"] = model
        else:
            candidate["requested_provider"] = ""
            candidate["requested_model"] = ""
    trace["selection_plan"] = deepcopy(plan)
    trace["agent_call_index"] = 1
    trace["request_outcome"] = "llm_response"
    final_request = trace["final_request"]
    assert isinstance(final_request, dict)
    usage = final_request["usage"]
    assert isinstance(usage, dict)
    usage.update(
        {
            "provider": "openrouter",
            "model": "agg",
            "requested_provider": "openrouter",
            "requested_model": "agg",
        }
    )
    final_request["execution"] = {
        "provider": "openrouter",
        "actual_provider": "openrouter",
        "model": "agg",
        "actual_model": "agg",
        "requested_provider": "openrouter",
        "requested_model": "agg",
    }
    return trace


def _load_resume_runner():
    spec = importlib.util.spec_from_file_location(
        "run_draco_routing_experiment_resume_under_test",
        RESUME_SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_finalizer():
    spec = importlib.util.spec_from_file_location(
        "finalize_draco_campaign_for_runner_test",
        FINALIZER_SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


resume_runner = _load_resume_runner()


@pytest.fixture
def configured_tool_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exercise web-tool behavior through a granted Standard sandbox context."""

    configure_runtime(
        SandboxSettings(),
        approval_queue=runner._BenchmarkApprovalQueue(),
        workspace=tmp_path,
    )
    original_builder = runner.build_benchmark_tool_context

    def _standard_context(**kwargs):
        context = original_builder(**kwargs)
        context.run_mode = RunMode.STANDARD.value
        context.sandbox_run_context = RunContext(
            run_mode=RunMode.STANDARD,
            workspace=str(tmp_path),
            public_network=(PublicNetworkGrant(scope="chat", source="test"),),
            source="test_standard_network_grant",
        )
        return context

    monkeypatch.setattr(runner, "build_benchmark_tool_context", _standard_context)
    try:
        yield
    finally:
        reset_runtime()


@pytest.mark.asyncio
async def test_judge_concurrency_is_capped_across_concurrent_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    peak = 0

    async def fake_judge_criterion(*, criterion, repeat_index, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {
            **criterion,
            "repeat_index": repeat_index,
            "verdict": "MET",
            "met": True,
            "rationale": "ok",
        }

    monkeypatch.setattr(runner, "judge_criterion", fake_judge_criterion)
    task = {
        "id": "task-1",
        "prompt": "prompt",
        "rubric": {
            "id": "rubric-1",
            "sections": [
                {
                    "id": "section-1",
                    "title": "Section",
                    "criteria": [
                        {"id": f"criterion-{index}", "weight": 1, "requirement": "x"}
                        for index in range(3)
                    ],
                }
            ],
        },
    }
    shared = asyncio.Semaphore(2)
    await asyncio.gather(
        *[
            runner.judge_text(
                judge_provider=object(),
                task=task,
                answer="answer",
                dry_run=False,
                judge_repeats=1,
                judge_concurrency=6,
                judge_semaphore=shared,
            )
            for _ in range(2)
        ]
    )

    assert peak == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module",
    [runner, _load_resume_runner()],
    ids=["main", "resume"],
)
async def test_legacy_judge_uses_the_experiment_wide_semaphore(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    peak = 0

    async def fake_collect_run(*_args, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return module.RunResult(
            final_text=json.dumps(
                {
                    "scores": {
                        "accuracy": 5,
                        "completeness": 5,
                        "objectivity": 5,
                        "citation": 5,
                    },
                    "total": 20,
                    "rationale": "ok",
                }
            ),
            done=None,
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    shared = asyncio.Semaphore(2)
    task = {"id": "task-1", "prompt": "prompt", "rubric": "legacy rubric"}
    await asyncio.gather(
        *[
            module.judge_text(
                judge_provider=object(),
                task=task,
                answer="answer",
                dry_run=False,
                judge_concurrency=8,
                judge_semaphore=shared,
            )
            for _ in range(5)
        ]
    )

    assert peak == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module",
    [runner, resume_runner],
    ids=["main", "resume"],
)
async def test_judge_http_failure_then_success_persists_route_bound_unknown_usage(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    judge_model = "google/gemini-3.1-pro-preview"
    provider = type(
        "JudgeProvider",
        (),
        {
            "provider_id": "openrouter",
            "model": judge_model,
        },
    )()
    calls = 0

    async def fake_collect_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return module.RunResult(
                final_text=json.dumps(
                    {
                        "verdict": "MET",
                        "rationale": "partial response before terminal error",
                    }
                ),
                done=None,
                error=(
                    "OpenRouter chat request failed (HTTP 503): "
                    "Cloudflare Worker exceeded resource limits"
                ),
                trace_events=[
                    {
                        "kind": "error",
                        "code": "503",
                        "request_started": True,
                        "physical_request_count": 1,
                    }
                ],
            )
        return module.RunResult(
            final_text=json.dumps(
                {
                    "verdict": "MET",
                    "rationale": "ok",
                }
            ),
            done=DoneEvent(
                stop_reason="stop",
                provider="openrouter",
                model=judge_model,
                requested_provider="openrouter",
                requested_model=judge_model,
            ),
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    judgment = await module.judge_criterion(
        judge_provider=provider,
        task={"id": "task-1", "prompt": "prompt"},
        answer="answer",
        criterion={
            "id": "criterion-1",
            "weight": 1,
            "requirement": "x",
        },
        max_attempts=2,
    )

    assert calls == 2
    assert judgment["met"] is True
    assert judgment["judge_attempt_count"] == 2
    failed_run = judgment["judge_attempts"][0]["run"]
    assert failed_run["llm_request_count"] == 1
    assert failed_run["usage_unknown_count"] == 1
    units = failed_run["usage"]["model_usage_breakdown"]
    assert len(units) == 1
    assert units[0]["role"] == "unknown_request"
    assert units[0]["requested_provider"] == "openrouter"
    assert units[0]["requested_model"] == judge_model
    assert units[0]["usage_unknown"] is True
    assert sum(attempt["run"]["llm_request_count"] for attempt in judgment["judge_attempts"]) == 2
    assert (
        sum(
            len(
                attempt["run"]["usage"].get(
                    "model_usage_breakdown",
                    [],
                )
            )
            for attempt in judgment["judge_attempts"]
        )
        == 2
    )

    finalizer = _load_finalizer()
    _, route_reasons = finalizer.canonical_judge_run_route_reasons(
        failed_run,
        attempt_id=judgment["judge_attempts"][0]["attempt_id"],
    )
    assert route_reasons == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module",
    [runner, resume_runner],
    ids=["main", "resume"],
)
async def test_legacy_judge_does_not_accept_valid_json_from_failed_request(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    judge_model = "google/gemini-3.1-pro-preview"
    provider = type(
        "JudgeProvider",
        (),
        {
            "provider_id": "openrouter",
            "model": judge_model,
        },
    )()
    calls = 0
    successful_payload = {
        "scores": {
            "accuracy": 5,
            "completeness": 5,
            "objectivity": 5,
            "citation": 5,
        },
        "total": 20,
        "rationale": "ok",
    }

    async def fake_collect_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return module.RunResult(
                final_text=json.dumps(successful_payload),
                done=None,
                error="OpenRouter chat request failed (HTTP 503)",
                trace_events=[
                    {
                        "kind": "error",
                        "code": "503",
                        "request_started": True,
                        "physical_request_count": 1,
                    }
                ],
            )
        return module.RunResult(
            final_text=json.dumps(successful_payload),
            done=DoneEvent(
                stop_reason="stop",
                provider="openrouter",
                model=judge_model,
                requested_provider="openrouter",
                requested_model=judge_model,
            ),
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    judged = await module.judge_text(
        judge_provider=provider,
        task={
            "id": "task-1",
            "prompt": "prompt",
            "rubric": "legacy rubric",
        },
        answer="answer",
        dry_run=False,
        judge_max_attempts=2,
    )

    assert judged is not None
    assert calls == 2
    assert judged["score_status"] == "complete"
    assert judged["judge_attempt_count"] == 2
    assert judged["judge_attempts"][0]["schema_valid"] is True
    failed_run = judged["judge_attempts"][0]["run"]
    assert failed_run["error"] == "OpenRouter chat request failed (HTTP 503)"
    failed_unit = failed_run["usage"]["model_usage_breakdown"][0]
    assert failed_unit["role"] == "unknown_request"
    assert failed_unit["requested_provider"] == "openrouter"
    assert failed_unit["requested_model"] == judge_model

    finalizer = _load_finalizer()
    _, route_reasons = finalizer.canonical_judge_run_route_reasons(
        failed_run,
        attempt_id=judged["judge_attempts"][0]["attempt_id"],
    )
    assert route_reasons == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module",
    [runner, resume_runner],
    ids=["main", "resume"],
)
async def test_judge_attempt_budget_is_cumulative_per_unit_across_waves(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    physical_calls = 0

    async def fake_collect_run(*_args, **_kwargs):
        nonlocal physical_calls
        physical_calls += 1
        return module.RunResult(
            final_text="not-json",
            done=None,
            trace_events=[
                {
                    "kind": "error",
                    "code": "provider_stream_close_failed",
                }
            ],
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    provider = type("JudgeProvider", (), {"model": "judge-model"})()
    task = {
        "id": "task-1",
        "prompt": "prompt",
        "rubric": {
            "id": "rubric-1",
            "sections": [
                {
                    "id": "section-1",
                    "title": "Section",
                    "criteria": [
                        {
                            "id": "criterion-1",
                            "weight": 1,
                            "requirement": "x",
                        }
                    ],
                }
            ],
        },
    }
    prior = None
    for expected_wave in range(1, 4):
        prior = await module.judge_text(
            judge_provider=provider,
            task=task,
            answer="answer",
            dry_run=False,
            judge_repeats=2,
            judge_concurrency=2,
            judge_max_attempts=3,
            prior_judge=prior,
        )
        assert prior is not None
        assert prior["judge_new_attempt_count"] == 2
        assert physical_calls == expected_wave * 2
        for judgment in prior["criterion_judgments"]:
            assert judgment["judge_attempt_count"] == expected_wave
            assert judgment["prior_judge_attempts_used"] == expected_wave - 1
            assert [attempt["attempt"] for attempt in judgment["judge_attempts"]] == (
                list(range(1, expected_wave + 1))
            )

    attempt_ids = [
        attempt["attempt_id"]
        for judgment in prior["criterion_judgments"]
        for attempt in judgment["judge_attempts"]
    ]
    assert len(attempt_ids) == 6
    assert len(set(attempt_ids)) == 6
    assert prior["judge_attempt_budget_exhausted"] is True
    assert prior["judge_attempt_budget_exhausted_count"] == 2

    closed = await module.judge_text(
        judge_provider=provider,
        task=task,
        answer="answer",
        dry_run=False,
        judge_repeats=2,
        judge_concurrency=2,
        judge_max_attempts=3,
        prior_judge=prior,
    )

    assert closed is not None
    assert physical_calls == 6
    assert closed["judge_new_attempt_count"] == 0
    assert closed["judge_attempt_budget_exhausted"] is True
    assert all(
        judgment["error"] == module.JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR
        and judgment["judge_attempt_count"] == 3
        and judgment["judge_attempt_budget_remaining"] == 0
        for judgment in closed["criterion_judgments"]
    )


def _experiment_config():
    return runner.load_draco_experiment_config(runner.DEFAULT_B2_EXPERIMENT_CONFIG_PATH).config


def test_analyzer_chain_and_fixed_fallback_models_have_default_provider_pins() -> None:
    from opensquilla.gateway.llm_runtime import OPENROUTER_DEFAULT_PROVIDER_ROUTING

    assert OPENROUTER_DEFAULT_PROVIDER_ROUTING["openai/gpt-5.6-sol"] == "azure"
    assert OPENROUTER_DEFAULT_PROVIDER_ROUTING["qwen/qwen3.8-max"] == "alibaba"


def _experiment_with_current_g1_contract(
    module,
    *,
    thinking_assignment_enabled: bool,
):
    from opensquilla.provider.ranking_router import (
        _legacy_registry_snapshot_projection,
        load_model_registry_snapshot,
        ranking_config_snapshot,
    )

    experiment = _experiment_config()
    registry = load_model_registry_snapshot()
    if not thinking_assignment_enabled:
        registry = _legacy_registry_snapshot_projection(registry)
    ranking = ranking_config_snapshot(
        thinking_assignment_enabled=thinking_assignment_enabled,
    )
    proposer_count = ranking["proposer_count"]
    payload = experiment.model_dump(mode="json")
    payload["g1_routing"].update(
        {
            "source_registry_snapshot_version": registry["snapshot_version"],
            "expected_source_registry_snapshot_sha256": (
                module.canonical_json_sha256(registry).removeprefix("sha256:")
            ),
            "expected_ranking_config_schema_version": ranking["schema_version"],
            "expected_ranking_config_version": ranking["config_version"],
            "expected_ranking_config_sha256": (
                module.canonical_json_sha256(ranking).removeprefix("sha256:")
            ),
            "expected_proposer_count_max": max(
                *(int(row["max"]) for row in proposer_count["by_tier"].values()),
                int(proposer_count["high_risk"]["max"]),
            ),
        }
    )
    return type(experiment).model_validate(payload)


def _resolved_g1_registry_contract(module, experiment, config: GatewayConfig) -> dict[str, object]:
    from opensquilla.provider.ranking_router import (
        ranking_config_resolution,
        task_analyzer_policy,
    )

    assert experiment.g1_routing is not None
    base_version = experiment.g1_routing.expected_ranking_config_version
    resolution = ranking_config_resolution(
        override=(experiment.router_dynamic_ranking_override or None),
        base_version=base_version,
    )
    config.llm_ensemble.freeze_ranking_config(base_version=base_version)
    analyzer_policy = task_analyzer_policy(resolution["effective_config"])
    config.llm.provider_routing[str(analyzer_policy["model"])] = str(
        analyzer_policy["upstream_provider"]
    )
    contract = module.validate_g1_registry_contract(experiment, config)
    assert contract["candidate_scope"] == "registry_all"
    assert contract["policy"] == "all_registry_models"
    return contract


def _g1_config_with_task_analyzer_override(module, *, live_chain: bool):
    experiment = _experiment_with_current_g1_contract(
        module,
        thinking_assignment_enabled=True,
    )
    payload = experiment.model_dump(mode="json")
    if not live_chain:
        payload["g1_routing"].pop("live_task_analyzer_chain")
        payload["g1_routing"].pop("analyzer_failure_fallback_ensemble")
    payload["router_dynamic_ranking_override"] = {
        "task_analyzer": {
            "model": "openai/gpt-5.6-sol",
            "upstream_provider": "azure",
        }
    }
    experiment = type(experiment).model_validate(payload)
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "ranking_thinking_assignment_enabled": True,
            "ranking_config_override": (
                experiment.router_dynamic_ranking_override
            ),
        },
    )
    return experiment, config


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_live_analyzer_chain_primary_cannot_drift_via_ranking_override(
    module,
) -> None:
    experiment, config = _g1_config_with_task_analyzer_override(
        module,
        live_chain=True,
    )

    with pytest.raises(
        ValueError,
        match="live task analyzer chain primary differs from effective ranking config",
    ):
        _resolved_g1_registry_contract(module, experiment, config)


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_legacy_single_analyzer_still_accepts_ranking_override(module) -> None:
    experiment, config = _g1_config_with_task_analyzer_override(
        module,
        live_chain=False,
    )

    contract = _resolved_g1_registry_contract(module, experiment, config)

    assert contract["task_analyzer"]["model"] == "openai/gpt-5.6-sol"
    assert "live_task_analyzer_chain" not in contract


def _experiment_with_exact_g1_routes(module):
    experiment = module.load_draco_experiment_config(
        module.DEFAULT_B2_EXPERIMENT_CONFIG_PATH
    ).config
    assert experiment.g1_routing is not None
    routes = {
        "deepseek/deepseek-v4-pro": "deepseek",
        "moonshotai/kimi-k2.7-code": "moonshotai",
        "qwen/qwen3.7-max": "alibaba",
        "x-ai/grok-4.5": "xai",
        "z-ai/glm-5.2": "z-ai",
    }
    payload = experiment.model_dump(mode="json")
    g1_payload = experiment.g1_routing.model_dump(mode="json", exclude_none=True)
    g1_payload.update(
        {
            "expected_candidate_count": len(routes),
            "expected_routes": routes,
            "expected_routes_sha256": module.canonical_json_sha256(routes).removeprefix("sha256:"),
        }
    )
    payload["g1_routing"] = g1_payload
    return type(experiment).model_validate(payload)


def test_reasoning_tokens_are_not_double_counted_as_total_tokens() -> None:
    usage = {
        "input_tokens": 50,
        "output_tokens": 100,
        "reasoning_tokens": 80,
    }

    assert runner._usage_token_count(usage) == 150
    assert _load_resume_runner()._usage_token_count(usage) == 150


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_task_analyzer_provider_preserves_routing_and_disables_replay(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    class _Selector:
        def __init__(self, config) -> None:
            captured["primary"] = config.primary

        def resolve(self):
            return sentinel

    monkeypatch.setattr(module, "ModelSelector", _Selector)
    routed = ProviderConfig(
        provider="openrouter",
        model="original",
        api_key="fake",
        base_url="https://openrouter.example/api/v1",
        provider_routing={"order": ["OpenAI"], "allow_fallbacks": False},
        replay_provider_state=True,
    )

    resolved = module.build_task_analyzer_provider(
        routed,
        provider_id="openrouter",
        model_id="openai/gpt-analyzer",
        upstream_provider="openai",
    )
    primary = captured["primary"]

    assert resolved is sentinel
    assert primary.provider_routing == {
        **routed.provider_routing,
        "openai/gpt-analyzer": "openai",
    }
    assert primary.replay_provider_state is False
    assert primary.model == "openai/gpt-analyzer"


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_task_analyzer_provider_never_reuses_another_provider_credential(module) -> None:
    routed = ProviderConfig(
        provider="anthropic",
        model="anthropic/claude-sonnet-5",
        api_key="anthropic-key",
    )

    with pytest.raises(ValueError, match="cannot reuse a different provider credential"):
        module.build_task_analyzer_provider(
            routed,
            provider_id="openrouter",
            model_id="anthropic/claude-opus-4.8",
            upstream_provider="anthropic",
        )


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_task_analyzer_usage_separates_actual_from_requested_identity(module) -> None:
    row = module.task_analyzer_usage_row(
        {
            "attempt": 1,
            "physical_attempt_id": "1" * 32,
            "provider": "physical-provider",
            "model": "physical-model",
            "input_tokens": 3,
            "output_tokens": 1,
        },
        provider_id="requested-provider",
        model_id="requested-model",
        source="llm",
        fallback_reason="",
    )
    unknown = module.task_analyzer_usage_row(
        {},
        provider_id="requested-provider",
        model_id="requested-model",
        source="fallback",
        fallback_reason="missing receipt",
    )
    legacy_aggregate = module.task_analyzer_usage_rows(
        {
            "provider": "physical-provider",
            "model": "physical-model",
            "attempt_count": 3,
            "input_tokens": 9,
            "output_tokens": 3,
            "billed_cost": 0.3,
            "cost_source": "provider_billed",
            "provider_usage": {"response_ids": ["r1", "r2", "r3"]},
        },
        provider_id="requested-provider",
        model_id="requested-model",
        source="llm",
        fallback_reason="",
    )

    assert row["provider"] == "physical-provider"
    assert row["model"] == "physical-model"
    assert row["requested_provider"] == "requested-provider"
    assert row["requested_model"] == "requested-model"
    assert row["request_count"] == 1
    assert row["physical_attempt_id"] == "1" * 32
    assert unknown["provider"] == ""
    assert unknown["model"] == ""
    assert unknown["requested_provider"] == "requested-provider"
    assert unknown["requested_model"] == "requested-model"
    assert unknown["request_count"] == 1
    assert unknown["role"] == "unknown_request"
    assert len(legacy_aggregate) == 3
    assert all(item["role"] == "unknown_request" for item in legacy_aggregate)
    assert all(item["request_count"] == 1 for item in legacy_aggregate)
    assert len({item["physical_attempt_id"] for item in legacy_aggregate}) == 3
    assert sum(item["billed_cost"] for item in legacy_aggregate) == 0.0
    aggregate_evidence = legacy_aggregate[0]["provider_usage"]["unallocated_aggregate_usage"]
    assert aggregate_evidence["response_ids"] == ["r1", "r2", "r3"]
    assert aggregate_evidence["billed_cost"] == pytest.approx(0.3)


@pytest.mark.parametrize("loaded_runner", [runner, resume_runner])
def test_task_analyzer_retry_usage_expands_to_distinct_physical_units(
    loaded_runner,
) -> None:
    usage = {
        "attempt_count": 2,
        "physical_attempts": [
            {
                "attempt": 1,
                "physical_attempt_id": "1" * 32,
                "provider": "openrouter",
                "model": "anthropic/claude-opus-4.8",
                "requested_provider": "openrouter",
                "requested_model": "anthropic/claude-opus-4.8",
                "input_tokens": 11,
                "output_tokens": 2,
                "billed_cost": 0.01,
                "cost_source": "provider_billed",
                "provider_usage": {"response_ids": ["analyzer-1"]},
            },
            {
                "attempt": 2,
                "physical_attempt_id": "2" * 32,
                "provider": "openrouter",
                "model": "anthropic/claude-opus-4.8",
                "requested_provider": "openrouter",
                "requested_model": "anthropic/claude-opus-4.8",
                "input_tokens": 12,
                "output_tokens": 3,
                "billed_cost": 0.02,
                "cost_source": "provider_billed",
                "provider_usage": {"response_ids": ["analyzer-2"]},
            },
        ],
    }
    rows = loaded_runner.task_analyzer_usage_rows(
        usage,
        provider_id="openrouter",
        model_id="anthropic/claude-opus-4.8",
        source="llm_provider",
        fallback_reason="",
    )

    assert len(rows) == 2
    assert [row["request_count"] for row in rows] == [1, 1]
    assert [row["attempt"] for row in rows] == [1, 2]
    assert [row["physical_attempt_id"] for row in rows] == [
        "1" * 32,
        "2" * 32,
    ]
    assert [row["provider_usage"]["response_ids"][0] for row in rows] == [
        "analyzer-1",
        "analyzer-2",
    ]
    assert loaded_runner.usage_rows_request_count(rows) == 2


@pytest.mark.parametrize("loaded_runner", [runner, resume_runner], ids=["main", "resume"])
def test_task_analyzer_explicit_zero_request_emits_no_usage_rows(
    loaded_runner,
) -> None:
    rows = loaded_runner.task_analyzer_usage_rows(
        {"attempt_count": 0, "physical_attempts": []},
        provider_id="openrouter",
        model_id="anthropic/claude-opus-4.8",
        source="router_fallback",
        fallback_reason="RuntimeError",
    )

    assert rows == []


def _openrouter_config() -> tuple[GatewayConfig, ProviderConfig]:
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "base_url": "https://openrouter.example/api/v1",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "static_openrouter_b5",
        },
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
        base_url="https://openrouter.example/api/v1",
        provider_routing={
            "deepseek/deepseek-v4-pro": "deepseek",
        },
    )
    return config, inherited


def test_b2_argument_alignment_applies_g12_derived_quality_first_envelope() -> None:
    args = runner.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B2",
            "--concurrency",
            "8",
            "--timeout",
            "180",
            "--runner-mode",
            "provider",
            "--tool-mode",
            "provider_only",
            "--local-web-search-provider",
            "duckduckgo",
        ]
    )

    record = runner.apply_b2_g12_argument_alignment(args, ["B2"])

    assert record is not None
    assert record["requested_args"]["concurrency"] == 8
    assert record["requested_args"]["timeout"] == 180.0
    assert args.concurrency == 2
    assert args.timeout == 10800.0
    assert args.ensemble_proposer_timeout == pytest.approx(907.5)
    assert args.ensemble_aggregator_timeout == pytest.approx(2662.5)
    assert args.runner_mode == "agent_loop"
    assert args.agent_max_iterations == 20
    assert args.deadline_wrapup_margin_seconds == 300
    assert args.deadline_wrapup_disable_tools is True
    assert args.deadline_thinking_off_margin_seconds == 0
    assert args.max_iterations_includes_finalization is False
    assert args.retrieval_loop_finalization_threshold == 0
    assert args.finalization_aggregator_only is False
    assert args.finalization_disable_thinking is False
    assert args.generation_max_tokens == 16_384
    assert args.generation_max_attempts == 3
    assert args.tool_mode == "local_web_tools"
    assert args.local_web_search_provider == "brave"
    assert args.local_web_search_api_key_env == "BRAVE_SEARCH_API_KEY"
    assert args.judge_model == "google/gemini-3.1-pro-preview"
    assert args.judge_repeats == 3
    assert args.judge_concurrency == 6
    assert args.judge_max_attempts == 3


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_inline_experiment_json_controls_runtime_args_without_launcher_sets(module) -> None:
    args = module.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "G1",
            # Deliberately conflicting top-level values model parser defaults or
            # legacy launchers. The effective experiment JSON remains the
            # runtime authority.
            "--concurrency",
            "9",
            "--timeout",
            "180",
            "--judge-concurrency",
            "1",
            "--generation-max-attempts",
            "3",
            "--experiment-config-override-json",
            json.dumps(
                {
                    "runner": {"concurrency": 4},
                    "timeouts": {"task_seconds": 7_200},
                    "judge": {"concurrency": 2},
                    "generation": {"max_attempts": 2},
                }
            ),
        ]
    )

    record = module.apply_b2_g12_argument_alignment(args, ["G1"])

    assert record is not None
    assert args.concurrency == 4
    assert args.timeout == 7_200
    assert args.judge_concurrency == 2
    assert args.generation_max_attempts == 2
    assert record["effective_args"]["concurrency"] == 4
    assert record["effective_args"]["timeout"] == 7_200
    assert record["effective_args"]["judge_concurrency"] == 2
    assert record["effective_args"]["generation_max_attempts"] == 2
    assert record["config_provenance"]["inline_overrides"] == {
        "count": 0,
        "paths": [],
    }


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    ("model", "expected_level", "expected_budget"),
    [
        ("anthropic/claude-opus-4.8", "max", 50_000),
        ("moonshotai/kimi-k2.7-code", "high", 20_000),
        ("qwen/qwen3.7-max", "high", 20_000),
    ],
)
def test_generation_config_uses_registry_model_max(
    module,
    model: str,
    expected_level: str,
    expected_budget: int,
) -> None:
    policy = module.generation_thinking_policy()

    config = module.generation_chat_config(
        policy,
        model=model,
    )

    assert config.thinking is True
    assert config.thinking_level == expected_level
    assert config.thinking_budget_tokens == expected_budget


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    ("model", "expected_level"),
    [
        ("anthropic/claude-opus-4.8", "max"),
        ("moonshotai/kimi-k2.7-code", "high"),
        ("openai/gpt-5.5", "xhigh"),
        ("qwen/qwen3.7-max", "high"),
    ],
)
def test_generation_config_enables_openrouter_baseline_reasoning(
    module,
    model: str,
    expected_level: str,
) -> None:
    config = module.generation_chat_config(
        module.generation_thinking_policy(),
        model=model,
    )

    assert config.thinking is True
    assert config.thinking_level == expected_level
    assert config.model_capabilities is not None
    assert config.model_capabilities.supports_reasoning is True
    assert config.model_capabilities.reasoning_format == "openrouter"


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_router_single_selected_model_receives_reasoning_capabilities(module) -> None:
    config = module.generation_chat_config(
        module.generation_thinking_policy(),
        model=None,
    )
    assert config.model_capabilities is None

    resolved = module.with_openrouter_model_capabilities(config, "openai/gpt-5.5")

    assert resolved.model_capabilities is not None
    assert resolved.model_capabilities.supports_reasoning is True
    assert resolved.model_capabilities.reasoning_format == "openrouter"


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_loop_preserves_provider_native_max_thinking(module) -> None:
    chat_config = module.generation_chat_config(
        module.generation_thinking_policy(),
        model="anthropic/claude-opus-4.8",
    )

    agent_config = module.agent_config_from_chat_config(
        chat_config,
        timeout=3600,
        model_id="anthropic/claude-opus-4.8",
        max_iterations=12,
    )

    assert agent_config.thinking is ThinkingLevel.MAX
    assert agent_config.resolve_thinking("prompt") == (True, 50_000)


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_finalization_cli_defaults_off_and_can_be_enabled(module) -> None:
    defaults = module.build_parser().parse_args(["--input", "tasks.jsonl", "--groups", "B1"])
    assert module.agent_finalization_policy_from_args(defaults) == (
        module.DEFAULT_AGENT_FINALIZATION_POLICY
    )

    enabled = module.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B1",
            "--deadline-wrapup-margin-seconds",
            "600",
            "--deadline-wrapup-disable-tools",
            "--deadline-thinking-off-margin-seconds",
            "600",
            "--max-iterations-includes-finalization",
            "--retrieval-loop-finalization-threshold",
            "3",
            "--finalization-aggregator-only",
            "--finalization-disable-thinking",
        ]
    )

    assert module.agent_finalization_policy_from_args(enabled) == {
        "deadline_wrapup_margin_seconds": 600,
        "deadline_wrapup_disable_tools": True,
        "deadline_thinking_off_margin_seconds": 600,
        "max_iterations_includes_finalization": True,
        "retrieval_loop_finalization_threshold": 3,
        "finalization_aggregator_only": True,
        "finalization_disable_thinking": True,
    }


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_runner_normalization_preserves_explicit_zero_iterations(module) -> None:
    args = module.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B1",
            "--agent-max-iterations",
            "0",
        ]
    )

    policy = module.normalize_agent_runner_args(args)

    assert args.agent_max_iterations == 0
    assert policy == module.DEFAULT_AGENT_FINALIZATION_POLICY


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_config_threads_finalization_policy(module) -> None:
    policy = {
        "deadline_wrapup_margin_seconds": 600,
        "deadline_wrapup_disable_tools": True,
        "deadline_thinking_off_margin_seconds": 600,
        "max_iterations_includes_finalization": True,
        "retrieval_loop_finalization_threshold": 3,
        "finalization_aggregator_only": True,
        "finalization_disable_thinking": True,
    }

    agent_config = module.agent_config_from_chat_config(
        None,
        timeout=3600,
        model_id="test/model",
        max_iterations=0,
        finalization_policy=policy,
    )

    assert agent_config.max_iterations == 0
    for field_name, expected in policy.items():
        assert getattr(agent_config, field_name) == expected
    assert agent_config.metadata["agent_finalization_policy"] == policy
    assert agent_config.metadata["provider_retry_owner"] == "caller"


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_agent_hard_timeout_is_preserved_without_generation_retry(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_collect_agent_run(*_args, **kwargs):
        calls.append(kwargs)
        return module.RunResult(
            final_text="partial progress",
            done=None,
            error="TimeoutError: agent run timed out after 3600s",
            trace_events=[{"kind": "timeout", "timeout_s": 3600}],
        )

    monkeypatch.setattr(module, "collect_agent_run", fake_collect_agent_run)
    policy = {
        **module.DEFAULT_AGENT_FINALIZATION_POLICY,
        "deadline_wrapup_margin_seconds": 600,
    }

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        object(),
        "prompt",
        timeout=3600,
        runner_mode=module.RUNNER_MODE_AGENT_LOOP,
        tool_policy={},
        finalization_policy=policy,
        max_attempts=3,
    )

    assert len(calls) == 1
    assert calls[0]["finalization_policy"] == policy
    assert result.error == "TimeoutError: agent run timed out after 3600s"
    assert result.final_text == "partial progress"
    assert selected_attempt == 0
    assert len(attempts) == 1
    assert len(attempts[0]["attempt_id"]) == 32
    assert set(attempts[0]["attempt_id"]) <= set("0123456789abcdef")
    assert attempts[0]["attempt_kind"] == "generation"
    assert attempts[0]["started_at"] <= attempts[0]["completed_at"]
    assert attempts[0]["will_retry"] is False
    assert attempts[0]["retry_suppressed_reason"] == "agent_hard_timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_generation_attempt_offset_uses_cumulative_ordinal(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_collect_run(*_args, **_kwargs):
        return module.RunResult(
            final_text="accepted",
            done=DoneEvent(
                provider="openrouter",
                model="model-a",
                requested_provider="openrouter",
                requested_model="model-a",
                stop_reason="stop",
            ),
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        object(),
        "prompt",
        timeout=30,
        max_attempts=3,
        attempt_offset=2,
        expected_model="model-a",
        expected_provider="openrouter",
    )

    assert result.final_text == "accepted"
    assert len(attempts) == 1
    assert attempts[0]["attempt"] == 3
    assert selected_attempt == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_generation_attempt_offset_obeys_dynamic_total_budget(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_collect_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return module.RunResult(
            final_text="accepted",
            done=DoneEvent(
                provider="openrouter",
                model="model-a",
                requested_provider="openrouter",
                requested_model="model-a",
                stop_reason="stop",
            ),
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        object(),
        "prompt",
        timeout=30,
        max_attempts=1,
        attempt_offset=1,
        attempt_budget_limit=2,
        expected_model="model-a",
        expected_provider="openrouter",
    )

    assert calls == 1
    assert result.final_text == "accepted"
    assert [attempt["attempt"] for attempt in attempts] == [2]
    assert selected_attempt == 2
    with pytest.raises(ValueError, match="configured budget"):
        await module.collect_generation_with_retries(
            object(),
            "prompt",
            timeout=30,
            max_attempts=1,
            attempt_offset=2,
            attempt_budget_limit=2,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_g1_thinking_switch_off_preserves_same_provider_retry(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = {
        "strategy": "router_dynamic",
        "decision_id": "default-off-transient-decision",
        "ranking_thinking_assignment_enabled": False,
        "selected_P": [
            "openrouter:model-p",
            "openrouter:model-b",
            "openrouter:model-c",
        ],
        "selected_A": "openrouter:model-a",
        "proposer_sample_count": 3,
    }

    class Provider:
        selection_plan = plan
        min_successful_proposers = 2

        @staticmethod
        def _draco_reasoning_only_retry_factory(_exclusions):
            raise AssertionError("transient failures must not rebuild the roster")

    provider = Provider()
    failed_trace = {
        "selection_plan": deepcopy(plan),
        "candidates": [
            {
                "index": 0,
                "provider": "openrouter",
                "model": "model-p",
                "requested_provider": "openrouter",
                "requested_model": "model-p",
                "ok": False,
                "request_started": True,
                "physical_request_count": 1,
                "stop_reason": "error",
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "error": "HTTP 503 upstream unavailable",
                "content": {"text": "", "chars": 0, "truncated": False},
            }
        ],
    }
    successful_trace = {
        "selection_plan": deepcopy(plan),
        "candidates": [],
    }
    results = iter(
        [
            module.RunResult(
                final_text="",
                done=DoneEvent(ensemble_trace=failed_trace),
                error="HTTP 503 upstream unavailable",
                routing_trace={"selection_plan": deepcopy(plan)},
            ),
            module.RunResult(
                final_text="accepted",
                done=DoneEvent(
                    stop_reason="stop",
                    ensemble_trace=successful_trace,
                ),
                routing_trace={"selection_plan": deepcopy(plan)},
            ),
        ]
    )
    used_providers: list[object] = []

    async def fake_collect_run(active_provider, *_args, **_kwargs):
        used_providers.append(active_provider)
        return next(results)

    retry_reasons = iter(["transient_upstream", ""])
    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    monkeypatch.setattr(
        module,
        "generation_retry_reason",
        lambda *_args, **_kwargs: next(retry_reasons),
    )

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        provider,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
    )

    assert result.final_text == "accepted"
    assert selected_attempt == 2
    assert used_providers == [provider, provider]
    assert attempts[0]["will_retry"] is True
    assert attempts[0]["retry_suppressed_reason"] == ""
    assert [attempt["selection_plan"] for attempt in attempts] == [plan, plan]
    assert [attempt["run"]["ensemble_trace"] for attempt in attempts] == [
        failed_trace,
        successful_trace,
    ]
    for attempt in attempts:
        assert attempt["deterministic_proposer_failures"] == []
        assert attempt["excluded_proposer_identities"] == []
        assert "retry_selection_plan" not in attempt
        assert "thinking_execution_projection" not in attempt


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_g1_default_off_reasoning_only_length_rebuilds_router_dynamic_roster(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_identities = sorted(
        [
            "openrouter:openai/gpt-5.6-sol",
            "openrouter:anthropic/claude-sonnet-5",
        ]
    )
    managed_fields = (
        "ranking_thinking_assignment_enabled",
        "executed_thinking_assignment",
        "thinking_assignment_details",
    )

    def default_off_plan(plan: dict[str, object]) -> dict[str, object]:
        routed = _with_g1_task_analysis_invariants(
            {
                "strategy": "router_dynamic",
                **plan,
            }
        )
        for field in managed_fields:
            routed.pop(field, None)
        return routed

    initial_plan = default_off_plan(
        {
            "decision_id": "default-off-decision-1",
            "selected_P": [
                "openrouter:openai/gpt-5.6-sol",
                "openrouter:anthropic/claude-sonnet-5",
                "openrouter:model-b",
            ],
            "proposer_sample_count": 3,
        }
    )
    retry_plan = _bind_g1_retry_plan(
        initial_plan,
        default_off_plan(
            {
                "decision_id": "default-off-decision-2",
                "selected_P": [
                    "openrouter:model-b",
                    "openrouter:model-c",
                    "openrouter:model-d",
                ],
                "proposer_sample_count": 3,
            }
        ),
        exclusions=failed_identities,
    )

    class Provider:
        def __init__(self, selection_plan: dict[str, object]) -> None:
            self.selection_plan = selection_plan
            self.min_successful_proposers = module.legal_proposer_quorum(3)

    initial = Provider(initial_plan)
    replacement = Provider(retry_plan)
    rebuild_calls: list[list[str]] = []

    def rebuild(exclusions: list[str]):
        rebuild_calls.append(exclusions)
        return replacement

    initial._draco_reasoning_only_retry_factory = rebuild
    failed_trace = {
        "selection_plan": deepcopy(initial_plan),
        "candidates": [
            {
                "index": 0,
                "provider": "openrouter",
                "model": "openai/gpt-5.6-sol",
                "requested_provider": "openrouter",
                "requested_model": "openai/gpt-5.6-sol",
                "ok": False,
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": True,
                "usage_missing_count": 0,
                "stop_reason": "length",
                "output_tokens": 16_384,
                "reasoning_tokens": 16_384,
                "content": {"text": "", "chars": 0, "truncated": False},
            },
            {
                "index": 1,
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-5",
                "requested_provider": "openrouter",
                "requested_model": "anthropic/claude-sonnet-5",
                "ok": False,
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": True,
                "usage_missing_count": 0,
                "stop_reason": "length",
                "output_tokens": 16_384,
                "reasoning_tokens": 16_384,
                "content": {"text": "", "chars": 0, "truncated": False},
            },
        ],
        "physical_request_count": 2,
        "llm_request_count": 2,
        "usage_missing_count": 0,
    }
    successful_trace = {
        "selection_plan": deepcopy(retry_plan),
        "candidates": [],
    }
    results = iter(
        [
            module.RunResult(
                final_text="",
                done=DoneEvent(
                    ensemble_trace=failed_trace,
                    usage_missing_count=0,
                    model_usage_breakdown=[
                        {"request_count": 1},
                        {"request_count": 1},
                    ],
                ),
                error="llm ensemble had 1 successful proposer(s), requires 2",
                routing_trace={"selection_plan": deepcopy(initial_plan)},
            ),
            module.RunResult(
                final_text="accepted",
                done=DoneEvent(
                    stop_reason="stop",
                    ensemble_trace=successful_trace,
                ),
                routing_trace={"selection_plan": deepcopy(retry_plan)},
            ),
        ]
    )
    used_providers: list[object] = []

    async def fake_collect_run(active_provider, *_args, **_kwargs):
        used_providers.append(active_provider)
        return next(results)

    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    monkeypatch.setattr(
        module,
        "generation_retry_reason",
        lambda result, **_kwargs: (
            "ensemble_insufficient_proposers" if not result.final_text else ""
        ),
    )

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        initial,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
    )

    deterministic_failures = module._reasoning_only_length_failures_from_trace(failed_trace)
    assert len(deterministic_failures) == 2
    assert result.final_text == "accepted"
    assert selected_attempt == 2
    assert used_providers == [initial, replacement]
    assert rebuild_calls == [failed_identities]
    assert not set(failed_identities).intersection(retry_plan["selected_P"])
    assert len(retry_plan["selected_P"]) == 3
    assert attempts[0]["selection_plan"] == initial_plan
    assert attempts[0]["deterministic_proposer_failures"] == deterministic_failures
    assert attempts[0]["excluded_proposer_identities"] == []
    assert attempts[0]["retry_excluded_proposer_identities"] == failed_identities
    assert attempts[0]["retry_selection_plan"] == retry_plan
    assert attempts[0]["run"]["ensemble_trace"] == failed_trace
    assert attempts[1]["selection_plan"] == retry_plan
    assert attempts[1]["deterministic_proposer_failures"] == []
    assert attempts[1]["excluded_proposer_identities"] == failed_identities
    assert attempts[1]["run"]["ensemble_trace"] == successful_trace
    assert initial._draco_selected_retry_provider is replacement
    assert module.legal_proposer_quorum(3) == 2
    assert replacement.min_successful_proposers == 2
    assert all("thinking_execution_projection" not in attempt for attempt in attempts)
    assert all(field not in plan for plan in (initial_plan, retry_plan) for field in managed_fields)


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_unmanaged_reasoning_usage_drives_deterministic_g1_roster_retry(module) -> None:
    failed_identity = "openrouter:openai/gpt-5.6-sol"
    candidate = ensemble_provider._CandidateResult(
        index=0,
        sample_index=0,
        label="proposer_1",
        provider="openrouter",
        model="openai/gpt-5.6-sol",
        requested_provider="openrouter",
        requested_model="openai/gpt-5.6-sol",
        text="",
        input_tokens=208,
        output_tokens=16_384,
        reasoning_tokens=16_384,
        stop_reason="length",
        request_started=True,
        physical_request_count=1,
        usage_reported=True,
    )

    trace = {
        "physical_request_count": 1,
        "llm_request_count": 1,
        "usage_missing_count": 0,
        "candidates": [
            candidate.trace_row(
                include_text=True,
                content_max_chars=8_000,
            )
        ],
    }

    failures = module._reasoning_only_length_failures_from_trace(trace)

    assert len(failures) == 1
    failure = failures[0]
    assert failure["identity"] == failed_identity
    assert failure["reason"] == "reasoning_only_length"
    assert failure["stop_reason"] == "length"
    assert failure["visible_output_chars"] == 0
    assert failure["output_tokens"] == 16_384
    assert failure["reasoning_tokens"] == 16_384
    assert failure["request_started"] is True
    assert failure["physical_request_count"] == 1
    assert failure["usage_reported"] is True


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("candidates", 0, "ok"), True),
        (("candidates", 0, "request_started"), False),
        (("candidates", 0, "physical_request_count"), 0),
        (("candidates", 0, "usage_reported"), False),
        (("candidates", 0, "usage_missing_count"), 1),
        (("candidates", 0, "stop_reason"), "error"),
        (("candidates", 0, "reasoning_tokens"), 0),
        (("candidates", 0, "content", "chars"), 1),
        (("candidates", 0, "error"), "HTTP 429 rate limited"),
        (("candidates", 0, "error"), "HTTP 503 upstream unavailable"),
        (("candidates", 0, "error"), "DNS resolution failed"),
        (("candidates", 0, "error_code"), "invalid_api_key"),
        (("physical_request_count",), 2),
        (("llm_request_count",), 2),
        (("usage_missing_count",), 1),
    ],
)
def test_strict_reasoning_only_extractor_rejects_inexact_or_transient_evidence(
    module,
    path: tuple[object, ...],
    value: object,
) -> None:
    trace = {
        "physical_request_count": 1,
        "llm_request_count": 1,
        "usage_missing_count": 0,
        "candidates": [
            {
                "index": 0,
                "provider": "openrouter",
                "model": "openai/gpt-5.6-sol",
                "requested_provider": "openrouter",
                "requested_model": "openai/gpt-5.6-sol",
                "ok": False,
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": True,
                "usage_missing_count": 0,
                "stop_reason": "length",
                "output_tokens": 16_384,
                "reasoning_tokens": 16_384,
                "error": "",
                "error_code": "",
                "content": {"text": "", "chars": 0, "truncated": False},
            }
        ],
    }
    target: object = trace
    for component in path[:-1]:
        target = target[component]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]

    assert module._reasoning_only_length_failures_from_trace(trace) == []


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_strict_reasoning_only_extractor_requires_done_usage_coherence(module) -> None:
    trace = {
        "physical_request_count": 1,
        "llm_request_count": 1,
        "usage_missing_count": 0,
        "candidates": [
            {
                "index": 0,
                "provider": "openrouter",
                "model": "openai/gpt-5.6-sol",
                "requested_provider": "openrouter",
                "requested_model": "openai/gpt-5.6-sol",
                "ok": False,
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": True,
                "usage_missing_count": 0,
                "stop_reason": "max_tokens",
                "output_tokens": 16_384,
                "reasoning_tokens": 16_384,
                "error": "",
                "error_code": "",
                "content": {"text": "", "chars": 0, "truncated": False},
            }
        ],
    }
    exact = module.RunResult(
        final_text="",
        done=DoneEvent(
            usage_missing_count=0,
            model_usage_breakdown=[
                {
                    "role": "proposer",
                    "provider": "openrouter",
                    "model": "openai/gpt-5.6-sol",
                    "request_count": 1,
                    "reasoning_tokens": 16_384,
                }
            ],
            ensemble_trace=trace,
        ),
    )
    assert len(module.deterministic_reasoning_only_length_failures(exact)) == 1

    missing_usage = deepcopy(exact)
    assert missing_usage.done is not None
    missing_usage.done.usage_missing_count = 1
    assert module.deterministic_reasoning_only_length_failures(missing_usage) == []

    unrepresented_request = deepcopy(exact)
    assert unrepresented_request.done is not None
    unrepresented_request.done.model_usage_breakdown = []
    assert module.deterministic_reasoning_only_length_failures(unrepresented_request) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_provider_native_g1_recovery_never_enters_outer_generation_retry(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "selected_P": [
            "openrouter:model-a",
            "openrouter:model-b",
            "openrouter:model-c",
        ],
        "proposer_models": ["model-a", "model-b", "model-c"],
        "proposer_sample_count": 3,
        "effective_min_successful_proposers": 2,
        "backup_P": [
            "openrouter:model-d",
            "openrouter:model-e",
        ],
        "configured_proposer_backup_count": 2,
        "effective_proposer_backup_count": 2,
        "selected_A": "openrouter:model-f",
        "aggregator_candidates": [
            "openrouter:model-f",
            "openrouter:model-g",
            "openrouter:model-h",
        ],
        "proposer_recovery_policy": {
            "schema": "opensquilla.router-dynamic-proposer-recovery/v1",
            "configured_backup_count": 2,
            "effective_backup_count": 2,
            "max_additional_physical_requests": 3,
            "quorum_required": 2,
            "max_tokens_cap": 65_536,
            "visible_answer_reserve_tokens": 4_096,
            "thinking_downgrade_order": ["one_strictly_lower"],
            "transient_same_model_retries": 1,
            "backup_reasoning_downgrades": 1,
        },
    }

    class Provider:
        selection_plan = plan

    calls: list[object] = []

    async def fake_collect_run(active_provider, *_args, **_kwargs):
        calls.append(active_provider)
        return module.RunResult(
            final_text="",
            done=DoneEvent(
                usage_missing_count=0,
                ensemble_trace={
                    "selection_plan": deepcopy(plan),
                    "proposer_recovery": {
                        "schema": "opensquilla.router-dynamic-proposer-recovery/v1",
                        "max_additional_physical_requests": 3,
                        "additional_physical_requests_started": 3,
                        "remaining_additional_physical_requests": 0,
                    },
                },
            ),
            error="ensemble_insufficient_proposers",
            routing_trace={"selection_plan": deepcopy(plan)},
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    monkeypatch.setattr(
        module,
        "generation_retry_reason",
        lambda *_args, **_kwargs: "ensemble_insufficient_proposers",
    )
    monkeypatch.setattr(
        module,
        "g1_retry_physical_usage_binding_reasons",
        lambda *_args, **_kwargs: [],
    )

    paid_attempt_sink: dict[str, object] = {}
    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        Provider(),
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=3,
        paid_attempt_sink=paid_attempt_sink,
    )

    assert result.error == "ensemble_insufficient_proposers"
    assert selected_attempt == 0
    assert len(calls) == 1
    assert len(attempts) == 1
    assert attempts[0]["will_retry"] is False
    assert attempts[0]["retry_suppressed_reason"] == ("provider_native_proposer_recovery_terminal")
    assert attempts[0]["proposer_recovery_owner"] == "provider"
    assert attempts[0]["selection_plan"] == plan
    assert attempts[0]["deterministic_proposer_failures"] == []
    assert attempts[0]["excluded_proposer_identities"] == []
    assert "ensemble_trace" in attempts[0]["run"]
    assert paid_attempt_sink["provider_native_g1_recovery"] is True


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_provider_native_retry_suppression_is_scoped_to_proposer_failures(module) -> None:
    assert module.provider_native_proposer_retry_reason(
        "ensemble_insufficient_proposers"
    )
    assert module.provider_native_proposer_retry_reason(
        "llm ensemble had 1 successful proposer(s), requires 2"
    )
    assert module.provider_native_proposer_retry_reason(
        "ensemble_proposer_close_timeout"
    )
    assert module.provider_native_proposer_retry_reason(
        "ensemble proposer cleanup did not finish; recovery, aggregation, "
        "and fallback were not started"
    )
    assert not module.provider_native_proposer_retry_reason(
        "invalid_agent_call_output_binding"
    )
    assert not module.provider_native_proposer_retry_reason(
        "Provider stream ended with an incomplete tool call"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_provider_native_non_proposer_failure_uses_outer_generation_retry(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "selected_P": [
            "openrouter:model-a",
            "openrouter:model-b",
            "openrouter:model-c",
        ],
        "proposer_models": ["model-a", "model-b", "model-c"],
        "proposer_sample_count": 3,
        "effective_min_successful_proposers": 2,
        "backup_P": ["openrouter:model-d", "openrouter:model-e"],
        "configured_proposer_backup_count": 2,
        "effective_proposer_backup_count": 2,
        "selected_A": "openrouter:model-f",
        "aggregator_candidates": ["openrouter:model-f"],
        "proposer_recovery_policy": {
            "schema": "opensquilla.router-dynamic-proposer-recovery/v1",
            "configured_backup_count": 2,
            "effective_backup_count": 2,
            "max_additional_physical_requests": 3,
            "quorum_required": 2,
            "max_tokens_cap": 65_536,
            "visible_answer_reserve_tokens": 4_096,
            "thinking_downgrade_order": ["one_strictly_lower"],
            "transient_same_model_retries": 1,
            "backup_reasoning_downgrades": 1,
        },
    }

    class Provider:
        selection_plan = plan

    calls: list[object] = []

    async def fake_collect_run(active_provider, *_args, **_kwargs):
        calls.append(active_provider)
        return module.RunResult(
            final_text="usable draft",
            done=DoneEvent(
                usage_missing_count=0,
                ensemble_trace={
                    "selection_plan": deepcopy(plan),
                    "proposer_recovery": {
                        "schema": "opensquilla.router-dynamic-proposer-recovery/v1",
                        "max_additional_physical_requests": 3,
                        "additional_physical_requests_started": 0,
                        "remaining_additional_physical_requests": 3,
                        "quorum_reached": True,
                    },
                },
            ),
            routing_trace={"selection_plan": deepcopy(plan)},
        )

    retry_reasons = iter(["invalid_agent_call_output_binding", ""])
    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    monkeypatch.setattr(
        module,
        "generation_retry_reason",
        lambda *_args, **_kwargs: next(retry_reasons),
    )
    monkeypatch.setattr(
        module,
        "g1_attempt_plan_consistency_reason",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        module,
        "g1_retry_physical_usage_binding_reasons",
        lambda *_args, **_kwargs: [],
    )

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        Provider(),
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=3,
        retry_backoff_seconds=0,
    )

    assert result.final_text == "usable draft"
    assert len(calls) == 2
    assert len(attempts) == 2
    assert attempts[0]["retry_reason"] == "invalid_agent_call_output_binding"
    assert attempts[0]["retry_suppressed_reason"] == ""
    assert attempts[0]["will_retry"] is True
    assert selected_attempt == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_reasoning_only_length_retry_rebuilds_roster_and_tracks_each_plan(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_identity = "openrouter:openai/gpt-5.6-sol"
    initial_plan = _with_g1_task_analysis_invariants(
        {
            "decision_id": "decision-1",
            "selected_P": [
                failed_identity,
                "openrouter:google/gemini-3.1-pro-preview",
                "openrouter:anthropic/claude-sonnet-5",
            ],
            "proposer_sample_count": 3,
        }
    )
    initial_plan.update(
        {
            "ranking_thinking_assignment_enabled": True,
            "selected_A": "openrouter:model-a",
            "aggregator_candidates": ["openrouter:model-a"],
            "executed_thinking_assignment": {
                "proposers": {identity: "highest" for identity in initial_plan["selected_P"]},
                "aggregator": "highest",
                "thinking_policy_version": "test-thinking-policy/v1",
            },
            "thinking_execution_fallbacks": [],
            "thinking_assignment_details": {
                "proposers": [
                    {
                        "identity": identity,
                        "model_id": identity.partition(":")[2],
                        "role": "proposer",
                        "requested_level": "highest",
                        "effective_level": "highest",
                        "provider_level": "xhigh",
                        "fallback_reason": "",
                        "reasons": [],
                        "provider_rejection_fallbacks": (
                            [
                                {
                                    "unified_level": "high",
                                    "provider_level": "high",
                                    "reason": "provider_rejection_fallback",
                                },
                                {
                                    "unified_level": "medium",
                                    "provider_level": "medium",
                                    "reason": "provider_rejection_fallback",
                                },
                                {
                                    "unified_level": "low",
                                    "provider_level": "low",
                                    "reason": "provider_rejection_fallback",
                                },
                            ]
                            if identity == failed_identity
                            else []
                        ),
                    }
                    for identity in initial_plan["selected_P"]
                ],
                "aggregator": {
                    "identity": "openrouter:model-a",
                    "model_id": "model-a",
                    "role": "aggregator",
                    "requested_level": "highest",
                    "effective_level": "highest",
                    "provider_level": "xhigh",
                    "fallback_reason": "",
                    "reasons": [],
                    "provider_rejection_fallbacks": [],
                },
                "aggregator_candidates": [
                    {
                        "identity": "openrouter:model-a",
                        "model_id": "model-a",
                        "role": "aggregator",
                        "requested_level": "highest",
                        "effective_level": "highest",
                        "provider_level": "xhigh",
                        "fallback_reason": "",
                        "reasons": [],
                        "provider_rejection_fallbacks": [],
                    }
                ],
            },
        }
    )
    retry_plan = _bind_g1_retry_plan(
        initial_plan,
        {
            "decision_id": "decision-2",
            "selected_P": [
                "openrouter:google/gemini-3.1-pro-preview",
                "openrouter:anthropic/claude-sonnet-5",
                "openrouter:openai/gpt-5.5-pro",
            ],
            "proposer_sample_count": 3,
        },
        exclusions=[failed_identity],
    )

    class Provider:
        def __init__(self, plan: dict[str, object]) -> None:
            self.selection_plan = plan
            self.min_successful_proposers = 2

    initial = Provider(initial_plan)
    replacement = Provider(retry_plan)
    rebuild_calls: list[list[str]] = []

    def rebuild(exclusions: list[str]):
        rebuild_calls.append(exclusions)
        return replacement

    initial._draco_reasoning_only_retry_factory = rebuild
    physical_initial_plan = deepcopy(initial_plan)
    physical_initial_plan["executed_thinking_assignment"]["proposers"][failed_identity] = "high"
    physical_initial_plan["thinking_execution_fallbacks"] = [
        {
            "trigger_stage": "proposer_execution",
            "fallback_type": "thinking_level_neighbor",
            "reason": "provider_rejected_thinking_level",
            "identity": failed_identity,
            "requested_thinking_level": "highest",
            "rejected_unified_level": "highest",
            "rejected_provider_level": "xhigh",
            "effective_thinking_level": "high",
            "effective_provider_level": "high",
            "thinking_policy_version": "test-thinking-policy/v1",
            "fallback_result": "failed",
        }
    ]
    failed_trace = {
        "selection_plan": physical_initial_plan,
        "candidates": [
            {
                "index": 0,
                "provider": "openrouter",
                "model": "openai/gpt-5.6-sol",
                "requested_provider": "openrouter",
                "requested_model": "openai/gpt-5.6-sol",
                "ok": False,
                "request_started": True,
                "physical_request_count": 2,
                "usage_reported": True,
                "usage_missing_count": 0,
                "stop_reason": "length",
                "output_tokens": 16_384,
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
                    "thinking_fallback_attempts": deepcopy(
                        physical_initial_plan["thinking_execution_fallbacks"]
                    ),
                },
                "content": {"text": "", "chars": 0, "truncated": False},
            }
        ],
        "physical_request_count": 2,
        "llm_request_count": 2,
        "usage_missing_count": 0,
    }
    results = iter(
        [
            module.RunResult(
                final_text="",
                done=DoneEvent(
                    ensemble_trace=failed_trace,
                    usage_missing_count=0,
                    model_usage_breakdown=[
                        {"request_count": 2},
                    ],
                ),
                error="llm ensemble had 1 successful proposer(s), requires 2",
                routing_trace={"selection_plan": deepcopy(initial_plan)},
            ),
            module.RunResult(
                final_text="accepted",
                done=DoneEvent(
                    ensemble_trace={
                        "selection_plan": deepcopy(retry_plan),
                        "candidates": [],
                    }
                ),
                routing_trace={"selection_plan": deepcopy(retry_plan)},
            ),
        ]
    )
    used_providers: list[object] = []

    async def fake_collect_run(provider, *_args, **_kwargs):
        used_providers.append(provider)
        return next(results)

    validated_plans: list[dict[str, object]] = []

    def fake_retry_reason(*_args, **kwargs):
        validated_plans.append(dict(kwargs["expected_selection_plan"]))
        return "ensemble_insufficient_proposers" if len(validated_plans) == 1 else ""

    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    monkeypatch.setattr(module, "generation_retry_reason", fake_retry_reason)

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        initial,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=3,
    )

    assert result.final_text == "accepted"
    assert selected_attempt == 2
    assert used_providers == [initial, replacement]
    assert rebuild_calls == [["openrouter:openai/gpt-5.6-sol"]]
    assert validated_plans == [initial_plan, retry_plan]
    assert attempts[0]["selection_plan"] == initial_plan
    assert attempts[0]["retry_selection_plan"] == retry_plan
    assert attempts[0]["deterministic_proposer_failures"] == (
        module._reasoning_only_length_failures_from_trace(failed_trace)
    )
    assert attempts[1]["selection_plan"] == retry_plan
    assert attempts[1]["excluded_proposer_identities"] == ["openrouter:openai/gpt-5.6-sol"]
    assert json.loads(json.dumps(attempts[0]["run"]))["ensemble_trace"] == failed_trace
    assert initial._draco_selected_retry_provider is replacement
    assert replacement.min_successful_proposers == module.legal_proposer_quorum(3)

    tampered_fallback_plan = deepcopy(physical_initial_plan)
    tampered_fallback_plan["thinking_execution_fallbacks"][0]["effective_thinking_level"] = "medium"
    assert (
        module.g1_execution_plan_mutation_reason(
            initial_plan,
            tampered_fallback_plan,
        )
        == "g1_attempt_thinking_execution_provenance_invalid"
    )

    upward_expected = deepcopy(initial_plan)
    failed_detail = next(
        row
        for row in upward_expected["thinking_assignment_details"]["proposers"]
        if row["identity"] == failed_identity
    )
    failed_detail["requested_level"] = "high"
    failed_detail["effective_level"] = "high"
    failed_detail["provider_level"] = "high"
    failed_detail["provider_rejection_fallbacks"] = [
        {
            "unified_level": "highest",
            "provider_level": "xhigh",
            "reason": "provider_rejection_fallback",
        }
    ]
    upward_expected["executed_thinking_assignment"]["proposers"][failed_identity] = "high"
    upward_fallback_plan = deepcopy(upward_expected)
    upward_fallback_plan["executed_thinking_assignment"]["proposers"][failed_identity] = "highest"
    upward_fallback_plan["thinking_execution_fallbacks"] = [
        {
            **physical_initial_plan["thinking_execution_fallbacks"][0],
            "requested_thinking_level": "high",
            "rejected_unified_level": "high",
            "rejected_provider_level": "high",
            "effective_thinking_level": "highest",
            "effective_provider_level": "xhigh",
        }
    ]
    assert (
        module.g1_execution_plan_mutation_reason(
            upward_expected,
            upward_fallback_plan,
        )
        == ""
    )

    skipped_upward_plan = deepcopy(upward_fallback_plan)
    skipped_upward_plan["thinking_execution_fallbacks"][0]["effective_thinking_level"] = "low"
    assert (
        module.g1_execution_plan_mutation_reason(
            upward_expected,
            skipped_upward_plan,
        )
        == "g1_attempt_thinking_execution_provenance_invalid"
    )

    arbitrary_reason_plan = deepcopy(physical_initial_plan)
    arbitrary_reason_plan["thinking_execution_fallbacks"][0]["reason"] = "forged_reason"
    assert (
        module.g1_execution_plan_mutation_reason(
            initial_plan,
            arbitrary_reason_plan,
        )
        == "g1_attempt_thinking_execution_provenance_invalid"
    )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_default_off_provider_setup_remains_exactly_one_shot(module) -> None:
    class Provider:
        selection_plan = {
            "decision_id": "legacy-decision",
            "ranking_thinking_assignment_enabled": False,
        }

        def selection_plan_execution_snapshot(self):
            raise AssertionError("default-off setup must not inspect execution snapshots")

    provider = Provider()
    provider._draco_frozen_routing_trace = {"stale": "managed-route"}
    usage = [{"role": "task_analyzer", "input_tokens": 3}]
    routing = {
        "selection_plan": deepcopy(provider.selection_plan),
        "legacy": True,
    }
    module.attach_provider_setup(
        provider,
        module.ProviderBuildResult(
            provider=provider,
            prompt="prompt",
            setup_latency_ms=17,
            setup_usage=usage,
            routing_trace=routing,
        ),
    )

    assert module.consume_provider_setup(provider) == {
        "latency_ms": 17,
        "usage": usage,
        "routing": routing,
    }
    assert module.consume_provider_setup(provider) == {}
    assert provider._draco_frozen_routing_trace is None


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_managed_provider_setup_keeps_frozen_routing_after_accounting_consumed(
    module,
) -> None:
    class Provider:
        selection_plan = {
            "decision_id": "managed-decision",
            "ranking_thinking_assignment_enabled": True,
            "executed_thinking_assignment": {"aggregator": "high"},
        }

        def selection_plan_execution_snapshot(self):
            return deepcopy(self.selection_plan)

    provider = Provider()
    module.attach_provider_setup(
        provider,
        module.ProviderBuildResult(
            provider=provider,
            prompt="prompt",
            setup_latency_ms=17,
            setup_usage=[{"role": "task_analyzer", "input_tokens": 3}],
            routing_trace={"legacy": True},
        ),
    )

    first = module.consume_provider_setup(provider)
    second = module.consume_provider_setup(provider)

    assert first["latency_ms"] == 17
    assert len(first["usage"]) == 1
    assert first["routing"]["selection_plan"] == provider.selection_plan
    assert second == {
        "latency_ms": 0,
        "usage": [],
        "routing": {
            "legacy": True,
            "selection_plan": provider.selection_plan,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
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
async def test_g1_retry_task_analysis_tamper_is_rejected_before_second_paid_call(
    module,
    monkeypatch: pytest.MonkeyPatch,
    tamper_field: str,
) -> None:
    failed_identity = "openrouter:openai/gpt-5.6-sol"
    initial_plan = _with_g1_task_analysis_invariants(
        {
            "decision_id": "decision-1",
            "selected_P": [
                failed_identity,
                "openrouter:model-b",
                "openrouter:model-c",
            ],
            "proposer_sample_count": 3,
        }
    )
    retry_plan = _bind_g1_retry_plan(
        initial_plan,
        {
            "decision_id": "decision-2",
            "selected_P": [
                "openrouter:model-b",
                "openrouter:model-c",
                "openrouter:model-d",
            ],
            "proposer_sample_count": 3,
        },
        exclusions=[failed_identity],
    )
    if tamper_field == "task_analyzer":
        retry_plan["task_analyzer"]["source"] = "tampered"
    elif tamper_field == "task_profile":
        retry_plan["task_profile"]["task_type"] = "tampered"
    elif tamper_field == "request_context":
        retry_plan["request_context"]["task_text"] = "tampered"
    elif tamper_field == "binding_hash":
        retry_plan["task_analysis_reuse"]["projection_sha256"] = "0" * 64
        retry_plan["retry_routing"]["task_analysis_reuse_sha256"] = "0" * 64
    elif tamper_field == "source_decision":
        retry_plan["task_analysis_reuse"]["source_decision_id"] = "tampered"
    elif tamper_field == "user_profile_enabled":
        retry_plan[tamper_field] = True
    elif tamper_field == "routing_confidence":
        retry_plan[tamper_field] = 0.1
    else:
        retry_plan[tamper_field] = "tampered"

    class Provider:
        def __init__(self, plan: dict[str, object]) -> None:
            self.selection_plan = plan
            self.min_successful_proposers = 2

    initial = Provider(initial_plan)
    replacement = Provider(retry_plan)
    initial._draco_reasoning_only_retry_factory = lambda _excluded: replacement
    failed_trace = {
        "selection_plan": deepcopy(initial_plan),
        "candidates": [
            {
                "provider": "openrouter",
                "model": "openai/gpt-5.6-sol",
                "requested_provider": "openrouter",
                "requested_model": "openai/gpt-5.6-sol",
                "ok": False,
                "request_started": True,
                "physical_request_count": 1,
                "stop_reason": "length",
                "reasoning_tokens": 8_192,
                "effective_thinking_level": "high",
                "provider_thinking_level": "high",
                "execution": _test_proposer_execution(failed_identity),
                "content": {"text": "", "chars": 0},
            }
        ],
    }
    paid_calls = 0

    async def fake_collect_run(*_args, **_kwargs):
        nonlocal paid_calls
        paid_calls += 1
        if paid_calls > 1:
            raise AssertionError("tampered retry must be rejected before a paid call")
        return module.RunResult(
            final_text="",
            done=DoneEvent(ensemble_trace=failed_trace),
            error="ensemble_insufficient_proposers",
            routing_trace={"selection_plan": deepcopy(initial_plan)},
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    monkeypatch.setattr(
        module,
        "generation_retry_reason",
        lambda *_args, **_kwargs: "ensemble_insufficient_proposers",
    )

    _, attempts, selected_attempt = await module.collect_generation_with_retries(
        initial,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
    )

    assert paid_calls == 1
    assert selected_attempt == 0
    assert attempts[0]["will_retry"] is False
    assert attempts[0]["retry_suppressed_reason"] == ("reasoning_only_retry_provenance_invalid")


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_g1_attempt_full_plan_equality_rejects_unbound_physical_field(module) -> None:
    plan = _with_g1_task_analysis_invariants(
        {
            "decision_id": "decision-1",
            "selected_P": ["openrouter:model-a"],
        }
    )
    physical = deepcopy(plan)
    physical["unbound_extra_field"] = "tampered"
    result = module.RunResult(
        final_text="",
        done=DoneEvent(
            ensemble_trace={
                "selection_plan": physical,
                "candidates": [],
            }
        ),
        routing_trace={"selection_plan": deepcopy(plan)},
    )

    assert module.g1_attempt_plan_consistency_reason(plan, result) == (
        "g1_attempt_plan_provenance_invalid"
    )


def _g1_lifecycle_receipt_serialization_plans() -> tuple[
    dict[str, object],
    dict[str, object],
]:
    receipt = ProviderBillingReceipt(
        currency="USD",
        status="confirmed",
        amount_nanos=10_000_000,
        usd_equivalent_nanos=10_000_000,
        fx_native_per_usd_nanos=1_000_000_000,
    )
    plan = _with_g1_task_analysis_invariants(
        {
            "decision_id": "decision-1",
            "selected_P": ["openrouter:model-a"],
        }
    )
    plan["task_analyzer"]["usage"] = {
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "input_tokens": 11,
        "output_tokens": 3,
        "billing_receipt": receipt,
        "provider_usage": {
            "physical_attempt_id": "a" * 32,
            "response_ids": ["analyzer-response-1"],
        },
        "physical_attempts": [
            {
                "attempt": 1,
                "physical_attempt_id": "a" * 32,
                "provider": "openrouter",
                "model": "anthropic/claude-opus-4.8",
                "input_tokens": 11,
                "output_tokens": 3,
                "billing_receipt": receipt,
            }
        ],
    }
    routing_plan = resume_runner.json_safe(deepcopy(plan))
    physical_plan = ensemble_provider._json_safe(deepcopy(plan))
    assert isinstance(routing_plan, dict)
    assert isinstance(physical_plan, dict)
    physical_plan["task_analyzer"]["usage"]["billing_receipt"] = str(receipt)
    physical_plan["task_analyzer"]["usage"]["physical_attempts"][0]["billing_receipt"] = str(
        receipt
    )
    return routing_plan, physical_plan


def _g1_lifecycle_plan_reasons(
    module,
    monkeypatch: pytest.MonkeyPatch,
    *,
    routing_plan: dict[str, object],
    physical_plan: dict[str, object],
    full_attempt_history: bool = False,
) -> list[str]:
    monkeypatch.setattr(
        module,
        "ensemble_call_trace_sequence",
        lambda _trace: ([{"selection_plan": physical_plan}], []),
    )
    row = {
        "group": "G1",
        "routing_trace": {"selection_plan": routing_plan},
        "ensemble_trace": {},
        "execution": {"generation_attempts": []},
    }
    if full_attempt_history:
        if hasattr(module, "effective_g1_lifecycle_routing"):
            from opensquilla.provider import thinking_execution

            monkeypatch.setattr(
                thinking_execution,
                "validate_thinking_execution_call",
                lambda _prior, call: (call["selection_plan"], ""),
            )
        row.update(
            {
                "final_text_sha256": "selected-answer",
                "usage": {},
                "execution": {
                    "generation_attempts": [
                        {
                            "attempt_id": "1" * 32,
                            "attempt": 1,
                            "selection_plan": routing_plan,
                            "excluded_proposer_identities": [],
                            "deterministic_proposer_failures": [],
                            "will_retry": False,
                            "run": {
                                "final_text_sha256": "selected-answer",
                                "usage": {},
                                "routing_trace": {
                                    "selection_plan": routing_plan,
                                },
                                "ensemble_trace": {
                                    "selection_plan": physical_plan,
                                },
                            },
                        }
                    ]
                },
            }
        )
    if hasattr(module, "effective_g1_lifecycle_routing"):
        monkeypatch.setattr(
            module,
            "g1_registry_plan_reasons",
            lambda *_args, **_kwargs: ([], (), ""),
        )
        return module.effective_g1_lifecycle_routing(
            row,
            contract={"g1_registry_contract": {}},
        )[2]
    monkeypatch.setattr(
        module,
        "g1_registry_contract_reasons",
        lambda *_args, **_kwargs: [],
    )
    return module.g1_provider_lifecycle_routing_evidence(
        row,
        contract={"g1_registry_contract": {}},
    )[2]


def test_g1_lifecycle_plan_projection_is_synced_with_finalizer() -> None:
    finalizer = _load_finalizer()

    for name in (
        "_g1_task_analyzer_decision_projection",
        "_g1_lifecycle_plan_field",
        "_g1_plans_match",
    ):
        assert inspect.getsource(getattr(finalizer, name)) == inspect.getsource(
            getattr(resume_runner, name)
        )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_g1_attempt_consistency_ignores_only_analyzer_receipt_serialization(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider import thinking_execution

    routing_plan, physical_plan = _g1_lifecycle_receipt_serialization_plans()
    monkeypatch.setattr(
        module,
        "ensemble_call_trace_sequence",
        lambda _trace: ([{"selection_plan": physical_plan}], []),
    )
    monkeypatch.setattr(
        thinking_execution,
        "validate_thinking_execution_call",
        lambda _prior, call: (call["selection_plan"], ""),
    )
    result = module.RunResult(
        final_text="",
        done=DoneEvent(
            ensemble_trace={"selection_plan": physical_plan},
        ),
        routing_trace={"selection_plan": routing_plan},
    )

    assert module.g1_attempt_plan_consistency_reason(routing_plan, result) == ""

    tampered_physical = deepcopy(physical_plan)
    tampered_physical["task_analyzer"]["usage"]["input_tokens"] += 1
    monkeypatch.setattr(
        module,
        "ensemble_call_trace_sequence",
        lambda _trace: ([{"selection_plan": tampered_physical}], []),
    )
    result.done.ensemble_trace = {"selection_plan": tampered_physical}
    assert (
        module.g1_attempt_plan_consistency_reason(
            routing_plan,
            result,
        )
        == "g1_attempt_plan_provenance_invalid"
    )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_g1_missing_top_level_routing_backfills_only_from_matching_calls(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider import thinking_execution

    routing_plan, physical_plan = _g1_lifecycle_receipt_serialization_plans()
    monkeypatch.setattr(
        module,
        "ensemble_call_trace_sequence",
        lambda _trace: ([{"selection_plan": physical_plan}], []),
    )
    monkeypatch.setattr(
        thinking_execution,
        "validate_thinking_execution_call",
        lambda _prior, call: (call["selection_plan"], ""),
    )
    result = module.RunResult(
        final_text="",
        done=DoneEvent(ensemble_trace={"selection_plan": physical_plan}),
        error="tool-enabled aggregation requires 2 fully completed proposer drafts",
        routing_trace={},
    )

    assert module.backfill_g1_result_routing_trace_from_ensemble(
        routing_plan,
        result,
    )
    assert result.routing_trace["selection_plan"] == physical_plan
    assert result.routing_trace["selection_plan_backfill"] == {
        "source": "ensemble_call_trace",
        "call_count": 1,
    }
    assert module.g1_attempt_plan_consistency_reason(routing_plan, result) == ""
    assert result.error.startswith("tool-enabled aggregation requires 2")

    tampered = deepcopy(physical_plan)
    tampered["selected_A"] = "openrouter:tampered"
    monkeypatch.setattr(
        module,
        "ensemble_call_trace_sequence",
        lambda _trace: ([{"selection_plan": tampered}], []),
    )
    rejected = module.RunResult(
        final_text="",
        done=DoneEvent(ensemble_trace={"selection_plan": tampered}),
        error="HTTP 402: insufficient credits",
        routing_trace={},
    )
    assert not module.backfill_g1_result_routing_trace_from_ensemble(
        routing_plan,
        rejected,
    )
    assert rejected.routing_trace == {}


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    "existing_value",
    [None, "corrupt-plan", [], {}],
    ids=["none", "string", "list", "empty-mapping"],
)
def test_g1_routing_backfill_preserves_existing_selection_plan_evidence(
    module,
    existing_value: object,
) -> None:
    expected_plan, physical_plan = _g1_lifecycle_receipt_serialization_plans()
    original_routing = {
        "selection_plan": deepcopy(existing_value),
        "preexisting_evidence": True,
    }
    result = module.RunResult(
        final_text="",
        done=DoneEvent(ensemble_trace={"selection_plan": physical_plan}),
        error="HTTP 402: insufficient credits",
        routing_trace=deepcopy(original_routing),
    )

    assert not module.backfill_g1_result_routing_trace_from_ensemble(
        expected_plan,
        result,
    )
    assert result.routing_trace == original_routing
    assert (
        module.g1_attempt_plan_consistency_reason(expected_plan, result)
        == "g1_attempt_plan_provenance_invalid"
    )


@pytest.mark.parametrize("implementation", ["finalizer", "resume"])
def test_g1_lifecycle_plan_ignores_real_analyzer_receipt_serialization(
    implementation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_finalizer() if implementation == "finalizer" else resume_runner
    routing_plan, physical_plan = _g1_lifecycle_receipt_serialization_plans()
    routing_usage = routing_plan["task_analyzer"]["usage"]
    physical_usage = physical_plan["task_analyzer"]["usage"]

    assert isinstance(routing_usage["billing_receipt"], dict)
    assert isinstance(physical_usage["billing_receipt"], str)
    assert isinstance(routing_usage["physical_attempts"][0]["billing_receipt"], dict)
    assert isinstance(physical_usage["physical_attempts"][0]["billing_receipt"], str)
    assert module._g1_plans_match(routing_plan, physical_plan)
    assert module._g1_full_plans_match(routing_plan, physical_plan)
    assert (
        module._g1_execution_plan_mutation_reasons(
            routing_plan,
            physical_plan,
        )
        == []
    )
    assert (
        _g1_lifecycle_plan_reasons(
            module,
            monkeypatch,
            routing_plan=routing_plan,
            physical_plan=physical_plan,
        )
        == []
    )


@pytest.mark.parametrize("implementation", ["finalizer", "resume"])
def test_g1_full_attempt_history_ignores_only_analyzer_receipt_serialization(
    implementation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_finalizer() if implementation == "finalizer" else resume_runner
    routing_plan, physical_plan = _g1_lifecycle_receipt_serialization_plans()

    assert (
        _g1_lifecycle_plan_reasons(
            module,
            monkeypatch,
            routing_plan=routing_plan,
            physical_plan=physical_plan,
            full_attempt_history=True,
        )
        == []
    )


@pytest.mark.parametrize("implementation", ["finalizer", "resume"])
@pytest.mark.parametrize(
    "tamper",
    [
        "source",
        "model",
        "confidence",
        "task_profile_hash",
        "request_context_hash",
        "request_context",
        "analysis_content",
        "usage_tokens",
        "usage_route",
        "usage_physical_id",
        "usage_response_id",
        "usage_physical_tokens",
    ],
)
def test_g1_lifecycle_plan_rejects_analyzer_decision_binding_tamper(
    implementation: str,
    tamper: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_finalizer() if implementation == "finalizer" else resume_runner
    routing_plan, physical_plan = _g1_lifecycle_receipt_serialization_plans()
    if tamper in {"source", "model", "confidence"}:
        routing_plan["task_analyzer"][tamper] = (
            0.1 if tamper == "confidence" else f"tampered-{tamper}"
        )
    elif tamper == "request_context":
        routing_plan["request_context"]["task_text"] = "tampered prompt"
    elif tamper == "analysis_content":
        routing_plan["task_profile"]["task_type"] = "tampered"
    elif tamper == "usage_tokens":
        routing_plan["task_analyzer"]["usage"]["input_tokens"] += 1
    elif tamper == "usage_route":
        routing_plan["task_analyzer"]["usage"]["model"] = "tampered/model"
    elif tamper == "usage_physical_id":
        routing_plan["task_analyzer"]["usage"]["physical_attempts"][0]["physical_attempt_id"] = (
            "b" * 32
        )
    elif tamper == "usage_response_id":
        routing_plan["task_analyzer"]["usage"]["provider_usage"]["response_ids"] = [
            "tampered-response"
        ]
    elif tamper == "usage_physical_tokens":
        routing_plan["task_analyzer"]["usage"]["physical_attempts"][0]["input_tokens"] += 1
    else:
        routing_plan[tamper] = "0" * 64

    assert not module._g1_plans_match(routing_plan, physical_plan)
    assert not module._g1_full_plans_match(routing_plan, physical_plan)
    assert module._g1_execution_plan_mutation_reasons(
        routing_plan,
        physical_plan,
    )
    assert "g1_attempt_selection_plan_differs_from_physical_plan" in (
        _g1_lifecycle_plan_reasons(
            module,
            monkeypatch,
            routing_plan=routing_plan,
            physical_plan=physical_plan,
            full_attempt_history=True,
        )
    )


def test_resume_g1_paid_detection_uses_usage_when_request_count_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = {"decision_id": "decision-1", "selected_P": ["openrouter:model-a"]}
    usage = {
        "model_usage_breakdown": [
            {
                "role": "proposer",
                "provider": "openrouter",
                "model": "model-a",
            }
        ]
    }
    row = {
        "group": "G1",
        "task_id": "task-1",
        "final_text_sha256": "answer",
        "usage": deepcopy(usage),
        "routing_trace": {"selection_plan": deepcopy(plan)},
        "execution": {
            "generation_attempts": [
                {
                    "attempt_id": "1" * 32,
                    "attempt": 1,
                    "selection_plan": deepcopy(plan),
                    "excluded_proposer_identities": [],
                    "deterministic_proposer_failures": [],
                    "will_retry": False,
                    "run": {
                        "final_text_sha256": "answer",
                        "usage": deepcopy(usage),
                        "routing_trace": {"selection_plan": deepcopy(plan)},
                    },
                }
            ]
        },
    }
    monkeypatch.setattr(
        resume_runner,
        "g1_registry_contract_reasons",
        lambda *_args, **_kwargs: [],
    )

    _, _, reasons = resume_runner._adaptive_g1_provider_lifecycle_routing_evidence(
        row,
        g1_contract={},
        physical_plans=[deepcopy(plan)],
        initial_reasons=[],
    )

    assert "missing_g1_attempt_ensemble_trace" in reasons


def test_resume_g1_analyzer_only_paid_attempt_does_not_require_ensemble_trace() -> None:
    run = {
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

    assert resume_runner.g1_run_expected_ensemble_request_count(run) == 0

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
    unknown_run = {
        "llm_request_count": 1,
        "usage_unknown_count": 1,
        "setup_usage": [deepcopy(unknown_analyzer)],
        "usage": {"model_usage_breakdown": [deepcopy(unknown_analyzer)]},
    }
    assert resume_runner.g1_run_expected_ensemble_request_count(unknown_run) == 0
    unknown_then_generation = deepcopy(unknown_run)
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
    assert resume_runner.g1_run_expected_ensemble_request_count(unknown_then_generation) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_reasoning_retry_exhaustion_restores_the_selected_attempt_provider(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_identity = "openrouter:openai/gpt-5.6-sol"
    initial_plan = _with_g1_task_analysis_invariants(
        {
            "decision_id": "decision-1",
            "selected_P": [
                failed_identity,
                "openrouter:model-b",
                "openrouter:model-c",
            ],
            "proposer_sample_count": 3,
        }
    )
    retry_plan = _bind_g1_retry_plan(
        initial_plan,
        {
            "decision_id": "decision-2",
            "selected_P": [
                "openrouter:model-b",
                "openrouter:model-c",
                "openrouter:model-d",
            ],
            "proposer_sample_count": 3,
        },
        exclusions=[failed_identity],
    )

    class Provider:
        def __init__(self, plan: dict[str, object]) -> None:
            self.selection_plan = plan
            self.min_successful_proposers = 2

    initial = Provider(initial_plan)
    replacement = Provider(retry_plan)
    initial._draco_reasoning_only_retry_factory = lambda _excluded: replacement
    failed_trace = {
        "selection_plan": deepcopy(initial_plan),
        "candidates": [
            {
                "provider": "openrouter",
                "model": "openai/gpt-5.6-sol",
                "requested_provider": "openrouter",
                "requested_model": "openai/gpt-5.6-sol",
                "ok": False,
                "request_started": True,
                "physical_request_count": 1,
                "stop_reason": "length",
                "reasoning_tokens": 8_192,
                "effective_thinking_level": "high",
                "provider_thinking_level": "high",
                "execution": _test_proposer_execution(failed_identity),
                "content": {"text": "", "chars": 0},
            }
        ],
    }
    earlier = module.RunResult(
        final_text="earlier usable answer",
        done=DoneEvent(ensemble_trace=failed_trace),
        routing_trace={"selection_plan": deepcopy(initial_plan)},
    )
    results = iter(
        [
            earlier,
            module.RunResult(
                final_text="",
                done=DoneEvent(
                    ensemble_trace={
                        "selection_plan": deepcopy(retry_plan),
                        "candidates": [],
                    }
                ),
                error="HTTP 429",
                routing_trace={"selection_plan": deepcopy(retry_plan)},
            ),
            module.RunResult(
                final_text="",
                done=DoneEvent(
                    ensemble_trace={
                        "selection_plan": deepcopy(retry_plan),
                        "candidates": [],
                    }
                ),
                error="HTTP 429",
                routing_trace={"selection_plan": deepcopy(retry_plan)},
            ),
        ]
    )
    used_providers: list[object] = []

    async def fake_collect_run(active_provider, *_args, **_kwargs):
        used_providers.append(active_provider)
        return next(results)

    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    monkeypatch.setattr(
        module,
        "generation_retry_reason",
        lambda result, **_kwargs: result.error or "ensemble_insufficient_proposers",
    )

    selected, attempts, selected_attempt = await module.collect_generation_with_retries(
        initial,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=3,
    )

    assert selected is earlier
    assert selected_attempt == 0
    assert used_providers == [initial, replacement, replacement]
    assert len(attempts) == 3
    assert initial._draco_selected_retry_provider is initial


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_b2_reasoning_only_length_retries_same_roster(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        selection_plan = {
            "selected_P": ["openrouter:model-a", "openrouter:model-b"],
            "proposer_sample_count": 2,
        }

    provider = Provider()
    provider._draco_reasoning_only_retry_factory = lambda _excluded: (_ for _ in ()).throw(
        AssertionError("B2 must not invoke the G1 roster rebuild factory")
    )
    failed_trace = {
        "selection_plan": deepcopy(provider.selection_plan),
        "candidates": [
            {
                "provider": "openrouter",
                "model": "model-a",
                "requested_provider": "openrouter",
                "requested_model": "model-a",
                "ok": False,
                "request_started": True,
                "physical_request_count": 1,
                "stop_reason": "max_output_tokens",
                "reasoning_tokens": 8_192,
                "content": {"text": "", "chars": 0},
            }
        ],
    }
    results = iter(
        [
            module.RunResult(
                final_text="",
                done=DoneEvent(ensemble_trace=deepcopy(failed_trace)),
            ),
            module.RunResult(
                final_text="",
                done=DoneEvent(ensemble_trace=deepcopy(failed_trace)),
            ),
            module.RunResult(final_text="accepted", done=DoneEvent()),
        ]
    )
    used_providers: list[object] = []

    async def fake_collect_run(active_provider, *_args, **_kwargs):
        used_providers.append(active_provider)
        return next(results)

    retry_reasons = iter(
        [
            "ensemble_insufficient_proposers",
            "ensemble_insufficient_proposers",
            "",
        ]
    )
    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    monkeypatch.setattr(
        module,
        "generation_retry_reason",
        lambda *_args, **_kwargs: next(retry_reasons),
    )

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        provider,
        "prompt",
        timeout=30,
        group="B2",
        max_attempts=3,
    )

    assert result.final_text == "accepted"
    assert selected_attempt == 3
    assert used_providers == [provider, provider, provider]
    assert all("retry_selection_plan" not in attempt for attempt in attempts)
    assert [attempt["will_retry"] for attempt in attempts] == [True, True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    ("reset_before_third", "immutable_drift_before_third"),
    [(False, False), (True, False), (False, True)],
    ids=["prefix-persisted", "prefix-reset", "immutable-drift"],
)
async def test_g1_deterministic_then_transient_then_success_reuses_route_not_setup_usage(
    module,
    monkeypatch: pytest.MonkeyPatch,
    reset_before_third: bool,
    immutable_drift_before_third: bool,
) -> None:
    failed_identity = "openrouter:openai/gpt-5.6-sol"
    initial_plan = _with_g1_task_analysis_invariants(
        {
            "decision_id": "decision-1",
            "selected_P": [
                failed_identity,
                "openrouter:model-b",
                "openrouter:model-c",
            ],
            "proposer_sample_count": 3,
        }
    )
    initial_plan["thinking_execution_fallbacks"] = []
    retry_plan = _bind_g1_retry_plan(
        initial_plan,
        {
            "decision_id": "decision-2",
            "selected_P": [
                "openrouter:model-b",
                "openrouter:model-c",
                "openrouter:model-d",
            ],
            "proposer_sample_count": 3,
        },
        exclusions=[failed_identity],
    )
    retry_plan.update(
        {
            "selected_A": "openrouter:model-a",
            "aggregator_candidates": [
                "openrouter:model-a",
                "openrouter:model-a-fallback",
            ],
            "ranking_thinking_assignment_enabled": True,
            "executed_thinking_assignment": {
                "proposers": {identity: "high" for identity in retry_plan["selected_P"]},
                "aggregator": "high",
                "thinking_policy_version": "test-thinking-policy/v1",
            },
        }
    )
    retry_plan["thinking_assignment_details"]["aggregator_candidates"] = [
        deepcopy(retry_plan["thinking_assignment_details"]["aggregator"]),
        {
            "identity": "openrouter:model-a-fallback",
            "model_id": "model-a-fallback",
            "role": "aggregator_fallback",
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
        },
    ]
    retry_plan_baseline = deepcopy(retry_plan)

    class Provider:
        def __init__(
            self,
            plan: dict[str, object],
            *,
            reset_execution_snapshot: bool = False,
        ) -> None:
            self.selection_plan = plan
            self.execution_plan = deepcopy(plan)
            self.reset_execution_snapshot = reset_execution_snapshot
            self.min_successful_proposers = 2

        def selection_plan_execution_snapshot(self):
            return deepcopy(
                self.selection_plan if self.reset_execution_snapshot else self.execution_plan
            )

    initial = Provider(initial_plan)
    replacement = Provider(
        retry_plan,
        reset_execution_snapshot=reset_before_third,
    )
    analyzer_usage = {
        "role": "task_analyzer",
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "requested_provider": "openrouter",
        "requested_model": "anthropic/claude-opus-4.8",
        "input_tokens": 10,
        "output_tokens": 2,
        "billed_cost": 0.25,
        "cost_source": "provider_billed",
        "request_count": 1,
    }
    module.attach_provider_setup(
        initial,
        module.ProviderBuildResult(
            provider=initial,
            prompt="prompt",
            setup_latency_ms=11,
            setup_usage=[analyzer_usage],
            routing_trace={"selection_plan": deepcopy(initial_plan)},
        ),
    )
    module.attach_provider_setup(
        replacement,
        module.ProviderBuildResult(
            provider=replacement,
            prompt="prompt",
            setup_latency_ms=7,
            setup_usage=[],
            routing_trace={"selection_plan": deepcopy(retry_plan)},
        ),
    )
    initial._draco_reasoning_only_retry_factory = lambda _excluded: replacement
    failed_trace = {
        "selection_plan": deepcopy(initial_plan),
        "candidates": [
            {
                "provider": "openrouter",
                "model": "openai/gpt-5.6-sol",
                "requested_provider": "openrouter",
                "requested_model": "openai/gpt-5.6-sol",
                "ok": False,
                "request_started": True,
                "physical_request_count": 1,
                "stop_reason": "length",
                "reasoning_tokens": 8_192,
                "effective_thinking_level": "high",
                "provider_thinking_level": "high",
                "execution": _test_proposer_execution(failed_identity),
                "content": {"text": "", "chars": 0},
            }
        ],
    }
    call_number = 0

    def aggregator_execution(
        identity: str,
        level: str,
    ) -> dict[str, object]:
        provider, _, model = identity.partition(":")
        return {
            "role": "aggregator",
            "requested_provider": provider,
            "provider": provider,
            "requested_model": model,
            "model": model,
            "assigned_thinking_level": level,
            "effective_thinking_level": level,
            "provider_thinking_level": level,
            "thinking_override": level,
            "effective_thinking": True,
            "effective_provider_thinking_level": level,
            "thinking_policy_managed": True,
        }

    async def fake_collect_run(active_provider, *_args, **_kwargs):
        nonlocal call_number
        call_number += 1
        executed_plan = active_provider.selection_plan_execution_snapshot()
        recovery_attempts: list[dict[str, object]] = []
        if call_number == 2:
            executed_plan["thinking_execution_fallbacks"] = [
                {
                    "trigger_stage": "aggregator_execution",
                    "fallback_type": "thinking_level_neighbor",
                    "reason": "provider_rejected_thinking_level",
                    "identity": "openrouter:model-a-fallback",
                    "requested_thinking_level": "high",
                    "rejected_unified_level": "high",
                    "rejected_provider_level": "high",
                    "effective_thinking_level": "medium",
                    "effective_provider_level": "medium",
                    "thinking_policy_version": "test-thinking-policy/v1",
                    "fallback_result": "succeeded",
                }
            ]
            recovery_attempts = [
                {
                    "request_started": True,
                    "requested_provider": "openrouter",
                    "requested_model": "model-a-fallback",
                    "outcome": "abandoned",
                    "execution": aggregator_execution(
                        "openrouter:model-a-fallback",
                        "high",
                    ),
                },
                {
                    "request_started": True,
                    "requested_provider": "openrouter",
                    "requested_model": "model-a-fallback",
                    "outcome": "succeeded",
                    "execution": aggregator_execution(
                        "openrouter:model-a-fallback",
                        "medium",
                    ),
                },
            ]
            if not reset_before_third:
                active_provider.execution_plan = deepcopy(executed_plan)
                if immutable_drift_before_third:
                    active_provider.execution_plan["test_immutable_drift"] = True
        elif call_number == 3:
            recovery_attempts = [
                {
                    "request_started": True,
                    "requested_provider": "openrouter",
                    "requested_model": "model-a-fallback",
                    "outcome": "succeeded",
                    "execution": aggregator_execution(
                        "openrouter:model-a-fallback",
                        "medium",
                    ),
                }
            ]
        setup = module.consume_provider_setup(active_provider)
        common = {
            "setup_latency_ms": setup["latency_ms"],
            "setup_usage": setup["usage"],
            "routing_trace": setup["routing"],
        }
        if call_number == 1:
            return module.RunResult(
                final_text="",
                done=DoneEvent(ensemble_trace=deepcopy(failed_trace)),
                **common,
            )
        if call_number == 2:
            return module.RunResult(
                final_text="",
                done=DoneEvent(
                    ensemble_trace={
                        "selection_plan": executed_plan,
                        "candidates": [],
                        "aggregator_recovery": {"attempts": recovery_attempts},
                        "final_request": {
                            "role": "aggregator",
                            "request_started": True,
                            "execution": aggregator_execution(
                                "openrouter:model-a-fallback",
                                "medium",
                            ),
                        },
                    }
                ),
                error="HTTP 429",
                **common,
            )
        return module.RunResult(
            final_text="accepted",
            done=DoneEvent(
                ensemble_trace={
                    "selection_plan": executed_plan,
                    "candidates": [],
                    "aggregator_recovery": {"attempts": recovery_attempts},
                    "final_request": {
                        "role": "aggregator",
                        "request_started": True,
                        "execution": aggregator_execution(
                            "openrouter:model-a-fallback",
                            "medium",
                        ),
                    },
                }
            ),
            **common,
        )

    retry_reasons = iter(["ensemble_insufficient_proposers", "HTTP 429", ""])
    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    monkeypatch.setattr(
        module,
        "generation_retry_reason",
        lambda *_args, **_kwargs: next(retry_reasons),
    )

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        initial,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=3,
    )

    if reset_before_third:
        assert result.error == (
            "g1_thinking_execution_pre_call_guard_failed:"
            "thinking_execution_target_prefix_not_closed"
        )
        assert selected_attempt == 0
        assert call_number == 2
        assert len(attempts) == 3
        assert attempts[-1]["attempt_kind"] == ("generation_pre_call_guard")
        assert attempts[-1]["will_retry"] is False
        assert attempts[-1]["retry_suppressed_reason"] == result.error
        assert attempts[-1]["run"]["trace_events"][0]["code"] == "g1_pre_call_guard_failed"
    elif immutable_drift_before_third:
        assert result.error == "HTTP 429"
        assert selected_attempt == 0
        assert call_number == 2
        assert len(attempts) == 2
        assert attempts[-1]["will_retry"] is False
        assert attempts[-1]["retry_suppressed_reason"] == ("g1_attempt_plan_provenance_invalid")
    else:
        assert result.final_text == "accepted"
        assert selected_attempt == 3
        assert call_number == 3
        assert [attempt["selection_plan"] for attempt in attempts] == [
            initial_plan,
            retry_plan_baseline,
            replacement.execution_plan,
        ]
        assert [attempt["run"]["routing_trace"]["selection_plan"] for attempt in attempts] == [
            initial_plan,
            replacement.execution_plan,
            replacement.execution_plan,
        ]
    assert (
        attempts[1]["run"]["ensemble_trace"]["selection_plan"]["thinking_execution_fallbacks"][0][
            "fallback_result"
        ]
        == "succeeded"
    )
    expected_setup_latencies = (
        [11, 7, 0]
        if reset_before_third
        else [11, 7]
        if immutable_drift_before_third
        else [11, 7, 0]
    )
    assert [attempt["run"]["setup_latency_ms"] for attempt in attempts] == expected_setup_latencies
    analyzer_units = [
        unit
        for attempt in attempts
        for unit in attempt["run"]["usage"].get("model_usage_breakdown", [])
        if unit.get("role") == "task_analyzer"
    ]
    assert analyzer_units == [analyzer_usage]
    assert sum(
        float(attempt["run"]["usage"].get("billed_cost") or 0.0) for attempt in attempts
    ) == pytest.approx(0.25)


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_managed_mode_downgrade_is_blocked_before_first_paid_call(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        selection_plan = {
            "decision_id": "managed-decision",
            "ranking_thinking_assignment_enabled": True,
        }

        def selection_plan_execution_snapshot(self):
            return {
                **self.selection_plan,
                "ranking_thinking_assignment_enabled": False,
            }

    calls = 0

    async def unexpected_collect_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("managed mode drift must stop before a paid call")

    monkeypatch.setattr(module, "collect_run", unexpected_collect_run)

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        Provider(),
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
    )

    assert calls == 0
    assert selected_attempt == 0
    assert result.error == ("g1_thinking_execution_managed_mode_changed_before_call")
    assert len(attempts) == 1
    assert attempts[0]["attempt_kind"] == ("generation_pre_call_guard")
    assert attempts[0]["attempt"] == 1
    assert attempts[0]["run"]["llm_request_count"] == 0
    assert attempts[0]["run"]["trace_events"][0]["code"] == "g1_pre_call_guard_failed"


@pytest.mark.parametrize(
    "module",
    [runner, resume_runner],
    ids=["main", "resume"],
)
def test_attach_provider_setup_wraps_paid_snapshot_failure_without_text(
    module,
) -> None:
    plan = _frozen_g1_plan(
        decision_id="attach-paid-setup-snapshot-error",
        proposers=["openrouter:model-p"],
    )

    class Provider:
        selection_plan = deepcopy(plan)

        def selection_plan_execution_snapshot(self):
            raise RuntimeError("private upstream diagnostic must not be persisted")

    paid_usage = {
        "role": "task_analyzer",
        "label": "task_analyzer",
        "request_count": 1,
        "physical_attempt_id": "e" * 32,
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "requested_provider": "openrouter",
        "requested_model": "anthropic/claude-opus-4.8",
        "input_tokens": 10,
        "output_tokens": 2,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": {
            "physical_attempt_id": "e" * 32,
        },
    }
    provider = Provider()
    with pytest.raises(module.ProviderBuildError) as exc_info:
        module.attach_provider_setup(
            provider,
            module.ProviderBuildResult(
                provider=provider,
                prompt="prompt",
                setup_latency_ms=29,
                setup_usage=[paid_usage],
                routing_trace={"selection_plan": deepcopy(plan)},
            ),
        )

    error = exc_info.value
    assert str(error) == ("provider_build_failed_after_setup:RuntimeError")
    assert "private upstream diagnostic" not in str(error)
    assert error.setup_latency_ms == 29
    assert error.setup_usage == [paid_usage]
    assert error.routing_trace == {
        "selection_plan": plan,
    }


@pytest.mark.parametrize(
    "module",
    [runner, resume_runner],
    ids=["main", "resume"],
)
def test_paid_provider_build_post_setup_initialization_is_protected(
    module,
) -> None:
    tree = ast.parse(inspect.getsource(module.build_experiment_provider))
    protected = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        body_source = "\n".join(ast.unparse(statement) for statement in node.body)
        if "attach_provider_setup(provider, result)" in body_source:
            protected.append(node)

    assert len(protected) == 1
    handlers_source = "\n".join(ast.unparse(handler) for handler in protected[0].handlers)
    assert "except ProviderBuildError" in handlers_source
    assert "if setup_usage" in handlers_source
    assert "raise ProviderBuildError" in handlers_source
    assert "safe_provider_build_routing_trace" in handlers_source


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_managed_pre_call_guard_preserves_paid_setup_evidence(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _frozen_g1_plan(
        decision_id="managed-paid-setup",
        proposers=["openrouter:model-p"],
    )

    class Provider:
        selection_plan = deepcopy(plan)
        downgraded = False

        def selection_plan_execution_snapshot(self):
            snapshot = deepcopy(self.selection_plan)
            if self.downgraded:
                snapshot["ranking_thinking_assignment_enabled"] = False
            return snapshot

    paid_usage = {
        "role": "task_analyzer",
        "label": "task_analyzer",
        "request_count": 1,
        "physical_attempt_id": "a" * 32,
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "requested_provider": "openrouter",
        "requested_model": "anthropic/claude-opus-4.8",
        "input_tokens": 10,
        "output_tokens": 2,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": {"physical_attempt_id": "a" * 32},
    }
    provider = Provider()
    module.attach_provider_setup(
        provider,
        module.ProviderBuildResult(
            provider=provider,
            prompt="prompt",
            setup_latency_ms=17,
            setup_usage=[paid_usage],
            routing_trace={"selection_plan": deepcopy(plan)},
        ),
    )
    provider.downgraded = True
    calls = 0

    async def unexpected_collect_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("managed mode drift must stop before generation")

    monkeypatch.setattr(module, "collect_run", unexpected_collect_run)

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        provider,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
    )

    assert calls == 0
    assert selected_attempt == 0
    assert result.error == ("g1_thinking_execution_managed_mode_changed_before_call")
    assert result.setup_latency_ms == 17
    assert result.setup_usage == [paid_usage]
    assert module.consume_provider_setup(provider)["usage"] == []
    assert len(attempts) == 1
    assert attempts[0]["attempt_kind"] == "provider_build_after_paid_setup"
    assert attempts[0]["attempt"] == 1
    assert attempts[0]["will_retry"] is False
    assert attempts[0]["retry_suppressed_reason"] == result.error
    assert attempts[0]["selection_plan"] == plan
    assert attempts[0]["excluded_proposer_identities"] == []
    assert attempts[0]["deterministic_proposer_failures"] == []
    assert attempts[0]["run"]["llm_request_count"] == 1
    assert attempts[0]["run"]["routing_trace"]["selection_plan"] == plan
    guard_trace = attempts[0]["run"]["routing_trace"]["pre_call_guard"]
    assert guard_trace["error"] == result.error
    assert guard_trace["expected_selection_plan"] == plan
    assert guard_trace["observed_selection_plan"]["ranking_thinking_assignment_enabled"] is False
    assert guard_trace["request_started"] is False
    assert guard_trace["physical_request_count"] == 0
    assert attempts[0]["run"]["usage"]["model_usage_breakdown"] == [paid_usage]


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_managed_pre_call_guard_snapshot_exception_preserves_paid_setup(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _frozen_g1_plan(
        decision_id="managed-paid-setup-snapshot-error",
        proposers=["openrouter:model-p"],
    )

    class Provider:
        selection_plan = deepcopy(plan)
        fail_snapshot = False

        def selection_plan_execution_snapshot(self):
            if self.fail_snapshot:
                raise RuntimeError("do not include exception text in evidence")
            return deepcopy(self.selection_plan)

    paid_usage = {
        "role": "task_analyzer",
        "label": "task_analyzer",
        "request_count": 1,
        "physical_attempt_id": "b" * 32,
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "requested_provider": "openrouter",
        "requested_model": "anthropic/claude-opus-4.8",
        "input_tokens": 10,
        "output_tokens": 2,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": {"physical_attempt_id": "b" * 32},
    }
    provider = Provider()
    module.attach_provider_setup(
        provider,
        module.ProviderBuildResult(
            provider=provider,
            prompt="prompt",
            setup_latency_ms=17,
            setup_usage=[paid_usage],
            routing_trace={"selection_plan": deepcopy(plan)},
        ),
    )
    provider.fail_snapshot = True
    calls = 0

    async def unexpected_collect_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("snapshot failure must stop before generation")

    monkeypatch.setattr(module, "collect_run", unexpected_collect_run)

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        provider,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
    )

    assert calls == 0
    assert selected_attempt == 0
    assert result.error == (
        "g1_thinking_execution_pre_call_guard_exception:selection_plan_snapshot:RuntimeError"
    )
    assert result.setup_latency_ms == 17
    assert result.setup_usage == [paid_usage]
    assert len(attempts) == 1
    assert attempts[0]["attempt_kind"] == "provider_build_after_paid_setup"
    assert attempts[0]["selection_plan"] == plan
    assert attempts[0]["run"]["routing_trace"]["selection_plan"] == plan
    guard_trace = attempts[0]["run"]["routing_trace"]["pre_call_guard"]
    assert guard_trace["error"] == result.error
    assert guard_trace["expected_selection_plan"] == plan
    assert guard_trace["observed_selection_plan"] is None
    assert guard_trace["observed_selection_plan_error"] == "RuntimeError"
    assert "do not include exception text" not in str(guard_trace)
    assert guard_trace["request_started"] is False
    assert guard_trace["physical_request_count"] == 0
    assert attempts[0]["run"]["usage"]["model_usage_breakdown"] == [paid_usage]


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize("failure_mode", ["setup_copy", "serializer"])
async def test_managed_pre_call_guard_capture_failure_preserves_paid_receipt(
    module,
    failure_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _frozen_g1_plan(
        decision_id="managed-paid-setup-copy-error",
        proposers=["openrouter:model-p"],
    )

    class Provider:
        selection_plan = deepcopy(plan)
        fail_snapshot = False

        def selection_plan_execution_snapshot(self):
            snapshot = deepcopy(self.selection_plan)
            if self.fail_snapshot:
                snapshot["ranking_thinking_assignment_enabled"] = False
            return snapshot

    paid_usage = {
        "role": "task_analyzer",
        "label": "task_analyzer",
        "request_count": 1,
        "attempt": 1,
        "physical_attempt_id": "c" * 32,
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "requested_provider": "openrouter",
        "requested_model": "anthropic/claude-opus-4.8",
        "input_tokens": 10,
        "output_tokens": 2,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": {"physical_attempt_id": "c" * 32},
    }
    provider = Provider()
    module.attach_provider_setup(
        provider,
        module.ProviderBuildResult(
            provider=provider,
            prompt="prompt",
            setup_latency_ms=29,
            setup_usage=[paid_usage],
            routing_trace={"selection_plan": deepcopy(plan)},
        ),
    )
    provider.fail_snapshot = True
    setup_metrics = getattr(provider, "_draco_setup_metrics")
    raw_setup_usage = setup_metrics["usage"]
    original_deepcopy = module.copy.deepcopy
    private_detail = f"private paid setup {failure_mode} detail"

    def fail_only_for_setup_usage(value, *args, **kwargs):
        if value is raw_setup_usage:
            raise RuntimeError(private_detail)
        return original_deepcopy(value, *args, **kwargs)

    if failure_mode == "setup_copy":
        monkeypatch.setattr(
            module.copy,
            "deepcopy",
            fail_only_for_setup_usage,
        )
    else:
        monkeypatch.setattr(
            module,
            "json_safe",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(private_detail)),
        )
    generation_calls = 0

    async def unexpected_collect_run(*_args, **_kwargs):
        nonlocal generation_calls
        generation_calls += 1
        raise AssertionError("setup copy failure must stop before generation")

    monkeypatch.setattr(module, "collect_run", unexpected_collect_run)

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        provider,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
    )

    assert generation_calls == 0
    assert selected_attempt == 0
    assert len(attempts) == 1
    assert attempts[0]["attempt_kind"] == "provider_build_after_paid_setup"
    assert result.error == ("g1_thinking_execution_managed_mode_changed_before_call")
    assert len(result.setup_usage) == 1
    receipt = result.setup_usage[0]
    assert receipt["request_count"] == 1
    assert receipt["physical_attempt_id"] == "c" * 32
    assert receipt["billed_cost"] == 0.01
    guard_trace = attempts[0]["run"]["routing_trace"]["pre_call_guard"]
    assert guard_trace["setup_snapshot_error"] == (
        "RuntimeError" if failure_mode == "setup_copy" else ""
    )
    if failure_mode == "serializer":
        assert guard_trace["expected_selection_plan"] == {
            "capture_failed": True,
            "exception_type": "RuntimeError",
        }
    assert private_detail not in json.dumps(attempts)
    assert getattr(provider, "_draco_setup_metrics") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_managed_pre_call_guard_summary_failure_keeps_paid_setup_until_commit(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _frozen_g1_plan(
        decision_id="managed-paid-setup-summary-error",
        proposers=["openrouter:model-p"],
    )

    class Provider:
        selection_plan = deepcopy(plan)
        fail_snapshot = False

        def selection_plan_execution_snapshot(self):
            snapshot = deepcopy(self.selection_plan)
            if self.fail_snapshot:
                snapshot["ranking_thinking_assignment_enabled"] = False
            return snapshot

    paid_usage = {
        "role": "task_analyzer",
        "label": "task_analyzer",
        "request_count": 1,
        "physical_attempt_id": "f" * 32,
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "requested_provider": "openrouter",
        "requested_model": "anthropic/claude-opus-4.8",
        "input_tokens": 10,
        "output_tokens": 2,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": {"physical_attempt_id": "f" * 32},
    }
    provider = Provider()
    module.attach_provider_setup(
        provider,
        module.ProviderBuildResult(
            provider=provider,
            prompt="prompt",
            setup_latency_ms=23,
            setup_usage=[paid_usage],
            routing_trace={"selection_plan": deepcopy(plan)},
        ),
    )
    provider.fail_snapshot = True
    monkeypatch.setattr(
        module,
        "run_result_summary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private summary detail")),
    )

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        provider,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
    )

    assert selected_attempt == 0
    assert result.error == ("g1_thinking_execution_managed_mode_changed_before_call")
    assert len(attempts) == 1
    run = attempts[0]["run"]
    assert run["llm_request_count"] == 1
    assert run["usage"]["model_usage_breakdown"] == [paid_usage]
    assert run["generation_postprocessing_failure"]["stage"] == "g1_pre_call_guard_evidence"
    assert "private summary detail" not in json.dumps(attempts)
    assert module.consume_provider_setup(provider)["usage"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_managed_pre_call_guard_closure_exception_preserves_paid_setup(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _frozen_g1_plan(
        decision_id="managed-paid-setup-closure-error",
        proposers=["openrouter:model-p"],
    )

    class Provider:
        selection_plan = deepcopy(plan)

        def selection_plan_execution_snapshot(self):
            return deepcopy(self.selection_plan)

    paid_usage = {
        "role": "task_analyzer",
        "label": "task_analyzer",
        "request_count": 1,
        "physical_attempt_id": "c" * 32,
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "requested_provider": "openrouter",
        "requested_model": "anthropic/claude-opus-4.8",
        "input_tokens": 10,
        "output_tokens": 2,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": {"physical_attempt_id": "c" * 32},
    }
    provider = Provider()
    module.attach_provider_setup(
        provider,
        module.ProviderBuildResult(
            provider=provider,
            prompt="prompt",
            setup_latency_ms=19,
            setup_usage=[paid_usage],
            routing_trace={"selection_plan": deepcopy(plan)},
        ),
    )

    def fail_closure(*_args, **_kwargs):
        raise RuntimeError("private closure failure text")

    monkeypatch.setattr(
        "opensquilla.provider.thinking_execution.validate_thinking_execution_history_closure",
        fail_closure,
    )
    calls = 0

    async def unexpected_collect_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("closure failure must stop before generation")

    monkeypatch.setattr(module, "collect_run", unexpected_collect_run)

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        provider,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
    )

    assert calls == 0
    assert selected_attempt == 0
    assert result.error == (
        "g1_thinking_execution_pre_call_guard_exception:history_closure:RuntimeError"
    )
    assert result.setup_usage == [paid_usage]
    assert len(attempts) == 1
    assert attempts[0]["attempt_kind"] == ("provider_build_after_paid_setup")
    assert "private closure failure text" not in str(attempts)


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_managed_pre_call_guard_hash_exception_preserves_paid_setup(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _frozen_g1_plan(
        decision_id="managed-paid-setup-hash-error",
        proposers=["openrouter:model-p"],
    )

    class Provider:
        selection_plan = deepcopy(plan)

        def selection_plan_execution_snapshot(self):
            return deepcopy(self.selection_plan)

    paid_usage = {
        "role": "task_analyzer",
        "label": "task_analyzer",
        "request_count": 1,
        "physical_attempt_id": "d" * 32,
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "requested_provider": "openrouter",
        "requested_model": "anthropic/claude-opus-4.8",
        "input_tokens": 10,
        "output_tokens": 2,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": {"physical_attempt_id": "d" * 32},
    }
    provider = Provider()
    module.attach_provider_setup(
        provider,
        module.ProviderBuildResult(
            provider=provider,
            prompt="prompt",
            setup_latency_ms=23,
            setup_usage=[paid_usage],
            routing_trace={"selection_plan": deepcopy(plan)},
        ),
    )
    calls = 0

    async def retrying_collect_run(active_provider, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        setup = module.consume_provider_setup(active_provider)
        return module.RunResult(
            final_text="",
            done=DoneEvent(
                ensemble_trace={
                    "selection_plan": deepcopy(plan),
                    "candidates": [],
                }
            ),
            error="transient",
            setup_latency_ms=module.coerce_metric_int(setup.get("latency_ms")),
            setup_usage=deepcopy(setup.get("usage") or []),
            routing_trace=deepcopy(setup.get("routing") or {"selection_plan": deepcopy(plan)}),
        )

    monkeypatch.setattr(module, "collect_run", retrying_collect_run)
    monkeypatch.setattr(
        module,
        "generation_retry_reason",
        lambda *_args, **_kwargs: "transient",
    )

    original_hash = module.canonical_json_sha256
    hash_calls = 0

    def fail_hash(value):
        nonlocal hash_calls
        hash_calls += 1
        if hash_calls == 4:
            raise RuntimeError("private hash failure text")
        return original_hash(value)

    monkeypatch.setattr(module, "canonical_json_sha256", fail_hash)

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        provider,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
    )

    assert calls == 1
    assert selected_attempt == 0
    assert result.error == (
        "g1_thinking_execution_pre_call_guard_exception:immutable_hash:RuntimeError"
    )
    assert result.setup_usage == []
    assert [attempt["attempt_kind"] for attempt in attempts] == [
        "generation",
        "generation_pre_call_guard",
    ]
    assert paid_usage in attempts[0]["run"]["usage"]["model_usage_breakdown"]
    assert attempts[1]["attempt"] == 2
    assert attempts[1]["run"]["llm_request_count"] == 0
    assert attempts[1]["run"]["routing_trace"]["pre_call_guard"]["error"] == result.error
    assert attempts[1]["run"]["trace_events"][0]["code"] == "g1_pre_call_guard_failed"
    assert "private hash failure text" not in str(attempts)


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_successful_managed_result_with_tampered_plan_is_not_accepted(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _frozen_g1_plan(
        decision_id="managed-success",
        proposers=["openrouter:model-p"],
    )

    class Provider:
        selection_plan = deepcopy(plan)

        def selection_plan_execution_snapshot(self):
            return deepcopy(self.selection_plan)

    tampered = deepcopy(plan)
    tampered["selected_A"] = "openrouter:tampered"
    calls = 0

    async def fake_collect_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return module.RunResult(
            final_text="apparently successful",
            done=DoneEvent(
                ensemble_trace={
                    "selection_plan": deepcopy(tampered),
                    "candidates": [],
                }
            ),
            routing_trace={"selection_plan": deepcopy(tampered)},
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    monkeypatch.setattr(
        module,
        "generation_retry_reason",
        lambda *_args, **_kwargs: "",
    )

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        Provider(),
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
    )

    assert calls == 1
    assert selected_attempt == 0
    assert len(attempts) == 1
    assert attempts[0]["will_retry"] is False
    assert attempts[0]["retry_suppressed_reason"] == ("g1_attempt_plan_provenance_invalid")
    assert result.error == "g1_attempt_plan_provenance_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize("switch_value", [None, False], ids=["missing", "false"])
async def test_transient_generation_failure_retries_same_roster_without_rebuild(
    module,
    monkeypatch: pytest.MonkeyPatch,
    switch_value: bool | None,
) -> None:
    class Provider:
        selection_plan = {
            "selected_P": [
                "openrouter:model-a",
                "openrouter:model-b",
                "openrouter:model-c",
            ],
            "proposer_sample_count": 3,
        }
        min_successful_proposers = 2

        @property
        def _draco_reasoning_only_retry_factory(self):
            raise AssertionError("default-off retries must not inspect the managed factory")

        def selection_plan_execution_snapshot(self):
            raise AssertionError("default-off retries must use the frozen legacy plan")

    provider = Provider()
    if switch_value is not None:
        provider.selection_plan = {
            **provider.selection_plan,
            "ranking_thinking_assignment_enabled": switch_value,
        }

    provider._draco_prior_excluded_proposer_identities = object()
    provider._draco_g1_thinking_execution_history = object()
    calls: list[object] = []

    async def fake_collect_run(active_provider, *_args, **_kwargs):
        calls.append(active_provider)
        return module.RunResult(
            final_text="",
            done=DoneEvent(
                ensemble_trace={
                    "selection_plan": deepcopy(active_provider.selection_plan),
                    "candidates": [],
                }
            ),
            error="HTTP 429: rate limited",
            routing_trace={"selection_plan": deepcopy(active_provider.selection_plan)},
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        provider,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
    )

    assert result.error == "HTTP 429: rate limited"
    assert selected_attempt == 0
    assert calls == [provider, provider]
    assert len(attempts) == 2
    assert all(
        all(
            field not in attempt
            for field in (
                "selection_plan",
                "deterministic_proposer_failures",
                "excluded_proposer_identities",
                "retry_selection_plan",
            )
        )
        for attempt in attempts
    )
    assert not hasattr(provider, "_draco_selected_retry_provider")


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_invalid_native_recovery_roster_is_structured_pre_call_failure(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _k21_router_dynamic_plan()
    plan["backup_P"] = [plan["selected_P"][0], "openrouter:b1"]
    assert module.g1_provider_native_recovery_policy_reason(plan) == (
        "invalid_g1_proposer_recovery_roster"
    )

    class Provider:
        selection_plan = deepcopy(plan)

    async def unexpected_paid_call(*_args, **_kwargs):
        raise AssertionError("an invalid recovery roster must fail before a paid call")

    monkeypatch.setattr(module, "collect_run", unexpected_paid_call)
    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        Provider(),
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
    )

    assert selected_attempt == 0
    assert result.error == "invalid_g1_proposer_recovery_roster"
    assert result.done is None
    assert len(attempts) == 1
    assert attempts[0]["attempt_kind"] == "generation_pre_call_guard"
    assert attempts[0]["retry_reason"] == result.error
    assert attempts[0]["retry_suppressed_reason"] == result.error
    assert attempts[0]["will_retry"] is False
    assert attempts[0]["run"]["llm_request_count"] == 0
    assert attempts[0]["run"]["routing_trace"]["pre_call_guard"]["error"] == (
        result.error
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_g1_missing_paid_physical_trace_blocks_next_paid_call(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _with_g1_task_analysis_invariants(
        {
            "decision_id": "decision-1",
            "selected_P": [
                "openrouter:model-a",
                "openrouter:model-b",
                "openrouter:model-c",
            ],
            "proposer_sample_count": 3,
        }
    )

    class Provider:
        selection_plan = plan
        min_successful_proposers = 2

    provider = Provider()
    paid_calls = 0

    async def fake_collect_run(*_args, **_kwargs):
        nonlocal paid_calls
        paid_calls += 1
        if paid_calls > 1:
            raise AssertionError("missing physical trace must block a second paid call")
        return module.RunResult(
            final_text="",
            done=DoneEvent(),
            error="HTTP 503",
            routing_trace={"selection_plan": deepcopy(plan)},
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    monkeypatch.setattr(
        module,
        "generation_retry_reason",
        lambda *_args, **_kwargs: "HTTP 503",
    )

    _, attempts, selected_attempt = await module.collect_generation_with_retries(
        provider,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
    )

    assert paid_calls == 1
    assert selected_attempt == 0
    assert attempts[0]["will_retry"] is False
    assert attempts[0]["retry_suppressed_reason"] == ("g1_attempt_plan_provenance_invalid")


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_g1_physical_counter_mismatch_blocks_next_paid_call(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider.thinking_execution import (
        THINKING_PHYSICAL_EVIDENCE_SCHEMA,
    )

    plan = _with_g1_task_analysis_invariants(
        {
            "decision_id": "strict-physical-binding",
            "selected_P": ["openrouter:model-a"],
            "proposer_sample_count": 1,
            "thinking_physical_evidence_schema": (THINKING_PHYSICAL_EVIDENCE_SCHEMA),
        }
    )

    class Provider:
        selection_plan = plan
        min_successful_proposers = 1

    paid_calls = 0

    async def fake_collect_run(*_args, **_kwargs):
        nonlocal paid_calls
        paid_calls += 1
        if paid_calls > 1:
            raise AssertionError("physical Counter mismatch must block a second paid call")
        return module.RunResult(
            final_text="",
            done=DoneEvent(
                model_usage_breakdown=[
                    {
                        "role": "proposer",
                        "physical_attempt_id": "b" * 32,
                        "provider": "openrouter",
                        "model": "model-a",
                        "requested_provider": "openrouter",
                        "requested_model": "model-a",
                        "input_tokens": 1,
                        "output_tokens": 0,
                        "billed_cost": 0.0,
                        "cost_source": "none",
                        "provider_usage": {
                            "physical_attempt_id": "b" * 32,
                        },
                    }
                ],
                ensemble_trace={
                    "selection_plan": deepcopy(plan),
                    "candidates": [
                        {
                            "request_started": True,
                            "execution": {
                                "physical_attempts": [
                                    {
                                        "request_started": True,
                                        "physical_attempt_id": "a" * 32,
                                    }
                                ]
                            },
                        }
                    ],
                    "aggregator_recovery": {"attempts": []},
                },
            ),
            error="HTTP 503",
            routing_trace={"selection_plan": deepcopy(plan)},
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    monkeypatch.setattr(
        module,
        "generation_retry_reason",
        lambda *_args, **_kwargs: "HTTP 503",
    )
    monkeypatch.setattr(
        module,
        "g1_attempt_plan_consistency_reason",
        lambda *_args, **_kwargs: "",
    )

    _, attempts, selected_attempt = await module.collect_generation_with_retries(
        Provider(),
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
    )

    assert paid_calls == 1
    assert selected_attempt == 0
    assert attempts[0]["will_retry"] is False
    assert attempts[0]["retry_suppressed_reason"] == (
        "g1_physical_usage_binding_failed:g1_thinking_physical_usage_set_mismatch"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    (
        "managed",
        "usage_attempt_id",
        "ledger_attempt_id",
        "expected_selected",
    ),
    [
        (True, "a" * 32, "b" * 32, 0),
        (True, "a" * 32, "a" * 32, 1),
        (False, "", "", 1),
    ],
    ids=[
        "managed-success-mismatch-blocked",
        "managed-success-matched",
        "default-off-unchanged",
    ],
)
async def test_managed_g1_audits_success_before_judge_or_acceptance(
    module,
    managed: bool,
    usage_attempt_id: str,
    ledger_attempt_id: str,
    expected_selected: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider.thinking_execution import (
        THINKING_PHYSICAL_EVIDENCE_SCHEMA,
    )

    plan = {
        "decision_id": "strict-success-audit",
        "selected_P": ["openrouter:model-a"],
        "proposer_sample_count": 1,
        "ranking_thinking_assignment_enabled": managed,
    }
    if managed:
        plan = _with_g1_task_analysis_invariants(
            {
                **plan,
                "thinking_physical_evidence_schema": (THINKING_PHYSICAL_EVIDENCE_SCHEMA),
            }
        )

    class Provider:
        selection_plan = plan
        min_successful_proposers = 1

    async def fake_collect_run(*_args, **_kwargs):
        if not managed:
            return module.RunResult(
                final_text="legacy accepted answer",
                done=DoneEvent(
                    ensemble_trace={
                        "selection_plan": deepcopy(plan),
                        "candidates": [],
                    },
                ),
                routing_trace={"selection_plan": deepcopy(plan)},
            )
        usage_row = {
            "role": "proposer",
            "physical_attempt_id": usage_attempt_id,
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": 1,
            "output_tokens": 1,
            "billed_cost": 0.0,
            "cost_source": "none",
            "provider_usage": {
                "physical_attempt_id": usage_attempt_id,
            },
        }
        return module.RunResult(
            final_text="accepted answer",
            done=DoneEvent(
                model_usage_breakdown=[usage_row],
                ensemble_trace={
                    "selection_plan": deepcopy(plan),
                    "candidates": [
                        {
                            "request_started": True,
                            "execution": {
                                "physical_attempts": [
                                    {
                                        "request_started": True,
                                        "physical_attempt_id": (ledger_attempt_id),
                                    }
                                ]
                            },
                        }
                    ],
                    "aggregator_recovery": {"attempts": []},
                },
            ),
            routing_trace={"selection_plan": deepcopy(plan)},
        )

    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    monkeypatch.setattr(
        module,
        "generation_retry_reason",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        module,
        "g1_attempt_plan_consistency_reason",
        lambda *_args, **_kwargs: "",
    )

    result, attempts, selected_attempt = await module.collect_generation_with_retries(
        Provider(),
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=1,
    )

    assert selected_attempt == expected_selected
    assert len(attempts) == 1
    if managed and usage_attempt_id != ledger_attempt_id:
        expected_error = "g1_physical_usage_binding_failed:g1_thinking_physical_usage_set_mismatch"
        assert result.error == expected_error
        assert attempts[0]["retry_reason"] == expected_error
        assert attempts[0]["retry_suppressed_reason"] == expected_error
        assert attempts[0]["will_retry"] is False
    else:
        assert result.error == ""
        assert attempts[0]["retry_reason"] == ""
        assert attempts[0]["retry_suppressed_reason"] == ""
        if not managed:
            assert "selection_plan" not in attempts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    "thinking_assignment_enabled",
    [True, False],
    ids=["managed", "default-off-missing"],
)
@pytest.mark.parametrize(
    ("explicit_analyzer_chain", "common_single_route"),
    [(True, False), (False, False), (False, True)],
    ids=["explicit-chain", "common-chain", "common-single-chain"],
)
async def test_g1_provider_native_recovery_runs_task_analysis_once(
    module,
    thinking_assignment_enabled: bool,
    explicit_analyzer_chain: bool,
    common_single_route: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider.ranking_router import (
        TaskAnalysisResult,
        _legacy_registry_snapshot_projection,
        fallback_task_profile,
        load_model_registry_snapshot,
        ranking_config_snapshot,
    )

    experiment = _experiment_config()
    current_registry = load_model_registry_snapshot()
    if not thinking_assignment_enabled:
        current_registry = _legacy_registry_snapshot_projection(current_registry)
    current_ranking = ranking_config_snapshot(
        thinking_assignment_enabled=thinking_assignment_enabled,
    )
    experiment_payload = experiment.model_dump(mode="json")
    if not explicit_analyzer_chain:
        experiment_payload["g1_routing"].pop("live_task_analyzer_chain")
        experiment_payload["g1_routing"].pop("analyzer_failure_fallback_ensemble")
    if common_single_route:
        experiment_payload["router_dynamic_ranking_override"] = {
            "task_analyzer": {"fallback_chain": []}
        }
    experiment_payload["ensemble"].update(
        {
            "shuffle_candidates": True,
            "candidate_order_seed": 0,
        }
    )
    experiment_payload["g1_routing"].update(
        {
            "source_registry_snapshot_version": current_registry["snapshot_version"],
            "expected_source_registry_snapshot_sha256": (
                module.canonical_json_sha256(current_registry).removeprefix("sha256:")
            ),
            "expected_ranking_config_schema_version": current_ranking["schema_version"],
            "expected_ranking_config_version": current_ranking["config_version"],
            "expected_ranking_config_sha256": (
                module.canonical_json_sha256(current_ranking).removeprefix("sha256:")
            ),
        }
    )
    experiment = type(experiment).model_validate(experiment_payload)
    ensemble_config: dict[str, object] = {
        "enabled": True,
        "selection_mode": "router_dynamic",
    }
    if common_single_route:
        ensemble_config["ranking_config_override"] = {
            "task_analyzer": {"fallback_chain": []}
        }
    if thinking_assignment_enabled:
        ensemble_config["ranking_thinking_assignment_enabled"] = True
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble=ensemble_config,
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
    )
    contract = _resolved_g1_registry_contract(module, experiment, config)
    analyzer_calls = 0
    captured_analyzer_routes: list[tuple[str, str, str]] = []
    captured_schema_repair_max_retries: list[int | None] = []

    async def fake_run_pipeline(turn, _steps):
        turn.model = inherited.model
        turn.metadata.update(
            {
                "routed_tier": "c1",
                "routing_confidence": 0.9,
                "routing_source": "test",
                "routing_applied": False,
            }
        )
        return turn

    async def fake_analyze_task_with_fallback_chain(**kwargs):
        nonlocal analyzer_calls
        analyzer_calls += 1
        captured_schema_repair_max_retries.append(kwargs["schema_repair_max_retries"])
        candidates = kwargs["candidates"]
        captured_analyzer_routes.extend(
            (
                candidate.provider_id,
                candidate.model_id,
                candidate.upstream_provider,
            )
            for candidate in candidates
        )
        analyzer_provider = candidates[0].provider_id
        analyzer_model = candidates[0].model_id
        return TaskAnalysisResult(
            profile=fallback_task_profile(
                routed_tier=kwargs["routed_tier"],
                request_context=kwargs["request_context"],
                ranking_config=kwargs["ranking_config"],
            ),
            source="llm_provider",
            schema_valid=True,
            confidence=0.9,
            usage={
                "provider": analyzer_provider,
                "model": analyzer_model,
                "requested_provider": analyzer_provider,
                "requested_model": analyzer_model,
                "input_tokens": 5,
                "output_tokens": 2,
                "attempt_count": 1,
                "physical_attempts": [
                    {
                        "attempt": 1,
                        "physical_attempt_id": "d" * 32,
                        "provider": analyzer_provider,
                        "model": analyzer_model,
                        "requested_provider": analyzer_provider,
                        "requested_model": analyzer_model,
                        "input_tokens": 5,
                        "output_tokens": 2,
                        "reasoning_tokens": 0,
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                        "billed_cost": 0.0,
                        "provider_usage": {
                            "provider": analyzer_provider,
                            "model": analyzer_model,
                            "physical_attempt_id": "d" * 32,
                        },
                    }
                ],
            },
            provider_id=str(analyzer_provider),
            model_id=str(analyzer_model),
            chain_trace={
                "protocol": "opensquilla.task-analyzer-fallback-chain/v1",
                "configured_routes": [
                    {
                        "provider": candidate.provider_id,
                        "model": candidate.model_id,
                        "upstream_provider": candidate.upstream_provider,
                    }
                    for candidate in candidates
                ],
                "attempt_outcomes": [
                    {
                        "candidate_index": 0,
                        "provider": analyzer_provider,
                        "model": analyzer_model,
                        "upstream_provider": candidates[0].upstream_provider,
                        "outcome": "success",
                        "reason": "",
                        "physical_request_count": 1,
                    }
                ],
                "selected_index": 0,
                "exhausted": False,
            },
        )

    monkeypatch.setattr(module, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_fallback_chain",
        fake_analyze_task_with_fallback_chain,
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "1")

    build = await module.build_experiment_provider(
        config=config,
        inherited=inherited,
        group="G1",
        prompt="test prompt",
        dry_run=False,
        enable_proposer_tools=False,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        experiment_config=experiment,
        g1_registry_contract=contract,
        generation_policy={},
    )
    plan = build.provider.selection_plan
    managed_fields = (
        "ranking_thinking_assignment_enabled",
        "thinking_assignment",
        "thinking_assignment_details",
        "executed_thinking_assignment",
        "thinking_execution_fallbacks",
    )
    if thinking_assignment_enabled:
        assert plan["ranking_thinking_assignment_enabled"] is True
    else:
        assert all(field not in plan for field in managed_fields)

    assert analyzer_calls == 1
    expected_analyzer_routes = [
        ("openrouter", "anthropic/claude-opus-4.8", "anthropic"),
        ("openrouter", "openai/gpt-5.6-sol", "azure"),
        (
            "openrouter",
            "google/gemini-3.1-pro-preview",
            "google-ai-studio",
        ),
    ]
    if common_single_route:
        expected_analyzer_routes = expected_analyzer_routes[:1]
    assert captured_analyzer_routes == expected_analyzer_routes
    assert captured_schema_repair_max_retries == [
        0 if explicit_analyzer_chain else None
    ]
    assert not hasattr(
        build.provider,
        "_draco_reasoning_only_retry_factory",
    )
    assert plan["proposer_recovery_policy"] == (module.FORMAL_PROPOSER_RECOVERY_POLICY)
    assert len(plan["backup_P"]) == 2
    assert plan["configured_proposer_backup_count"] == 2
    assert plan["effective_proposer_backup_count"] == 2
    assert plan["effective_min_successful_proposers"] == 2
    assert build.provider.min_successful_proposers == 2
    assert build.provider.shuffle_candidates is True
    assert build.provider.candidate_order_seed == 0
    assert plan["configured_candidate_order_seed"] == 0
    assert plan["effective_candidate_order_seed"] == 0

    setup = module.consume_provider_setup(build.provider)
    assert setup["usage"] == build.setup_usage
    assert sum(int(unit.get("request_count") or 0) for unit in setup["usage"]) == 1
    assert module.consume_provider_setup(build.provider).get("usage", []) == []
    if not thinking_assignment_enabled:
        assert "thinking_execution_projection" not in setup["routing"]


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_g1_exhausted_analyzer_chain_materializes_fixed_complete_only_fallback(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider.ranking_router import (
        TaskAnalysisResult,
        fallback_task_profile,
    )

    experiment = _experiment_with_current_g1_contract(
        module,
        thinking_assignment_enabled=True,
    )
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "ranking_thinking_assignment_enabled": True,
        },
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
    )
    contract = _resolved_g1_registry_contract(module, experiment, config)
    captured_routes: list[tuple[str, str, str]] = []

    async def fake_run_pipeline(turn, _steps):
        turn.model = inherited.model
        turn.metadata.update(
            {
                "routed_tier": "c1",
                "routing_confidence": 0.9,
                "routing_source": "test",
                "routing_applied": False,
            }
        )
        return turn

    async def fake_analyze_task_with_fallback_chain(**kwargs):
        candidates = kwargs["candidates"]
        captured_routes.extend(
            (
                candidate.provider_id,
                candidate.model_id,
                candidate.upstream_provider,
            )
            for candidate in candidates
        )
        physical_attempts = [
            {
                "attempt": index,
                "physical_attempt_id": f"{index:032x}",
                "provider": candidate.provider_id,
                "model": candidate.model_id,
                "requested_provider": candidate.provider_id,
                "requested_model": candidate.model_id,
                "input_tokens": 5,
                "output_tokens": 2,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "billed_cost": 0.0,
                "provider_usage": {
                    "provider": candidate.provider_id,
                    "model": candidate.model_id,
                    "physical_attempt_id": f"{index:032x}",
                },
            }
            for index, candidate in enumerate(candidates, start=1)
        ]
        return TaskAnalysisResult(
            profile=fallback_task_profile(
                routed_tier=kwargs["routed_tier"],
                request_context=kwargs["request_context"],
                ranking_config=kwargs["ranking_config"],
            ),
            source="router_fallback",
            schema_valid=False,
            confidence=0.0,
            fallback_reason="TimeoutError",
            usage={
                "attempt_count": len(physical_attempts),
                "physical_attempts": physical_attempts,
            },
            provider_id=candidates[-1].provider_id,
            model_id=candidates[-1].model_id,
            chain_trace={
                "protocol": "opensquilla.task-analyzer-fallback-chain/v1",
                "configured_routes": [
                    {
                        "provider": candidate.provider_id,
                        "model": candidate.model_id,
                        "upstream_provider": candidate.upstream_provider,
                    }
                    for candidate in candidates
                ],
                "attempt_outcomes": [
                    {
                        "candidate_index": index,
                        "provider": candidate.provider_id,
                        "model": candidate.model_id,
                        "upstream_provider": candidate.upstream_provider,
                        "outcome": "failed",
                        "reason": "TimeoutError",
                        "physical_request_count": 1,
                    }
                    for index, candidate in enumerate(candidates)
                ],
                "selected_index": None,
                "exhausted": True,
            },
        )

    monkeypatch.setattr(module, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_fallback_chain",
        fake_analyze_task_with_fallback_chain,
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "1")

    build = await module.build_experiment_provider(
        config=config,
        inherited=inherited,
        group="G1",
        prompt="test prompt",
        dry_run=False,
        enable_proposer_tools=False,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        experiment_config=experiment,
        g1_registry_contract=contract,
        generation_policy={},
    )

    assert captured_routes == [
        ("openrouter", "anthropic/claude-opus-4.8", "anthropic"),
        ("openrouter", "openai/gpt-5.6-sol", "azure"),
        (
            "openrouter",
            "google/gemini-3.1-pro-preview",
            "google-ai-studio",
        ),
    ]
    assert build.provider.selection_plan["selected_P"] == [
        "openrouter:z-ai/glm-5.2",
        "openrouter:moonshotai/kimi-k2.7-code",
        "openrouter:qwen/qwen3.8-max",
        "openrouter:deepseek/deepseek-v4-pro",
    ]
    assert build.provider.selection_plan["selected_A"] == (
        "openrouter:z-ai/glm-5.2"
    )
    assert build.provider.selection_plan["complete_proposers_only"] is True
    assert build.provider.selection_plan["effective_min_successful_proposers"] == 1
    assert build.provider.min_successful_proposers == 1
    assert [row["requested_model"] for row in build.setup_usage] == [
        route[1] for route in captured_routes
    ]


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_frozen_replay_registry_contract_omits_inactive_live_analyzer_pair(
    module,
) -> None:
    experiment = _experiment_with_inactive_live_pair_and_frozen_replay(module)
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "ranking_thinking_assignment_enabled": True,
        },
    )

    contract = _resolved_g1_registry_contract(module, experiment, config)

    assert contract["task_analysis_execution"]["mode"] == "frozen_replay"
    assert "live_task_analyzer_chain" not in contract
    assert "analyzer_failure_fallback_ensemble" not in contract
    assert contract["task_analyzer"]["model"] == "anthropic/claude-opus-4.8"
    assert contract["task_analyzer"]["upstream_provider"] == "anthropic"


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_schema_invalid_frozen_replay_does_not_activate_live_analyzer_or_fixed_fallback(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider.ranking_router import (
        TaskAnalysisResult,
        fallback_task_profile,
    )

    experiment = _experiment_with_inactive_live_pair_and_frozen_replay(module)
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "ranking_thinking_assignment_enabled": True,
        },
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
    )
    contract = _resolved_g1_registry_contract(module, experiment, config)

    async def fake_run_pipeline(turn, _steps):
        turn.model = inherited.model
        turn.metadata.update(
            {
                "routed_tier": "c1",
                "routing_confidence": 0.9,
                "routing_source": "test",
                "routing_applied": False,
            }
        )
        return turn

    async def unexpected_live_analyzer(**_kwargs):
        raise AssertionError("frozen replay must not invoke a live Analyzer")

    def fake_frozen_task_analysis_result(
        _replay_contract,
        **kwargs,
    ):
        return TaskAnalysisResult(
            profile=fallback_task_profile(
                routed_tier=kwargs["routed_tier"],
                request_context=kwargs["request_context"],
                ranking_config=kwargs["ranking_config"],
            ),
            source="frozen_replay",
            schema_valid=False,
            confidence=0.0,
            fallback_reason="task_analysis_not_supplied",
            usage={},
            provider_id="openrouter",
            model_id="legacy-analyzer",
        )

    monkeypatch.setattr(module, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_fallback_chain",
        unexpected_live_analyzer,
    )
    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_provider",
        unexpected_live_analyzer,
    )
    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.frozen_task_analysis_result",
        fake_frozen_task_analysis_result,
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "1")

    build = await module.build_experiment_provider(
        config=config,
        inherited=inherited,
        group="G1",
        prompt="historical replay prompt",
        task_id="task-1",
        task_input_sha256=f"sha256:{'a' * 64}",
        prompt_sha256="b" * 64,
        dry_run=False,
        enable_proposer_tools=False,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        experiment_config=experiment,
        g1_registry_contract=contract,
        generation_policy={},
    )

    assert build.setup_usage == []
    assert build.provider.selection_plan.get("analyzer_failure_fallback") is not True
    assert build.provider.selection_plan["effective_min_successful_proposers"] == 2
    assert build.provider.selection_plan["proposer_recovery_policy"][
        "quorum_required"
    ] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_unavailable_live_analyzer_chain_enters_fixed_fallback_without_synthetic_usage(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = _experiment_with_current_g1_contract(
        module,
        thinking_assignment_enabled=True,
    )
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "ranking_thinking_assignment_enabled": True,
        },
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
    )
    contract = _resolved_g1_registry_contract(module, experiment, config)

    async def fake_run_pipeline(turn, _steps):
        turn.model = inherited.model
        turn.metadata.update(
            {
                "routed_tier": "c1",
                "routing_confidence": 0.9,
                "routing_source": "test",
                "routing_applied": False,
            }
        )
        return turn

    monkeypatch.setattr(module, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(
        module,
        "build_task_analyzer_provider",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "1")

    build = await module.build_experiment_provider(
        config=config,
        inherited=inherited,
        group="G1",
        prompt="all live analyzers unavailable",
        dry_run=False,
        enable_proposer_tools=False,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        experiment_config=experiment,
        g1_registry_contract=contract,
        generation_policy={},
    )

    chain = build.routing_trace["task_analyzer"]["chain"]
    assert chain["exhausted"] is True
    assert [row["physical_request_count"] for row in chain["attempt_outcomes"]] == [
        0,
        0,
        0,
    ]
    assert build.setup_usage == []
    assert build.provider.selection_plan["analyzer_failure_fallback"] is True
    assert build.provider.selection_plan["effective_min_successful_proposers"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    "failure_mode",
    ["trace", "usage_materialization", "usage_mapping"],
)
async def test_task_analyzer_post_return_failure_preserves_paid_receipts(
    module,
    failure_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider.ranking_router import fallback_task_profile

    experiment = _experiment_with_current_g1_contract(
        module,
        thinking_assignment_enabled=True,
    )
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "ranking_thinking_assignment_enabled": True,
        },
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
    )
    contract = _resolved_g1_registry_contract(
        module,
        experiment,
        config,
    )

    async def fake_run_pipeline(turn, _steps):
        turn.model = inherited.model
        turn.metadata.update(
            {
                "routed_tier": "c1",
                "routing_confidence": 0.9,
                "routing_source": "test",
                "routing_applied": False,
            }
        )
        return turn

    analyzer_calls = 0
    private_detail = "private analyzer trace detail"

    class UsageMapping(Mapping[str, object]):
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def __getitem__(self, key: str) -> object:
            return self._payload[key]

        def __iter__(self) -> Iterator[str]:
            raise RuntimeError(private_detail)

        def __len__(self) -> int:
            return len(self._payload)

        def get(self, key: str, default=None):
            return self._payload.get(key, default)

    async def fake_analyze_task_with_fallback_chain(**kwargs):
        nonlocal analyzer_calls
        analyzer_calls += 1
        first_candidate = kwargs["candidates"][0]
        analyzer_provider = first_candidate.provider_id
        analyzer_model = first_candidate.model_id
        physical_attempts = [
            {
                "attempt": ordinal,
                "physical_attempt_id": str(ordinal) * 32,
                "provider": analyzer_provider,
                "model": analyzer_model,
                "requested_provider": analyzer_provider,
                "requested_model": analyzer_model,
                "input_tokens": ordinal,
                "output_tokens": 1,
                "provider_usage": {
                    "physical_attempt_id": str(ordinal) * 32,
                },
            }
            for ordinal in range(
                1,
                4 if failure_mode != "trace" else 2,
            )
        ]

        class Analysis:
            profile = fallback_task_profile(
                routed_tier=kwargs["routed_tier"],
                request_context=kwargs["request_context"],
                ranking_config=kwargs["ranking_config"],
            )
            source = "test_analyzer"
            fallback_reason = ""
            schema_valid = True
            confidence = 0.9
            usage_payload = {
                "attempt_count": 3,
                "physical_attempts": physical_attempts,
            }
            usage = (
                UsageMapping(usage_payload)
                if failure_mode == "usage_mapping"
                else usage_payload
                if failure_mode == "usage_materialization"
                else {
                    **physical_attempts[0],
                    "attempt_count": 1,
                }
            )

            def trace(self, _ranking_config):
                if failure_mode == "trace":
                    raise RuntimeError(private_detail)
                return {}

        return Analysis()

    monkeypatch.setattr(module, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_fallback_chain",
        fake_analyze_task_with_fallback_chain,
    )
    if failure_mode == "usage_materialization":
        monkeypatch.setattr(
            module,
            "task_analyzer_usage_rows",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(private_detail)),
        )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    monkeypatch.setenv(
        "OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS",
        "1",
    )

    with pytest.raises(module.ProviderBuildError) as captured:
        await module.build_experiment_provider(
            config=config,
            inherited=inherited,
            group="G1",
            prompt="test prompt",
            dry_run=False,
            enable_proposer_tools=False,
            ensemble_proposer_timeout=None,
            ensemble_aggregator_timeout=None,
            experiment_config=experiment,
            g1_registry_contract=contract,
            generation_policy={},
        )

    assert analyzer_calls == 1
    error = captured.value
    assert str(error) == ("provider_build_failed_after_setup:RuntimeError")
    assert private_detail not in str(error)
    expected_count = 3 if failure_mode != "trace" else 1
    assert len(error.setup_usage) == expected_count
    assert [usage["attempt"] for usage in error.setup_usage] == list(range(1, expected_count + 1))
    assert {usage["physical_attempt_id"] for usage in error.setup_usage} == {
        str(ordinal) * 32 for ordinal in range(1, expected_count + 1)
    }
    assert all(
        usage["role"] == "task_analyzer"
        and usage["request_count"] == 1
        and usage["requested_provider"] == "openrouter"
        and usage["requested_model"] == "anthropic/claude-opus-4.8"
        for usage in error.setup_usage
    )
    assert error.routing_trace["task_analyzer"]["source"] == ("analyzer_postprocess_failed")
    assert private_detail not in json.dumps(error.routing_trace)


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_single_generation_identity_is_checked_before_judge(module) -> None:
    valid = module.RunResult(
        final_text="answer",
        done=DoneEvent(
            provider="openrouter",
            model="model-a",
            requested_provider="openrouter",
            requested_model="model-a",
            stop_reason="stop",
        ),
    )
    assert (
        module.generation_retry_reason(
            valid,
            expected_model="model-a",
            expected_provider="openrouter",
        )
        == ""
    )

    wrong_provider = module.RunResult(
        final_text="answer",
        done=DoneEvent(
            provider="unexpected",
            model="model-a",
            requested_provider="openrouter",
            requested_model="model-a",
            stop_reason="stop",
        ),
    )
    assert (
        module.generation_retry_reason(
            wrong_provider,
            expected_model="model-a",
            expected_provider="openrouter",
        )
        == "wrong_actual_provider"
    )

    inferred = module.RunResult(
        final_text="answer",
        done=DoneEvent(
            provider="",
            model="",
            requested_provider="openrouter",
            requested_model="model-a",
            stop_reason="stop",
            model_usage_breakdown=[
                {
                    "provider": "openrouter",
                    "model": "model-a",
                    "input_tokens": 1,
                    "output_tokens": 1,
                }
            ],
        ),
    )
    assert (
        module.generation_retry_reason(
            inferred,
            expected_model="model-a",
            expected_provider="openrouter",
        )
        == ""
    )


@pytest.mark.asyncio
async def test_run_one_records_and_forwards_agent_finalization_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, inherited = _openrouter_config()
    captured: dict[str, object] = {}
    policy = {
        "deadline_wrapup_margin_seconds": 600,
        "deadline_wrapup_disable_tools": True,
        "deadline_thinking_off_margin_seconds": 600,
        "max_iterations_includes_finalization": True,
        "retrieval_loop_finalization_threshold": 3,
        "finalization_aggregator_only": True,
        "finalization_disable_thinking": True,
    }
    result = runner.RunResult(
        final_text="answer",
        done=DoneEvent(model="test/model", stop_reason="stop"),
    )

    async def fake_build_experiment_provider(**_kwargs):
        return runner.ProviderBuildResult(provider=object(), prompt="prompt")

    async def fake_collect_generation_with_retries(*_args, **kwargs):
        captured.update(kwargs)
        return result, [], 1

    async def fake_judge_text(**_kwargs):
        return None

    monkeypatch.setattr(
        runner,
        "build_experiment_provider",
        fake_build_experiment_provider,
    )
    monkeypatch.setattr(
        runner,
        "collect_generation_with_retries",
        fake_collect_generation_with_retries,
    )
    monkeypatch.setattr(runner, "judge_text", fake_judge_text)

    row = await runner.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="B3",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=None,
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=3600,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={"tools_enabled": False, "tool_mode": "provider_only"},
        generation_policy={},
        runner_mode=runner.RUNNER_MODE_AGENT_LOOP,
        agent_finalization_policy=policy,
    )

    assert captured["finalization_policy"] == policy
    assert row["agent_finalization_policy"] == policy
    assert row["execution"]["agent_finalization_policy"] == policy


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_run_one_records_dynamic_cumulative_generation_budget(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, inherited = _openrouter_config()
    expected_model = str(module.GROUP_SPECS["B0"]["model"])
    calls = 0

    async def fake_build_experiment_provider(**_kwargs):
        return module.ProviderBuildResult(provider=object(), prompt="prompt")

    async def fake_collect_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return module.RunResult(
            final_text="accepted",
            done=DoneEvent(
                provider="openrouter",
                model=expected_model,
                requested_provider="openrouter",
                requested_model=expected_model,
                stop_reason="stop",
            ),
        )

    monkeypatch.setattr(
        module,
        "build_experiment_provider",
        fake_build_experiment_provider,
    )
    monkeypatch.setattr(module, "collect_run", fake_collect_run)

    row = await module.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="B0",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=None,
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=30,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={"tools_enabled": False, "tool_mode": "provider_only"},
        generation_policy={},
        generation_max_attempts=2,
        generation_attempt_offset=1,
    )

    assert calls == 1
    assert row["generation_attempt_budget_limit"] == 2
    assert row["generation_attempt_budget_used"] == 2
    assert row["generation_max_attempts"] == 2
    assert row["execution"]["generation_max_attempts"] == 2
    assert row["execution"]["generation_attempt_budget_remaining"] == 0
    assert row["execution"]["generation_attempts"][0]["attempt"] == 2


@pytest.mark.asyncio
async def test_run_one_separates_selected_attempt_metrics_from_actual_spend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, inherited = _openrouter_config()

    def attempt_result(
        *,
        final_text: str,
        billed_cost: float,
        response_id: str,
        latency_ms: int,
        error: str = "",
    ) -> runner.RunResult:
        rows = [
            {
                "provider": "openrouter",
                "model": f"model-{index}",
                "input_tokens": 10,
                "output_tokens": 2,
                "billed_cost": billed_cost / 2,
                "cost_source": "provider_billed",
                "provider_usage": _openrouter_exact_evidence(
                    billed_cost / 2,
                    f"{response_id}-{index}",
                ),
            }
            for index in range(2)
        ]
        return runner.RunResult(
            final_text=final_text,
            done=DoneEvent(
                input_tokens=20,
                output_tokens=4,
                billed_cost=billed_cost,
                cost_source="provider_billed",
                model="aggregator",
                model_usage_breakdown=rows,
                ensemble_trace=_valid_ensemble_trace(
                    selection_mode="router_tree_baseline",
                    llm_request_count=2,
                    final_text=final_text,
                ),
            ),
            error=error,
            latency_ms=latency_ms,
        )

    failed = attempt_result(
        final_text="",
        billed_cost=1.0,
        response_id="failed",
        latency_ms=10,
        error="empty_generation_output",
    )
    selected = attempt_result(
        final_text="accepted answer",
        billed_cost=0.2,
        response_id="selected",
        latency_ms=20,
    )
    attempts = [
        {
            "attempt": 1,
            "retryable": True,
            "retry_reason": "empty_generation_output",
            "will_retry": True,
            "run": runner.run_result_summary(failed),
        },
        {
            "attempt": 2,
            "retryable": False,
            "retry_reason": "",
            "will_retry": False,
            "run": runner.run_result_summary(selected),
        },
    ]

    async def fake_build_experiment_provider(**_kwargs):
        return runner.ProviderBuildResult(provider=object(), prompt="prompt")

    async def fake_collect_generation_with_retries(*_args, **_kwargs):
        return selected, attempts, 2

    monkeypatch.setattr(
        runner,
        "build_experiment_provider",
        fake_build_experiment_provider,
    )
    monkeypatch.setattr(
        runner,
        "collect_generation_with_retries",
        fake_collect_generation_with_retries,
    )

    row = await runner.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="B3",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=None,
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=60,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={"tools_enabled": False, "tool_mode": "provider_only"},
        generation_policy={},
    )

    assert row["final_text"] == "accepted answer"
    assert row["generation_attempt_count"] == 2
    assert row["latency_ms"] == 20
    assert row["llm_request_count"] == 2
    assert row["selected_attempt_billed_cost_usd"] == pytest.approx(0.2)
    assert row["selected_attempt_metrics"]["generation_attempt"] == 2
    assert row["actual_spend_metrics"]["latency_ms"] == 30
    assert row["actual_spend_metrics"]["llm_request_count"] == 4
    assert row["actual_spend_billed_cost_usd"] == pytest.approx(1.2)
    assert row["cost_accounting"]["generation"]["recorded_cost_usd"] == (pytest.approx(0.2))
    assert row["cost_accounting"]["actual_generation_spend"]["recorded_cost_usd"] == pytest.approx(
        1.2
    )


@pytest.mark.asyncio
async def test_run_one_keeps_failed_generation_spend_out_of_selected_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, inherited = _openrouter_config()
    usage_row = {
        "provider": "openrouter",
        "model": "failed-model",
        "input_tokens": 10,
        "output_tokens": 2,
        "billed_cost": 0.5,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.5, "failed-generation"),
    }
    failed = runner.RunResult(
        final_text="partial answer",
        done=DoneEvent(
            model="failed-model",
            billed_cost=0.5,
            cost_source="provider_billed",
            model_usage_breakdown=[usage_row],
            ensemble_trace={"llm_request_count": 1},
        ),
        error="TimeoutError: generation failed",
        latency_ms=25,
    )
    attempts = [
        {
            "attempt": 1,
            "retryable": True,
            "retry_reason": failed.error,
            "retry_suppressed_reason": "agent_hard_timeout",
            "will_retry": False,
            "run": runner.run_result_summary(failed),
        }
    ]

    async def fake_build_experiment_provider(**_kwargs):
        return runner.ProviderBuildResult(provider=object(), prompt="prompt")

    async def fake_collect_generation_with_retries(*_args, **_kwargs):
        return failed, attempts, 0

    monkeypatch.setattr(
        runner,
        "build_experiment_provider",
        fake_build_experiment_provider,
    )
    monkeypatch.setattr(
        runner,
        "collect_generation_with_retries",
        fake_collect_generation_with_retries,
    )

    row = await runner.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="B3",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=None,
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=60,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={"tools_enabled": False, "tool_mode": "provider_only"},
        generation_policy={},
    )

    assert row["selected_generation_succeeded"] is False
    assert row["selected_attempt_metrics"]["generation_attempt"] == 0
    assert row["selected_attempt_metrics"]["llm_request_count"] == 0
    assert row["selected_attempt_billed_cost_usd"] == 0.0
    assert row["actual_spend_metrics"]["llm_request_count"] == 1
    assert row["actual_spend_billed_cost_usd"] == pytest.approx(0.5)
    assert row["cost_accounting"]["generation"]["recorded_cost_usd"] == 0.0
    assert row["cost_accounting"]["actual_generation_spend"]["recorded_cost_usd"] == pytest.approx(
        0.5
    )


def _ensemble_member(module, model: str, *, thinking: str = "low"):
    return module.EnsembleMemberConfig(
        provider_config=ProviderConfig(
            provider="openrouter",
            model=model,
            provider_routing={model: "verified-upstream"},
        ),
        label=model.rsplit("/", 1)[-1],
        temperature=0.7,
        max_tokens=1024,
        thinking=thinking,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_g1_dry_build_uses_final_generation_policy_for_request_contract(
    module,
) -> None:
    experiment = _experiment_with_current_g1_contract(
        module,
        thinking_assignment_enabled=True,
    )
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "max_tokens": 4_096,
            "temperature": 0.7,
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "ranking_thinking_assignment_enabled": True,
        },
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
    )
    contract = _resolved_g1_registry_contract(module, experiment, config)
    generation_policy = {
        "temperature": None,
        "max_tokens": 16_384,
        "max_tokens_overridden": True,
    }

    build = await module.build_experiment_provider(
        config=config,
        inherited=inherited,
        group="G1",
        prompt="test prompt",
        dry_run=True,
        enable_proposer_tools=False,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        experiment_config=experiment,
        g1_registry_contract=contract,
        generation_policy=generation_policy,
    )

    plan = build.routing_trace["selection_plan"]
    assert plan["request_context"]["routing_budget"]["aggregator_output_tokens"] == 16_384
    model_facts = next(
        row["registry_facts"]
        for row in plan["registry_snapshot"]["models"]
        if row["registry_facts"]["model_id"] == "deepseek/deepseek-v4-pro"
    )
    assert model_facts["runtime_temperature_parameter_required"] is False


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_empty_generation_policy_preserves_configured_temperature(module) -> None:
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "max_tokens": 4_096,
            "temperature": 0.7,
        }
    )

    assert module.resolve_effective_generation_request_parameters(
        llm_config=config.llm,
        generation_policy={},
    ) == (4_096, 0.7)


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_run_wide_generation_policy_overrides_realized_ensemble_members(module) -> None:
    provider = module.EnsembleProvider(
        profile_name="router_dynamic/c3",
        proposers=[
            _ensemble_member(module, "anthropic/claude-opus-4.8"),
            _ensemble_member(module, "qwen/qwen3.7-max"),
            _ensemble_member(module, "x-ai/grok-4.5"),
        ],
        proposer_backups=[
            _ensemble_member(module, "deepseek/deepseek-v4-pro"),
        ],
        aggregator=_ensemble_member(module, "anthropic/claude-sonnet-5"),
        aggregator_fallbacks=[
            _ensemble_member(module, "google/gemini-3.1-pro-preview"),
        ],
    )
    policy = {
        **module.generation_thinking_policy(),
        "temperature": 0.0,
        "max_tokens": 16_384,
        "max_tokens_overridden": True,
        "thinking_budget_tokens": 50_000,
    }

    aligned = module.apply_generation_policy_to_ensemble_provider(provider, policy)

    assert [member.thinking for member in aligned.proposers] == [
        "max",
        "high",
        "high",
    ]
    assert aligned.aggregator.thinking == "max"
    assert all(member.temperature == 0.0 for member in aligned.proposers)
    assert aligned.aggregator.temperature == 0.0
    assert all(member.max_tokens == 16_384 for member in aligned.proposers)
    assert aligned.aggregator.max_tokens == 16_384
    assert all(
        member.temperature == 0.0
        and member.max_tokens == 16_384
        and member.thinking
        == module.generation_thinking_for_model(
            member.provider_config.model,
            policy,
        )
        for member in [
            *aligned.proposer_backups,
            *aligned.aggregator_fallbacks,
        ]
    )
    assert aligned.selection_plan["generation_policy_applied"] is True
    assert {
        row["model"]: row["thinking"] for row in aligned.selection_plan["member_generation"]
    } == {
        "anthropic/claude-opus-4.8": "max",
        "qwen/qwen3.7-max": "high",
        "x-ai/grok-4.5": "high",
        "anthropic/claude-sonnet-5": "max",
    }
    assert {
        (row["role"], row["model"]): row["thinking"]
        for row in aligned.selection_plan["recovery_member_generation"]
    } == {
        (
            "proposer_backup",
            "deepseek/deepseek-v4-pro",
        ): module.generation_thinking_for_model(
            "deepseek/deepseek-v4-pro",
            policy,
        ),
        (
            "aggregator_fallback",
            "google/gemini-3.1-pro-preview",
        ): module.generation_thinking_for_model(
            "google/gemini-3.1-pro-preview",
            policy,
        ),
    }


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_default_model_specific_budget_is_resolved_per_ensemble_member(module) -> None:
    provider = module.EnsembleProvider(
        profile_name="router_dynamic/c3",
        proposers=[_ensemble_member(module, "anthropic/claude-opus-4.8")],
        aggregator=_ensemble_member(module, "x-ai/grok-4.5"),
    )
    policy = module.generation_thinking_policy()
    assert policy["thinking_budget_tokens"] == "model-specific"

    aligned = module.apply_generation_policy_to_ensemble_provider(provider, policy)

    assert {
        row["model"]: row["thinking_budget_tokens"]
        for row in aligned.selection_plan["member_generation"]
    } == {
        "anthropic/claude-opus-4.8": 50_000,
        "x-ai/grok-4.5": 20_000,
    }


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_strict_ensemble_validation_rejects_unproved_reasoning_member(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "unknown/vendor-model"
    provider = module.EnsembleProvider(
        profile_name="router_dynamic/c3",
        proposers=[_ensemble_member(module, model, thinking="xhigh")],
        aggregator=_ensemble_member(module, "x-ai/grok-4.5", thinking="xhigh"),
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "1")

    with pytest.raises(ValueError, match="cannot prove support"):
        module.validate_strict_openrouter_ensemble_members(
            provider,
            module.generation_thinking_policy(),
        )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_highest_thinking_uses_registry_level_and_rejects_explicit_drift(module) -> None:
    policy = {
        **module.generation_thinking_policy(),
        "require_highest_thinking": True,
        "model_thinking_levels": {},
    }

    assert module.generation_thinking_for_model("qwen/qwen3.7-max", policy) == "high"
    assert module.generation_thinking_for_model("qwen/qwen3-coder-next", policy) == "off"

    policy["model_thinking_levels"] = {"moonshotai/kimi-k2.7-code": "max"}
    with pytest.raises(ValueError, match="registry highest supported level is 'high'"):
        module.generation_thinking_for_model("moonshotai/kimi-k2.7-code", policy)


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_model_max_normalizes_registry_drift_when_strict_highest_is_disabled(module) -> None:
    policy = {
        **module.generation_thinking_policy(),
        "require_highest_thinking": False,
        "model_thinking_levels": {"moonshotai/kimi-k2.7-code": "max"},
    }

    assert module.generation_thinking_for_model("moonshotai/kimi-k2.7-code", policy) == "high"


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_strict_ensemble_validation_rejects_unsupported_registry_level(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = module.EnsembleProvider(
        profile_name="static_openrouter_b5",
        proposers=[
            _ensemble_member(
                module,
                "moonshotai/kimi-k2.7-code",
                thinking="max",
            )
        ],
        aggregator=_ensemble_member(module, "z-ai/glm-5.2", thinking="xhigh"),
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "1")

    with pytest.raises(ValueError, match="does not support frozen thinking='max'"):
        module.validate_strict_openrouter_ensemble_members(
            provider,
            module.generation_thinking_policy(),
        )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize("drifted_thinking", ["", "off"], ids=["empty", "off"])
def test_strict_ensemble_validation_rejects_empty_or_off_registry_drift(
    module,
    monkeypatch: pytest.MonkeyPatch,
    drifted_thinking: str,
) -> None:
    provider = module.EnsembleProvider(
        profile_name="static_openrouter_b5",
        proposers=[
            _ensemble_member(
                module,
                "moonshotai/kimi-k2.7-code",
                thinking=drifted_thinking,
            )
        ],
        aggregator=_ensemble_member(module, "z-ai/glm-5.2", thinking="xhigh"),
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "1")

    with pytest.raises(ValueError, match="does not support frozen thinking="):
        module.validate_strict_openrouter_ensemble_members(
            provider,
            {
                **module.generation_thinking_policy(),
                "require_highest_thinking": True,
            },
        )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_strict_ensemble_validation_rejects_supported_off_when_highest_is_required(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = module.EnsembleProvider(
        profile_name="static_openrouter_b5",
        proposers=[
            _ensemble_member(
                module,
                "qwen/qwen3.7-max",
                thinking="off",
            )
        ],
        aggregator=_ensemble_member(module, "z-ai/glm-5.2", thinking="xhigh"),
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "1")

    with pytest.raises(ValueError, match="requires highest thinking='high', not 'off'"):
        module.validate_strict_openrouter_ensemble_members(
            provider,
            {
                **module.generation_thinking_policy(),
                "require_highest_thinking": True,
            },
        )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_strict_ensemble_validation_requires_upstream_pin(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "x-ai/grok-4.5"
    member = _ensemble_member(module, model, thinking="high")
    member.provider_config.provider_routing.clear()
    provider = module.EnsembleProvider(
        profile_name="router_dynamic/c3",
        proposers=[member],
        aggregator=_ensemble_member(module, "anthropic/claude-sonnet-5"),
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "1")

    with pytest.raises(ValueError, match="no strict upstream provider pin"):
        module.validate_strict_openrouter_ensemble_members(
            provider,
            module.generation_thinking_policy(),
        )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_strict_ensemble_validation_checks_fallback_and_enabled_boolean(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "deepseek/deepseek-v4-pro"
    provider = module.EnsembleProvider(
        profile_name="static_openrouter_b5",
        proposers=[_ensemble_member(module, model, thinking="xhigh")],
        aggregator=_ensemble_member(module, "z-ai/glm-5.2", thinking="xhigh"),
        fallback_model=model,
    )
    fallback_config = ProviderConfig(
        provider="openrouter",
        model=model,
        provider_routing={},
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "enabled")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "enabled")

    with pytest.raises(ValueError, match="no strict upstream provider pin"):
        module.validate_strict_openrouter_ensemble_members(
            provider,
            module.generation_thinking_policy(),
            fallback_config=fallback_config,
        )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_ensemble_fallback_model_receives_openrouter_reasoning_capabilities(module) -> None:
    policy = module.generation_thinking_policy()
    config = module.generation_chat_config(policy, model=None)

    resolved = module.with_openrouter_model_capabilities(
        config,
        "deepseek/deepseek-v4-pro",
    )

    assert resolved.model_capabilities is not None
    assert resolved.model_capabilities.supports_reasoning is True
    assert resolved.model_capabilities.reasoning_format == "openrouter"


def test_non_b2_groups_do_not_apply_g12_argument_alignment() -> None:
    args = runner.build_parser().parse_args(
        ["--input", "tasks.jsonl", "--groups", "B1", "--concurrency", "8"]
    )

    record = runner.apply_b2_g12_argument_alignment(args, ["B1"])

    assert record is None
    assert args.concurrency == 8
    assert not hasattr(args, "_benchmark_alignments")


def test_local_brave_runtime_allows_missing_key_only_for_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    configure_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "opensquilla.tools.builtin.web.configure_search",
        lambda **kwargs: configure_calls.append(kwargs),
    )
    config = GatewayConfig(search_api_key="")
    args = runner.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B2",
            "--tool-mode",
            "local_web_tools",
            "--local-web-search-provider",
            "brave",
            "--local-web-search-api-key-env",
            "BRAVE_SEARCH_API_KEY",
        ]
    )
    policy = runner.benchmark_tool_policy(args)

    runtime = runner.configure_local_web_search_runtime(config, policy, dry_run=True)

    assert runtime["credential_status"] == "missing_allowed_dry_run"
    assert runtime["runtime_configured"] is False
    assert runtime["api_key_configured"] is False
    assert configure_calls == []
    with pytest.raises(ValueError, match="requires an API key"):
        runner.configure_local_web_search_runtime(config, policy, dry_run=False)


def test_explicit_brave_environment_key_overrides_stale_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "fresh-env-key")
    configure_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "opensquilla.tools.builtin.web.configure_search",
        lambda **kwargs: configure_calls.append(kwargs),
    )
    config = GatewayConfig(search_api_key="stale-config-key")
    args = runner.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B1",
            "--tool-mode",
            "local_web_tools",
            "--local-web-search-provider",
            "brave",
            "--local-web-search-api-key-env",
            "BRAVE_SEARCH_API_KEY",
        ]
    )

    runtime = runner.configure_local_web_search_runtime(
        config,
        runner.benchmark_tool_policy(args),
    )

    assert runtime["api_key_source"] == "env:BRAVE_SEARCH_API_KEY"
    assert configure_calls[0]["api_key"] == "fresh-env-key"


def _local_web_tool_policy():
    args = runner.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B1",
            "--tool-mode",
            "local_web_tools",
        ]
    )
    return runner.benchmark_tool_policy(args)


def test_benchmark_tool_context_does_not_force_full_host_access() -> None:
    policy = _local_web_tool_policy()

    context = runner.build_benchmark_tool_context(
        task_id="task-1",
        group="B1",
        tool_policy=policy,
    )

    assert context.run_mode is None
    assert context.sandbox_run_context is None
    assert context.allowed_tools == {"web_search", "web_fetch"}


@pytest.mark.asyncio
async def test_local_web_preflight_does_not_enable_full_host_access(
    monkeypatch: pytest.MonkeyPatch,
    configured_tool_runtime,
) -> None:
    calls = {"search": 0, "fetch": 0}

    async def _fake_search(query: str, max_results: int, *, exclude_domains, provider: str):
        calls["search"] += 1
        assert "OpenAI official website" in query
        assert max_results == 1
        assert "github.com" in exclude_domains
        assert provider == "duckduckgo"
        return {
            "query": query,
            "results": [
                {
                    "title": "OpenAI",
                    "url": "https://openai.com/",
                    "snippet": "Official site",
                }
            ],
        }

    async def _fake_fetch(
        url: str,
        *,
        extract_mode: str,
        max_chars: int | None,
        extractor: str,
    ):
        from opensquilla.tools.run_mode import full_host_access_active

        calls["fetch"] += 1
        assert full_host_access_active() is False
        assert url == "https://example.com/"
        assert extract_mode == "text"
        assert max_chars == 1_000
        assert extractor == "auto"
        return {
            "url": url,
            "final_url": url,
            "status": 200,
            "text": "<external-content>Example Domain</external-content>",
        }

    monkeypatch.setattr(
        "opensquilla.tools.builtin.web.run_web_search_payload",
        _fake_search,
    )
    monkeypatch.setattr(
        "opensquilla.tools.builtin.web_fetch.run_web_fetch_payload",
        _fake_fetch,
    )

    result = await runner.run_local_web_tools_preflight(_local_web_tool_policy())

    assert result["status"] == "passed"
    assert "run_mode" not in result
    assert result["web_search_result_count"] == 1
    assert result["web_fetch_http_status"] == 200
    assert result["attempts_used"] == 1
    assert result["preflight_calls"] == {"web_search": 1, "web_fetch": 1}
    assert calls == {"search": 1, "fetch": 1}


@pytest.mark.asyncio
async def test_local_web_fetch_keeps_draco_contamination_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_called = False

    async def _fake_fetch(*args, **kwargs):
        nonlocal fetch_called
        fetch_called = True
        return {"status": 200, "text": "should not be fetched"}

    monkeypatch.setattr(
        "opensquilla.tools.builtin.web_fetch.run_web_fetch_payload",
        _fake_fetch,
    )
    policy = _local_web_tool_policy()
    registry = runner.build_local_web_tool_registry(policy)
    context = runner.build_benchmark_tool_context(
        task_id="task-1",
        group="B1",
        tool_policy=policy,
    )
    handler = runner.build_tool_handler(registry, context)

    result = await handler(
        ToolCall(
            tool_use_id="blocked-fetch",
            tool_name="web_fetch",
            arguments={"url": "https://github.com/openai/example"},
        )
    )

    payload = json.loads(result.content)
    assert fetch_called is False
    assert payload["error_class"] == "BlockedDomain"
    assert payload["blocked_domain"] == "github.com"


@pytest.mark.asyncio
async def test_local_web_preflight_fails_closed_on_denial_payload(
    monkeypatch: pytest.MonkeyPatch,
    configured_tool_runtime,
) -> None:
    async def _fake_search(query: str, max_results: int, *, exclude_domains, provider: str):
        assert provider == "duckduckgo"
        return {
            "query": query,
            "results": [{"title": "OpenAI", "url": "https://openai.com/"}],
        }

    async def _denied_fetch(*args, **kwargs):
        return {
            "status": "error",
            "reason": "policy_denied",
            "error": "network denied",
        }

    monkeypatch.setattr(
        "opensquilla.tools.builtin.web.run_web_search_payload",
        _fake_search,
    )
    monkeypatch.setattr(
        "opensquilla.tools.builtin.web_fetch.run_web_fetch_payload",
        _denied_fetch,
    )

    with pytest.raises(RuntimeError, match="web_fetch preflight failed"):
        await runner.run_local_web_tools_preflight(
            _local_web_tool_policy(),
            max_attempts=1,
        )


@pytest.mark.asyncio
async def test_benchmark_tools_respect_ambient_standard_context(
    monkeypatch: pytest.MonkeyPatch,
    configured_tool_runtime,
) -> None:
    observed: list[tuple[bool, str | None]] = []

    async def _fake_fetch(
        url: str,
        *,
        extract_mode: str,
        max_chars: int | None,
        extractor: str,
    ):
        from opensquilla.tools.run_mode import full_host_access_active

        active = current_tool_context.get()
        observed.append((full_host_access_active(), active.task_id if active else None))
        return {"url": url, "final_url": url, "status": 200, "text": "ok"}

    monkeypatch.setattr(
        "opensquilla.tools.builtin.web_fetch.run_web_fetch_payload",
        _fake_fetch,
    )
    policy = _local_web_tool_policy()
    benchmark_context = runner.build_benchmark_tool_context(
        task_id="benchmark-task",
        group="B1",
        tool_policy=policy,
    )
    ambient = runner.ToolContext(
        run_mode="standard",
        sandbox_run_context=benchmark_context.sandbox_run_context,
        task_id="outer-task",
    )
    handler = runner.build_tool_handler(
        runner.build_local_web_tool_registry(policy),
        benchmark_context,
    )

    token = current_tool_context.set(ambient)
    try:
        result = await handler(
            ToolCall(
                tool_use_id="ambient-override",
                tool_name="web_fetch",
                arguments={"url": "https://example.com/"},
            )
        )
        assert result.is_error is False
        assert current_tool_context.get() is ambient
    finally:
        current_tool_context.reset(token)
    assert observed == [(False, "outer-task")]


@pytest.mark.asyncio
async def test_local_web_search_filters_results_sources_and_internal_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _fake_search(query: str, max_results: int, *, exclude_domains, provider: str):
        captured.update(
            query=query,
            max_results=max_results,
            exclude_domains=list(exclude_domains),
            provider=provider,
        )
        return {
            "query": query,
            "results": [
                {"title": "blocked", "url": "https://github.com/example"},
                {"title": "allowed", "url": "https://example.com/allowed"},
            ],
            "sources": [
                {"url": "https://huggingface.co/datasets/example", "text": "blocked"},
                {"url": "https://example.com/source", "text": "allowed"},
            ],
        }

    monkeypatch.setattr(
        "opensquilla.tools.builtin.web.run_web_search_payload",
        _fake_search,
    )
    policy = _local_web_tool_policy()
    policy["local_web_tools"]["web_search"]["provider"] = "brave"
    context = runner.build_benchmark_tool_context(
        task_id="search-task",
        group="B1",
        tool_policy=policy,
    )
    handler = runner.build_tool_handler(
        runner.build_local_web_tool_registry(policy),
        context,
    )
    result = await handler(
        ToolCall(
            tool_use_id="search-contamination",
            tool_name="web_search",
            arguments={"query": "test"},
        )
    )

    payload = json.loads(result.content)
    assert captured["query"] == "test"
    assert captured["max_results"] == runner.local_web_search_max_results(policy)
    assert "github.com" in captured["exclude_domains"]
    assert captured["provider"] == "brave"
    assert [item["url"] for item in payload["results"]] == ["https://example.com/allowed"]
    assert [item["url"] for item in payload["sources"]] == ["https://example.com/source"]
    assert payload["blocked_result_count"] == 1
    assert payload["blocked_source_count"] == 1


@pytest.mark.asyncio
async def test_local_web_fetch_discards_blocked_redirect_content(
    monkeypatch: pytest.MonkeyPatch,
    configured_tool_runtime,
) -> None:
    async def _redirected_fetch(*args, **kwargs):
        return {
            "url": "https://example.com/start",
            "final_url": "https://github.com/private/answer",
            "status": 200,
            "text": "must not reach the model",
        }

    monkeypatch.setattr(
        "opensquilla.tools.builtin.web_fetch.run_web_fetch_payload",
        _redirected_fetch,
    )
    policy = _local_web_tool_policy()
    context = runner.build_benchmark_tool_context(
        task_id="redirect-task",
        group="B1",
        tool_policy=policy,
    )
    handler = runner.build_tool_handler(
        runner.build_local_web_tool_registry(policy),
        context,
    )
    result = await handler(
        ToolCall(
            tool_use_id="redirect-contamination",
            tool_name="web_fetch",
            arguments={"url": "https://example.com/start"},
        )
    )

    payload = json.loads(result.content)
    assert payload["error_class"] == "BlockedDomain"
    assert payload["blocked_domain"] == "github.com"
    assert "must not reach the model" not in result.content


@pytest.mark.asyncio
async def test_local_web_preflight_retries_and_records_all_setup_calls(
    monkeypatch: pytest.MonkeyPatch,
    configured_tool_runtime,
) -> None:
    fetch_attempt = 0

    async def _fake_search(query: str, max_results: int, *, exclude_domains, provider: str):
        assert provider == "duckduckgo"
        return {"query": query, "results": [{"url": "https://openai.com/"}]}

    async def _flaky_fetch(*args, **kwargs):
        nonlocal fetch_attempt
        fetch_attempt += 1
        if fetch_attempt == 1:
            return {"status": 503, "text": "temporarily unavailable"}
        return {
            "url": "https://example.com/",
            "final_url": "https://example.com/",
            "status": 200,
            "text": "Example Domain",
        }

    monkeypatch.setattr("opensquilla.tools.builtin.web.run_web_search_payload", _fake_search)
    monkeypatch.setattr(
        "opensquilla.tools.builtin.web_fetch.run_web_fetch_payload",
        _flaky_fetch,
    )

    result = await runner.run_local_web_tools_preflight(
        _local_web_tool_policy(),
        retry_backoff_seconds=0,
    )

    assert result["attempts_used"] == 2
    assert result["preflight_calls"] == {"web_search": 2, "web_fetch": 2}


@pytest.mark.asyncio
async def test_local_web_preflight_times_out_closed(
    monkeypatch: pytest.MonkeyPatch,
    configured_tool_runtime,
) -> None:
    async def _fake_search(query: str, max_results: int, *, exclude_domains, provider: str):
        assert provider == "duckduckgo"
        return {"query": query, "results": [{"url": "https://openai.com/"}]}

    async def _hanging_fetch(*args, **kwargs):
        await asyncio.sleep(1)
        return {"status": 200, "text": "too late"}

    monkeypatch.setattr("opensquilla.tools.builtin.web.run_web_search_payload", _fake_search)
    monkeypatch.setattr(
        "opensquilla.tools.builtin.web_fetch.run_web_fetch_payload",
        _hanging_fetch,
    )

    with pytest.raises(RuntimeError, match="timed out"):
        await runner.run_local_web_tools_preflight(
            _local_web_tool_policy(),
            max_attempts=1,
            call_timeout_seconds=0.01,
        )


def test_runner_tool_mode_combinations_fail_closed() -> None:
    with pytest.raises(ValueError, match="requires --runner-mode=agent_loop"):
        runner.validate_tool_mode_for_runner("provider", "local_web_tools")
    with pytest.raises(ValueError, match="requires --runner-mode=provider"):
        runner.validate_tool_mode_for_runner("agent_loop", "openrouter_server_tools")
    runner.validate_tool_mode_for_runner(
        "provider",
        "local_web_tools",
        smoke_only=True,
    )


def test_select_tasks_by_ids_preserves_reference_order_and_rejects_bad_ids() -> None:
    tasks = [
        {"id": "task-a", "prompt": "a"},
        {"id": "task-b", "prompt": "b"},
        {"id": "task-c", "prompt": "c"},
    ]

    selected = runner.select_tasks_by_ids(tasks, ["task-c", "task-a"])

    assert [task["id"] for task in selected] == ["task-a", "task-c"]
    with pytest.raises(ValueError, match="duplicate --task-ids"):
        runner.select_tasks_by_ids(tasks, ["task-a", "task-a"])
    with pytest.raises(ValueError, match="unknown --task-ids"):
        runner.select_tasks_by_ids(tasks, ["task-missing"])


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_task_and_group_identity_inputs_fail_closed(
    module,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(
        "\n".join(
            [
                json.dumps({"id": "task-a", "prompt": "first"}),
                json.dumps({"task_id": " task-a ", "problem": "duplicate"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate task id"):
        module.load_tasks(input_path)
    with pytest.raises(ValueError, match="at least one experiment group"):
        module.parse_groups(",")
    with pytest.raises(ValueError, match=r"duplicate group\(s\): B2"):
        module.parse_groups("B2,b2")


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_result_key_coverage_requires_exactly_one_row_per_key(module) -> None:
    expected = {("B2", "task-a"), ("G1", "task-a")}
    complete = module.result_key_coverage(
        [
            {"group": "b2", "task_id": " task-a "},
            {"group": "G1", "task_id": "task-a"},
        ],
        expected_keys=expected,
    )
    assert complete["pass"] is True

    invalid = module.result_key_coverage(
        [
            {"group": "B2", "task_id": "task-a"},
            {"group": "B2", "task_id": "task-a"},
            {"group": "B4", "task_id": "task-a"},
        ],
        expected_keys=expected,
    )
    assert invalid["pass"] is False
    assert invalid["missing_keys"] == [["G1", "task-a"]]
    assert invalid["unexpected_keys"] == [["B4", "task-a"]]
    assert invalid["duplicate_keys"] == [{"key": ["B2", "task-a"], "count": 2}]


def test_draco_artifact_index_helpers_are_direct_compatibility_reexports() -> None:
    resume_runner = _load_resume_runner()

    for name in ("parse_maybe_json", "load_tasks", "result_key_coverage"):
        shared = getattr(draco_artifact_index, name)
        assert getattr(runner, name) is shared
        assert getattr(resume_runner, name) is shared


def test_shared_load_tasks_preserves_normalization_and_early_limit(tmp_path: Path) -> None:
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "task_id": " task-a ",
                        "problem": " prompt ",
                        "rubric": json.dumps({"criterion": "exact"}),
                    }
                ),
                "{not-valid-json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    tasks = draco_artifact_index.load_tasks(input_path, max_tasks=1)

    assert tasks == [
        {
            "task_id": " task-a ",
            "problem": " prompt ",
            "rubric": {"criterion": "exact"},
            "id": "task-a",
            "prompt": "prompt",
        }
    ]
    assert draco_artifact_index.parse_maybe_json("plain rubric") == "plain rubric"


def test_recovery_cli_arguments_are_manifested_and_reconstructed() -> None:
    args = runner.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B0,B1",
            "--task-ids",
            "task-a",
            "--task-ids",
            "task-c",
            "--agent-max-iterations",
            "0",
            "--deadline-wrapup-margin-seconds",
            "600",
            "--deadline-wrapup-disable-tools",
            "--deadline-thinking-off-margin-seconds",
            "600",
            "--max-iterations-includes-finalization",
            "--retrieval-loop-finalization-threshold",
            "3",
            "--finalization-aggregator-only",
            "--finalization-disable-thinking",
            "--continue-after-cost-audit-failure",
        ]
    )

    manifest = runner.manifest_args(args)
    reconstructed = runner.reconstructed_cli_args(args)
    assert manifest["task_ids"] == ["task-a", "task-c"]
    assert manifest["continue_after_cost_audit_failure"] is True
    assert manifest["agent_max_iterations"] == 0
    assert manifest["deadline_wrapup_margin_seconds"] == 600
    assert manifest["deadline_wrapup_disable_tools"] is True
    assert manifest["deadline_thinking_off_margin_seconds"] == 600
    assert manifest["max_iterations_includes_finalization"] is True
    assert manifest["retrieval_loop_finalization_threshold"] == 3
    assert manifest["finalization_aggregator_only"] is True
    assert manifest["finalization_disable_thinking"] is True
    assert reconstructed.count("--task-ids") == 2
    assert reconstructed[reconstructed.index("--agent-max-iterations") + 1] == "0"
    assert "--deadline-wrapup-disable-tools" in reconstructed
    assert "--max-iterations-includes-finalization" in reconstructed
    assert "--finalization-aggregator-only" in reconstructed
    assert "--finalization-disable-thinking" in reconstructed
    assert "--continue-after-cost-audit-failure" in reconstructed


@pytest.mark.asyncio
async def test_worker_failure_cancels_siblings_and_publishes_aborted_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(
        "\n".join(
            json.dumps({"id": task_id, "prompt": task_id})
            for task_id in ("task-a", "task-b")
        )
        + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    args = runner.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--groups",
            "B0",
            "--dry-run",
            "--concurrency",
            "2",
        ]
    )
    monkeypatch.setattr(runner.GatewayConfig, "load", lambda _path: GatewayConfig())
    slow_started = asyncio.Event()
    slow_cancelled = asyncio.Event()

    async def failing_or_slow_run_one(*_args, **kwargs):
        if kwargs["task"]["id"] == "task-b":
            slow_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                slow_cancelled.set()
                await asyncio.sleep(0)
                raise
        await slow_started.wait()
        raise RuntimeError("sensitive provider detail must not enter manifest")

    monkeypatch.setattr(runner, "run_one", failing_or_slow_run_one)

    with pytest.raises(RuntimeError, match="sensitive provider detail"):
        await runner.amain(args)

    assert slow_cancelled.is_set()
    manifest_paths = list(output_dir.glob("draco_run_*.manifest.json"))
    assert len(manifest_paths) == 1
    manifest_text = manifest_paths[0].read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["status"] == "aborted"
    assert manifest["rows_written"] == 0
    assert manifest["finished_at"] is not None
    assert manifest["failure"] == {
        "schema": "opensquilla.draco-task-supervisor-failure/v1",
        "stage": "task_execution",
        "exception_type": "RuntimeError",
        "scheduled_task_count": 2,
        "rows_written": 0,
        "model_or_judge_started": True,
        "cleanup": {
            "complete": True,
            "cancel_requested_count": 1,
            "cancelled_count": 1,
            "remaining_task_count": 0,
            "exception_types": ["RuntimeError"],
        },
    }
    assert "sensitive provider detail" not in manifest_text
    result_path = next(output_dir.glob("draco_ensemble_*.jsonl"))
    trace_path = next(output_dir.glob("draco_run_*.trace.jsonl"))
    checkpoint_path = next(output_dir.glob("draco_run_*.checkpoint.json"))
    assert result_path.read_text(encoding="utf-8") == ""
    assert trace_path.read_text(encoding="utf-8") == ""
    assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["rows_written"] == 0
    assert manifest["artifacts"]["checkpoint_json"] == str(checkpoint_path)


@pytest.mark.asyncio
async def test_aborted_manifest_uses_authoritative_count_after_commit_boundary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(
        json.dumps({"id": "task-a", "prompt": "task-a"}) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    args = runner.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--groups",
            "B0",
            "--dry-run",
        ]
    )
    monkeypatch.setattr(runner.GatewayConfig, "load", lambda _path: GatewayConfig())
    real_append = runner.AsyncDurableDracoArtifactWriter.append

    async def commit_then_fail(self, result, trace):
        await real_append(self, result, trace)
        raise RuntimeError("synthetic failure after durable commit")

    monkeypatch.setattr(
        runner.AsyncDurableDracoArtifactWriter,
        "append",
        commit_then_fail,
    )

    with pytest.raises(RuntimeError, match="after durable commit"):
        await runner.amain(args)

    manifest_path = next(output_dir.glob("draco_run_*.manifest.json"))
    checkpoint_path = next(output_dir.glob("draco_run_*.checkpoint.json"))
    result_path = next(output_dir.glob("draco_ensemble_*.jsonl"))
    trace_path = next(output_dir.glob("draco_run_*.trace.jsonl"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    capability = runner.durable_artifact_capability_contract()
    assert manifest["schema"] == runner.DRACO_RUN_MANIFEST_SCHEMA_V2
    assert manifest["durable_artifact_capability"] == capability
    assert manifest["status"] == "aborted"
    assert manifest["rows_written"] == 1
    assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["rows_written"] == 1
    assert len(result_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(trace_path.read_text(encoding="utf-8").splitlines()) == 1
    durable_row = json.loads(result_path.read_text(encoding="utf-8"))
    assert durable_row[runner.DRACO_DURABLE_RESULT_ROW_FIELD] == capability


@pytest.mark.asyncio
async def test_summary_failure_explicitly_releases_durable_run_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(
        json.dumps({"id": "task-a", "prompt": "task-a"}) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    args = runner.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--groups",
            "B0",
            "--dry-run",
        ]
    )
    monkeypatch.setattr(runner.GatewayConfig, "load", lambda _path: GatewayConfig())
    monkeypatch.setattr(
        runner,
        "summarize",
        lambda _rows: (_ for _ in ()).throw(RuntimeError("synthetic summary failure")),
    )

    with pytest.raises(RuntimeError, match="summary failure"):
        await runner.amain(args)

    checkpoint_path = next(output_dir.glob("draco_run_*.checkpoint.json"))
    lock_path = checkpoint_path.with_suffix(checkpoint_path.suffix + ".lock")
    with runner.DracoArtifactRunLock(lock_path):
        pass


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.asyncio
async def test_amain_outer_guard_releases_run_lock_after_cleanup_base_exception(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "cleanup-failure.lock"

    class SyntheticCleanupAbort(BaseException):
        pass

    async def fail_after_acquire(_args, *, run_lock_holder) -> int:
        run_lock = module.DracoArtifactRunLock(lock_path)
        run_lock.acquire()
        run_lock_holder[0] = run_lock
        raise SyntheticCleanupAbort

    monkeypatch.setattr(module, "_amain_with_run_lock", fail_after_acquire)

    with pytest.raises(SyntheticCleanupAbort):
        await module.amain(SimpleNamespace())

    with module.DracoArtifactRunLock(lock_path):
        pass


@pytest.mark.asyncio
async def test_preflight_failure_writes_audit_manifest_before_any_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(
        json.dumps({"id": "task-1", "prompt": "test prompt"}) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    args = runner.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--groups",
            "B1",
            "--runner-mode",
            "agent_loop",
            "--tool-mode",
            "local_web_tools",
            "--local-web-search-provider",
            "duckduckgo",
        ]
    )

    monkeypatch.setattr(runner.GatewayConfig, "load", lambda _path: GatewayConfig())
    monkeypatch.setattr(
        runner,
        "configure_local_web_search_runtime",
        lambda *_args, **_kwargs: {"provider": "duckduckgo"},
    )

    async def _failed_preflight(*_args, **_kwargs):
        raise RuntimeError("synthetic preflight failure")

    monkeypatch.setattr(runner, "run_local_web_tools_preflight", _failed_preflight)

    with pytest.raises(RuntimeError, match="synthetic preflight failure"):
        await runner.amain(args)

    manifests = list(output_dir.glob("*.preflight-failed.manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "preflight_failed"
    assert manifest["failure"]["stage"] == "local_web_tools_preflight"
    assert manifest["failure"]["model_or_judge_started"] is False
    assert manifest["rows_written"] == 0
    assert not list(output_dir.glob("draco_ensemble_*.jsonl"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("continue_after_failure", "expected_rows"),
    [(False, 2), (True, 2)],
    ids=["default", "deprecated-continue-flag"],
)
async def test_strict_non_byok_dry_run_skips_receipt_audit_but_keeps_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    continue_after_failure: bool,
    expected_rows: int,
) -> None:
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(
        "\n".join(
            json.dumps({"id": task_id, "prompt": f"prompt {task_id}"})
            for task_id in ("task-a", "task-b")
        )
        + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    argv = [
        "--input",
        str(input_path),
        "--output-dir",
        str(output_dir),
        "--groups",
        "B0",
        "--dry-run",
        "--require-openrouter-non-byok",
        "--concurrency",
        "1",
    ]
    if continue_after_failure:
        argv.append("--continue-after-cost-audit-failure")
    args = runner.build_parser().parse_args(argv)
    monkeypatch.setattr(runner.GatewayConfig, "load", lambda _path: GatewayConfig())

    status = await runner.amain(args)

    assert status == 0
    result_paths = list(output_dir.glob("draco_ensemble_*.jsonl"))
    trace_paths = list(output_dir.glob("draco_run_*.trace.jsonl"))
    checkpoint_paths = list(output_dir.glob("draco_run_*.checkpoint.json"))
    manifest_paths = list(output_dir.glob("draco_run_*.manifest.json"))
    assert len(result_paths) == 1
    assert len(trace_paths) == 1
    assert len(checkpoint_paths) == 1
    assert len(manifest_paths) == 1
    rows = [
        json.loads(line)
        for line in result_paths[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    manifest = json.loads(manifest_paths[0].read_text(encoding="utf-8"))
    trace_rows = [
        json.loads(line)
        for line in trace_paths[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    checkpoint = json.loads(checkpoint_paths[0].read_text(encoding="utf-8"))
    assert len(rows) == expected_rows
    assert len(trace_rows) == expected_rows
    assert checkpoint["rows_written"] == expected_rows
    assert manifest["rows_written"] == expected_rows
    assert manifest["artifacts"]["checkpoint_json"] == str(checkpoint_paths[0])
    assert manifest["status"] == "complete"
    assert (
        manifest["run_compatibility"]["contracts"]["B0"]["cost_policy"][
            "require_openrouter_non_byok"
        ]
        is True
    )
    assert all(not row["error"] for row in rows)
    assert all("openrouter_non_byok_audit" not in row for row in rows)


def test_local_web_fetch_runtime_disables_hidden_firecrawl_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "secret-not-serialized")
    policy = _local_web_tool_policy()

    runtime = runner.configure_local_web_fetch_runtime(policy)

    assert runtime["firecrawl_allowed"] is False
    assert runtime["firecrawl_api_key_active"] is False
    assert runtime["firecrawl_disabled_for_reproducibility"] is True
    assert "FIRECRAWL_API_KEY" not in runner.os.environ


def test_cost_accounting_includes_judge_and_never_prices_unknown_as_zero() -> None:
    generation_usage = {
        "model_usage_breakdown": [
            {
                "provider": "openrouter",
                "model": "known",
                "input_tokens": 100,
                "output_tokens": 20,
                "billed_cost": 0.10,
                "cost_source": "provider_billed",
                "provider_usage": _openrouter_exact_evidence(0.10, "gen-known"),
            },
            {
                "model": "unknown",
                "input_tokens": 200,
                "output_tokens": 40,
                "billed_cost": 0.0,
                "cost_source": "none",
            },
        ]
    }
    judge_attempts = [
        {
            "attempt": 1,
            "run": {
                "llm_request_count": 1,
                "usage": {
                    "provider": "openrouter",
                    "model": "judge-model",
                    "input_tokens": 50,
                    "output_tokens": 5,
                    "billed_cost": 0.03,
                    "cost_source": "provider_billed",
                    "provider_usage": _openrouter_exact_evidence(0.03, "judge-1"),
                },
            },
        },
        {
            "attempt": 2,
            "run": {
                "llm_request_count": 1,
                "usage": {
                    "provider": "openrouter",
                    "model": "judge-model",
                    "input_tokens": 60,
                    "output_tokens": 6,
                    "billed_cost": 0.04,
                    "cost_source": "provider_billed",
                    "provider_usage": _openrouter_exact_evidence(0.04, "judge-2"),
                },
            },
        },
    ]
    row = {
        "tool_policy": {"tool_mode": "provider_only"},
        "llm_request_count": 2,
        # Compatibility metrics are scoped to the accepted generation attempt.
        "usage": generation_usage,
        "execution": {
            "generation_attempts": [
                {
                    "attempt": 1,
                    "run": {"llm_request_count": 2, "usage": generation_usage},
                }
            ]
        },
        "judge": {
            "criterion_judgments": [
                {
                    "judge_attempts": judge_attempts,
                    # This is a duplicate of the final attempt and must not be counted.
                    "judge_run": judge_attempts[-1]["run"],
                }
            ]
        },
        "candidate_judges": [],
    }

    accounting = runner.row_cost_accounting(row)

    assert runner.usage_unknown_count_from_usage_payload(generation_usage) == 1
    assert accounting["generation"]["request_count"] == 2
    assert accounting["generation"]["unknown_request_count"] == 1
    assert accounting["generation"]["unknown_tokens"] == 240
    assert accounting["generation"]["recorded_cost_usd"] == pytest.approx(0.10)
    assert accounting["actual_generation_spend"]["recorded_cost_usd"] == (pytest.approx(0.10))
    assert accounting["judge"]["request_count"] == 2
    assert accounting["judge"]["recorded_cost_usd"] == pytest.approx(0.07)
    assert accounting["llm_total"]["recorded_cost_usd"] == pytest.approx(0.17)
    assert accounting["result_cost_complete"] is False


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_unpriced_brave_cost_is_separate_from_llm_completion(module) -> None:
    usage = {
        "provider": "openrouter",
        "model": "model-a",
        "requested_provider": "openrouter",
        "requested_model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            "brave-separate-cost",
        ),
    }
    prompt_hash = module.text_sha256("same prompt")
    row = {
        "group": "B1",
        "provider_spec": dict(module.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "total_tool_call_count": 1,
        "actual_spend_metrics": {
            "total_tool_call_count": 1,
            "llm_request_count": 1,
        },
        "tool_policy": {
            "tool_mode": "local_web_tools",
            "local_web_tools": {
                "web_search": {"provider": "brave"},
                "web_fetch": {"allow_firecrawl": False},
            },
        },
        "usage": usage,
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("brave-separate-cost-judge"),
    }

    accounting = module.row_cost_accounting(row)

    assert accounting["actual_llm_cost_complete"] is True
    assert accounting["actual_llm_cost_exact"] is True
    assert accounting["actual_external_tools"]["cost_complete"] is False
    assert accounting["actual_external_tools"]["cost_precision"] == "unknown"
    assert accounting["actual_external_tools"]["estimated_cost_usd"] is None
    assert accounting["actual_external_tools"]["recorded_cost_usd_is_lower_bound"] is True
    assert accounting["actual_external_tools"]["separate_from_task_completion"] is True
    assert accounting["actual_spend_cost_complete"] is False
    assert accounting["actual_spend_recorded_total_cost_is_lower_bound"] is True

    if module is resume_runner:
        state = module.resume_row_completion_state(
            module.seal_result_row(row),
            expected_prompt_sha256=prompt_hash,
            expected_task_input_sha256="sha256:task-input",
            expected_run_compatibility_fingerprint="sha256:run-contract",
        )
        assert state["generation_valid"] is True
        assert state["cost_metadata_complete"] is True
        assert state["action"] == "complete"


def test_resume_runner_does_not_force_full_host_access() -> None:
    resume_runner = _load_resume_runner()
    args = resume_runner.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B1",
            "--tool-mode",
            "local_web_tools",
        ]
    )
    policy = resume_runner.benchmark_tool_policy(args)

    context = resume_runner.build_benchmark_tool_context(
        task_id="task-1",
        group="B1",
        tool_policy=policy,
    )

    assert context.run_mode is None
    assert context.sandbox_run_context is None


def test_agent_llm_error_without_usage_is_counted_as_unknown_cost() -> None:
    breakdown = runner.aggregate_agent_model_usage(
        [
            {
                "kind": "llm_error",
                "payload": {"iteration": 2, "attempt": 1, "error": "timeout"},
            }
        ]
    )

    assert len(breakdown) == 1
    assert breakdown[0]["role"] == "agent_llm_request_unknown"
    accounting = runner.usage_cost_accounting(
        {"model_usage_breakdown": breakdown},
        expected_requests=1,
        scope="generation",
    )
    assert accounting["unknown_request_count"] == 1
    assert accounting["cost_exact"] is False
    assert runner.usage_unknown_count_from_usage_payload({"model_usage_breakdown": breakdown}) == 1


def test_agent_llm_error_without_usage_honors_missing_request_count() -> None:
    breakdown = runner.aggregate_agent_model_usage(
        [
            {
                "kind": "llm_error",
                "payload": {"iteration": 2, "attempt": 1, "usage_missing_count": 2},
            }
        ]
    )

    assert len(breakdown) == 2
    assert all(row["cost_source"] == "none" for row in breakdown)


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_agent_llm_usage_without_breakdown_preserves_native_receipt(module) -> None:
    receipt = ProviderBillingReceipt(
        currency="USD",
        status="confirmed",
        amount_nanos=10_000_000,
        usd_equivalent_nanos=10_000_000,
        fx_native_per_usd_nanos=1_000_000_000,
    )
    breakdown = module.aggregate_agent_model_usage(
        [
            {
                "kind": "llm_response",
                "payload": {
                    "iteration": 1,
                    "attempt": 1,
                    "usage_missing_count": 1,
                    "usage": {
                        "provider": "openrouter",
                        "model": "model-a",
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "billed_cost": 0.01,
                        "cost_source": "provider_billed",
                        "billing_receipt": receipt,
                    },
                },
            }
        ]
    )

    assert len(breakdown) == 2
    assert breakdown[0]["billing_receipt"] is receipt
    assert breakdown[1]["role"] == "agent_llm_request_unknown"


@pytest.mark.parametrize(
    ("physical_request_count", "expected_unknown"),
    [(0, 0), (2, 2)],
)
def test_run_result_summary_honors_explicit_physical_request_count(
    physical_request_count: int,
    expected_unknown: int,
) -> None:
    result = runner.RunResult(
        final_text="",
        done=None,
        error="provider_error",
        trace_events=[
            {
                "kind": "error",
                "code": "provider_error",
                "request_started": physical_request_count > 0,
                "physical_request_count": physical_request_count,
            }
        ],
    )

    summary = runner.run_result_summary(result)

    assert summary["llm_request_count"] == physical_request_count
    assert summary["usage_unknown_count"] == expected_unknown


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_run_result_summary_keeps_ensemble_trace_managed_only(module) -> None:
    trace = {"llm_request_count": 1, "physical_request_count": 1}
    unmanaged = module.RunResult(
        final_text="answer",
        done=DoneEvent(ensemble_trace=deepcopy(trace)),
        routing_trace={
            "selection_plan": {
                "ranking_thinking_assignment_enabled": False,
            }
        },
    )
    managed = module.RunResult(
        final_text="answer",
        done=DoneEvent(ensemble_trace=deepcopy(trace)),
        routing_trace={
            "selection_plan": {
                "ranking_thinking_assignment_enabled": True,
            }
        },
    )

    assert "ensemble_trace" not in module.run_result_summary(unmanaged)
    assert module.run_result_summary(managed)["ensemble_trace"] == trace


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_no_done_paid_setup_unknown_usage_is_counted(module) -> None:
    physical_attempt_id = "a" * 32
    unknown_analyzer = {
        "role": "unknown_request",
        "label": "task_analyzer",
        "provider": "",
        "model": "",
        "requested_provider": "openrouter",
        "requested_model": "anthropic/claude-opus-4.8",
        "input_tokens": 0,
        "output_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
        "attempt": 1,
        "physical_attempt_id": physical_attempt_id,
        "provider_usage": {
            "usage_unknown": True,
            "physical_attempt_id": physical_attempt_id,
        },
    }
    result = module.RunResult(
        final_text="",
        done=None,
        error="analyzer_cleanup_failed",
        setup_usage=[unknown_analyzer],
        trace_events=[
            {
                "kind": "error",
                "code": "provider_build_failed_after_setup",
                "request_started": False,
                "physical_request_count": 0,
            }
        ],
    )

    summary = module.run_result_summary(result)

    assert summary["llm_request_count"] == 1
    assert summary["usage_unknown_count"] == 1
    assert summary["usage"]["model_usage_breakdown"] == [unknown_analyzer]
    assert summary["setup_usage"] == [unknown_analyzer]


def test_error_diagnostic_reconciles_skewed_trace_and_disjoint_receipt() -> None:
    proposer_row = {
        "role": "proposer",
        "provider": "openrouter",
        "model": "p1",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
    }
    aggregator_row = {
        "role": "aggregator",
        "provider": "openrouter",
        "model": "agg",
        "input_tokens": 6,
        "output_tokens": 1,
        "billed_cost": 0.02,
        "cost_source": "provider_billed",
    }
    event = ErrorEvent(
        message="failed after two requests",
        model_usage_breakdown=[proposer_row],
        diagnostic_done=DoneEvent(
            input_tokens=6,
            output_tokens=1,
            billed_cost=0.02,
            cost_source="provider_billed",
            provider="openrouter",
            model="agg",
            model_usage_breakdown=[aggregator_row],
        ),
        ensemble_trace={
            "llm_request_count": 1,
            "physical_request_count": 1,
        },
        request_started=True,
        physical_request_count=2,
    )

    done = runner.diagnostic_done_from_error_event(event)

    assert done is not None
    assert done.model_usage_breakdown == [proposer_row, aggregator_row]
    assert done.ensemble_trace["llm_request_count"] == 2
    assert done.ensemble_trace["physical_request_count"] == 2
    assert done.usage_missing_count == 0
    assert done.billed_cost == pytest.approx(0.03)


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_error_diagnostic_preserves_identical_receipt_multiplicity(module) -> None:
    receipt = {
        "role": "proposer",
        "provider": "openrouter",
        "model": "same-model",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
    }
    event = ErrorEvent(
        message="two identical physical receipts",
        model_usage_breakdown=[receipt],
        diagnostic_done=DoneEvent(
            input_tokens=10,
            output_tokens=2,
            billed_cost=0.02,
            cost_source="provider_billed",
            provider="openrouter",
            model="same-model",
            model_usage_breakdown=[receipt, receipt],
        ),
        request_started=True,
        physical_request_count=2,
    )

    done = module.diagnostic_done_from_error_event(event)

    assert done is not None
    assert len(done.model_usage_breakdown) == 2
    assert done.billed_cost == pytest.approx(0.02)
    assert done.ensemble_trace["physical_request_count"] == 2
    assert done.usage_missing_count == 0


def test_error_diagnostic_infers_receipt_plus_missing_without_trace() -> None:
    event = ErrorEvent(
        message="one known and one unknown",
        model_usage_breakdown=[
            {
                "role": "proposer",
                "provider": "openrouter",
                "model": "p1",
                "input_tokens": 5,
                "output_tokens": 1,
                "billed_cost": 0.01,
                "cost_source": "provider_billed",
            }
        ],
        usage_missing_count=1,
    )

    done = runner.diagnostic_done_from_error_event(event)

    assert done is not None
    assert done.ensemble_trace["llm_request_count"] == 2
    assert done.usage_missing_count == 1


def test_error_diagnostic_explicit_zero_wins_over_stale_missing_marker() -> None:
    event = ErrorEvent(
        message="local preflight",
        request_started=False,
        physical_request_count=0,
        usage_missing_count=1,
    )

    assert runner.diagnostic_done_from_error_event(event) is None


def test_cost_accounting_does_not_reinflate_explicit_zero_request_attempts() -> None:
    no_request_run = {
        "llm_request_count": 0,
        "usage_unknown_count": 0,
        "usage": {},
        "trace_events": [
            {
                "kind": "error",
                "code": "provider_request_budget_exhausted",
                "request_started": False,
                "physical_request_count": 0,
            }
        ],
    }
    row = {
        "execution": {
            "generation_attempts": [
                {"attempt": 1, "run": no_request_run},
            ]
        }
    }
    judge = {
        "judge_attempt_count": 1,
        "judge_attempts": [
            {"attempt": 1, "run": no_request_run},
        ],
    }

    generation = runner.actual_generation_spend_accounting(row)
    judge_accounting = runner.judge_cost_accounting(judge, scope="judge")

    assert generation["request_count"] == 0
    assert generation["unknown_request_count"] == 0
    assert judge_accounting["request_count"] == 0
    assert judge_accounting["unknown_request_count"] == 0


@pytest.mark.asyncio
async def test_collect_run_preserves_partial_receipts_from_terminal_error() -> None:
    receipt = {
        "role": "proposer",
        "provider": "openrouter",
        "model": "model-a",
        "requested_provider": "openrouter",
        "requested_model": "model-a",
        "input_tokens": 10,
        "output_tokens": 2,
        "billed_cost": 0.05,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.05, "partial-error"),
    }

    class PartialFailureProvider:
        def chat(self, *_args, **_kwargs):
            async def events():
                yield ErrorEvent(
                    message="ensemble failed",
                    code="ensemble_all_failed",
                    model_usage_breakdown=[receipt],
                    usage_missing_count=1,
                    ensemble_trace={
                        "llm_request_count": 2,
                        "physical_request_count": 2,
                        "usage_missing_count": 1,
                    },
                )

            return events()

    result = await runner.collect_run(
        PartialFailureProvider(),
        "prompt",
        timeout=1.0,
    )
    summary = runner.run_result_summary(result)
    accounting = runner.usage_cost_accounting(
        summary["usage"],
        expected_requests=summary["llm_request_count"],
        scope="failed_generation",
    )

    assert result.error == "ensemble failed"
    assert result.done is not None
    assert result.done.stop_reason == "error"
    assert result.done.model_usage_breakdown == [receipt]
    assert summary["llm_request_count"] == 2
    assert summary["usage_unknown_count"] == 1
    assert accounting["request_count"] == 2
    assert accounting["exact_request_count"] == 1
    assert accounting["unknown_request_count"] == 1
    assert accounting["recorded_cost_usd"] == pytest.approx(0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module",
    [runner, resume_runner],
    ids=["main", "resume"],
)
async def test_collect_run_persists_only_stable_exception_type(module) -> None:
    private_detail = "private prompt api_key=secret-value Bearer private-upstream-token"

    class ExplodingProvider:
        def chat(self, *_args, **_kwargs):
            async def events():
                if False:
                    yield None
                raise RuntimeError(private_detail)

            return events()

    result = await module.collect_run(
        ExplodingProvider(),
        "prompt",
        timeout=1.0,
    )
    summary = module.run_result_summary(result)

    assert result.error == "RuntimeError"
    assert summary["error"] == "RuntimeError"
    assert summary["trace_events"][-1]["error"] == "RuntimeError"
    assert private_detail not in json.dumps(summary)


@pytest.mark.asyncio
async def test_collect_run_preserves_zero_request_ensemble_preflight() -> None:
    class PreflightFailureProvider:
        def chat(self, *_args, **_kwargs):
            async def events():
                yield ErrorEvent(
                    message="no request started",
                    code="ensemble_call_in_progress",
                    request_started=False,
                    physical_request_count=0,
                    ensemble_trace={
                        "llm_request_count": 0,
                        "physical_request_count": 0,
                        "usage_missing_count": 0,
                    },
                )

            return events()

    result = await runner.collect_run(
        PreflightFailureProvider(),
        "prompt",
        timeout=1.0,
    )
    summary = runner.run_result_summary(result)

    assert result.done is None
    assert result.error == "no request started"
    assert summary["llm_request_count"] == 0
    assert summary["usage_unknown_count"] == 0


@pytest.mark.asyncio
async def test_collect_run_keeps_outer_receipts_when_error_has_diagnostic_done() -> None:
    proposer_row = {
        "role": "proposer",
        "provider": "openrouter",
        "model": "proposer-model",
        "input_tokens": 8,
        "output_tokens": 2,
        "billed_cost": 0.02,
        "cost_source": "provider_billed",
        "billing_receipt": ProviderBillingReceipt(
            currency="USD",
            status="confirmed",
            amount_nanos=20_000_000,
            usd_equivalent_nanos=20_000_000,
            fx_native_per_usd_nanos=1_000_000_000,
        ),
    }
    aggregator_row = {
        "role": "aggregator",
        "provider": "openrouter",
        "model": "aggregator-model",
        "input_tokens": 12,
        "output_tokens": 1,
        "billed_cost": 0.03,
        "cost_source": "provider_billed",
        "billing_receipt": ProviderBillingReceipt(
            currency="USD",
            status="confirmed",
            amount_nanos=30_000_000,
            usd_equivalent_nanos=30_000_000,
            fx_native_per_usd_nanos=1_000_000_000,
        ),
    }
    diagnostic = DoneEvent(
        input_tokens=12,
        output_tokens=1,
        billed_cost=0.03,
        cost_source="provider_billed",
        provider="openrouter",
        model="aggregator-model",
        model_usage_breakdown=[aggregator_row],
    )

    class PartialDiagnosticFailureProvider:
        def chat(self, *_args, **_kwargs):
            async def events():
                yield ErrorEvent(
                    message="aggregator metadata invalid",
                    code="aggregator_metadata_invalid",
                    model_usage_breakdown=[proposer_row, aggregator_row],
                    diagnostic_done=diagnostic,
                    ensemble_trace={
                        "llm_request_count": 2,
                        "physical_request_count": 2,
                        "usage_missing_count": 0,
                    },
                )

            return events()

    result = await runner.collect_run(
        PartialDiagnosticFailureProvider(),
        "prompt",
        timeout=1.0,
    )
    summary = runner.run_result_summary(result)

    assert result.done is not diagnostic
    assert result.done is not None
    assert result.done.model_usage_breakdown == [proposer_row, aggregator_row]
    assert result.done.billed_cost == pytest.approx(0.05)
    assert summary["llm_request_count"] == 2
    assert summary["usage_unknown_count"] == 0


@pytest.mark.parametrize(
    "placeholder_role",
    sorted(runner.MISSING_USAGE_PLACEHOLDER_ROLES),
)
def test_agent_usage_does_not_double_count_missing_usage_placeholder(
    placeholder_role: str,
) -> None:
    breakdown = runner.aggregate_agent_model_usage(
        [
            {
                "kind": "llm_response",
                "payload": {
                    "iteration": 1,
                    "attempt": 1,
                    "usage_missing_count": 1,
                    "usage": {
                        "model_usage_breakdown": [
                            {
                                "role": placeholder_role,
                                "provider": "openrouter",
                                "model": "model-a",
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "billed_cost": 0.0,
                                "cost_source": "none",
                            }
                        ]
                    },
                },
            }
        ]
    )

    assert len(breakdown) == 1
    assert breakdown[0]["role"] == placeholder_role
    assert runner.usage_unknown_count_from_usage_payload({"model_usage_breakdown": breakdown}) == 1


def test_agent_usage_does_not_treat_generic_unpriced_row_as_missing_placeholder() -> None:
    breakdown = runner.aggregate_agent_model_usage(
        [
            {
                "kind": "llm_error",
                "payload": {
                    "iteration": 1,
                    "attempt": 1,
                    "usage_missing_count": 1,
                    "usage": {
                        "model_usage_breakdown": [
                            {
                                "role": "proposer",
                                "provider": "openrouter",
                                "model": "model-a",
                                "input_tokens": 3,
                                "output_tokens": 1,
                                "billed_cost": 0.0,
                                "cost_source": "none",
                            }
                        ]
                    },
                },
            }
        ]
    )

    assert len(breakdown) == 2
    assert [row["role"] for row in breakdown] == [
        "proposer",
        "agent_llm_request_unknown",
    ]


def test_agent_retry_partial_error_preserves_missing_physical_request() -> None:
    first_attempt_rows = [
        {
            "provider": "openrouter",
            "model": f"proposer-{index}",
            "input_tokens": 10,
            "output_tokens": 2,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(0.01, f"failed-attempt-proposer-{index}"),
        }
        for index in range(4)
    ]
    retry_rows = [
        {
            "provider": "openrouter",
            "model": f"retry-model-{index}",
            "input_tokens": 10,
            "output_tokens": 2,
            "billed_cost": 0.02,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(0.02, f"successful-retry-{index}"),
        }
        for index in range(5)
    ]

    breakdown = runner.aggregate_agent_model_usage(
        [
            {
                "kind": "llm_error",
                "payload": {
                    "iteration": 1,
                    "attempt": 1,
                    "usage_missing_count": 1,
                    "usage": {"model_usage_breakdown": first_attempt_rows},
                },
            },
            {
                "kind": "llm_response",
                "payload": {
                    "iteration": 1,
                    "attempt": 2,
                    "usage": {"model_usage_breakdown": retry_rows},
                },
            },
        ]
    )

    assert len(breakdown) == 10
    assert sum(row["cost_source"] == "provider_billed" for row in breakdown) == 9
    unknown = [row for row in breakdown if row["cost_source"] == "none"]
    assert len(unknown) == 1
    assert unknown[0]["role"] == "agent_llm_request_unknown"
    accounting = runner.usage_cost_accounting(
        {"model_usage_breakdown": breakdown},
        expected_requests=9,
        scope="generation",
    )
    assert accounting["request_count"] == 10
    assert accounting["unknown_request_count"] == 1
    assert accounting["cost_exact"] is False


def test_estimated_and_mixed_usage_record_their_actual_cost_fields() -> None:
    accounting = runner.usage_cost_accounting(
        {
            "model_usage_breakdown": [
                {
                    "provider": "openrouter",
                    "model": "estimated-model",
                    "input_tokens": 3,
                    "output_tokens": 1,
                    "billed_cost": 0.0,
                    "estimated_cost_usd": 0.40,
                    "cost_usd": 0.40,
                    "cost_source": "opensquilla_estimate",
                },
                {
                    "provider": "openrouter",
                    "model": "mixed-model",
                    "input_tokens": 4,
                    "output_tokens": 2,
                    "billed_cost": 0.10,
                    "billed_cost_usd": 0.10,
                    "estimated_cost_usd": 0.20,
                    "cost_usd": 0.30,
                    "cost_source": "mixed",
                },
            ]
        },
        expected_requests=2,
        scope="generation",
    )

    assert accounting["estimated_request_count"] == 1
    assert accounting["mixed_request_count"] == 1
    assert accounting["recorded_cost_usd"] == pytest.approx(0.70)


def test_agent_model_usage_preserves_physical_provider_evidence() -> None:
    provider_usage = _openrouter_exact_evidence(0.01, "generation-1")
    breakdown = runner.aggregate_agent_model_usage(
        [
            {
                "kind": "llm_response",
                "payload": {
                    "iteration": 1,
                    "call_attempt": 1,
                    "usage": {
                        "provider": "openrouter",
                        "model": "model-a",
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "billed_cost": 0.01,
                        "cost_source": "provider_billed",
                        "provider_usage": provider_usage,
                    },
                },
            }
        ]
    )

    assert breakdown[0]["provider"] == "openrouter"
    assert breakdown[0]["provider_usage"] == provider_usage
    assert (
        runner.usage_cost_accounting(
            {"model_usage_breakdown": breakdown},
            expected_requests=1,
            scope="generation",
        )["cost_exact"]
        is True
    )


def test_failed_agent_attempt_synthesizes_diagnostic_usage_from_recorder() -> None:
    recorder = runner.BenchmarkTurnCallRecorder()
    recorder.write(
        "llm_error",
        {
            "iteration": 1,
            "call_attempt": 1,
            "usage": {
                "provider": "openrouter",
                "model": "model-a",
                "input_tokens": 12,
                "output_tokens": 3,
                "billed_cost": 0.25,
                "cost_source": "provider_billed",
                "provider_usage": _openrouter_exact_evidence(0.25, "failed-1"),
            },
            "error": {"code": "timeout", "message": "timed out"},
        },
    )

    done = runner.provider_done_from_agent_done(
        None,
        recorder=recorder,
        fallback_model="model-a",
    )

    assert done is not None
    assert done.stop_reason == "error"
    assert done.input_tokens == 12
    assert done.output_tokens == 3
    assert done.billed_cost == pytest.approx(0.25)
    assert done.provider_usage["diagnostic_usage_only"] is True
    assert done.model_usage_breakdown[0]["provider_usage"]["response_ids"] == ["failed-1"]
    assert (
        runner.usage_cost_accounting(
            runner.done_payload(done),
            expected_requests=1,
            scope="generation",
        )["cost_exact"]
        is True
    )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_all_untraced_agent_calls_preserve_unknown_physical_requests(module) -> None:
    recorder = module.BenchmarkTurnCallRecorder()
    recorder.write(
        "llm_abandoned",
        {
            "call_id": "call-1",
            "iteration": 1,
            "attempt": 1,
            "usage_missing_count": 1,
        },
    )
    recorder.write(
        "llm_error",
        {
            "call_id": "call-2",
            "iteration": 2,
            "attempt": 1,
            "usage_missing_count": 2,
        },
    )

    trace = module.aggregate_agent_ensemble_trace(recorder.records)
    done = module.provider_done_from_agent_done(
        None,
        recorder=recorder,
        fallback_model="fallback-model",
    )

    assert trace["agent_llm_call_count"] == 2
    assert trace["untraced_agent_llm_call_count"] == 2
    assert trace["llm_request_count"] == 3
    assert [call["request_outcome"] for call in trace["calls"]] == [
        "llm_abandoned",
        "llm_error",
    ]
    assert all(call["trace_missing"] is True for call in trace["calls"])
    assert done is not None
    assert done.stop_reason == "error"
    assert done.model == ""
    assert done.requested_model == "fallback-model"
    assert len(done.model_usage_breakdown) == 3
    assert module.usage_unknown_count_from_usage_payload(module.done_payload(done)) == 3


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    "placeholder_role",
    sorted(runner.MISSING_USAGE_PLACEHOLDER_ROLES),
)
def test_missing_usage_placeholders_never_supply_actual_identity(
    module,
    placeholder_role: str,
) -> None:
    placeholder = {
        "role": placeholder_role,
        "provider": "openrouter",
        "model": "requested-model",
        "requested_provider": "openrouter",
        "requested_model": "requested-model",
        "input_tokens": 0,
        "output_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
    }
    result = module.RunResult(
        final_text="answer",
        done=DoneEvent(
            requested_provider="openrouter",
            requested_model="requested-model",
            stop_reason="stop",
            model_usage_breakdown=[placeholder],
        ),
    )

    assert (
        module.single_generation_identity_reason(
            result,
            expected_provider="openrouter",
            expected_model="requested-model",
        )
        == "actual_model_evidence_missing"
    )

    recorder = module.BenchmarkTurnCallRecorder()
    recorder.write(
        "llm_error",
        {
            "usage_missing_count": 1,
            "usage": {"model_usage_breakdown": [placeholder]},
        },
    )
    diagnostic = module.provider_done_from_agent_done(
        None,
        recorder=recorder,
        fallback_model="requested-model",
    )

    assert diagnostic is not None
    assert diagnostic.model == ""
    assert diagnostic.provider == ""
    assert diagnostic.requested_model == "requested-model"
    assert module.usage_unknown_count_from_usage_payload(module.done_payload(diagnostic)) == 1


@pytest.mark.parametrize(
    "placeholder_role",
    sorted(runner.MISSING_USAGE_PLACEHOLDER_ROLES),
)
def test_resume_actual_identity_evidence_ignores_missing_usage_placeholders(
    placeholder_role: str,
) -> None:
    evidence = resume_runner.usage_actual_identity_evidence(
        {
            "model_usage_breakdown": [
                {
                    "role": placeholder_role,
                    "provider": "openrouter",
                    "model": "requested-model",
                }
            ]
        }
    )

    assert evidence["receipt_count"] == 0
    assert evidence["provider"] == ""
    assert evidence["model"] == ""


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_diagnostic_explicit_zero_discards_stale_placeholder(module) -> None:
    placeholder = {
        "role": "abandoned_stream_request",
        "provider": "openrouter",
        "model": "requested-model",
        "cost_source": "none",
    }
    event = ErrorEvent(
        message="request was rejected before send",
        code="preflight",
        model_usage_breakdown=[placeholder],
        request_started=False,
        physical_request_count=0,
    )

    assert module.diagnostic_done_from_error_event(event) is None


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize("diagnostic_id_first", [False, True])
def test_diagnostic_receipt_matching_prioritizes_response_ids(
    module,
    diagnostic_id_first: bool,
) -> None:
    def receipt(response_id: str | None) -> dict[str, object]:
        return {
            "provider": "openrouter",
            "model": "same-model",
            "input_tokens": 5,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": ({"response_ids": [response_id]} if response_id is not None else {}),
        }

    diagnostic_rows = [receipt("response-a"), receipt(None)]
    if not diagnostic_id_first:
        diagnostic_rows.reverse()
    event = ErrorEvent(
        message="diagnostic copies arrive in an adversarial order",
        code="response_invalid",
        model_usage_breakdown=[receipt("response-a"), receipt("response-b")],
        diagnostic_done=DoneEvent(
            input_tokens=10,
            output_tokens=2,
            billed_cost=0.02,
            cost_source="provider_billed",
            model_usage_breakdown=diagnostic_rows,
        ),
        request_started=True,
        physical_request_count=2,
    )

    done = module.diagnostic_done_from_error_event(event)

    assert done is not None
    assert len(done.model_usage_breakdown) == 2
    assert done.ensemble_trace["physical_request_count"] == 2
    assert done.usage_missing_count == 0


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_diagnostic_same_id_uses_richer_authoritative_receipt(module) -> None:
    outer = {
        "provider": "",
        "model": "",
        "requested_provider": "openrouter",
        "requested_model": "requested-model",
        "input_tokens": 0,
        "output_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
        "provider_usage": {"response_ids": ["response-a"]},
    }
    richer = {
        **outer,
        "provider": "openrouter",
        "model": "actual-model",
        "input_tokens": 7,
        "output_tokens": 3,
        "billed_cost": 0.25,
        "cost_source": "provider_billed",
    }
    event = ErrorEvent(
        message="outer wrapper retained partial usage",
        code="response_invalid",
        model_usage_breakdown=[outer],
        diagnostic_done=DoneEvent(
            input_tokens=7,
            output_tokens=3,
            billed_cost=0.25,
            cost_source="provider_billed",
            model_usage_breakdown=[richer],
        ),
        request_started=True,
        physical_request_count=1,
    )

    done = module.diagnostic_done_from_error_event(event)

    assert done is not None
    assert len(done.model_usage_breakdown) == 1
    assert done.model == "actual-model"
    assert done.provider == "openrouter"
    assert done.input_tokens == 7
    assert done.output_tokens == 3
    assert done.billed_cost == pytest.approx(0.25)


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_nested_diagnostic_missing_evidence_is_preserved(module) -> None:
    outer = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 4,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
    }
    nested = {
        **outer,
        "model": "model-b",
    }
    event = ErrorEvent(
        message="nested composite retained one unknown request",
        code="response_invalid",
        model_usage_breakdown=[outer],
        diagnostic_done=DoneEvent(
            input_tokens=4,
            output_tokens=1,
            billed_cost=0.01,
            cost_source="provider_billed",
            model_usage_breakdown=[nested],
            usage_missing_count=1,
            ensemble_trace={
                "llm_request_count": 2,
                "physical_request_count": 2,
                "usage_missing_count": 1,
            },
        ),
        request_started=True,
    )

    done = module.diagnostic_done_from_error_event(event)

    assert done is not None
    assert len(done.model_usage_breakdown) == 2
    assert done.usage_missing_count == 1
    assert done.ensemble_trace["physical_request_count"] == 3


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_native_receipt_precedence_is_shared_by_cost_paths(module) -> None:
    confirmed = ProviderBillingReceipt(
        currency="USD",
        status="confirmed",
        amount_nanos=200_000_000,
        usd_equivalent_nanos=200_000_000,
        fx_native_per_usd_nanos=1_000_000_000,
    )
    pending = ProviderBillingReceipt(
        currency="USD",
        status="pending",
        amount_nanos=None,
        usd_equivalent_nanos=None,
        fx_native_per_usd_nanos=1_000_000_000,
    )
    base = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.1,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.1, "response-a"),
    }
    exact = {**base, "billing_receipt": confirmed}
    unresolved = {**base, "billing_receipt": pending}

    exact_accounting = module.usage_cost_accounting(
        {"model_usage_breakdown": [exact]},
        expected_requests=1,
        scope="generation",
    )
    pending_accounting = module.usage_cost_accounting(
        {"model_usage_breakdown": [unresolved]},
        expected_requests=1,
        scope="generation",
    )

    assert module.exact_provider_usage_cost(exact) == pytest.approx(0.2)
    assert exact_accounting["recorded_cost_usd"] == pytest.approx(0.2)
    assert exact_accounting["exact_request_count"] == 1
    assert module.exact_provider_usage_cost(unresolved) is None
    assert module.trusted_provider_billed_cost(unresolved) == 0.0
    assert pending_accounting["unknown_request_count"] == 1


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_native_receipt_requires_complete_schema_and_fails_closed(module) -> None:
    valid = {
        "currency": "USD",
        "status": "confirmed",
        "amount_nanos": 10_000_000,
        "usd_equivalent_nanos": 10_000_000,
        "fx_native_per_usd_nanos": 1_000_000_000,
        "schema_version": 1,
    }
    invalid_receipts = [
        {**valid, "currency": "US"},
        {**valid, "currency": "usd"},
        {**valid, "currency": "ＵＳＤ"},
        {**valid, "amount_nanos": None},
        {**valid, "usd_equivalent_nanos": 9_999_999},
        {**valid, "fx_native_per_usd_nanos": 0},
        {**valid, "amount_nanos": 1 << 63},
        {**valid, "usd_equivalent_nanos": 1 << 63},
        {**valid, "fx_native_per_usd_nanos": 1 << 63},
        {**valid, "schema_version": True},
        {**valid, "schema_version": 2},
        {**valid, "status": "pending"},
    ]
    base = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.01, "response-a"),
    }

    assert module.exact_provider_usage_cost({**base, "billing_receipt": valid}) == (
        pytest.approx(0.01)
    )
    for receipt in invalid_receipts:
        unit = {**base, "billing_receipt": receipt}
        assert module.exact_provider_usage_cost(unit) is None
        assert module.trusted_provider_billed_cost(unit) == 0.0
        audit = module.openrouter_non_byok_audit({"llm_request_count": 1, "usage": unit})
        assert audit["pass"] is False
        assert audit["unverified_or_byok_request_count"] == 1


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_non_byok_audit_is_stricter_than_ordinary_cost_accounting(module) -> None:
    native_receipt = {
        "currency": "USD",
        "status": "confirmed",
        "amount_nanos": 10_000_000,
        "usd_equivalent_nanos": 10_000_000,
        "fx_native_per_usd_nanos": 1_000_000_000,
        "schema_version": 1,
    }
    generic_confirmed = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "billing_receipt": native_receipt,
        "provider_usage": {},
    }
    legacy_openrouter_usage = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "openrouter_usage",
        "provider_usage": {},
    }
    missing_serving_provider_metadata = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": {
            "is_byok": False,
            "provider_reported_cost": 0.01,
            "response_ids": ["response-a"],
            "router_metadata": {"is_byok": False},
        },
    }

    for unit in (
        generic_confirmed,
        legacy_openrouter_usage,
        missing_serving_provider_metadata,
    ):
        assert module.exact_provider_usage_cost(unit) == pytest.approx(0.01)
        audit = module.openrouter_non_byok_audit({"llm_request_count": 1, "usage": unit})
        assert audit["pass"] is False
        assert audit["unverified_or_byok_request_count"] == 1

    fully_attested = {
        **generic_confirmed,
        "provider_usage": _openrouter_exact_evidence(0.01, "response-a"),
    }
    assert (
        module.openrouter_non_byok_audit({"llm_request_count": 1, "usage": fully_attested})["pass"]
        is True
    )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_duplicate_response_receipt_counts_as_one_physical_request(module) -> None:
    receipt = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.01, "response-duplicate"),
    }
    usage = {"model_usage_breakdown": [receipt, json.loads(json.dumps(receipt))]}

    accounting = module.usage_cost_accounting(
        usage,
        expected_requests=2,
        scope="generation",
    )
    audit = module.openrouter_non_byok_audit(
        {
            "llm_request_count": 2,
            "usage": usage,
        }
    )

    assert accounting["request_count"] == 2
    assert accounting["usage_observed_request_count"] == 1
    assert accounting["duplicate_stable_receipt_count"] == 1
    assert accounting["recorded_cost_usd"] == pytest.approx(0.01)
    assert accounting["exact_request_count"] == 1
    assert accounting["unknown_request_count"] == 1
    assert accounting["cost_exact"] is False
    assert audit["pass"] is False
    assert audit["exact_request_count"] == 1
    assert audit["unverified_or_byok_request_count"] == 1

    # Matching token/cost values alone do not prove two rows are one call.
    without_stable_id = [
        {"input_tokens": 5, "output_tokens": 1, "billed_cost": 0.01},
        {"input_tokens": 5, "output_tokens": 1, "billed_cost": 0.01},
    ]
    assert len(module.deduplicate_stable_usage_receipts(without_stable_id)) == 2


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_duplicate_response_receipt_preserves_contradictory_byok_evidence(
    module,
    reverse: bool,
) -> None:
    exact = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            "response-conflicting-byok",
        ),
    }
    explicit_byok = {
        **exact,
        "provider_usage": {
            **_openrouter_exact_evidence(
                0.01,
                "response-conflicting-byok",
            ),
            "is_byok": True,
            "router_metadata": {
                **_openrouter_exact_evidence(
                    0.01,
                    "unused",
                )["router_metadata"],
                "is_byok": True,
            },
        },
    }
    units = [exact, explicit_byok]
    if reverse:
        units.reverse()

    deduplicated = module.deduplicate_stable_usage_receipts(units)
    audit = module.openrouter_non_byok_audit(
        {
            "llm_request_count": 1,
            "usage": {"model_usage_breakdown": units},
        }
    )

    assert len(deduplicated) == 1
    evidence = deduplicated[0]["provider_usage"][module.STABLE_RECEIPT_EVIDENCE_KEY]
    assert evidence["usage_is_byok_values"] == [False, True]
    assert evidence["router_is_byok_values"] == [False, True]
    assert evidence["receipt_conflict"] is True
    assert audit["pass"] is False
    assert audit["status"] == "policy_violation"
    assert audit["conflict_request_count"] == 1


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize(
    "conflicting_field",
    ["provider", "model", "cost", "input_tokens"],
)
def test_duplicate_response_receipt_preserves_other_conflicts_order_independently(
    module,
    reverse: bool,
    conflicting_field: str,
) -> None:
    first = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            "response-conflicting-receipt",
        ),
    }
    second = json.loads(json.dumps(first))
    if conflicting_field == "provider":
        second["provider"] = "another-provider"
    elif conflicting_field == "model":
        second["model"] = "model-b"
    elif conflicting_field == "cost":
        second["billed_cost"] = 0.02
        second["provider_usage"]["provider_reported_cost"] = 0.02
    else:
        second["input_tokens"] = 6
    units = [first, second]
    if reverse:
        units.reverse()

    deduplicated = module.deduplicate_stable_usage_receipts(units)
    audit = module.openrouter_non_byok_audit(
        {
            "llm_request_count": 1,
            "usage": {"model_usage_breakdown": units},
        }
    )

    assert len(deduplicated) == 1
    evidence = deduplicated[0]["provider_usage"][module.STABLE_RECEIPT_EVIDENCE_KEY]
    assert evidence["receipt_conflict"] is True
    assert audit["pass"] is False
    assert audit["status"] == "policy_violation"
    assert audit["conflict_request_count"] == 1


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_duplicate_response_receipt_is_deduplicated_across_generation_attempts(
    module,
) -> None:
    receipt = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            "response-cross-attempt",
        ),
    }
    run = {"llm_request_count": 1, "usage": receipt}
    row = {
        "generation_attempt_count": 2,
        "execution": {
            "generation_attempts": [
                {"run": run},
                {"run": json.loads(json.dumps(run))},
            ]
        },
    }

    accounting = module.actual_generation_spend_accounting(row)

    assert accounting["request_count"] == 2
    assert accounting["usage_observed_request_count"] == 1
    assert accounting["duplicate_stable_receipt_count"] == 1
    assert accounting["recorded_cost_usd"] == pytest.approx(0.01)
    assert accounting["exact_request_count"] == 1
    assert accounting["unknown_request_count"] == 1
    assert accounting["cost_exact"] is False


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_duplicate_response_receipt_is_deduplicated_across_llm_scopes(module) -> None:
    receipt = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            "response-cross-scope",
        ),
    }
    judge = {
        "judge_model": "judge-model",
        "judge_attempt_count": 1,
        "judge_attempts": [
            {
                "run": {
                    "llm_request_count": 1,
                    "usage": json.loads(json.dumps(receipt)),
                }
            }
        ],
    }
    row = {
        "final_text": "answer",
        "llm_request_count": 1,
        "usage": receipt,
        "judge": judge,
        "candidate_judges": [json.loads(json.dumps(judge))],
    }

    accounting = module.row_cost_accounting(row)
    for total_name in ("llm_total", "actual_llm_total"):
        total = accounting[total_name]
        assert total["request_count"] == 3
        assert total["usage_observed_request_count"] == 1
        assert total["duplicate_stable_receipt_count"] == 2
        assert total["recorded_cost_usd"] == pytest.approx(0.01)
        assert total["exact_request_count"] == 1
        assert total["unknown_request_count"] == 2
        assert total["cost_exact"] is False

    audit = module.openrouter_non_byok_audit(row)
    assert audit["pass"] is False
    assert audit["request_count"] == 3
    assert audit["exact_request_count"] == 1
    assert audit["unverified_or_byok_request_count"] == 2

    public_accounting = module.public_cost_accounting(accounting)
    serialized = json.dumps(public_accounting)
    assert "_stable_usage_receipts" not in serialized
    assert "_receipt_provenance_complete" not in serialized


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_unrepresented_missing_request_increases_all_request_counts(module) -> None:
    receipt = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 5,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
    }
    done = DoneEvent(
        input_tokens=5,
        output_tokens=1,
        billed_cost=0.01,
        cost_source="provider_billed",
        model_usage_breakdown=[receipt],
        usage_missing_count=1,
    )
    result = module.RunResult(final_text="answer", done=done)
    row = {"usage": module.done_payload(done)}

    assert (
        module.llm_request_count_for_run(
            spec={"kind": "single"},
            done=done,
            provider_attempted=True,
        )
        == 2
    )
    assert module.run_result_summary(result)["llm_request_count"] == 2
    assert module.row_llm_request_count(row) == 2


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_no_breakdown_receipt_plus_missing_respects_total_request_count(module) -> None:
    done = DoneEvent(
        provider="openrouter",
        model="model-a",
        input_tokens=5,
        output_tokens=1,
        billed_cost=0.01,
        cost_source="provider_billed",
        usage_missing_count=2,
        ensemble_trace={
            "llm_request_count": 3,
            "physical_request_count": 3,
        },
    )
    result = module.RunResult(final_text="answer", done=done)
    row = {"llm_request_count": 3, "usage": module.done_payload(done)}

    assert (
        module.llm_request_count_for_run(
            spec={"kind": "single"},
            done=done,
            provider_attempted=True,
        )
        == 3
    )
    assert module.run_result_summary(result)["llm_request_count"] == 3
    assert module.row_llm_request_count(row) == 3


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_conflicting_ensemble_request_counts_fail_closed(module) -> None:
    done = DoneEvent(
        provider="openrouter",
        model="model-a",
        ensemble_trace={
            "llm_request_count": 2,
            "physical_request_count": 3,
        },
    )

    with pytest.raises(
        ValueError,
        match="conflicting physical request count declarations",
    ):
        module.done_payload(done)


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_done_without_recorder_breakdown_preserves_receipt_and_missing(
    module,
) -> None:
    receipt = ProviderBillingReceipt(
        currency="USD",
        status="confirmed",
        amount_nanos=10_000_000,
        usd_equivalent_nanos=10_000_000,
        fx_native_per_usd_nanos=1_000_000_000,
    )
    done = AgentDoneEvent(
        text="answer",
        input_tokens=5,
        output_tokens=1,
        cost_usd=0.01,
        billed_cost=0.01,
        cost_source="provider_billed",
        provider="openrouter",
        model="model-a",
        requested_provider="openrouter",
        requested_model="model-a",
    )
    setattr(done, "billing_receipt", receipt)
    setattr(done, "provider_usage", _openrouter_exact_evidence(0.01, "response-a"))
    setattr(done, "usage_missing_count", 2)

    provider_done = module.provider_done_from_agent_done(
        done,
        recorder=module.BenchmarkTurnCallRecorder(),
        fallback_model="model-a",
    )

    assert provider_done is not None
    assert provider_done.billing_receipt is receipt
    assert provider_done.usage_missing_count == 2
    assert len(provider_done.model_usage_breakdown) == 1
    assert provider_done.ensemble_trace["physical_request_count"] == 3
    assert module.done_payload(provider_done)["billed_cost"] == pytest.approx(0.01)


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_done_rollup_does_not_duplicate_recorded_physical_requests(
    module,
) -> None:
    recorder = module.BenchmarkTurnCallRecorder()
    for index, (input_tokens, output_tokens, billed_cost) in enumerate(
        [(5, 1, 0.01), (7, 2, 0.02)],
        start=1,
    ):
        recorder.write(
            "llm_response",
            {
                "iteration": index,
                "call_attempt": 1,
                "usage": {
                    "provider": "openrouter",
                    "model": "model-a",
                    "requested_provider": "openrouter",
                    "requested_model": "model-a",
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "billed_cost": billed_cost,
                    "cost_source": "provider_billed",
                    "provider_usage": _openrouter_exact_evidence(
                        billed_cost,
                        f"response-{index}",
                    ),
                },
            },
        )

    # AgentDoneEvent is the aggregate envelope for both recorded calls. It is
    # not a third physical request and must not be appended to the breakdown.
    done = AgentDoneEvent(
        text="answer",
        input_tokens=12,
        output_tokens=3,
        cost_usd=0.03,
        billed_cost=0.03,
        cost_source="opensquilla_static_estimate",
        provider="openrouter",
        model="model-a",
        requested_provider="openrouter",
        requested_model="model-a",
    )
    provider_done = module.provider_done_from_agent_done(
        done,
        recorder=recorder,
        fallback_model="model-a",
    )

    assert provider_done is not None
    assert len(provider_done.model_usage_breakdown) == 2
    assert {item["role"] for item in provider_done.model_usage_breakdown} == {"agent_llm_call"}
    usage = module.done_payload(provider_done)
    row = {"llm_request_count": 2, "usage": usage}
    accounting = module.usage_cost_accounting(
        usage,
        expected_requests=2,
        scope="generation",
    )

    assert accounting["request_count"] == 2
    assert accounting["usage_observed_request_count"] == 2
    assert accounting["exact_request_count"] == 2
    assert accounting["estimated_request_count"] == 0
    assert accounting["recorded_cost_usd"] == pytest.approx(0.03)
    assert accounting["cost_exact"] is True
    assert module.row_llm_request_count(row) == 2
    assert module.usage_unknown_count_from_usage_payload(usage) == 0
    non_byok_audit = module.openrouter_non_byok_audit(row)
    assert non_byok_audit["pass"] is True
    assert non_byok_audit["status"] == "exact"
    assert non_byok_audit["policy_safe_to_continue"] is True
    assert non_byok_audit["request_count"] == 2
    assert non_byok_audit["exact_request_count"] == 2
    assert non_byok_audit["unverified_request_count"] == 0
    assert non_byok_audit["explicit_byok_request_count"] == 0
    assert non_byok_audit["conflict_request_count"] == 0
    assert non_byok_audit["unverified_or_byok_request_count"] == 0


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_done_breakdown_rollups_do_not_duplicate_ensemble_receipts(
    module,
) -> None:
    recorder = module.BenchmarkTurnCallRecorder()
    physical_rows = []
    for index, (input_tokens, output_tokens, billed_cost) in enumerate(
        [(5, 1, 0.01), (7, 2, 0.02)],
        start=1,
    ):
        physical = {
            "role": "proposer",
            "label": "proposer_1",
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "billed_cost": billed_cost,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                billed_cost,
                f"ensemble-response-{index}",
            ),
        }
        physical_rows.append(physical)
        recorder.write(
            "llm_response",
            {
                "iteration": index,
                "call_attempt": 1,
                "usage": {"model_usage_breakdown": [physical]},
            },
        )

    done = AgentDoneEvent(
        text="answer",
        input_tokens=12,
        output_tokens=3,
        cost_usd=0.03,
        billed_cost=0.03,
        cost_source="opensquilla_static_estimate",
        provider="openrouter",
        model="model-a",
        requested_provider="openrouter",
        requested_model="model-a",
    )
    setattr(
        done,
        "model_usage_breakdown",
        [
            {
                "role": "proposer",
                "label": "proposer_1",
                "request_count": 2,
                "provider": "openrouter",
                "model": "model-a",
                "requested_provider": "openrouter",
                "requested_model": "model-a",
                "input_tokens": 12,
                "output_tokens": 3,
                "billed_cost": 0.03,
                "cost_source": "opensquilla_static_estimate",
            }
        ],
    )

    provider_done = module.provider_done_from_agent_done(
        done,
        recorder=recorder,
        fallback_model="model-a",
    )

    assert provider_done is not None
    assert len(provider_done.model_usage_breakdown) == 2
    assert all(
        item.get("agent_call_index") in {1, 2} for item in provider_done.model_usage_breakdown
    )
    assert provider_done.provider_usage["agent_done_summary_rows_ignored"] == 1
    usage = module.done_payload(provider_done)
    accounting = module.usage_cost_accounting(
        usage,
        expected_requests=2,
        scope="generation",
    )
    assert accounting["recorded_cost_usd"] == pytest.approx(0.03)
    assert accounting["total_tokens"] == 15
    assert accounting["cost_exact"] is True
    assert (
        module.openrouter_non_byok_audit({"llm_request_count": 2, "usage": usage})["pass"] is True
    )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    ("evidence_kind", "expected_classification", "expected_field"),
    [
        ("explicit_byok", "explicit_byok", None),
        ("is_byok_conflict", "conflict", "is_byok"),
        ("provider_conflict", "conflict", "provider"),
        ("model_conflict", "conflict", "model"),
        ("receipt_conflict", "conflict", "cost_usd_nanos"),
    ],
)
def test_ignored_agent_done_rollup_preserves_independent_policy_evidence(
    module,
    evidence_kind: str,
    expected_classification: str,
    expected_field: str | None,
) -> None:
    recorder = module.BenchmarkTurnCallRecorder()
    for index, cost in enumerate((0.01, 0.02), start=1):
        recorder.write(
            "llm_response",
            {
                "iteration": index,
                "call_attempt": 1,
                "usage": {
                    "provider": "openrouter",
                    "model": "model-a",
                    "input_tokens": index + 2,
                    "output_tokens": 1,
                    "billed_cost": cost,
                    "cost_source": "provider_billed",
                    "provider_usage": _openrouter_exact_evidence(
                        cost,
                        f"physical-{index}",
                    ),
                },
            },
        )
    summary_provider_usage: dict[str, object] = {}
    summary_provider = "openrouter"
    summary_model = "model-a"
    if evidence_kind == "explicit_byok":
        summary_provider_usage = {
            "is_byok": True,
            "router_metadata": {"is_byok": True},
        }
    elif evidence_kind == "is_byok_conflict":
        summary_provider_usage = {
            "is_byok": False,
            "router_metadata": {"is_byok": True},
        }
    elif evidence_kind == "provider_conflict":
        summary_provider = "unexpected-provider"
    elif evidence_kind == "model_conflict":
        summary_model = "unexpected-model"
    else:
        summary_provider_usage = {
            module.STABLE_RECEIPT_EVIDENCE_KEY: {
                "conflict_fields": ["cost_usd_nanos"],
                "receipt_conflict": True,
            }
        }
    done = AgentDoneEvent(
        text="answer",
        input_tokens=7,
        output_tokens=2,
        cost_usd=0.03,
        billed_cost=0.03,
        cost_source="opensquilla_static_estimate",
        provider="openrouter",
        model="model-a",
    )
    setattr(
        done,
        "model_usage_breakdown",
        [
            {
                "request_count": 2,
                "provider": summary_provider,
                "model": summary_model,
                "input_tokens": 7,
                "output_tokens": 2,
                "billed_cost": 0.03,
                "cost_source": "opensquilla_static_estimate",
                "provider_usage": summary_provider_usage,
            }
        ],
    )

    provider_done = module.provider_done_from_agent_done(
        done,
        recorder=recorder,
        fallback_model="model-a",
    )

    assert provider_done is not None
    assert len(provider_done.model_usage_breakdown) == 2
    usage = module.done_payload(provider_done)
    accounting = module.usage_cost_accounting(
        usage,
        expected_requests=2,
        scope="generation",
    )
    assert accounting["request_count"] == 2
    assert accounting["recorded_cost_usd"] == pytest.approx(0.03)
    assert accounting["total_tokens"] == 9
    evidence = provider_done.provider_usage[module.IGNORED_AGENT_DONE_POLICY_EVIDENCE_KEY]
    assert len(evidence) == 1
    assert evidence[0]["classification"] == expected_classification
    if expected_field is not None:
        assert expected_field in evidence[0]["conflict_fields"]
    audit = module.openrouter_non_byok_audit({"llm_request_count": 2, "usage": usage})
    assert audit["status"] == "policy_violation"
    assert audit["policy_safe_to_continue"] is False
    assert audit["request_count"] == 2
    assert audit["exact_request_count"] == 2
    assert audit["explicit_byok_request_count"] == 0
    assert audit["conflict_request_count"] == 0
    assert audit["independent_policy_evidence_count"] == 1


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_ignored_agent_done_rollup_accepts_frozen_serving_model_alias(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "_formal_openrouter_model_aliases",
        lambda: {
            "vendor/model-base": frozenset(
                {
                    "vendor/model-base",
                    "vendor/model-base-20260725",
                }
            )
        },
    )

    evidence = module.ignored_agent_done_summary_policy_evidence(
        {
            "request_count": 2,
            "provider": "openrouter",
            "model": "vendor/model-base",
        },
        physical_rows=[
            {
                "provider": "openrouter",
                "model": "vendor/model-base-20260725",
            }
        ],
    )

    assert evidence is None


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_done_unmatched_atomic_receipt_can_recover_missing_ledger_row(
    module,
) -> None:
    recorder = module.BenchmarkTurnCallRecorder()
    recorder.write(
        "llm_response",
        {
            "iteration": 1,
            "call_attempt": 1,
            "usage": {
                "provider": "openrouter",
                "model": "model-a",
                "input_tokens": 5,
                "output_tokens": 1,
                "billed_cost": 0.01,
                "cost_source": "provider_billed",
                "provider_usage": _openrouter_exact_evidence(
                    0.01,
                    "recorded-response",
                ),
            },
        },
    )
    recovered = {
        "role": "agent_done",
        "request_count": 1,
        "provider": "openrouter",
        "model": "model-a",
        "requested_provider": "openrouter",
        "requested_model": "model-a",
        "input_tokens": 7,
        "output_tokens": 2,
        "billed_cost": 0.02,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.02,
            "recovered-response",
        ),
    }
    done = AgentDoneEvent(
        text="answer",
        input_tokens=12,
        output_tokens=3,
        cost_usd=0.03,
        billed_cost=0.03,
        cost_source="provider_billed",
        provider="openrouter",
        model="model-a",
    )
    setattr(done, "model_usage_breakdown", [recovered])

    provider_done = module.provider_done_from_agent_done(
        done,
        recorder=recorder,
        fallback_model="model-a",
    )

    assert provider_done is not None
    assert len(provider_done.model_usage_breakdown) == 2
    assert {
        response_id
        for row in provider_done.model_usage_breakdown
        for response_id in module.usage_row_response_ids(row)
    } == {"recorded-response", "recovered-response"}
    accounting = module.usage_cost_accounting(
        module.done_payload(provider_done),
        expected_requests=2,
        scope="generation",
    )
    assert accounting["recorded_cost_usd"] == pytest.approx(0.03)
    assert accounting["cost_exact"] is True


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_agent_done_envelope_does_not_create_request_after_explicit_zero(
    module,
) -> None:
    recorder = module.BenchmarkTurnCallRecorder()
    recorder.write(
        "llm_error",
        {
            "iteration": 1,
            "call_attempt": 1,
            "request_started": False,
            "physical_request_count": 0,
            "usage_missing_count": 0,
        },
    )
    done = AgentDoneEvent(
        text="answer",
        input_tokens=5,
        output_tokens=1,
        cost_usd=0.01,
        billed_cost=0.01,
        cost_source="opensquilla_static_estimate",
        provider="openrouter",
        model="model-a",
    )

    provider_done = module.provider_done_from_agent_done(
        done,
        recorder=recorder,
        fallback_model="model-a",
    )

    assert provider_done is not None
    assert provider_done.model_usage_breakdown == []
    assert provider_done.ensemble_trace["physical_request_count"] == 0
    assert provider_done.ensemble_trace["llm_request_count"] == 0


def test_main_and_resume_share_identical_critical_runtime_functions() -> None:
    from opensquilla.eval import draco_usage_evidence

    resume_runner = _load_resume_runner()
    shared_usage_functions = (
        "build_stable_receipt_evidence",
        "deduplicate_stable_usage_receipts",
        "merge_usage_receipt_provenance",
        "usage_receipt_fingerprint",
        "usage_row_is_missing_placeholder",
        "usage_row_match_priority",
        "usage_row_response_ids",
    )
    assert runner.STABLE_RECEIPT_EVIDENCE_KEY == (
        draco_usage_evidence.STABLE_RECEIPT_EVIDENCE_KEY
    )
    assert resume_runner.STABLE_RECEIPT_EVIDENCE_KEY == (
        draco_usage_evidence.STABLE_RECEIPT_EVIDENCE_KEY
    )
    for name in shared_usage_functions:
        shared = getattr(draco_usage_evidence, name)
        assert getattr(runner, name) is shared
        assert getattr(resume_runner, name) is shared
    critical = (
        "validate_tool_mode_for_runner",
        "configure_benchmark_sandbox_runtime",
        "configure_local_web_fetch_runtime",
        "filter_blocked_search_results",
        "build_local_web_tool_registry",
        "build_benchmark_tool_context",
        "run_local_web_tools_preflight",
        "build_task_analyzer_provider",
        "result_key_coverage",
        "llm_request_count_for_run",
        "run_result_summary",
        "row_llm_request_count",
        "_finite_nonnegative_number",
        "_openrouter_router_provider_metadata_is_complete",
        "_normalize_openrouter_provider_identity",
        "_formal_openrouter_model_aliases",
        "_formal_openrouter_models_equivalent",
        "_openrouter_router_provider_metadata_pin_state",
        "_openrouter_provider_billed_cost_is_exact",
        "_openrouter_non_byok_receipt_is_exact",
        "_first_usage_cost",
        "_coerce_provider_billing_receipt",
        "_billing_receipt_state",
        "exact_provider_usage_cost",
        "trusted_provider_billed_cost",
        "_mixed_usage_cost",
        "_load_frozen_model_registry_snapshot",
        "_registry_price_value",
        "_frozen_openrouter_registry_price_index",
        "_frozen_estimate_price",
        "_stored_estimate_price_source",
        "_discard_non_frozen_stored_estimate",
        "estimate_missing_usage_costs",
        "repair_row_cost_metadata_with_estimates",
        "build_stable_receipt_evidence",
        "ignored_agent_done_summary_policy_evidence",
        "merge_usage_receipt_provenance",
        "deduplicate_stable_usage_receipts",
        "usage_cost_accounting",
        "merge_cost_accounting",
        "public_cost_accounting",
        "summarized_run_expected_request_count",
        "diagnostic_done_from_error_event",
        "aggregate_agent_model_usage",
        "aggregate_agent_ensemble_trace",
        "ensemble_call_trace_sequence",
        "admissible_empty_nonterminal_fallback_reasons",
        "agent_call_output_sequence_reasons",
        "ensemble_generation_retry_reason",
        "deterministic_reasoning_only_length_failures",
        "provider_done_from_agent_done",
        "backfill_result_requested_identity",
        "collect_generation_with_retries",
        "external_tool_cost_accounting",
        "row_cost_accounting",
        "_usage_units_for_openrouter_non_byok_audit",
        "_actual_llm_usage_units_for_openrouter_non_byok_audit",
        "_independent_openrouter_policy_evidence",
        "classify_openrouter_non_byok_unit",
        "normalized_agent_finalization_policy",
        "legal_proposer_quorum",
        "expanded_proposer_slot_identities",
        "validate_g1_registry_contract",
        "g1_registry_contract_reasons",
        "apply_b2_g12_argument_alignment",
        "enforce_draco_legal_proposer_quorum",
        "enforce_formal_draco_runtime_config",
        "canonical_json_sha256",
        "g1_immutable_selection_plan_payload",
        "gateway_execution_contract",
        "validate_strict_openrouter_non_byok_environment",
        "resolved_llm_runtime_contract",
        "build_run_compatibility",
        "openrouter_non_byok_audit",
        "judge_text",
        "run_one",
        "trace_row",
        "render_markdown",
    )

    for name in critical:
        assert inspect.getsource(getattr(runner, name)) == inspect.getsource(
            getattr(resume_runner, name)
        )
    assert (
        runner._ADMISSIBLE_NONTERMINAL_FALLBACK_CORE_REASONS
        == resume_runner._ADMISSIBLE_NONTERMINAL_FALLBACK_CORE_REASONS
    )


def test_all_serialized_cost_accounting_assignments_strip_private_provenance() -> None:
    for path in (SCRIPT_PATH, RESUME_SCRIPT_PATH):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assignments = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "row"
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "cost_accounting"
                for target in node.targets
            )
        ]
        assert assignments
        for assignment in assignments:
            assert isinstance(assignment.value, ast.Call)
            assert isinstance(assignment.value.func, ast.Name)
            assert assignment.value.func.id == "public_cost_accounting"


def test_resume_only_skips_strict_valid_matching_prompt(tmp_path: Path) -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    valid = resume_runner.seal_result_row(
        {
            "group": "B1",
            "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
            "routing_trace": {
                "applied_model": "model-a",
                "fallback_model": "model-a",
            },
            "task_id": "task-1",
            "prompt_sha256": prompt_hash,
            "task_input_sha256": "sha256:task-input",
            "run_compatibility_fingerprint": "sha256:run-contract",
            "error": None,
            "final_text": "answer",
            "quality_total": 80.0,
            "judge": _complete_legacy_judge("resume-valid-judge"),
            "ensemble_trace": {},
            "llm_request_count": 1,
            "usage": {
                "provider": "openrouter",
                "model": "model-a",
                "requested_provider": "openrouter",
                "requested_model": "model-a",
                "input_tokens": 3,
                "output_tokens": 1,
                "billed_cost": 0.01,
                "cost_source": "provider_billed",
                "provider_usage": _openrouter_exact_evidence(0.01, "resume-valid"),
            },
        }
    )
    failed = resume_runner.seal_result_row(
        {**valid, "task_id": "task-2", "error": "provider failure"}
    )
    wrong_prompt = resume_runner.seal_result_row(
        {**valid, "task_id": "task-3", "prompt_sha256": "wrong"}
    )
    path = tmp_path / "prior.jsonl"
    path.write_text(
        "\n".join(json.dumps(row) for row in (valid, failed, wrong_prompt)) + "\n",
        encoding="utf-8",
    )
    selected = {("B1", "task-1"), ("B1", "task-2"), ("B1", "task-3")}

    completed, audit = resume_runner.load_strict_completed_group_task_keys(
        resume_paths=[path],
        selected_keys=selected,
        prompt_hashes={task_id: prompt_hash for _, task_id in selected},
        task_input_hashes={task_id: "sha256:task-input" for _, task_id in selected},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )

    assert completed == {("B1", "task-1")}
    assert audit["matching_attempt_count"] == 3
    assert audit["strict_valid_pair_count"] == 1
    assert audit["strict_invalid_attempt_count"] == 2


def test_resume_rejects_legacy_or_incompatible_contract_rows(tmp_path: Path) -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:old-contract",
        "error": None,
        "final_text": "answer",
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("resume-contract-judge"),
        "ensemble_trace": {},
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "cost_source": "none",
        },
    }
    legacy = {
        key: value
        for key, value in {**base, "task_id": "task-1"}.items()
        if key not in {"task_input_sha256", "run_compatibility_fingerprint"}
    }
    mismatched = resume_runner.seal_result_row({**base, "task_id": "task-2"})
    path = tmp_path / "prior.jsonl"
    path.write_text(
        "\n".join(json.dumps(row) for row in (legacy, mismatched)) + "\n",
        encoding="utf-8",
    )

    completed, audit = resume_runner.load_strict_completed_group_task_keys(
        resume_paths=[path],
        selected_keys={("B1", "task-1"), ("B1", "task-2")},
        prompt_hashes={"task-1": prompt_hash, "task-2": prompt_hash},
        task_input_hashes={"task-1": "sha256:task-input", "task-2": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:new-contract"},
    )

    assert completed == set()
    assert audit["strict_invalid_reason_counts"] == {
        "invalid_result_evidence": 1,
        "missing_run_compatibility_fingerprint": 1,
        "missing_task_input_sha256": 1,
        "run_compatibility_fingerprint_mismatch": 1,
    }


def test_resume_requires_recomputed_non_byok_cost_evidence() -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "answer",
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("resume-non-byok-judge"),
        "ensemble_trace": {},
    }
    invalid = resume_runner.seal_result_row(base)
    invalid_reasons = resume_runner.strict_resume_row_invalid_reasons(
        invalid,
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        require_openrouter_non_byok=True,
    )
    invalid_state = resume_runner.resume_row_completion_state(
        invalid,
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        require_openrouter_non_byok=True,
    )
    assert "openrouter_non_byok_metadata_incomplete" not in invalid_reasons
    assert "openrouter_non_byok_metadata_incomplete" in invalid_state["audit_reasons"]

    exact = {
        **base,
        "llm_request_count": 1,
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(0.01, "resume-1"),
        },
    }
    exact["openrouter_non_byok_audit"] = resume_runner.openrouter_non_byok_audit(exact)
    sealed_exact = resume_runner.seal_result_row(exact)
    exact_reasons = resume_runner.strict_resume_row_invalid_reasons(
        sealed_exact,
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        require_openrouter_non_byok=True,
    )
    assert exact_reasons == []


def test_resume_byok_violation_is_audit_only_and_never_overrides_execution(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    frozen_snapshot = {
        "schema_version": "test/v1",
        "snapshot_version": "resume-byok-price-v1",
        "models": [
            {
                "registry_facts": {
                    "provider": "openrouter",
                    "model_id": "model-a",
                    "price": {
                        "input_per_million": 2.0,
                        "output_per_million": 4.0,
                    },
                }
            }
        ],
    }
    resume_runner._frozen_openrouter_registry_price_index.cache_clear()
    request.addfinalizer(resume_runner._frozen_openrouter_registry_price_index.cache_clear)
    monkeypatch.setattr(
        resume_runner,
        "_load_frozen_model_registry_snapshot",
        lambda: frozen_snapshot,
    )
    prompt_hash = resume_runner.text_sha256("same prompt")
    explicit_byok_usage = {
        "provider": "openrouter",
        "model": "model-a",
        "requested_provider": "openrouter",
        "requested_model": "model-a",
        "input_tokens": 3,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": {
            "is_byok": True,
            "provider_reported_cost": 0.01,
            "response_ids": ["resume-explicit-byok"],
            "router_metadata": {
                **_openrouter_exact_evidence(
                    0.01,
                    "unused",
                )["router_metadata"],
                "is_byok": True,
            },
        },
    }
    row = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": "openrouter_non_byok_metadata_incomplete",
        "final_text": "",
        "llm_request_count": 1,
        "usage": explicit_byok_usage,
    }
    row["openrouter_non_byok_audit"] = resume_runner.openrouter_non_byok_audit(row)
    state = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        require_openrouter_non_byok=True,
    )

    assert "empty_final_text" in state["generation_reasons"]
    assert state["action"] == "regenerate"
    assert "openrouter_non_byok_policy_violation" in state["audit_reasons"]
    assert state["fatal_policy_reasons"] == []

    stored_marker = {
        **row,
        "error": "openrouter_non_byok_policy_violation",
        "final_text": "accepted answer",
    }
    stored_state = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(stored_marker),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        require_openrouter_non_byok=False,
        judge_required=False,
    )
    # The explicit BYOK receipt is not an exact OpenRouter charge. Repair its
    # missing dollars from token usage before recording the audit-only state.
    assert stored_state["action"] == "metadata_only"
    assert "openrouter_non_byok_policy_violation" in stored_state["audit_reasons"]
    assert stored_state["fatal_policy_reasons"] == []

    estimated_marker = deepcopy(stored_marker)
    assert resume_runner.repair_row_cost_metadata_with_estimates(estimated_marker) is True
    estimated_marker["execution"] = {"metadata_repair_attempted": True}
    estimated_state = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(estimated_marker),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        require_openrouter_non_byok=True,
        judge_required=False,
    )
    assert estimated_state["action"] == "audit_only"
    assert estimated_state["cost_metadata_complete"] is True

    recorded_marker = deepcopy(estimated_marker)
    recorded_marker["error"] = None
    recorded_marker["execution"]["audit_only_recorded"] = True
    recorded_state = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(recorded_marker),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        require_openrouter_non_byok=True,
        judge_required=False,
    )
    assert recorded_state["action"] == "complete"
    assert "openrouter_non_byok_policy_violation" in recorded_state["audit_reasons"]


@pytest.mark.asyncio
async def test_resume_source_byok_history_does_not_abort_or_schedule_new_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {"id": "task-a", "prompt": "same prompt"}
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(json.dumps(task) + "\n", encoding="utf-8")
    output_dir = tmp_path / "output"
    model = str(resume_runner.GROUP_SPECS["B0"]["model"])
    base = {
        "group": "B0",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B0"]),
        "routing_trace": {
            "applied_model": model,
            "fallback_model": model,
        },
        "task_id": "task-a",
        "prompt_sha256": resume_runner.text_sha256(task["prompt"]),
        "task_input_sha256": resume_runner.canonical_json_sha256(task),
        "run_compatibility_fingerprint": "sha256:run-contract",
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "generation_attempt_budget_used": 1,
        "usage": {
            "provider": "openrouter",
            "model": model,
            "requested_provider": "openrouter",
            "requested_model": model,
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
        },
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("resume-policy-judge"),
    }
    explicit = json.loads(json.dumps(base))
    explicit["usage"]["provider_usage"] = {
        "is_byok": True,
        "provider_reported_cost": 0.01,
        "response_ids": ["resume-policy-generation"],
        "router_metadata": {
            **_openrouter_exact_evidence(
                0.01,
                "unused",
            )["router_metadata"],
            "is_byok": True,
        },
    }
    explicit["error"] = "openrouter_non_byok_policy_violation"
    explicit["openrouter_non_byok_audit"] = resume_runner.openrouter_non_byok_audit(explicit)
    resume_path = tmp_path / "prior.jsonl"
    resume_path.write_text(
        json.dumps(resume_runner.seal_result_row(explicit)) + "\n",
        encoding="utf-8",
    )
    args = resume_runner.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--groups",
            "B0",
            "--tool-mode",
            "local_web_tools",
            "--resume-from-jsonl",
            str(resume_path),
            "--judge-model",
            "judge-model",
            "--dry-run",
            "--require-openrouter-non-byok",
            "--continue-after-cost-audit-failure",
        ]
    )
    monkeypatch.setattr(
        resume_runner.GatewayConfig,
        "load",
        lambda _path: GatewayConfig(),
    )
    monkeypatch.setattr(
        resume_runner,
        "build_run_compatibility",
        lambda **_kwargs: {
            "contracts": {
                "B0": {
                    "resolved_llm_runtime": {
                        "provider": "openrouter",
                    }
                }
            },
            "fingerprints": {"B0": "sha256:run-contract"},
        },
    )
    call_counts = {"preflight": 0, "provider": 0, "generation": 0, "judge": 0}

    async def forbidden_preflight(*_args, **_kwargs):
        call_counts["preflight"] += 1
        raise AssertionError("completed resume source must not need web preflight")

    def forbidden_provider(*_args, **_kwargs):
        call_counts["provider"] += 1
        raise AssertionError("provider construction must not start")

    async def forbidden_generation(*_args, **_kwargs):
        call_counts["generation"] += 1
        raise AssertionError("generation must not start")

    async def forbidden_judge(*_args, **_kwargs):
        call_counts["judge"] += 1
        raise AssertionError("Judge must not start")

    monkeypatch.setattr(
        resume_runner,
        "run_local_web_tools_preflight",
        forbidden_preflight,
    )
    monkeypatch.setattr(resume_runner, "build_single_provider", forbidden_provider)
    monkeypatch.setattr(resume_runner, "run_one", forbidden_generation)
    monkeypatch.setattr(resume_runner, "judge_text", forbidden_judge)

    status = await resume_runner.amain(args)

    assert status == 0
    assert call_counts == {
        "preflight": 0,
        "provider": 0,
        "generation": 0,
        "judge": 0,
    }
    result_paths = list(output_dir.glob("draco_ensemble_*.jsonl"))
    trace_paths = list(output_dir.glob("draco_run_*.trace.jsonl"))
    checkpoint_paths = list(output_dir.glob("draco_run_*.checkpoint.json"))
    assert len(result_paths) == 1
    assert len(trace_paths) == 1
    assert len(checkpoint_paths) == 1
    repaired = json.loads(
        next(
            line
            for line in result_paths[0].read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    )
    assert repaired["error"] is None
    assert repaired["execution"]["resume_action"] == "metadata_only"
    assert repaired["execution"]["metadata_repair_attempted"] is True
    assert repaired["execution"]["metadata_repair_summary"]["generation_called"] is False
    assert repaired["execution"]["metadata_repair_summary"]["judge_called"] is False
    assert repaired["resume_completion"]["post_repair_action"] == "audit_only"
    assert repaired["usage"]["cost_source"] == "opensquilla_static_estimate"
    assert repaired["usage"]["provider_usage"]["estimate_basis"] in {
        "cache_aware",
        "cache_blind",
        "free",
    }
    assert repaired["completion_status"]["status"] == "complete"
    assert repaired["audit_status"]["policy"]["compliant"] is False
    assert repaired["openrouter_non_byok_audit"]["status"] == "policy_violation"
    assert len(trace_paths[0].read_text(encoding="utf-8").splitlines()) == 1
    assert json.loads(checkpoint_paths[0].read_text(encoding="utf-8"))["rows_written"] == 1
    assert not list(output_dir.glob("*.policy-violation.manifest.json"))


@pytest.mark.asyncio
async def test_resume_missing_non_byok_receipt_reruns_only_missing_judge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {"id": "task-a", "prompt": "same prompt"}
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(json.dumps(task) + "\n", encoding="utf-8")
    output_dir = tmp_path / "output"
    model = str(resume_runner.GROUP_SPECS["B0"]["model"])
    prior = {
        "group": "B0",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B0"]),
        "routing_trace": {
            "applied_model": model,
            "fallback_model": model,
        },
        "task_id": "task-a",
        "prompt_sha256": resume_runner.text_sha256(task["prompt"]),
        "task_input_sha256": resume_runner.canonical_json_sha256(task),
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": "openrouter_non_byok_metadata_incomplete",
        "final_text": "accepted generation",
        "latency_ms": 1,
        "llm_request_count": 2,
        "generation_attempt_count": 1,
        "generation_attempt_budget_used": 1,
        "usage": {
            "provider": "openrouter",
            "model": model,
            "requested_provider": "openrouter",
            "requested_model": model,
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                "resume-generation-exact",
            ),
        },
        "judge": None,
        "quality_total": None,
    }
    prior["openrouter_non_byok_audit"] = resume_runner.openrouter_non_byok_audit(prior)
    prior = resume_runner.seal_result_row(prior)
    resume_path = tmp_path / "prior.jsonl"
    resume_path.write_text(json.dumps(prior) + "\n", encoding="utf-8")
    args = resume_runner.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--groups",
            "B0",
            "--tool-mode",
            "local_web_tools",
            "--resume-from-jsonl",
            str(resume_path),
            "--judge-model",
            "judge-model",
            "--dry-run",
            "--require-openrouter-non-byok",
        ]
    )
    monkeypatch.setattr(
        resume_runner.GatewayConfig,
        "load",
        lambda _path: GatewayConfig(),
    )
    monkeypatch.setattr(
        resume_runner,
        "build_run_compatibility",
        lambda **_kwargs: {
            "contracts": {
                "B0": {
                    "resolved_llm_runtime": {
                        "provider": "openrouter",
                    }
                }
            },
            "fingerprints": {"B0": "sha256:run-contract"},
        },
    )
    call_counts = {"provider": 0, "generation": 0, "judge": 0}

    def fake_provider(*_args, **_kwargs):
        call_counts["provider"] += 1
        return object()

    async def forbidden_generation(*_args, **_kwargs):
        call_counts["generation"] += 1
        raise AssertionError("accepted generation must not rerun")

    async def fake_judge(*_args, **_kwargs):
        call_counts["judge"] += 1
        return _complete_legacy_judge("resume-new-judge")

    monkeypatch.setattr(resume_runner, "build_single_provider", fake_provider)
    monkeypatch.setattr(resume_runner, "run_one", forbidden_generation)
    monkeypatch.setattr(resume_runner, "judge_text", fake_judge)

    status = await resume_runner.amain(args)

    assert status == 0
    assert call_counts == {"provider": 1, "generation": 0, "judge": 1}
    result_paths = list(output_dir.glob("draco_ensemble_*.jsonl"))
    assert len(result_paths) == 1
    rows = [
        json.loads(line)
        for line in result_paths[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    repaired = rows[0]
    assert repaired["final_text"] == prior["final_text"]
    assert repaired["usage"]["input_tokens"] == prior["usage"]["input_tokens"]
    assert repaired["usage"]["output_tokens"] == prior["usage"]["output_tokens"]
    assert repaired["execution"]["generation_reused"] is True
    assert repaired["execution"]["judge_reran"] is True
    assert repaired["execution"]["prior_generation_attempts_used"] == 1
    assert repaired["generation_attempt_budget_used"] == 1
    assert repaired["judge"]["score_status"] == "complete"
    assert repaired["openrouter_non_byok_audit"]["status"] == ("metadata_incomplete")
    assert repaired["error"] is None
    assert repaired["completion_status"]["status"] == "complete"
    assert repaired["audit_status"]["status"] == "warning"
    assert repaired["audit_status"]["separate_from_execution"] is True
    manifests = list(output_dir.glob("draco_run_*.manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    preflight = manifest["tool_policy"]["local_web_tools"]["preflight"]
    assert preflight["status"] == "skipped_not_required"
    assert preflight["model_regenerate_pair_count"] == 0
    assert preflight["preflight_calls"] == {"web_search": 0, "web_fetch": 0}
    assert manifest["resume_selection"]["model_regenerate_pair_count"] == 0


@pytest.mark.asyncio
async def test_resume_metadata_only_repairs_once_without_generation_or_judge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {"id": "task-a", "prompt": "same prompt"}
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(json.dumps(task) + "\n", encoding="utf-8")
    output_dir = tmp_path / "output"
    model = str(resume_runner.GROUP_SPECS["B0"]["model"])
    prior = resume_runner.seal_result_row(
        {
            "group": "B0",
            "provider_spec": dict(resume_runner.GROUP_SPECS["B0"]),
            "routing_trace": {
                "applied_model": model,
                "fallback_model": model,
            },
            "task_id": "task-a",
            "prompt_sha256": resume_runner.text_sha256(task["prompt"]),
            "task_input_sha256": resume_runner.canonical_json_sha256(task),
            "run_compatibility_fingerprint": "sha256:run-contract",
            "error": "cost_metadata_incomplete",
            "final_text": "accepted generation",
            "latency_ms": 1,
            "llm_request_count": 1,
            "generation_attempt_count": 1,
            "generation_attempt_budget_used": 1,
            "usage": {
                "provider": "openrouter",
                "model": model,
                "requested_provider": "openrouter",
                "requested_model": model,
                "input_tokens": 3,
                "output_tokens": 1,
                "billed_cost": 0.0,
                "cost_source": "none",
            },
            "judge": _complete_legacy_judge("resume-existing-judge"),
            "quality_total": 80.0,
        }
    )
    resume_path = tmp_path / "prior.jsonl"
    resume_path.write_text(json.dumps(prior) + "\n", encoding="utf-8")
    args = resume_runner.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--groups",
            "B0",
            "--tool-mode",
            "local_web_tools",
            "--resume-from-jsonl",
            str(resume_path),
            "--judge-model",
            "judge-model",
            "--dry-run",
        ]
    )
    monkeypatch.setattr(
        resume_runner.GatewayConfig,
        "load",
        lambda _path: GatewayConfig(),
    )
    monkeypatch.setattr(
        resume_runner,
        "build_run_compatibility",
        lambda **_kwargs: {
            "contracts": {
                "B0": {
                    "resolved_llm_runtime": {
                        "provider": "openrouter",
                    }
                }
            },
            "fingerprints": {"B0": "sha256:run-contract"},
        },
    )
    call_counts = {"generation": 0, "judge": 0, "metadata": 0}

    async def forbidden_generation(*_args, **_kwargs):
        call_counts["generation"] += 1
        raise AssertionError("metadata-only repair must not regenerate")

    async def forbidden_judge(*_args, **_kwargs):
        call_counts["judge"] += 1
        raise AssertionError("metadata-only repair must not call Judge")

    original_cost_repair = resume_runner.repair_row_cost_metadata_with_estimates

    def counted_cost_repair(row):
        call_counts["metadata"] += 1
        return original_cost_repair(row)

    monkeypatch.setattr(resume_runner, "run_one", forbidden_generation)
    monkeypatch.setattr(resume_runner, "judge_text", forbidden_judge)
    monkeypatch.setattr(
        resume_runner,
        "repair_row_cost_metadata_with_estimates",
        counted_cost_repair,
    )

    status = await resume_runner.amain(args)

    assert status == 0
    assert call_counts == {"generation": 0, "judge": 0, "metadata": 1}
    result_paths = list(output_dir.glob("draco_ensemble_*.jsonl"))
    assert len(result_paths) == 1
    repaired = json.loads(
        next(
            line
            for line in result_paths[0].read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    )
    execution = repaired["execution"]
    assert execution["generation_reused"] is True
    assert execution["judge_reran"] is False
    assert execution["metadata_repair_attempted"] is True
    assert isinstance(execution["metadata_repair_attempted_at"], float)
    assert execution["metadata_repair_summary"]["generation_called"] is False
    assert execution["metadata_repair_summary"]["judge_called"] is False
    manifests = list(output_dir.glob("draco_run_*.manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    preflight = manifest["tool_policy"]["local_web_tools"]["preflight"]
    assert preflight["status"] == "skipped_not_required"
    assert preflight["preflight_calls"] == {"web_search": 0, "web_fetch": 0}
    assert manifest["resume_selection"]["model_regenerate_pair_count"] == 0


def test_resume_classifies_generation_judge_and_metadata_independently() -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                "resume-classification",
            ),
        },
    }

    judge_only = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(base),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert judge_only["generation_valid"] is True
    assert judge_only["action"] == "judge_only"

    judge_disabled = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(base),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        judge_required=False,
    )
    assert judge_disabled["judge_complete"] is True
    assert judge_disabled["action"] == "complete"

    legacy_row = {
        **base,
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("resume-legacy-judge"),
    }
    legacy = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(legacy_row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert legacy["judge_complete"] is True
    assert legacy["action"] == "complete"

    metadata_only_row = {
        **base,
        "llm_request_count": 1,
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "cost_source": "none",
        },
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("resume-duplicate-judge"),
    }
    metadata_only = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(metadata_only_row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert metadata_only["generation_valid"] is True
    assert metadata_only["judge_complete"] is True
    assert metadata_only["cost_metadata_complete"] is False
    assert metadata_only["metadata_repair_attempted"] is False
    assert metadata_only["action"] == "metadata_only"

    already_attempted_row = json.loads(json.dumps(metadata_only_row))
    already_attempted_row["execution"] = {
        "metadata_repair_attempted": True,
        "metadata_repair_attempted_at": 123.0,
        "metadata_repair_summary": {
            "status": "applied",
            "generation_called": False,
            "judge_called": False,
            "changed": False,
        },
    }
    already_attempted = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(already_attempted_row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert already_attempted["action"] == "audit_only"
    assert already_attempted["metadata_repair_attempted"] is True

    already_audited_row = json.loads(json.dumps(already_attempted_row))
    already_audited_row["execution"]["audit_only_recorded"] = True
    already_audited = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(already_audited_row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert already_audited["action"] == "complete"
    assert already_audited["cost_metadata_complete"] is False

    regenerate = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row({**base, "final_text": ""}),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert regenerate["action"] == "regenerate"


def test_resume_only_treats_resolved_error_markers_as_stale() -> None:
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                "stale-error-generation",
            ),
        },
    }

    real_judge_gap = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(
            {
                **base,
                "error": "judge_incomplete",
                "judge": None,
            }
        ),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert real_judge_gap["action"] == "judge_only"
    assert "stale_completion_error" not in real_judge_gap["cost_metadata_reasons"]

    stale_judge_marker = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(
            {
                **base,
                "error": "judge_incomplete",
                "judge": _complete_legacy_judge("stale-error-judge"),
                "quality_total": 80.0,
            }
        ),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert stale_judge_marker["action"] == "metadata_only"
    assert "stale_completion_error" in stale_judge_marker["cost_metadata_reasons"]


def test_resume_backfills_actual_identity_only_from_unique_receipts() -> None:
    prompt_hash = resume_runner.text_sha256("same prompt")
    generation_receipt = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 3,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            "identity-backfill",
        ),
    }
    row = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "usage": {
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "model_usage_breakdown": [generation_receipt],
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
        },
        "judge": _complete_legacy_judge("identity-backfill-judge"),
        "quality_total": 80.0,
    }
    before = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert before["generation_valid"] is True
    assert before["action"] == "metadata_only"
    assert "actual_provider_metadata_backfill_required" in (before["cost_metadata_reasons"])
    assert "actual_model_metadata_backfill_required" in before["cost_metadata_reasons"]

    assert resume_runner.backfill_usage_actual_identity(row["usage"]) is True
    after = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert after["action"] == "complete"
    assert row["usage"]["provider"] == "openrouter"
    assert row["usage"]["model"] == "model-a"

    no_receipt = json.loads(json.dumps(row))
    no_receipt["usage"].pop("provider", None)
    no_receipt["usage"].pop("model", None)
    no_receipt["usage"]["model_usage_breakdown"] = []
    no_receipt_state = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(no_receipt),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert no_receipt_state["generation_valid"] is True
    assert no_receipt_state["action"] == "metadata_only"
    assert "actual_model_evidence_missing" in no_receipt_state["cost_metadata_reasons"]
    assert "actual_provider_evidence_missing" in no_receipt_state["cost_metadata_reasons"]


def test_resume_backfills_missing_requested_identity_but_rejects_mismatch() -> None:
    prompt_hash = resume_runner.text_sha256("same prompt")
    contract = {"resolved_llm_runtime": {"provider": "openrouter"}}
    row = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                "requested-identity-backfill",
            ),
        },
        "judge": _complete_legacy_judge("requested-identity-judge"),
        "quality_total": 80.0,
    }

    before = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        expected_run_compatibility_contract=contract,
    )

    assert before["generation_valid"] is True
    assert before["action"] == "metadata_only"
    assert "requested_model_metadata_backfill_required" in before["cost_metadata_reasons"]
    assert "requested_provider_metadata_backfill_required" in before["cost_metadata_reasons"]
    assert (
        resume_runner.backfill_saved_row_requested_identity(
            row,
            expected_run_compatibility_contract=contract,
        )
        is True
    )
    after = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        expected_run_compatibility_contract=contract,
    )
    assert after["action"] == "complete"
    assert row["usage"]["requested_model"] == "model-a"
    assert row["usage"]["requested_provider"] == "openrouter"

    tampered = json.loads(json.dumps(row))
    tampered["usage"]["requested_model"] = "model-b"
    mismatch = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(tampered),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        expected_run_compatibility_contract=contract,
    )
    assert mismatch["action"] == "regenerate"
    assert "wrong_requested_model" in mismatch["generation_reasons"]


def test_resume_prefers_cost_complete_duplicate_that_can_converge(
    tmp_path: Path,
) -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "accepted answer",
        "llm_request_count": 1,
    }
    cost_complete_missing_judge = resume_runner.seal_result_row(
        {
            **base,
            "source_marker": "repairable",
            "usage": {
                "provider": "openrouter",
                "model": "model-a",
                "requested_provider": "openrouter",
                "requested_model": "model-a",
                "input_tokens": 3,
                "output_tokens": 1,
                "billed_cost": 0.01,
                "cost_source": "provider_billed",
                "provider_usage": _openrouter_exact_evidence(
                    0.01,
                    "repairable-duplicate",
                ),
            },
        }
    )
    judge_complete_missing_cost = resume_runner.seal_result_row(
        {
            **base,
            "source_marker": "unrepairable",
            "usage": {
                "provider": "openrouter",
                "model": "model-a",
                "requested_provider": "openrouter",
                "requested_model": "model-a",
                "input_tokens": 3,
                "output_tokens": 1,
                "cost_source": "none",
            },
            "quality_total": 80.0,
            "judge": _complete_legacy_judge("unrepairable-judge"),
        }
    )
    path = tmp_path / "duplicates.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                cost_complete_missing_judge,
                judge_complete_missing_cost,
            )
        )
        + "\n",
        encoding="utf-8",
    )

    states, audit = resume_runner.load_resume_group_task_states(
        resume_paths=[path],
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": prompt_hash},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )

    state = states[("B1", "task-1")]
    assert state["action"] == "judge_only"
    assert state["row"]["source_marker"] == "repairable"
    assert audit["strict_invalid_attempt_count"] == 2
    assert set(audit["resume_action_counts"]) == {
        "regenerate",
        "judge_only",
        "metadata_only",
        "audit_only",
        "complete",
        "policy_violation",
    }
    assert audit["resume_action_counts"]["policy_violation"] == 0


@pytest.mark.parametrize("reverse_order", [False, True])
def test_resume_prefers_latest_generation_within_same_completion_rank(
    tmp_path: Path,
    reverse_order: bool,
) -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "generation_attempt_budget_used": 1,
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                "latest-generation",
            ),
        },
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("latest-generation-judge"),
    }
    older = resume_runner.seal_result_row(
        {
            **base,
            "source_marker": "older",
            "generation_completed_at": 100.0,
            "completed_at": 500.0,
        }
    )
    newer = resume_runner.seal_result_row(
        {
            **base,
            "source_marker": "newer",
            "generation_completed_at": 200.0,
            "completed_at": 300.0,
        }
    )
    rows = [older, newer]
    if reverse_order:
        rows.reverse()
    path = tmp_path / "latest-duplicates.jsonl"
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    states, _ = resume_runner.load_resume_group_task_states(
        resume_paths=[path],
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": prompt_hash},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )

    state = states[("B1", "task-1")]
    assert state["action"] == "complete"
    assert state["row"]["source_marker"] == "newer"


def test_resume_generation_attempt_budget_is_cumulative() -> None:
    resume_runner = _load_resume_runner()

    assert resume_runner.remaining_generation_attempts(0, 3) == 3
    assert resume_runner.remaining_generation_attempts(1, 3) == 2
    assert resume_runner.remaining_generation_attempts(2, 3) == 1
    assert resume_runner.remaining_generation_attempts(3, 3) == 0
    assert resume_runner.remaining_generation_attempts(8, 3) == 0
    assert resume_runner.remaining_generation_attempts(0, 2) == 2
    assert resume_runner.remaining_generation_attempts(1, 2) == 1
    assert resume_runner.remaining_generation_attempts(2, 2) == 0


def test_resume_paid_adaptive_g1_requires_frozen_lifecycle_reconstruction() -> None:
    enabled_contract = {
        "gateway_execution": {
            "llm_ensemble": {
                "ranking_thinking_assignment_enabled": True,
            }
        }
    }
    adaptive_state = {
        "action": "regenerate",
        "row": {
            "execution": {
                "generation_attempts": [
                    {
                        "attempt_id": "a" * 32,
                        "attempt": 1,
                        "selection_plan": {
                            "decision_id": "frozen-decision",
                            "ranking_thinking_assignment_enabled": True,
                        },
                        "excluded_proposer_identities": [],
                        "deterministic_proposer_failures": [],
                        "run": {"llm_request_count": 1},
                    }
                ]
            }
        },
    }

    assert resume_runner.g1_cross_wave_lifecycle_requires_reconstruction(
        group="G1",
        prior_attempts_used=1,
        state=adaptive_state,
        current_run_compatibility_contract=enabled_contract,
    )
    fixed_fallback_state = deepcopy(adaptive_state)
    fixed_fallback_plan = fixed_fallback_state["row"]["execution"][
        "generation_attempts"
    ][0]["selection_plan"]
    fixed_fallback_plan["analyzer_failure_fallback"] = True
    assert resume_runner.g1_cross_wave_lifecycle_requires_reconstruction(
        group="G1",
        prior_attempts_used=1,
        state=fixed_fallback_state,
        current_run_compatibility_contract=enabled_contract,
    )
    assert not resume_runner.g1_cross_wave_lifecycle_requires_reconstruction(
        group="G1",
        prior_attempts_used=0,
        state=adaptive_state,
        current_run_compatibility_contract=enabled_contract,
    )
    assert not resume_runner.g1_cross_wave_lifecycle_requires_reconstruction(
        group="B1",
        prior_attempts_used=1,
        state=adaptive_state,
        current_run_compatibility_contract=enabled_contract,
    )
    legacy_state = deepcopy(adaptive_state)
    legacy_attempt = legacy_state["row"]["execution"]["generation_attempts"][0]
    for field in (
        "selection_plan",
        "excluded_proposer_identities",
        "deterministic_proposer_failures",
    ):
        legacy_attempt.pop(field)
    assert not resume_runner.g1_cross_wave_lifecycle_requires_reconstruction(
        group="G1",
        prior_attempts_used=1,
        state=legacy_state,
        current_run_compatibility_contract=enabled_contract,
    )

    paid_setup_failure = deepcopy(legacy_state)
    failed_attempt = paid_setup_failure["row"]["execution"]["generation_attempts"][0]
    failed_attempt["attempt_kind"] = "provider_build_after_paid_setup"
    failed_attempt["run"] = {
        "llm_request_count": 1,
        "setup_usage": [
            {
                "role": "task_analyzer",
                "provider": "openrouter",
                "model": "anthropic/claude-opus-4.8",
                "request_count": 1,
            }
        ],
    }
    assert resume_runner.g1_cross_wave_lifecycle_requires_reconstruction(
        group="G1",
        prior_attempts_used=1,
        state=paid_setup_failure,
        current_run_compatibility_contract=enabled_contract,
    )


@pytest.mark.parametrize(
    "ensemble_contract",
    [
        {},
        {"ranking_thinking_assignment_enabled": False},
    ],
    ids=["switch-missing", "switch-false"],
)
def test_resume_g1_frozen_lifecycle_default_off_preserves_legacy_analyzer_path(
    ensemble_contract: dict[str, object],
) -> None:
    state = {
        "action": "regenerate",
        "row": {
            "execution": {
                "generation_attempts": [
                    {
                        "attempt_id": "a" * 32,
                        "attempt": 1,
                        "run": {
                            "llm_request_count": 2,
                            "usage": {
                                "model_usage_breakdown": [
                                    {
                                        "role": "task_analyzer",
                                        "provider": "openrouter",
                                        "requested_provider": "openrouter",
                                        "model": "anthropic/claude-opus-4.8",
                                        "requested_model": "anthropic/claude-opus-4.8",
                                    }
                                ]
                            },
                        },
                    }
                ]
            }
        },
    }

    assert not resume_runner.g1_cross_wave_lifecycle_requires_reconstruction(
        group="G1",
        prior_attempts_used=1,
        state=state,
        current_run_compatibility_contract={
            "gateway_execution": {"llm_ensemble": ensemble_contract}
        },
    )


def test_resume_generation_attempt_budget_is_cumulative_across_jsonl_waves(
    tmp_path: Path,
) -> None:
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": "provider_error",
        "final_text": "",
        "llm_request_count": 1,
        "generation_attempt_count": 1,
        "usage": {},
    }
    paths: list[Path] = []
    for wave, cumulative_budget in enumerate((1, 2, 3), start=1):
        row = resume_runner.seal_result_row(
            {
                **base,
                "generation_attempt_budget_used": cumulative_budget,
                "generation_completed_at": float(wave),
            }
        )
        path = tmp_path / f"wave-{wave}.jsonl"
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        paths.append(path)

    states_after_two, _ = resume_runner.load_resume_group_task_states(
        resume_paths=paths[:2],
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": prompt_hash},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )
    state_after_two = states_after_two[("B1", "task-1")]
    assert state_after_two["action"] == "regenerate"
    assert state_after_two["prior_generation_attempts_used"] == 2
    assert (
        resume_runner.remaining_generation_attempts(
            state_after_two["prior_generation_attempts_used"],
            3,
        )
        == 1
    )

    states_after_three, _ = resume_runner.load_resume_group_task_states(
        resume_paths=paths,
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": prompt_hash},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )
    state_after_three = states_after_three[("B1", "task-1")]
    assert state_after_three["action"] == "regenerate"
    assert state_after_three["prior_generation_attempts_used"] == 3
    assert (
        resume_runner.remaining_generation_attempts(
            state_after_three["prior_generation_attempts_used"],
            3,
        )
        == 0
    )


def _strict_attempt_resume_row(
    *,
    attempt_id: str,
    cumulative_budget: int,
    generation_completed_at: float,
) -> dict[str, object]:
    prompt_hash = resume_runner.text_sha256("same prompt")
    attempt = {
        "attempt_id": attempt_id,
        "attempt_kind": "generation",
        "attempt": cumulative_budget,
        "started_at": generation_completed_at - 0.5,
        "completed_at": generation_completed_at,
        "retryable": True,
        "retry_reason": "provider_error",
        "retry_suppressed_reason": "",
        "will_retry": False,
        "retry_backoff_s": 0.0,
        "run": {
            "llm_request_count": 1,
            "usage_unknown_count": 1,
            "error": "provider_error",
            "usage": {},
        },
    }
    return {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": "provider_error",
        "final_text": "",
        "llm_request_count": 1,
        "generation_attempt_count": 1,
        "generation_attempt_evidence_schema": (resume_runner.GENERATION_ATTEMPT_EVIDENCE_SCHEMA),
        "generation_attempt_budget_limit": 3,
        "generation_attempt_budget_used": cumulative_budget,
        "generation_completed_at": generation_completed_at,
        "actual_spend_metrics": {"generation_attempt_count": 1},
        "execution": {
            "generation_attempts": [attempt],
            "prior_generation_attempts_used": cumulative_budget - 1,
            "generation_attempt_budget_remaining": 3 - cumulative_budget,
        },
        "usage": {},
    }


def test_resume_strict_attempt_evidence_uses_contract_generation_budget(
    tmp_path: Path,
) -> None:
    contract = {"generation": {"max_attempts": 2}}
    paths: list[Path] = []
    for index, attempt_id in enumerate(("a" * 32, "b" * 32), start=1):
        row = _strict_attempt_resume_row(
            attempt_id=attempt_id,
            cumulative_budget=index,
            generation_completed_at=float(index),
        )
        row["generation_attempt_budget_limit"] = 2
        row["execution"]["generation_attempt_budget_remaining"] = 2 - index
        path = tmp_path / f"dynamic-wave-{index}.jsonl"
        path.write_text(
            json.dumps(resume_runner.seal_result_row(row)) + "\n",
            encoding="utf-8",
        )
        paths.append(path)

    states, _ = resume_runner.load_resume_group_task_states(
        resume_paths=paths,
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": resume_runner.text_sha256("same prompt")},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
        run_compatibility_contracts={"B1": contract},
    )

    assert states[("B1", "task-1")]["prior_generation_attempts_used"] == 2

    tampered = _strict_attempt_resume_row(
        attempt_id="c" * 32,
        cumulative_budget=1,
        generation_completed_at=3.0,
    )
    tampered["generation_attempt_budget_limit"] = 3
    tampered["execution"]["generation_attempt_budget_remaining"] = 2
    tampered_path = tmp_path / "tampered-budget.jsonl"
    tampered_path.write_text(
        json.dumps(resume_runner.seal_result_row(tampered)) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="budget limit differs"):
        resume_runner.load_resume_group_task_states(
            resume_paths=[tampered_path],
            selected_keys={("B1", "task-1")},
            prompt_hashes={"task-1": resume_runner.text_sha256("same prompt")},
            task_input_hashes={"task-1": "sha256:task-input"},
            run_compatibility_fingerprints={"B1": "sha256:run-contract"},
            run_compatibility_contracts={"B1": contract},
        )


def _enabled_g1_frozen_lifecycle_contract() -> dict[str, object]:
    return {
        "gateway_execution": {
            "llm_ensemble": {
                "ranking_thinking_assignment_enabled": True,
            }
        },
        "g1_registry_contract": {},
    }


def test_resume_blocks_automatic_resend_after_paid_postprocessing_failure() -> None:
    reason = "generation_postprocessing_failed:run_result_summary:RuntimeError"
    row = _strict_attempt_resume_row(
        attempt_id="1" * 32,
        cumulative_budget=1,
        generation_completed_at=1.0,
    )
    row.update(
        {
            "error": reason,
            "selected_generation_succeeded": False,
        }
    )
    attempt = row["execution"]["generation_attempts"][0]
    attempt.update(
        {
            "retryable": False,
            "retry_reason": reason,
            "retry_suppressed_reason": reason,
            "will_retry": False,
            "generation_postprocessing_failure": {
                "stage": "run_result_summary",
                "exception_type": "RuntimeError",
            },
        }
    )
    attempt["run"].update(
        {
            "error": reason,
            "llm_request_count": 1,
            "generation_postprocessing_failure": {
                "stage": "run_result_summary",
                "exception_type": "RuntimeError",
            },
        }
    )
    row = resume_runner.seal_result_row(row)

    state = resume_runner.resume_row_completion_state(
        row,
        expected_prompt_sha256=resume_runner.text_sha256("same prompt"),
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint=("sha256:run-contract"),
        judge_required=False,
    )

    assert state["action"] == "regenerate"
    assert state["generation_auto_retry_blocked"] is True
    assert state["generation_postprocessing_terminal"] == {
        "schema": ("opensquilla.draco.generation-postprocessing-terminal/v1"),
        "reason": reason,
        "stage": "run_result_summary",
        "exception_type": "RuntimeError",
        "attempt_id": "1" * 32,
        "attempt": 1,
        "llm_request_count": 1,
        "automatic_generation_retry_allowed": False,
    }

    tree = ast.parse(inspect.getsource(resume_runner._amain_with_run_lock))
    guarded = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_guarded"
    )
    terminal_branch = next(
        node
        for node in ast.walk(guarded)
        if isinstance(node, ast.If) and "generation_auto_retry_blocked" in ast.unparse(node.test)
    )
    terminal_return = next(
        node for node in ast.walk(terminal_branch) if isinstance(node, ast.Return)
    )
    model_call = next(
        node
        for node in ast.walk(guarded)
        if isinstance(node, ast.Await) and "run_one" in ast.unparse(node)
    )
    assert terminal_return.lineno < model_call.lineno


def _provider_native_exhausted_resume_row() -> dict[str, object]:
    plan: dict[str, object] = {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "selected_P": [
            "openrouter:model-a",
            "openrouter:model-b",
            "openrouter:model-c",
        ],
        "proposer_models": ["model-a", "model-b", "model-c"],
        "proposer_sample_count": 3,
        "effective_min_successful_proposers": 2,
        "backup_P": [
            "openrouter:model-d",
            "openrouter:model-e",
        ],
        "configured_proposer_backup_count": 2,
        "effective_proposer_backup_count": 2,
        "selected_A": "openrouter:model-f",
        "aggregator_candidates": [
            "openrouter:model-f",
            "openrouter:model-g",
            "openrouter:model-h",
        ],
        "proposer_recovery_policy": deepcopy(resume_runner.FORMAL_PROPOSER_RECOVERY_POLICY),
    }
    from opensquilla.provider.protocol import (
        provider_retry_roster_fingerprint,
    )

    fingerprint = provider_retry_roster_fingerprint(plan)

    def physical(
        ordinal: int,
        attempt_id: str,
        identity: str,
        outcome: str,
    ) -> dict[str, object]:
        return {
            "attempt": ordinal,
            "physical_attempt_id": attempt_id,
            "identity": identity,
            "request_started": True,
            "stream_closed": True,
            "outcome": outcome,
        }

    call = {
        "selection_plan": deepcopy(plan),
        "successful_proposers": 1,
        "candidates": [
            {
                "request_started": True,
                "physical_request_count": 4,
                "execution": {
                    "physical_attempts": [
                        physical(
                            1,
                            "a" * 32,
                            "openrouter:model-a",
                            "failed",
                        ),
                        physical(
                            2,
                            "b" * 32,
                            "openrouter:model-a",
                            "failed",
                        ),
                        physical(
                            3,
                            "c" * 32,
                            "openrouter:model-d",
                            "failed",
                        ),
                        physical(
                            4,
                            "d" * 32,
                            "openrouter:model-e",
                            "failed",
                        ),
                    ]
                },
            },
            {
                "request_started": True,
                "physical_request_count": 1,
                "execution": {
                    "physical_attempts": [
                        physical(
                            1,
                            "e" * 32,
                            "openrouter:model-b",
                            "succeeded",
                        )
                    ]
                },
            },
            {
                "request_started": True,
                "physical_request_count": 1,
                "execution": {
                    "physical_attempts": [
                        physical(
                            1,
                            "f" * 32,
                            "openrouter:model-c",
                            "failed",
                        )
                    ]
                },
            },
        ],
        "proposer_recovery": {
            "schema": resume_runner.PROPOSER_RECOVERY_SCHEMA,
            "selection_plan_fingerprint": fingerprint,
            "scope": "run_turn",
            "scope_id": "scope-1",
            "max_additional_physical_requests": 3,
            "external_physical_requests_reserved": 0,
            "additional_physical_requests_started": 3,
            "remaining_additional_physical_requests": 0,
            "quorum_required": 2,
            "quorum_reached": False,
            "cumulative_excluded_identities": [
                "openrouter:model-a",
                "openrouter:model-d",
                "openrouter:model-e",
            ],
            "visited_identities": [
                "openrouter:model-d",
                "openrouter:model-e",
            ],
            "executed_proposer_roster_before": list(plan["selected_P"]),
            "executed_proposer_roster_after": list(plan["selected_P"]),
            "attempts": [
                {
                    "sequence": 1,
                    "slot_index": 0,
                    "kind": "thinking_downgrade",
                    "source_identity": "openrouter:model-a",
                    "target_identity": "openrouter:model-a",
                    "failure_kind": "reasoning_only_length",
                    "reason": "reasoning_only_length",
                    "thinking_before": "high",
                    "thinking_after": "medium",
                    "request_started": True,
                    "physical_request_count": 1,
                    "physical_attempt_id": "b" * 32,
                    "stream_closed": True,
                    "usage_reported": True,
                    "usage_missing_count": 0,
                    "outcome": "failed",
                },
                {
                    "sequence": 2,
                    "slot_index": 0,
                    "kind": "backup_replacement",
                    "source_identity": "openrouter:model-a",
                    "target_identity": "openrouter:model-d",
                    "failure_kind": "reasoning_only_length",
                    "reason": "frozen_backup_order",
                    "request_started": True,
                    "physical_request_count": 1,
                    "physical_attempt_id": "c" * 32,
                    "stream_closed": True,
                    "usage_reported": True,
                    "usage_missing_count": 0,
                    "outcome": "failed",
                },
                {
                    "sequence": 3,
                    "slot_index": 0,
                    "kind": "backup_replacement",
                    "source_identity": "openrouter:model-a",
                    "target_identity": "openrouter:model-e",
                    "failure_kind": "reasoning_only_length",
                    "reason": "frozen_backup_order",
                    "request_started": True,
                    "physical_request_count": 1,
                    "physical_attempt_id": "d" * 32,
                    "stream_closed": True,
                    "usage_reported": True,
                    "usage_missing_count": 0,
                    "outcome": "failed",
                },
            ],
        },
    }
    return {
        "group": "G1",
        "error": "llm ensemble had 1 successful proposer(s), requires 2",
        "selected_generation_succeeded": False,
        "execution": {
            "generation_attempts": [
                {
                    "attempt_id": "1" * 32,
                    "attempt_kind": "generation",
                    "attempt": 1,
                    "will_retry": False,
                    "retry_suppressed_reason": ("provider_native_proposer_recovery_terminal"),
                    "proposer_recovery_owner": "provider",
                    "selection_plan": deepcopy(plan),
                    "run": {
                        "error": ("llm ensemble had 1 successful proposer(s), requires 2"),
                        "ensemble_trace": call,
                    },
                }
            ]
        },
    }


def test_resume_provider_native_exhaustion_is_terminal_across_waves() -> None:
    row = _provider_native_exhausted_resume_row()

    evidence = resume_runner.provider_native_proposer_recovery_terminal_evidence(row)

    assert evidence is not None
    assert evidence["status"] == "budget_exhausted"
    assert evidence["receipt_valid"] is True
    assert evidence["additional_physical_requests_started"] == 3
    assert evidence["remaining_additional_physical_requests"] == 0
    state = {
        "action": "regenerate",
        "row": row,
        "generation_auto_retry_blocked": True,
    }
    assert (
        resume_runner.g1_cross_wave_lifecycle_requires_reconstruction(
            group="G1",
            prior_attempts_used=1,
            state=state,
            current_run_compatibility_contract=(_enabled_g1_frozen_lifecycle_contract()),
        )
        is False
    )


def test_resume_provider_native_cleanup_code_is_terminal_across_waves() -> None:
    row = _provider_native_exhausted_resume_row()
    attempt = row["execution"]["generation_attempts"][0]
    attempt["retry_reason"] = (
        "ensemble proposer cleanup did not finish; recovery, aggregation, "
        "and fallback were not started"
    )
    attempt["retry_suppressed_reason"] = "ensemble_proposer_close_timeout"

    evidence = resume_runner.provider_native_proposer_recovery_terminal_evidence(
        row
    )

    assert evidence is not None
    assert evidence["status"] == "budget_exhausted"
    assert evidence["receipt_valid"] is True
    assert evidence["automatic_generation_retry_allowed"] is False


def test_resume_provider_native_non_proposer_failure_can_retry_across_waves() -> None:
    row = _provider_native_exhausted_resume_row()
    attempt = row["execution"]["generation_attempts"][0]
    attempt["retry_reason"] = "invalid_agent_call_output_binding"
    attempt["retry_suppressed_reason"] = ""

    assert (
        resume_runner.provider_native_proposer_recovery_terminal_evidence(row)
        is None
    )


def _mark_provider_native_candidate_success(
    candidate: dict[str, object],
    *,
    text: str,
) -> None:
    candidate.update(
        {
            "ok": True,
            "error": "",
            "usable_for_aggregation": True,
            "completion_outcome": "complete",
            "content": {"text": text, "chars": len(text)},
        }
    )


def test_resume_provider_native_success_needs_no_failure_suppression_marker() -> None:
    row = _provider_native_exhausted_resume_row()
    attempt = row["execution"]["generation_attempts"][0]
    trace = attempt["run"]["ensemble_trace"]
    candidates = trace["candidates"]
    _mark_provider_native_candidate_success(candidates[0], text="recovered draft")
    _mark_provider_native_candidate_success(candidates[1], text="primary draft")
    candidates[0]["execution"]["physical_attempts"][-1]["outcome"] = "succeeded"
    trace["proposer_recovery"]["attempts"][-1]["outcome"] = "succeeded"
    trace["proposer_recovery"]["quorum_reached"] = True
    trace["successful_proposers"] = 2
    row["selected_generation_succeeded"] = True
    row["error"] = ""
    attempt["retry_reason"] = ""
    attempt["retry_suppressed_reason"] = ""
    attempt["run"]["error"] = ""

    evidence = resume_runner.provider_native_proposer_recovery_terminal_evidence(row)

    assert evidence is not None
    assert evidence["status"] == "provider_scope_terminal"
    assert evidence["receipt_valid"] is True
    assert "provider_native_outer_retry_not_suppressed" not in evidence[
        "receipt_reasons"
    ]


def test_resume_provider_native_failed_terminal_still_requires_marker() -> None:
    row = _provider_native_exhausted_resume_row()
    attempt = row["execution"]["generation_attempts"][0]
    attempt["retry_suppressed_reason"] = ""

    evidence = resume_runner.provider_native_proposer_recovery_terminal_evidence(row)

    assert evidence is not None
    assert evidence["receipt_valid"] is False
    assert "provider_native_outer_retry_not_suppressed" in evidence[
        "receipt_reasons"
    ]


def test_resume_provider_native_transport_success_is_not_candidate_quorum() -> None:
    row = _provider_native_exhausted_resume_row()
    attempt = row["execution"]["generation_attempts"][0]
    trace = attempt["run"]["ensemble_trace"]
    candidates = trace["candidates"]
    # Provider transport completed, but the payload produced no usable draft.
    candidates[0]["execution"]["physical_attempts"][0]["outcome"] = "succeeded"
    candidates[0].update(
        {
            "ok": False,
            "error": "provider payload was unusable",
            "error_code": "provider_payload_unusable",
            "usable_for_aggregation": False,
            "completion_outcome": "failed",
            "content": {"text": "", "chars": 0},
        }
    )
    _mark_provider_native_candidate_success(candidates[1], text="usable primary")

    evidence = resume_runner.provider_native_proposer_recovery_terminal_evidence(row)

    assert evidence is not None
    assert evidence["status"] == "budget_exhausted"
    assert evidence["receipt_valid"] is True
    assert "provider_native_recovery_continued_after_quorum" not in evidence[
        "receipt_reasons"
    ]


def test_resume_provider_native_partial_candidate_reaches_quorum_at_last_receipt() -> None:
    row = _provider_native_exhausted_resume_row()
    attempt = row["execution"]["generation_attempts"][0]
    trace = attempt["run"]["ensemble_trace"]
    candidates = trace["candidates"]
    partial_text = "partial but usable draft"
    candidates[0].update(
        {
            "ok": False,
            "error": "provider stream ended after usable text",
            "error_code": "provider_stream_interrupted",
            "usable_for_aggregation": True,
            "completion_outcome": "partial_usable",
            "content": {"text": partial_text, "chars": len(partial_text)},
        }
    )
    _mark_provider_native_candidate_success(candidates[1], text="primary draft")
    trace["proposer_recovery"]["quorum_reached"] = True

    evidence = resume_runner.provider_native_proposer_recovery_terminal_evidence(row)

    assert evidence is not None
    assert evidence["status"] == "provider_scope_terminal"
    assert evidence["receipt_valid"] is True
    assert "provider_native_recovery_continued_after_quorum" not in evidence[
        "receipt_reasons"
    ]


def test_resume_provider_native_accepts_receipt_bound_local_attempt_ordinals() -> None:
    row = _provider_native_exhausted_resume_row()
    attempt = row["execution"]["generation_attempts"][0]
    candidates = attempt["run"]["ensemble_trace"]["candidates"]
    recovery_rows = candidates[0]["execution"]["physical_attempts"][1:]
    assert [physical["physical_attempt_id"] for physical in recovery_rows] == [
        "b" * 32,
        "c" * 32,
        "d" * 32,
    ]
    for physical in recovery_rows:
        physical["attempt"] = 1

    evidence = resume_runner.provider_native_proposer_recovery_terminal_evidence(row)

    assert evidence is not None
    assert evidence["status"] == "budget_exhausted"
    assert evidence["receipt_valid"] is True
    assert evidence["receipt_reasons"] == []


def test_resume_provider_native_local_ordinal_requires_matching_receipt_slot() -> None:
    row = _provider_native_exhausted_resume_row()
    attempt = row["execution"]["generation_attempts"][0]
    trace = attempt["run"]["ensemble_trace"]
    recovery_row = trace["candidates"][0]["execution"]["physical_attempts"][1]
    recovery_row["attempt"] = 1
    trace["proposer_recovery"]["attempts"][0]["slot_index"] = 1

    evidence = resume_runner.provider_native_proposer_recovery_terminal_evidence(row)

    assert evidence is not None
    assert evidence["status"] == "receipt_invalid"
    assert evidence["receipt_valid"] is False
    assert (
        "provider_native_recovery_candidate_slot_mismatch"
        in evidence["receipt_reasons"]
    )


def test_resume_provider_native_local_ordinal_rejects_boolean_attempt() -> None:
    row = _provider_native_exhausted_resume_row()
    attempt = row["execution"]["generation_attempts"][0]
    recovery_row = attempt["run"]["ensemble_trace"]["candidates"][0][
        "execution"
    ]["physical_attempts"][1]
    recovery_row["attempt"] = True

    evidence = resume_runner.provider_native_proposer_recovery_terminal_evidence(row)

    assert evidence is not None
    assert evidence["status"] == "receipt_invalid"
    assert evidence["receipt_valid"] is False
    assert (
        "invalid_provider_native_candidate_physical_attempt"
        in evidence["receipt_reasons"]
    )


def test_resume_provider_native_malformed_receipt_fails_closed() -> None:
    row = _provider_native_exhausted_resume_row()
    attempt = row["execution"]["generation_attempts"][0]
    receipt = attempt["run"]["ensemble_trace"]["proposer_recovery"]
    receipt["selection_plan_fingerprint"] = "0" * 64

    evidence = resume_runner.provider_native_proposer_recovery_terminal_evidence(row)

    assert evidence is not None
    assert evidence["status"] == "receipt_invalid"
    assert evidence["receipt_valid"] is False
    assert "invalid_provider_native_recovery_receipt" in evidence["receipt_reasons"]
    assert evidence["automatic_generation_retry_allowed"] is False


def test_resume_provider_native_external_replay_reservation_fails_closed() -> None:
    row = _provider_native_exhausted_resume_row()
    attempt = row["execution"]["generation_attempts"][0]
    receipt = attempt["run"]["ensemble_trace"]["proposer_recovery"]
    receipt["external_physical_requests_reserved"] = 1

    evidence = resume_runner.provider_native_proposer_recovery_terminal_evidence(row)

    assert evidence is not None
    assert evidence["status"] == "receipt_invalid"
    assert evidence["receipt_valid"] is False
    assert "invalid_provider_native_recovery_receipt" in evidence["receipt_reasons"]
    assert evidence["automatic_generation_retry_allowed"] is False


@pytest.mark.parametrize(
    ("scope", "scope_id"),
    (("chat", "scope-1"), ("run_turn", "")),
)
def test_resume_provider_native_scope_must_be_stable_run_turn(
    scope: str,
    scope_id: str,
) -> None:
    row = _provider_native_exhausted_resume_row()
    attempt = row["execution"]["generation_attempts"][0]
    receipt = attempt["run"]["ensemble_trace"]["proposer_recovery"]
    receipt["scope"] = scope
    receipt["scope_id"] = scope_id

    evidence = resume_runner.provider_native_proposer_recovery_terminal_evidence(row)

    assert evidence is not None
    assert evidence["status"] == "receipt_invalid"
    assert evidence["receipt_valid"] is False
    assert "invalid_provider_native_recovery_scope" in evidence["receipt_reasons"]
    assert evidence["automatic_generation_retry_allowed"] is False


def _task_analyzer_unit(
    *,
    attempt: int = 1,
    physical_attempt_id: str = "a" * 32,
) -> dict[str, object]:
    return {
        "role": "task_analyzer",
        "attempt": attempt,
        "physical_attempt_id": physical_attempt_id,
        "provider": "openrouter",
        "requested_provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "requested_model": "anthropic/claude-opus-4.8",
        "input_tokens": 10,
        "output_tokens": 2,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "request_count": 1,
        "provider_usage": {
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "physical_attempt_id": physical_attempt_id,
        },
    }


def _task_analyzer_usage(
    units: list[dict[str, object]],
) -> dict[str, object]:
    attempts = deepcopy(units)
    usage: dict[str, object] = {
        "attempt_count": len(attempts),
        "physical_attempts": attempts,
    }
    for field in (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_write_tokens",
    ):
        usage[field] = sum(int(unit.get(field) or 0) for unit in attempts)
    usage["billed_cost"] = sum(
        float(unit.get("billed_cost") or 0.0) for unit in attempts
    )
    unknown_count = sum(unit.get("usage_unknown") is True for unit in attempts)
    if unknown_count:
        usage["usage_unknown_count"] = unknown_count
    return usage


def _unknown_task_analyzer_unit(
    *,
    attempt: int,
    physical_attempt_id: str,
) -> dict[str, object]:
    return {
        "role": "unknown_request",
        "label": "task_analyzer",
        "attempt": attempt,
        "physical_attempt_id": physical_attempt_id,
        "provider": "",
        "requested_provider": "openrouter",
        "model": "",
        "requested_model": "anthropic/claude-opus-4.8",
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
        "usage_unknown": True,
        "unknown_reason": "physical_receipt_unavailable",
        "request_count": 1,
        "provider_usage": {
            "usage_unknown": True,
            "unknown_reason": "physical_receipt_unavailable",
            "physical_attempt_id": physical_attempt_id,
        },
    }


def _frozen_g1_plan(
    *,
    decision_id: str,
    proposers: list[str],
) -> dict[str, object]:
    from opensquilla.provider.ranking_router import load_ranking_config

    plan = _with_g1_task_analysis_invariants(
        {
            "decision_id": decision_id,
            "selected_P": proposers,
            "proposer_models": [identity.partition(":")[2] for identity in proposers],
            "proposer_sample_count": len(proposers),
        }
    )
    ranking_parameters = load_ranking_config()
    analyzer = ranking_parameters["task_analyzer"]
    analyzer.pop("fallback_chain", None)
    analyzer.pop("schema_repair_max_retries", None)
    analyzer.pop("total_timeout_seconds", None)
    analyzer["max_retries"] = 2
    plan["ranking_parameters"] = ranking_parameters
    return plan


def _frozen_g1_failure_run(
    module,
    *,
    plan: dict[str, object],
    failed_identity: str | None,
    analyzer_units: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    candidates: list[dict[str, object]] = []
    generation_units: list[dict[str, object]] = []
    if failed_identity is not None:
        provider, _, model = failed_identity.partition(":")
        physical_attempt_id = hashlib.sha256(
            (f"{plan.get('decision_id')}:{failed_identity}:reasoning-only-length").encode()
        ).hexdigest()[:32]
        execution = _test_proposer_execution(failed_identity)
        execution["physical_attempts"] = [
            {
                "attempt": 1,
                "request_started": True,
                "stream_closed": True,
                "physical_attempt_id": physical_attempt_id,
                "identity": failed_identity,
                "outcome": "failed",
            }
        ]
        candidates.append(
            {
                "index": 0,
                "provider": provider,
                "requested_provider": provider,
                "model": model,
                "requested_model": model,
                "ok": False,
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": True,
                "usage_missing_count": 0,
                "stop_reason": "length",
                "reasoning_tokens": 8_192,
                "output_tokens": 8_192,
                "effective_thinking_level": "high",
                "provider_thinking_level": "high",
                "execution": execution,
                "content": {"text": "", "chars": 0},
            }
        )
        generation_units.append(
            {
                "role": "proposer",
                "physical_attempt_id": physical_attempt_id,
                "provider": provider,
                "requested_provider": provider,
                "model": model,
                "requested_model": model,
                "input_tokens": 1,
                "output_tokens": 8_192,
                "reasoning_tokens": 8_192,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "billed_cost": 0.0,
                "cost_source": "none",
                "request_count": 1,
                "provider_usage": {
                    "physical_attempt_id": physical_attempt_id,
                },
            }
        )
    trace = {
        "selection_plan": deepcopy(plan),
        "candidates": candidates,
        "physical_request_count": len(generation_units),
        "llm_request_count": len(generation_units),
        "usage_missing_count": 0,
    }
    setup_units = deepcopy(analyzer_units or [])
    units = [*deepcopy(setup_units), *generation_units]
    run = {
        "llm_request_count": len(units),
        "physical_request_count": len(units),
        "usage_missing_count": 0,
        "usage": {"model_usage_breakdown": deepcopy(units)},
        "setup_usage": deepcopy(setup_units),
        "routing_trace": {"selection_plan": deepcopy(plan)},
        "ensemble_trace": trace,
        "error": "generation_failed",
    }
    return run, module._g1_reasoning_only_length_failures(run)


def _frozen_g1_failure_result(
    module,
    *,
    plan: dict[str, object],
    failed_identity: str,
):
    provider, _, model = failed_identity.partition(":")
    physical_attempt_id = hashlib.sha256(
        (f"{plan.get('decision_id')}:{failed_identity}:reasoning-only-length-result").encode()
    ).hexdigest()[:32]
    execution = _test_proposer_execution(failed_identity)
    execution["physical_attempts"] = [
        {
            "attempt": 1,
            "request_started": True,
            "stream_closed": True,
            "physical_attempt_id": physical_attempt_id,
            "identity": failed_identity,
            "outcome": "failed",
        }
    ]
    trace = {
        "selection_plan": deepcopy(plan),
        "candidates": [
            {
                "index": 0,
                "provider": provider,
                "requested_provider": provider,
                "model": model,
                "requested_model": model,
                "ok": False,
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": True,
                "usage_missing_count": 0,
                "stop_reason": "length",
                "reasoning_tokens": 8_192,
                "output_tokens": 8_192,
                "effective_thinking_level": "high",
                "provider_thinking_level": "high",
                "execution": execution,
                "content": {"text": "", "chars": 0},
            }
        ],
        "physical_request_count": 1,
        "llm_request_count": 1,
        "usage_missing_count": 0,
    }
    return module.RunResult(
        final_text="",
        done=DoneEvent(
            ensemble_trace=trace,
            usage_missing_count=0,
            model_usage_breakdown=[
                {
                    "role": "proposer",
                    "physical_attempt_id": physical_attempt_id,
                    "provider": provider,
                    "requested_provider": provider,
                    "model": model,
                    "requested_model": model,
                    "request_count": 1,
                    "input_tokens": 1,
                    "output_tokens": 8_192,
                    "reasoning_tokens": 8_192,
                    "cached_tokens": 0,
                    "cache_write_tokens": 0,
                    "billed_cost": 0.0,
                    "cost_source": "none",
                    "provider_usage": {
                        "physical_attempt_id": physical_attempt_id,
                    },
                }
            ],
        ),
        error="ensemble_insufficient_proposers",
        routing_trace={"selection_plan": deepcopy(plan)},
    )


@pytest.mark.asyncio
async def test_resume_frozen_g1_b_to_c_retry_uses_original_a_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_a = "openrouter:model-a"
    failed_b = "openrouter:model-b"
    plan_a = _frozen_g1_plan(
        decision_id="decision-a",
        proposers=[failed_a, failed_b, "openrouter:model-c"],
    )
    plan_b = _bind_g1_retry_plan(
        plan_a,
        _frozen_g1_plan(
            decision_id="decision-b",
            proposers=[failed_b, "openrouter:model-c", "openrouter:model-d"],
        ),
        exclusions=[failed_a],
    )
    plan_c = _bind_g1_retry_plan(
        plan_a,
        _frozen_g1_plan(
            decision_id="decision-c",
            proposers=[
                "openrouter:model-c",
                "openrouter:model-d",
                "openrouter:model-e",
            ],
        ),
        exclusions=[failed_a, failed_b],
    )

    class Provider:
        def __init__(self, plan: dict[str, object]) -> None:
            self.selection_plan = plan
            self.min_successful_proposers = 2

    resumed_b = Provider(plan_b)
    retry_c = Provider(plan_c)
    resumed_b._draco_g1_initial_selection_plan = deepcopy(plan_a)
    resumed_b._draco_prior_excluded_proposer_identities = [failed_a]
    rebuild_calls: list[list[str]] = []

    def rebuild(exclusions: list[str]):
        rebuild_calls.append(exclusions)
        return retry_c

    resumed_b._draco_reasoning_only_retry_factory = rebuild
    results = iter(
        [
            _frozen_g1_failure_result(
                resume_runner,
                plan=plan_b,
                failed_identity=failed_b,
            ),
            resume_runner.RunResult(
                final_text="accepted",
                done=DoneEvent(
                    ensemble_trace={
                        "selection_plan": deepcopy(plan_c),
                        "candidates": [],
                    }
                ),
                routing_trace={"selection_plan": deepcopy(plan_c)},
            ),
        ]
    )
    paid_providers: list[object] = []

    async def fake_collect_run(active_provider, *_args, **_kwargs):
        paid_providers.append(active_provider)
        return next(results)

    monkeypatch.setattr(resume_runner, "collect_run", fake_collect_run)
    monkeypatch.setattr(
        resume_runner,
        "generation_retry_reason",
        lambda result, **_kwargs: (
            "ensemble_insufficient_proposers" if not result.final_text else ""
        ),
    )

    result, attempts, selected_attempt = await resume_runner.collect_generation_with_retries(
        resumed_b,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
        attempt_offset=1,
    )

    assert result.final_text == "accepted"
    assert selected_attempt == 3
    assert paid_providers == [resumed_b, retry_c]
    assert rebuild_calls == [[failed_a, failed_b]]
    assert attempts[0]["selection_plan"] == plan_b
    assert attempts[0]["retry_selection_plan"] == plan_c
    assert attempts[1]["selection_plan"] == plan_c
    assert attempts[1]["excluded_proposer_identities"] == [
        failed_a,
        failed_b,
    ]


@pytest.mark.asyncio
async def test_resume_frozen_g1_wave_boundary_seals_pending_retry_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_a = "openrouter:model-a"
    failed_b = "openrouter:model-b"
    plan_a = _frozen_g1_plan(
        decision_id="decision-a",
        proposers=[failed_a, failed_b, "openrouter:model-c"],
    )
    plan_b = _bind_g1_retry_plan(
        plan_a,
        _frozen_g1_plan(
            decision_id="decision-b",
            proposers=[failed_b, "openrouter:model-c", "openrouter:model-d"],
        ),
        exclusions=[failed_a],
    )
    plan_c = _bind_g1_retry_plan(
        plan_a,
        _frozen_g1_plan(
            decision_id="decision-c",
            proposers=[
                "openrouter:model-c",
                "openrouter:model-d",
                "openrouter:model-e",
            ],
        ),
        exclusions=[failed_a, failed_b],
    )

    class Provider:
        def __init__(self, plan: dict[str, object]) -> None:
            self.selection_plan = plan
            self.min_successful_proposers = 2

    resumed_b = Provider(plan_b)
    resumed_b._draco_g1_initial_selection_plan = deepcopy(plan_a)
    resumed_b._draco_prior_excluded_proposer_identities = [failed_a]
    resumed_b._draco_reasoning_only_retry_factory = lambda exclusions: (
        Provider(plan_c)
        if exclusions == [failed_a, failed_b]
        else (_ for _ in ()).throw(AssertionError(exclusions))
    )
    paid_calls = 0

    async def fake_collect_run(*_args, **_kwargs):
        nonlocal paid_calls
        paid_calls += 1
        return _frozen_g1_failure_result(
            resume_runner,
            plan=plan_b,
            failed_identity=failed_b,
        )

    monkeypatch.setattr(resume_runner, "collect_run", fake_collect_run)
    monkeypatch.setattr(
        resume_runner,
        "generation_retry_reason",
        lambda *_args, **_kwargs: "ensemble_insufficient_proposers",
    )

    _, attempts, selected_attempt = await resume_runner.collect_generation_with_retries(
        resumed_b,
        "prompt",
        timeout=30,
        group="G1",
        max_attempts=1,
        attempt_offset=1,
    )

    assert paid_calls == 1
    assert selected_attempt == 0
    assert len(attempts) == 1
    assert attempts[0]["attempt"] == 2
    assert attempts[0]["will_retry"] is False
    assert attempts[0]["retry_deferred_to_next_wave"] is True
    assert attempts[0]["retry_selection_plan"] == plan_c
    assert attempts[0]["retry_excluded_proposer_identities"] == [
        failed_a,
        failed_b,
    ]


def test_resume_restores_only_validated_physical_thinking_prefix() -> None:
    identity = "openrouter:model-a"
    target_plan = _frozen_g1_plan(
        decision_id="decision-a",
        proposers=[
            identity,
            "openrouter:model-b",
            "openrouter:model-c",
        ],
    )
    target_detail = next(
        row
        for row in target_plan["thinking_assignment_details"]["proposers"]
        if row["identity"] == identity
    )
    target_detail["provider_rejection_fallbacks"] = [
        {
            "unified_level": "medium",
            "provider_level": "medium",
            "reason": "provider_rejection_fallback",
        },
        {
            "unified_level": "low",
            "provider_level": "low",
            "reason": "provider_rejection_fallback",
        },
    ]
    frozen_row = {
        "trigger_stage": "proposer_execution",
        "fallback_type": "thinking_level_neighbor",
        "reason": "provider_rejected_thinking_level",
        "identity": identity,
        "requested_thinking_level": "high",
        "rejected_unified_level": "high",
        "rejected_provider_level": "high",
        "effective_thinking_level": "medium",
        "effective_provider_level": "medium",
        "thinking_policy_version": "test-thinking-policy/v1",
        "fallback_result": "failed",
    }
    physical_prefix = deepcopy(target_plan)
    physical_prefix["executed_thinking_assignment"]["proposers"][identity] = "medium"
    physical_prefix["thinking_execution_fallbacks"] = [deepcopy(frozen_row)]
    assert (
        resume_runner.g1_execution_plan_mutation_reason(
            target_plan,
            physical_prefix,
        )
        == ""
    )

    class Config:
        provider = "openrouter"
        model = "model-a"

    class Member:
        provider_config = Config()
        requested_thinking_level = "high"
        effective_thinking_level = "high"
        thinking = "high"

    class Provider:
        def __init__(self) -> None:
            self.selection_plan = deepcopy(target_plan)
            self.proposers = [Member()]
            self.aggregator = None
            self.aggregator_fallbacks = []

        def selection_plan_execution_snapshot(self):
            return deepcopy(self.selection_plan)

        def _record_thinking_fallback(
            self,
            *,
            member,
            role,
            rejected_unified_level,
            rejected_provider_level,
            effective_unified_level,
            effective_provider_level,
            fallback_result,
            reason,
        ):
            row = {
                "trigger_stage": f"{role}_execution",
                "fallback_type": "thinking_level_neighbor",
                "reason": reason,
                "identity": (f"{member.provider_config.provider}:{member.provider_config.model}"),
                "requested_thinking_level": (member.requested_thinking_level),
                "rejected_unified_level": rejected_unified_level,
                "rejected_provider_level": rejected_provider_level,
                "effective_thinking_level": effective_unified_level,
                "effective_provider_level": effective_provider_level,
                "thinking_policy_version": (
                    self.selection_plan["executed_thinking_assignment"]["thinking_policy_version"]
                ),
                "fallback_result": fallback_result,
            }
            member.effective_thinking_level = effective_unified_level
            member.thinking = effective_provider_level
            self.selection_plan["executed_thinking_assignment"]["proposers"][row["identity"]] = (
                effective_unified_level
            )
            self.selection_plan.setdefault(
                "thinking_execution_fallbacks",
                [],
            ).append(row)
            return deepcopy(row)

    provider = Provider()
    resume_runner.restore_g1_thinking_execution_prefix(
        provider,
        target_plan=target_plan,
        physical_execution_prefix=physical_prefix,
    )

    assert provider.selection_plan_execution_snapshot() == physical_prefix
    tampered_prefix = deepcopy(physical_prefix)
    tampered_prefix["selected_A"] = "openrouter:tampered"
    with pytest.raises(
        ValueError,
        match="thinking execution prefix is incompatible",
    ):
        resume_runner.restore_g1_thinking_execution_prefix(
            Provider(),
            target_plan=target_plan,
            physical_execution_prefix=tampered_prefix,
        )


@pytest.mark.asyncio
async def test_resume_frozen_g1_provider_build_does_not_repeat_analyzer_or_cost(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.provider.ranking_router import (
        TaskAnalysisResult,
        fallback_task_profile,
    )

    experiment = _experiment_with_current_g1_contract(
        resume_runner,
        thinking_assignment_enabled=True,
    )
    experiment_payload = experiment.model_dump(mode="json")
    experiment_payload["ensemble"].update(
        {
            "shuffle_candidates": True,
            "candidate_order_seed": 0,
        }
    )
    experiment = type(experiment).model_validate(experiment_payload)
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "ranking_thinking_assignment_enabled": True,
        },
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
    )
    contract = _resolved_g1_registry_contract(
        resume_runner,
        experiment,
        config,
    )
    analyzer_calls = 0

    async def fake_run_pipeline(turn, _steps):
        turn.model = inherited.model
        turn.metadata.update(
            {
                "routed_tier": "c1",
                "routing_confidence": 0.9,
                "routing_source": "test",
                "routing_applied": False,
            }
        )
        return turn

    async def fake_analyze_task_with_fallback_chain(**kwargs):
        nonlocal analyzer_calls
        analyzer_calls += 1
        candidates = kwargs["candidates"]
        analyzer_provider = candidates[0].provider_id
        analyzer_model = candidates[0].model_id
        return TaskAnalysisResult(
            profile=fallback_task_profile(
                routed_tier=kwargs["routed_tier"],
                request_context=kwargs["request_context"],
                ranking_config=kwargs["ranking_config"],
            ),
            source="llm_provider",
            schema_valid=True,
            confidence=0.9,
            usage={
                "provider": analyzer_provider,
                "model": analyzer_model,
                "requested_provider": analyzer_provider,
                "requested_model": analyzer_model,
                "input_tokens": 5,
                "output_tokens": 2,
                "attempt_count": 1,
                "physical_attempts": [
                    {
                        "attempt": 1,
                        "physical_attempt_id": "d" * 32,
                        "provider": analyzer_provider,
                        "model": analyzer_model,
                        "requested_provider": analyzer_provider,
                        "requested_model": analyzer_model,
                        "input_tokens": 5,
                        "output_tokens": 2,
                        "reasoning_tokens": 0,
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                        "billed_cost": 0.0,
                        "provider_usage": {
                            "provider": analyzer_provider,
                            "model": analyzer_model,
                            "physical_attempt_id": "d" * 32,
                        },
                    }
                ],
            },
            provider_id=str(analyzer_provider),
            model_id=str(analyzer_model),
            chain_trace={
                "protocol": "opensquilla.task-analyzer-fallback-chain/v1",
                "configured_routes": [
                    {
                        "provider": candidate.provider_id,
                        "model": candidate.model_id,
                        "upstream_provider": candidate.upstream_provider,
                    }
                    for candidate in candidates
                ],
                "attempt_outcomes": [
                    {
                        "candidate_index": 0,
                        "provider": analyzer_provider,
                        "model": analyzer_model,
                        "upstream_provider": candidates[0].upstream_provider,
                        "outcome": "success",
                        "reason": "",
                        "physical_request_count": 1,
                    }
                ],
                "selected_index": 0,
                "exhausted": False,
            },
        )

    monkeypatch.setattr(
        resume_runner,
        "run_pipeline",
        fake_run_pipeline,
    )
    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_fallback_chain",
        fake_analyze_task_with_fallback_chain,
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "1")

    first = await resume_runner.build_experiment_provider(
        config=config,
        inherited=inherited,
        group="G1",
        prompt="test prompt",
        dry_run=False,
        enable_proposer_tools=False,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        experiment_config=experiment,
        g1_registry_contract=contract,
        generation_policy=None,
    )
    initial_plan = deepcopy(first.provider.selection_plan)
    from opensquilla.provider.thinking_execution import (
        project_thinking_execution_history,
    )

    target_execution_prefix, projection_audit, projection_reason = (
        project_thinking_execution_history([], initial_plan)
    )
    assert projection_reason == ""
    lifecycle = {
        "schema": (resume_runner.G1_CROSS_WAVE_FROZEN_LIFECYCLE_SCHEMA),
        "initial_plan": deepcopy(initial_plan),
        "target_plan": deepcopy(initial_plan),
        "cumulative_excluded_proposer_identities": [],
        "target_execution_prefix": target_execution_prefix,
        "thinking_execution_projection": projection_audit,
        "thinking_execution_history": [],
        "task_analyzer_physical_request_count": 1,
    }

    resumed = await resume_runner.build_experiment_provider(
        config=config,
        inherited=inherited,
        group="G1",
        prompt="test prompt",
        dry_run=False,
        enable_proposer_tools=False,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        experiment_config=experiment,
        g1_registry_contract=contract,
        generation_policy=None,
        frozen_g1_lifecycle=lifecycle,
    )

    assert analyzer_calls == 1
    assert resumed.setup_usage == []
    assert resume_runner.consume_provider_setup(resumed.provider)["usage"] == []
    assert resumed.provider._draco_g1_initial_selection_plan == initial_plan
    assert resumed.provider.shuffle_candidates is True
    assert resumed.provider.candidate_order_seed == 0
    assert resumed.provider.selection_plan["configured_candidate_order_seed"] == 0
    assert resumed.provider.selection_plan["effective_candidate_order_seed"] == 0
    assert resumed.routing_trace["task_analyzer_reuse"]["physical_request_count"] == 0
    assert resumed.routing_trace["task_analyzer_reuse"]["historical_physical_request_count"] == 1

    drifted_lifecycle = deepcopy(lifecycle)
    drifted_lifecycle["initial_plan"]["ranking_parameters"]["trace"]["profile_decimal_places"] += 1
    drifted_lifecycle["target_plan"] = deepcopy(drifted_lifecycle["initial_plan"])
    with pytest.raises(
        ValueError,
        match="ranker evidence differs from the current registry/config",
    ):
        await resume_runner.build_experiment_provider(
            config=config,
            inherited=inherited,
            group="G1",
            prompt="test prompt",
            dry_run=False,
            enable_proposer_tools=False,
            ensemble_proposer_timeout=None,
            ensemble_aggregator_timeout=None,
            experiment_config=experiment,
            g1_registry_contract=contract,
            generation_policy=None,
            frozen_g1_lifecycle=drifted_lifecycle,
        )
    assert analyzer_calls == 1

    analyzer_usage = deepcopy(first.setup_usage)
    selected_member = first.provider.proposers[0]
    selected_provider = selected_member.provider_config.provider
    selected_model = selected_member.provider_config.model
    selected_identity = f"{selected_provider}:{selected_model}"
    selected_level = selected_member.effective_thinking_level or "off"
    selected_provider_level = selected_member.thinking or "off"
    generation_usage = {
        "role": "proposer",
        "provider": selected_provider,
        "requested_provider": selected_provider,
        "model": selected_model,
        "requested_model": selected_model,
        "physical_attempt_id": "f" * 32,
        "request_count": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_source": "none",
        "provider_usage": {
            "physical_attempt_id": "f" * 32,
        },
    }
    proposer_execution = {
        "role": "proposer",
        "requested_provider": selected_provider,
        "provider": selected_provider,
        "requested_model": selected_model,
        "model": selected_model,
        "assigned_thinking_level": selected_level,
        "effective_thinking_level": selected_level,
        "provider_thinking_level": selected_provider_level,
        "thinking_override": selected_provider_level,
        "effective_thinking": (selected_provider_level not in {"off", "none", "false"}),
        "effective_provider_thinking_level": selected_provider_level,
        "thinking_policy_managed": True,
        "thinking_fallback_attempts": [],
        "physical_attempts": [
            {
                "attempt": 1,
                "physical_attempt_id": "f" * 32,
                "identity": selected_identity,
                "request_started": True,
                "outcome": "succeeded",
                "effective_thinking_level": selected_level,
                "provider_thinking_level": selected_provider_level,
            }
        ],
        "thinking_fallback_bindings": [],
    }
    historical_attempt = {
        "attempt_id": "1" * 32,
        "attempt_kind": "generation",
        "attempt": 1,
        "selection_plan": deepcopy(initial_plan),
        "excluded_proposer_identities": [],
        "deterministic_proposer_failures": [],
        "will_retry": False,
        "run": {
            "llm_request_count": len(analyzer_usage) + 1,
            "setup_usage": deepcopy(analyzer_usage),
            "usage": {
                "model_usage_breakdown": [
                    *deepcopy(analyzer_usage),
                    generation_usage,
                ],
            },
            "routing_trace": {
                "selection_plan": deepcopy(initial_plan),
            },
            "ensemble_trace": {
                "selection_plan": deepcopy(initial_plan),
                "candidates": [
                    {
                        "requested_provider": selected_provider,
                        "provider": selected_provider,
                        "requested_model": selected_model,
                        "model": selected_model,
                        "request_started": True,
                        "physical_request_count": 1,
                        "effective_thinking_level": selected_level,
                        "provider_thinking_level": (selected_provider_level),
                        "execution": proposer_execution,
                    }
                ],
                "aggregator_recovery": {
                    "attempts": [],
                    "selected_attempt": None,
                },
                "llm_request_count": 1,
                "physical_request_count": 1,
            },
        },
    }
    compatibility = {
        **_enabled_g1_frozen_lifecycle_contract(),
        "g1_registry_contract": deepcopy(contract),
    }
    guarded_attempt = deepcopy(historical_attempt)
    guarded_attempt["attempt_kind"] = "provider_build_after_paid_setup"
    guarded_attempt["run"]["trace_events"] = [
        {
            "kind": "error",
            "code": "g1_pre_call_guard_failed",
            "request_started": False,
            "physical_request_count": 0,
        }
    ]
    with pytest.raises(ValueError, match="g1_pre_call_guard_failed"):
        resume_runner.reconstruct_g1_cross_wave_frozen_lifecycle(
            attempts=[guarded_attempt],
            current_run_compatibility_contract=compatibility,
        )
    reconstructed = resume_runner.reconstruct_g1_cross_wave_frozen_lifecycle(
        attempts=[historical_attempt],
        current_run_compatibility_contract=compatibility,
    )
    assert reconstructed["target_plan"] == initial_plan
    mismatched_physical_usage = deepcopy(historical_attempt)
    mismatched_usage_unit = mismatched_physical_usage["run"]["usage"]["model_usage_breakdown"][-1]
    mismatched_usage_unit["physical_attempt_id"] = "9" * 32
    mismatched_usage_unit["provider_usage"]["physical_attempt_id"] = "9" * 32
    with pytest.raises(
        ValueError,
        match="g1_thinking_physical_usage_set_mismatch",
    ):
        resume_runner.reconstruct_g1_cross_wave_frozen_lifecycle(
            attempts=[mismatched_physical_usage],
            current_run_compatibility_contract=compatibility,
        )

    duplicate_physical_attempt = deepcopy(historical_attempt)
    duplicate_physical_attempt["attempt_id"] = "2" * 32
    duplicate_physical_attempt["attempt"] = 2
    duplicate_physical_attempt["run"]["setup_usage"] = []
    duplicate_physical_attempt["run"]["usage"]["model_usage_breakdown"] = [
        deepcopy(generation_usage)
    ]
    duplicate_physical_attempt["run"]["llm_request_count"] = 1
    with pytest.raises(
        ValueError,
        match=("duplicate_cross_attempt_g1_thinking_physical_attempt_id"),
    ):
        resume_runner.reconstruct_g1_cross_wave_frozen_lifecycle(
            attempts=[
                historical_attempt,
                duplicate_physical_attempt,
            ],
            current_run_compatibility_contract=compatibility,
        )
    drifted_compatibility = deepcopy(compatibility)
    drifted_compatibility["g1_registry_contract"]["expected_candidate_count"] += 1
    with pytest.raises(ValueError, match="invalid cross-Wave frozen G1"):
        resume_runner.reconstruct_g1_cross_wave_frozen_lifecycle(
            attempts=[historical_attempt],
            current_run_compatibility_contract=drifted_compatibility,
        )

    source_attempt = deepcopy(historical_attempt)
    source_attempt.update(
        {
            "started_at": 1.0,
            "completed_at": 2.0,
            "retryable": True,
            "retry_reason": "provider_error",
            "retry_suppressed_reason": "",
            "retry_backoff_s": 0.0,
        }
    )
    prompt_hash = resume_runner.text_sha256("test prompt")
    source_row = resume_runner.seal_result_row(
        {
            "group": "G1",
            "provider_spec": dict(resume_runner.GROUP_SPECS["G1"]),
            "routing_trace": {
                "selection_plan": deepcopy(initial_plan),
            },
            "task_id": "task-1",
            "prompt_sha256": prompt_hash,
            "task_input_sha256": "sha256:task-input",
            "run_compatibility_fingerprint": "sha256:run-contract",
            "error": "provider_error",
            "final_text": "",
            "llm_request_count": source_attempt["run"]["llm_request_count"],
            "generation_attempt_count": 1,
            "generation_attempt_evidence_schema": (
                resume_runner.GENERATION_ATTEMPT_EVIDENCE_SCHEMA
            ),
            "generation_attempt_budget_limit": 3,
            "generation_attempt_budget_used": 1,
            "generation_completed_at": 2.0,
            "actual_spend_metrics": {
                "generation_attempt_count": 1,
            },
            "execution": {
                "generation_attempts": [source_attempt],
                "prior_generation_attempts_used": 0,
                "generation_attempt_budget_remaining": 2,
            },
            "usage": deepcopy(source_attempt["run"]["usage"]),
        }
    )
    source_path = tmp_path / "g1-wave.jsonl"
    source_path.write_text(
        json.dumps(source_row) + "\n",
        encoding="utf-8",
    )
    states, _ = resume_runner.load_resume_group_task_states(
        resume_paths=[source_path],
        selected_keys={("G1", "task-1")},
        prompt_hashes={"task-1": prompt_hash},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={
            "G1": "sha256:run-contract",
        },
        run_compatibility_contracts={"G1": compatibility},
    )
    state = states[("G1", "task-1")]
    assert state["action"] == "regenerate"
    attached_lifecycle = state["g1_frozen_resume_lifecycle"]
    assert attached_lifecycle["target_plan"] == initial_plan

    attached_build = await resume_runner.build_experiment_provider(
        config=config,
        inherited=inherited,
        group="G1",
        prompt="test prompt",
        dry_run=False,
        enable_proposer_tools=False,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        experiment_config=experiment,
        g1_registry_contract=contract,
        generation_policy=None,
        frozen_g1_lifecycle=attached_lifecycle,
    )
    assert analyzer_calls == 1
    assert attached_build.setup_usage == []
    paid_providers: list[object] = []

    async def fake_collect_run(active_provider, *_args, **_kwargs):
        paid_providers.append(active_provider)
        active_plan = deepcopy(active_provider.selection_plan)
        active_trace = deepcopy(historical_attempt["run"]["ensemble_trace"])
        active_trace["selection_plan"] = active_plan
        active_candidate = active_trace["candidates"][0]
        active_physical_attempt = active_candidate["execution"]["physical_attempts"][0]
        active_physical_attempt["physical_attempt_id"] = "e" * 32
        active_usage = deepcopy(generation_usage)
        active_usage["physical_attempt_id"] = "e" * 32
        active_usage["provider_usage"]["physical_attempt_id"] = "e" * 32
        return resume_runner.RunResult(
            final_text="accepted",
            done=DoneEvent(
                model_usage_breakdown=[active_usage],
                ensemble_trace=active_trace,
            ),
            routing_trace={"selection_plan": active_plan},
        )

    monkeypatch.setattr(
        resume_runner,
        "collect_run",
        fake_collect_run,
    )
    monkeypatch.setattr(
        resume_runner,
        "generation_retry_reason",
        lambda *_args, **_kwargs: "",
    )
    result, attempts, selected_attempt = await resume_runner.collect_generation_with_retries(
        attached_build.provider,
        "test prompt",
        timeout=30,
        group="G1",
        max_attempts=2,
        attempt_offset=1,
    )
    assert result.final_text == "accepted"
    assert selected_attempt == 2
    assert paid_providers == [attached_build.provider]
    assert attempts[0]["excluded_proposer_identities"] == []


def test_resume_reconstructs_frozen_g1_a_to_b_to_c_pending_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_a = "openrouter:model-a"
    failed_b = "openrouter:model-b"
    plan_a = _frozen_g1_plan(
        decision_id="decision-a",
        proposers=[failed_a, failed_b, "openrouter:model-c"],
    )
    plan_b = _bind_g1_retry_plan(
        plan_a,
        _frozen_g1_plan(
            decision_id="decision-b",
            proposers=[failed_b, "openrouter:model-c", "openrouter:model-d"],
        ),
        exclusions=[failed_a],
    )
    plan_c = _bind_g1_retry_plan(
        plan_a,
        _frozen_g1_plan(
            decision_id="decision-c",
            proposers=[
                "openrouter:model-c",
                "openrouter:model-d",
                "openrouter:model-e",
            ],
        ),
        exclusions=[failed_a, failed_b],
    )
    analyzer = [_task_analyzer_unit()]
    run_a, failures_a = _frozen_g1_failure_run(
        resume_runner,
        plan=plan_a,
        failed_identity=failed_a,
        analyzer_units=analyzer,
    )
    from opensquilla.provider.thinking_execution import (
        project_thinking_execution_history,
    )

    projected_b, projection_audit_b, projection_reason_b = project_thinking_execution_history(
        [plan_a], plan_b
    )
    assert projection_reason_b == ""
    plan_b = projected_b
    run_b, failures_b = _frozen_g1_failure_run(
        resume_runner,
        plan=plan_b,
        failed_identity=failed_b,
    )
    projected_c, projection_audit_c, projection_reason_c = project_thinking_execution_history(
        [plan_a, plan_b], plan_c
    )
    assert projection_reason_c == ""
    plan_c = projected_c
    attempts = [
        {
            "attempt_id": "1" * 32,
            "attempt_kind": "generation",
            "attempt": 1,
            "selection_plan": plan_a,
            "excluded_proposer_identities": [],
            "deterministic_proposer_failures": failures_a,
            "retry_selection_plan": plan_b,
            "retry_excluded_proposer_identities": [failed_a],
            "thinking_execution_projection": projection_audit_b,
            "will_retry": True,
            "run": run_a,
        },
        {
            "attempt_id": "2" * 32,
            "attempt_kind": "generation",
            "attempt": 2,
            "selection_plan": plan_b,
            "excluded_proposer_identities": [failed_a],
            "deterministic_proposer_failures": failures_b,
            "retry_selection_plan": plan_c,
            "retry_excluded_proposer_identities": [failed_a, failed_b],
            "thinking_execution_projection": projection_audit_c,
            "will_retry": False,
            "retry_deferred_to_next_wave": True,
            "run": run_b,
        },
    ]
    monkeypatch.setattr(
        resume_runner,
        "_g1_frozen_lifecycle_plan_reasons",
        lambda *_args, **_kwargs: [],
    )

    lifecycle = resume_runner.reconstruct_g1_cross_wave_frozen_lifecycle(
        attempts=attempts,
        current_run_compatibility_contract=(_enabled_g1_frozen_lifecycle_contract()),
    )

    assert lifecycle["initial_parent_decision_id"] == "decision-a"
    assert lifecycle["target_plan"] == plan_c
    assert lifecycle["target_plan_source"] == "pending_retry_selection_plan"
    assert lifecycle["cumulative_excluded_proposer_identities"] == [
        failed_a,
        failed_b,
    ]
    assert lifecycle["target_execution_prefix"] == plan_c
    assert lifecycle["thinking_execution_projection"] == projection_audit_c
    assert lifecycle["task_analyzer_physical_request_count"] == 1

    tampered = deepcopy(attempts)
    tampered[0]["run"]["ensemble_trace"]["candidates"][0]["execution"][
        "effective_provider_thinking_level"
    ] = "low"
    with pytest.raises(
        ValueError,
        match="invalid_g1_physical_thinking_execution",
    ):
        resume_runner.reconstruct_g1_cross_wave_frozen_lifecycle(
            attempts=tampered,
            current_run_compatibility_contract=(_enabled_g1_frozen_lifecycle_contract()),
        )


@pytest.mark.parametrize("invalid_marker", [0, 1])
def test_resume_frozen_g1_retry_deferred_marker_is_exact_boolean(
    monkeypatch: pytest.MonkeyPatch,
    invalid_marker: int,
) -> None:
    failed = "openrouter:model-a"
    plan_a = _frozen_g1_plan(
        decision_id="decision-a",
        proposers=[
            failed,
            "openrouter:model-b",
            "openrouter:model-c",
        ],
    )
    plan_b = _bind_g1_retry_plan(
        plan_a,
        _frozen_g1_plan(
            decision_id="decision-b",
            proposers=[
                "openrouter:model-b",
                "openrouter:model-c",
                "openrouter:model-d",
            ],
        ),
        exclusions=[failed],
    )
    run, failures = _frozen_g1_failure_run(
        resume_runner,
        plan=plan_a,
        failed_identity=failed,
        analyzer_units=[_task_analyzer_unit()],
    )
    monkeypatch.setattr(
        resume_runner,
        "_g1_frozen_lifecycle_plan_reasons",
        lambda *_args, **_kwargs: [],
    )

    with pytest.raises(ValueError, match="invalid_g1_retry_deferred_marker"):
        resume_runner.reconstruct_g1_cross_wave_frozen_lifecycle(
            attempts=[
                {
                    "attempt_id": "1" * 32,
                    "attempt_kind": "generation",
                    "attempt": 1,
                    "selection_plan": plan_a,
                    "excluded_proposer_identities": [],
                    "deterministic_proposer_failures": failures,
                    "retry_selection_plan": plan_b,
                    "retry_excluded_proposer_identities": [failed],
                    "will_retry": False,
                    "retry_deferred_to_next_wave": invalid_marker,
                    "run": run,
                }
            ],
            current_run_compatibility_contract=(_enabled_g1_frozen_lifecycle_contract()),
        )


def test_resume_frozen_g1_accepts_unknown_then_successful_analyzer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _frozen_g1_plan(
        decision_id="decision-a",
        proposers=[
            "openrouter:model-a",
            "openrouter:model-b",
            "openrouter:model-c",
        ],
    )
    analyzer_units = [
        _unknown_task_analyzer_unit(
            attempt=1,
            physical_attempt_id="a" * 32,
        ),
        _task_analyzer_unit(
            attempt=2,
            physical_attempt_id="b" * 32,
        ),
    ]
    plan["task_analyzer"]["usage"] = _task_analyzer_usage(analyzer_units)
    run, failures = _frozen_g1_failure_run(
        resume_runner,
        plan=plan,
        failed_identity=None,
        analyzer_units=analyzer_units,
    )
    monkeypatch.setattr(
        resume_runner,
        "_g1_frozen_lifecycle_plan_reasons",
        lambda *_args, **_kwargs: [],
    )

    lifecycle = resume_runner.reconstruct_g1_cross_wave_frozen_lifecycle(
        attempts=[
            {
                "attempt_id": "1" * 32,
                "attempt_kind": "generation",
                "attempt": 1,
                "selection_plan": plan,
                "excluded_proposer_identities": [],
                "deterministic_proposer_failures": failures,
                "will_retry": False,
                "run": run,
            }
        ],
        current_run_compatibility_contract=(_enabled_g1_frozen_lifecycle_contract()),
    )

    assert lifecycle["task_analyzer_physical_request_count"] == 2


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("setup_usage_mismatch", "g1_task_analyzer_setup_usage_mismatch"),
        ("setup_usage_missing", "missing_g1_task_analyzer_setup_usage"),
        ("aggregate_usage_missing", "missing_g1_task_analyzer_aggregate_usage"),
        ("analyzer_ordinal_gap", "invalid_g1_task_analyzer_attempt_sequence"),
        ("analyzer_duplicate_id", "invalid_g1_task_analyzer_attempt_sequence"),
        (
            "analyzer_retry_limit_exceeded",
            "invalid_g1_task_analyzer_attempt_sequence",
        ),
    ],
)
def test_resume_frozen_g1_rejects_analyzer_evidence_contradictions(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_reason: str,
) -> None:
    plan = _frozen_g1_plan(
        decision_id="decision-a",
        proposers=[
            "openrouter:model-a",
            "openrouter:model-b",
            "openrouter:model-c",
        ],
    )
    units = [
        _task_analyzer_unit(
            attempt=1,
            physical_attempt_id="a" * 32,
        ),
        _task_analyzer_unit(
            attempt=2,
            physical_attempt_id="b" * 32,
        ),
    ]
    if mutation == "analyzer_retry_limit_exceeded":
        plan["ranking_parameters"]["task_analyzer"]["max_retries"] = 0
    run, failures = _frozen_g1_failure_run(
        resume_runner,
        plan=plan,
        failed_identity=None,
        analyzer_units=units,
    )
    if mutation == "setup_usage_mismatch":
        run["setup_usage"][1]["output_tokens"] = 99
    elif mutation == "setup_usage_missing":
        run["setup_usage"] = []
    elif mutation == "aggregate_usage_missing":
        run["usage"]["model_usage_breakdown"] = []
    elif mutation == "analyzer_ordinal_gap":
        run["usage"]["model_usage_breakdown"][1]["attempt"] = 3
        run["setup_usage"][1]["attempt"] = 3
    elif mutation == "analyzer_duplicate_id":
        run["usage"]["model_usage_breakdown"][1]["physical_attempt_id"] = "a" * 32
        run["setup_usage"][1]["physical_attempt_id"] = "a" * 32
    monkeypatch.setattr(
        resume_runner,
        "_g1_frozen_lifecycle_plan_reasons",
        lambda *_args, **_kwargs: [],
    )

    with pytest.raises(ValueError, match=expected_reason):
        resume_runner.reconstruct_g1_cross_wave_frozen_lifecycle(
            attempts=[
                {
                    "attempt_id": "1" * 32,
                    "attempt_kind": "generation",
                    "attempt": 1,
                    "selection_plan": plan,
                    "excluded_proposer_identities": [],
                    "deterministic_proposer_failures": failures,
                    "will_retry": False,
                    "run": run,
                }
            ],
            current_run_compatibility_contract=(_enabled_g1_frozen_lifecycle_contract()),
        )


def test_resume_frozen_g1_rejects_tail_deterministic_failure_without_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = "openrouter:model-a"
    plan = _frozen_g1_plan(
        decision_id="decision-a",
        proposers=[failed, "openrouter:model-b", "openrouter:model-c"],
    )
    run, failures = _frozen_g1_failure_run(
        resume_runner,
        plan=plan,
        failed_identity=failed,
        analyzer_units=[_task_analyzer_unit()],
    )
    monkeypatch.setattr(
        resume_runner,
        "_g1_frozen_lifecycle_plan_reasons",
        lambda *_args, **_kwargs: [],
    )

    with pytest.raises(
        ValueError,
        match="g1_deterministic_failure_lacks_pending_retry_plan",
    ):
        resume_runner.reconstruct_g1_cross_wave_frozen_lifecycle(
            attempts=[
                {
                    "attempt_id": "1" * 32,
                    "attempt_kind": "generation",
                    "attempt": 1,
                    "selection_plan": plan,
                    "excluded_proposer_identities": [],
                    "deterministic_proposer_failures": failures,
                    "will_retry": False,
                    "run": run,
                }
            ],
            current_run_compatibility_contract=(_enabled_g1_frozen_lifecycle_contract()),
        )


def test_resume_strict_attempt_evidence_reconstructs_cumulative_budget(
    tmp_path: Path,
) -> None:
    paths: list[Path] = []
    for wave in range(1, 4):
        row = resume_runner.seal_result_row(
            _strict_attempt_resume_row(
                attempt_id=f"{wave:032x}",
                cumulative_budget=wave,
                generation_completed_at=float(wave),
            )
        )
        path = tmp_path / f"strict-wave-{wave}.jsonl"
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        paths.append(path)

    states, _ = resume_runner.load_resume_group_task_states(
        resume_paths=paths,
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": resume_runner.text_sha256("same prompt")},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )

    state = states[("B1", "task-1")]
    assert state["prior_generation_attempts_used"] == 3
    assert state["observed_unique_generation_attempt_count"] == 3
    assert state["generation_attempt_evidence_mode"] == "strict_v1"


@pytest.mark.parametrize(
    ("terminal_kind", "expected_reason"),
    [
        (
            "generation_pre_call_guard",
            "g1_pre_call_guard_failed",
        ),
        (
            "provider_build_after_paid_setup",
            "g1_paid_setup_attempt_lacks_frozen_lifecycle",
        ),
    ],
)
def test_resume_strict_g1_terminal_history_cannot_be_washed_by_later_complete(
    tmp_path: Path,
    terminal_kind: str,
    expected_reason: str,
) -> None:
    compatibility = _enabled_g1_frozen_lifecycle_contract()
    fingerprint = "sha256:g1-run-contract"
    terminal = _strict_attempt_resume_row(
        attempt_id="1" * 32,
        cumulative_budget=1,
        generation_completed_at=1.0,
    )
    terminal.update(
        {
            "group": "G1",
            "provider_spec": dict(resume_runner.GROUP_SPECS["G1"]),
            "run_compatibility_fingerprint": fingerprint,
        }
    )
    terminal_attempt = terminal["execution"]["generation_attempts"][0]
    terminal_attempt["attempt_kind"] = terminal_kind
    if terminal_kind == "generation_pre_call_guard":
        terminal_attempt["run"].update(
            {
                "llm_request_count": 0,
                "usage_unknown_count": 0,
                "trace_events": [
                    {
                        "kind": "error",
                        "code": "g1_pre_call_guard_failed",
                        "request_started": False,
                        "physical_request_count": 0,
                    }
                ],
                "routing_trace": {
                    "pre_call_guard": {
                        "code": "immutable_plan_hash_drift",
                    }
                },
            }
        )

    later_complete = _strict_attempt_resume_row(
        attempt_id="2" * 32,
        cumulative_budget=2,
        generation_completed_at=2.0,
    )
    later_complete.update(
        {
            "group": "G1",
            "provider_spec": dict(resume_runner.GROUP_SPECS["G1"]),
            "run_compatibility_fingerprint": fingerprint,
            "error": None,
            "final_text": "later successful answer",
            "quality_total": 80.0,
            "judge": _complete_legacy_judge("later-complete-after-terminal-history"),
        }
    )
    later_attempt = later_complete["execution"]["generation_attempts"][0]
    later_attempt.update(
        {
            "retryable": False,
            "retry_reason": "",
        }
    )
    later_attempt["run"].update(
        {
            "error": "",
            "usage_unknown_count": 0,
        }
    )

    paths: list[Path] = []
    for wave, row in enumerate(
        (terminal, later_complete),
        start=1,
    ):
        path = tmp_path / f"terminal-g1-wave-{wave}.jsonl"
        path.write_text(
            json.dumps(resume_runner.seal_result_row(row)) + "\n",
            encoding="utf-8",
        )
        paths.append(path)

    with pytest.raises(ValueError, match=expected_reason):
        resume_runner.load_resume_group_task_states(
            resume_paths=paths,
            selected_keys={("G1", "task-1")},
            prompt_hashes={"task-1": resume_runner.text_sha256("same prompt")},
            task_input_hashes={"task-1": "sha256:task-input"},
            run_compatibility_fingerprints={"G1": fingerprint},
            run_compatibility_contracts={"G1": compatibility},
        )


def test_resume_legacy_attempt_schema_does_not_use_strict_guard_scan(
    tmp_path: Path,
) -> None:
    row = _strict_attempt_resume_row(
        attempt_id="1" * 32,
        cumulative_budget=1,
        generation_completed_at=1.0,
    )
    row.pop("generation_attempt_evidence_schema")
    row["error"] = "g1_pre_call_guard_failed"
    path = tmp_path / "legacy-attempt.jsonl"
    path.write_text(
        json.dumps(resume_runner.seal_result_row(row)) + "\n",
        encoding="utf-8",
    )

    states, _ = resume_runner.load_resume_group_task_states(
        resume_paths=[path],
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": resume_runner.text_sha256("same prompt")},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )

    assert states[("B1", "task-1")]["action"] == "regenerate"


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("prompt_sha256", "sha256:stale-prompt"),
        ("task_input_sha256", "sha256:stale-task"),
        (
            "run_compatibility_fingerprint",
            "sha256:stale-run-contract",
        ),
    ],
)
def test_resume_strict_attempt_source_rows_are_bound_before_aggregation(
    tmp_path: Path,
    field_name: str,
    replacement: str,
) -> None:
    row = _strict_attempt_resume_row(
        attempt_id="1" * 32,
        cumulative_budget=1,
        generation_completed_at=1.0,
    )
    row[field_name] = replacement
    path = tmp_path / "stale-strict-attempt.jsonl"
    path.write_text(
        json.dumps(resume_runner.seal_result_row(row)) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="strict generation attempt source binding differs",
    ):
        resume_runner.load_resume_group_task_states(
            resume_paths=[path],
            selected_keys={("B1", "task-1")},
            prompt_hashes={"task-1": resume_runner.text_sha256("same prompt")},
            task_input_hashes={"task-1": "sha256:task-input"},
            run_compatibility_fingerprints={"B1": "sha256:run-contract"},
        )


def test_resume_budget_exhaustion_returns_before_provider_construction() -> None:
    tree = ast.parse(inspect.getsource(resume_runner._amain_with_run_lock))
    guarded = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_guarded"
    )
    exhausted_branch = next(
        node
        for node in guarded.body
        if isinstance(node, ast.If) and "remaining_attempts <= 0" in ast.unparse(node.test)
    )
    exhausted_return = next(
        node for node in ast.walk(exhausted_branch) if isinstance(node, ast.Return)
    )
    run_one_await = next(
        node
        for node in ast.walk(guarded)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "run_one"
    )

    assert "_generation_budget_exhausted_row" in ast.unparse(exhausted_return.value)
    assert exhausted_return.lineno < run_one_await.lineno


def test_resume_strict_attempt_evidence_accepts_monotonic_repair(
    tmp_path: Path,
) -> None:
    attempt_id = "a" * 32
    first = _strict_attempt_resume_row(
        attempt_id=attempt_id,
        cumulative_budget=1,
        generation_completed_at=1.0,
    )
    first_attempt = first["execution"]["generation_attempts"][0]
    first_attempt["run"]["usage"] = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 10,
        "output_tokens": 2,
        "cost_source": "none",
    }
    repaired = json.loads(json.dumps(first))
    repaired["generation_completed_at"] = 2.0
    repaired["execution"]["resume_action"] = "metadata_only"
    repaired["execution"]["generation_reused"] = True
    repaired["execution"]["prior_generation_attempts_used"] = 1
    repaired["execution"]["generation_attempt_budget_remaining"] = 2
    repaired_usage = repaired["execution"]["generation_attempts"][0]["run"]["usage"]
    repaired_usage.update(
        {
            "cost_source": "opensquilla_static_estimate",
            "estimated_cost_usd": 0.002,
            "cost_usd": 0.002,
            "provider_usage": {
                "cost_repair": "token_price_estimate",
                "price_source": "frozen",
            },
        }
    )

    paths: list[Path] = []
    for wave, row in enumerate((first, repaired), start=1):
        path = tmp_path / f"repair-wave-{wave}.jsonl"
        path.write_text(
            json.dumps(resume_runner.seal_result_row(row)) + "\n",
            encoding="utf-8",
        )
        paths.append(path)

    states, _ = resume_runner.load_resume_group_task_states(
        resume_paths=paths,
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": resume_runner.text_sha256("same prompt")},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )

    state = states[("B1", "task-1")]
    assert state["prior_generation_attempts_used"] == 1
    assert state["observed_unique_generation_attempt_count"] == 1
    assert state["row"]["execution"]["generation_attempts"][0]["attempt_id"] == attempt_id


def test_resume_strict_attempt_evidence_allows_cost_confidence_upgrade_only(
    tmp_path: Path,
) -> None:
    attempt_id = "c" * 32
    initial = _strict_attempt_resume_row(
        attempt_id=attempt_id,
        cumulative_budget=1,
        generation_completed_at=1.0,
    )
    initial_usage = initial["execution"]["generation_attempts"][0]["run"]["usage"]
    initial_usage.update(
        {
            "provider": "openrouter",
            "model": "model-a",
            "input_tokens": 10,
            "output_tokens": 2,
            "cost_source": "none",
        }
    )
    estimate = json.loads(json.dumps(initial))
    estimate["generation_completed_at"] = 2.0
    estimate["execution"].update(
        {
            "resume_action": "metadata_only",
            "generation_reused": True,
            "prior_generation_attempts_used": 1,
            "generation_attempt_budget_remaining": 2,
        }
    )
    estimate_usage = estimate["execution"]["generation_attempts"][0]["run"]["usage"]
    estimate_usage.update(
        {
            "cost_source": "opensquilla_static_estimate",
            "estimated_cost_usd": 0.002,
            "cost_usd": 0.002,
            "provider_usage": {
                "cost_repair": "token_price_estimate",
                "price_source": "frozen",
            },
        }
    )
    exact = json.loads(json.dumps(estimate))
    exact["generation_completed_at"] = 3.0
    exact_usage = exact["execution"]["generation_attempts"][0]["run"]["usage"]
    exact_usage.update(
        {
            "cost_source": "provider_billed",
            "billed_cost": 0.0021,
            "provider_usage": {
                **exact_usage["provider_usage"],
                **_openrouter_exact_evidence(
                    0.0021,
                    "strict-attempt-cost-upgrade",
                ),
            },
        }
    )

    accepted_paths: list[Path] = []
    for wave, row in enumerate((initial, estimate, exact), start=1):
        path = tmp_path / f"cost-upgrade-wave-{wave}.jsonl"
        path.write_text(
            json.dumps(resume_runner.seal_result_row(row)) + "\n",
            encoding="utf-8",
        )
        accepted_paths.append(path)

    states, _ = resume_runner.load_resume_group_task_states(
        resume_paths=accepted_paths,
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": resume_runner.text_sha256("same prompt")},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )
    retained_usage = states[("B1", "task-1")]["row"]["execution"]["generation_attempts"][0]["run"][
        "usage"
    ]
    assert retained_usage["estimated_cost_usd"] == pytest.approx(0.002)
    assert retained_usage["cost_usd"] == pytest.approx(0.002)
    assert retained_usage["billed_cost"] == pytest.approx(0.0021)
    assert retained_usage["cost_source"] == "provider_billed"

    conflicting_exact = json.loads(json.dumps(exact))
    conflicting_exact["generation_completed_at"] = 4.0
    conflicting_usage = conflicting_exact["execution"]["generation_attempts"][0]["run"]["usage"]
    conflicting_usage["billed_cost"] = 0.003
    conflicting_usage["provider_usage"]["provider_reported_cost"] = 0.003
    conflict_path = tmp_path / "cost-upgrade-wave-4.jsonl"
    conflict_path.write_text(
        json.dumps(resume_runner.seal_result_row(conflicting_exact)) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="conflicting evidence"):
        resume_runner.load_resume_group_task_states(
            resume_paths=[*accepted_paths, conflict_path],
            selected_keys={("B1", "task-1")},
            prompt_hashes={"task-1": resume_runner.text_sha256("same prompt")},
            task_input_hashes={"task-1": "sha256:task-input"},
            run_compatibility_fingerprints={"B1": "sha256:run-contract"},
        )


def test_resume_strict_attempt_evidence_rejects_cross_task_owner(
    tmp_path: Path,
) -> None:
    attempt_id = "b" * 32
    first = _strict_attempt_resume_row(
        attempt_id=attempt_id,
        cumulative_budget=1,
        generation_completed_at=1.0,
    )
    second = _strict_attempt_resume_row(
        attempt_id=attempt_id,
        cumulative_budget=1,
        generation_completed_at=2.0,
    )
    second["task_id"] = "task-2"
    second["prompt_sha256"] = resume_runner.text_sha256("other prompt")
    second["task_input_sha256"] = "sha256:other-task-input"

    paths: list[Path] = []
    for wave, row in enumerate((first, second), start=1):
        path = tmp_path / f"owner-wave-{wave}.jsonl"
        path.write_text(
            json.dumps(resume_runner.seal_result_row(row)) + "\n",
            encoding="utf-8",
        )
        paths.append(path)

    with pytest.raises(ValueError, match="already owned"):
        resume_runner.load_resume_group_task_states(
            resume_paths=paths,
            selected_keys={("B1", "task-1"), ("B1", "task-2")},
            prompt_hashes={
                "task-1": resume_runner.text_sha256("same prompt"),
                "task-2": resume_runner.text_sha256("other prompt"),
            },
            task_input_hashes={
                "task-1": "sha256:task-input",
                "task-2": "sha256:other-task-input",
            },
            run_compatibility_fingerprints={"B1": "sha256:run-contract"},
        )


@pytest.mark.parametrize(
    ("mutation", "error_match"),
    [
        ("non_monotonic", "contradicts unique physical evidence"),
        ("over_limit", "budget declaration is invalid"),
        ("conflicting_identity", "conflicting evidence"),
        ("ordinal_reset", "ordinal is not cumulative"),
        ("mixed_schema", "mixed generation attempt evidence schemas"),
        ("actual_count", "actual-spend generation attempt count contradicts"),
    ],
)
def test_resume_strict_attempt_evidence_fails_closed_on_contradiction(
    tmp_path: Path,
    mutation: str,
    error_match: str,
) -> None:
    first = _strict_attempt_resume_row(
        attempt_id="1" * 32,
        cumulative_budget=1,
        generation_completed_at=1.0,
    )
    second = _strict_attempt_resume_row(
        attempt_id="2" * 32,
        cumulative_budget=2,
        generation_completed_at=2.0,
    )
    if mutation == "non_monotonic":
        second["generation_attempt_budget_used"] = 0
        second["execution"]["generation_attempt_budget_remaining"] = 3
    elif mutation == "over_limit":
        second["generation_attempt_budget_used"] = 4
    elif mutation == "conflicting_identity":
        second["execution"]["generation_attempts"][0]["attempt_id"] = "1" * 32
    elif mutation == "ordinal_reset":
        second["execution"]["generation_attempts"][0]["attempt"] = 1
    elif mutation == "mixed_schema":
        second.pop("generation_attempt_evidence_schema")
    else:
        second["actual_spend_metrics"]["generation_attempt_count"] = 2

    paths: list[Path] = []
    for wave, row in enumerate((first, second), start=1):
        path = tmp_path / f"contradictory-wave-{wave}.jsonl"
        path.write_text(
            json.dumps(resume_runner.seal_result_row(row)) + "\n",
            encoding="utf-8",
        )
        paths.append(path)

    with pytest.raises(ValueError, match=error_match):
        resume_runner.load_resume_group_task_states(
            resume_paths=paths,
            selected_keys={("B1", "task-1")},
            prompt_hashes={"task-1": resume_runner.text_sha256("same prompt")},
            task_input_hashes={"task-1": "sha256:task-input"},
            run_compatibility_fingerprints={"B1": "sha256:run-contract"},
        )


def test_resume_invalid_attempt_count_does_not_mix_rows_and_pairs(
    tmp_path: Path,
) -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    base = {
        "group": "B1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B1"]),
        "routing_trace": {
            "applied_model": "model-a",
            "fallback_model": "model-a",
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": None,
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "usage": {
            "provider": "openrouter",
            "model": "model-a",
            "requested_provider": "openrouter",
            "requested_model": "model-a",
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                "complete-duplicate",
            ),
        },
        "quality_total": 80.0,
        "judge": _complete_legacy_judge("complete-duplicate-judge"),
    }
    rows = [resume_runner.seal_result_row({**base, "copy": copy_index}) for copy_index in (1, 2)]
    path = tmp_path / "complete-duplicates.jsonl"
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    _, audit = resume_runner.load_resume_group_task_states(
        resume_paths=[path],
        selected_keys={("B1", "task-1")},
        prompt_hashes={"task-1": prompt_hash},
        task_input_hashes={"task-1": "sha256:task-input"},
        run_compatibility_fingerprints={"B1": "sha256:run-contract"},
    )

    assert audit["matching_attempt_count"] == 2
    assert audit["strict_valid_pair_count"] == 1
    assert audit["strict_invalid_attempt_count"] == 0


@pytest.mark.parametrize("group", ["B2", "G1"])
def test_resume_requires_ensemble_completion_proof_for_routing_groups(
    group: str,
) -> None:
    resume_runner = _load_resume_runner()
    prompt_hash = resume_runner.text_sha256("same prompt")
    row = resume_runner.seal_result_row(
        {
            "group": group,
            "task_id": "task-1",
            "prompt_sha256": prompt_hash,
            "task_input_sha256": "sha256:task-input",
            "run_compatibility_fingerprint": "sha256:run-contract",
            "error": None,
            "final_text": "answer without an ensemble proof",
            "quality_total": 80.0,
            "judge": _complete_legacy_judge("missing-ensemble-judge"),
        }
    )

    state = resume_runner.resume_row_completion_state(
        row,
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )

    assert state["action"] == "regenerate"
    assert "missing_ensemble_trace" in state["generation_reasons"]


@pytest.mark.parametrize("group", ["B0", "B1", "B4"])
def test_resume_does_not_apply_ensemble_proof_to_single_agent_loop_calls(
    group: str,
) -> None:
    prompt_hash = resume_runner.text_sha256("same prompt")
    spec = dict(resume_runner.GROUP_SPECS[group])
    expected_model = str(spec.get("model") or "routed-model")
    row = {
        "group": group,
        "provider_spec": spec,
        "routing_trace": {
            "applied_model": expected_model,
            "fallback_model": expected_model,
        },
        "task_id": "task-1",
        "prompt_sha256": prompt_hash,
        "task_input_sha256": "sha256:task-input",
        "run_compatibility_fingerprint": "sha256:run-contract",
        "error": "openrouter_non_byok_verification_failed",
        "final_text": "accepted answer",
        "llm_request_count": 1,
        "usage": {
            "provider": "openrouter",
            "model": expected_model,
            "requested_provider": "openrouter",
            "requested_model": expected_model,
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                f"single-agent-loop-{group}",
            ),
        },
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 1,
            "llm_request_count": 1,
            "physical_request_count": 1,
            "calls": [
                {
                    "agent_call_index": 1,
                    "request_outcome": "llm_response",
                    "trace_missing": True,
                }
            ],
        },
        "quality_total": 80.0,
        "judge": _complete_legacy_judge(f"single-agent-loop-judge-{group}"),
        "openrouter_non_byok_audit": {
            "pass": False,
            "request_count": 2,
            "exact_request_count": 1,
            "unverified_or_byok_request_count": 1,
        },
    }

    assert resume_runner.ensemble_generation_completion_reasons(row) == []
    state = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )

    assert state["generation_valid"] is True
    assert state["generation_reasons"] == []
    assert state["action"] == "audit_only"


def test_resume_binds_final_aggregator_output_to_agent_text_tail() -> None:
    resume_runner = _load_resume_runner()
    final_answer = "authoritative final answer"
    selection_plan = {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "profile": "router_dynamic/c3",
        "proposer_models": ["p1", "p2", "p3", "p4"],
        "proposer_sample_count": 4,
        "selected_P": [
            "openrouter:p1",
            "openrouter:p2",
            "openrouter:p3",
            "openrouter:p4",
        ],
        "aggregator_model": "agg",
        "selected_A": "openrouter:agg",
    }
    final_trace = {
        "agent_call_index": 1,
        "request_outcome": "llm_response",
        "selection_strategy": "router_dynamic",
        "selection_plan": selection_plan,
        "successful_proposers": 3,
        "total_candidates": 4,
        "fallback_used": False,
        "candidates": [
            {
                "provider": "openrouter",
                "requested_provider": "openrouter",
                "model": model,
                "requested_model": model,
                "ok": index < 3,
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": index < 3,
                "stop_reason": "stop" if index < 3 else "",
                "content": {
                    "text": f"candidate-{index}" if index < 3 else "",
                    "chars": len(f"candidate-{index}") if index < 3 else 0,
                    "truncated": False,
                },
                **({} if index < 3 else {"error": "failed", "error_code": "test_failure"}),
            }
            for index, model in enumerate(selection_plan["proposer_models"])
        ],
        "final_request_role": "aggregator",
        "final_request": {
            "role": "aggregator",
            "request_started": True,
            "execution": {
                "provider": "openrouter",
                "requested_provider": "openrouter",
                "requested_model": "agg",
            },
            "usage": {
                "stop_reason": "stop",
                "provider": "openrouter",
                "model": "agg",
                "requested_provider": "openrouter",
                "requested_model": "agg",
            },
            "output": {
                "text": final_answer,
                "chars": len(final_answer),
                "truncated": False,
            },
        },
    }
    row = {
        "group": "G1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["G1"]),
        "routing_trace": {"selection_plan": selection_plan},
        "final_text": f"earlier tool-loop narration\n{final_answer}",
        "ensemble_trace": {"calls": [final_trace], "agent_llm_call_count": 1},
    }

    reasons = resume_runner.ensemble_generation_completion_reasons(row)

    assert "aggregator_output_mismatch" not in reasons
    assert reasons == []

    row["ensemble_trace"]["calls"][0]["final_request"]["output"] = {
        "text": final_answer[:8],
        "chars": len(final_answer),
        "truncated": True,
    }
    assert resume_runner.ensemble_generation_completion_reasons(row) == []


def test_resume_verifies_b2_runtime_lineup_against_current_contract() -> None:
    resume_runner = _load_resume_runner()
    experiment = json.loads(
        resume_runner.DEFAULT_B2_EXPERIMENT_CONFIG_PATH.read_text(encoding="utf-8")
    )
    ensemble = experiment["ensemble"]
    expected_models = [member["model"] for member in ensemble["proposers"]]
    expected_quorum = resume_runner.legal_proposer_quorum(len(expected_models))
    selection_plan = {
        "strategy": experiment["routing"]["selection_mode"],
        "selection_mode": experiment["routing"]["selection_mode"],
        "profile": ensemble["profile_name"],
        "proposer_models": expected_models,
        "proposer_sample_count": len(expected_models),
        "selected_P": [
            f"{member['provider']}:{member['model']}" for member in ensemble["proposers"]
        ],
        "aggregator_model": ensemble["aggregator"]["model"],
        "selected_A": (f"{ensemble['aggregator']['provider']}:{ensemble['aggregator']['model']}"),
        "configured_min_successful_proposers": expected_quorum,
        "effective_min_successful_proposers": expected_quorum,
        "legal_min_successful_proposers": expected_quorum,
    }
    final_answer = "answer"
    final_trace = {
        "agent_call_index": 1,
        "request_outcome": "llm_response",
        "selection_strategy": experiment["routing"]["selection_mode"],
        "selection_plan": selection_plan,
        "successful_proposers": expected_quorum,
        "total_candidates": len(expected_models),
        "fallback_used": False,
        "candidates": [
            {
                "provider": member["provider"],
                "requested_provider": member["provider"],
                "model": member["model"],
                "requested_model": member["model"],
                "ok": index < expected_quorum,
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": index < expected_quorum,
                "stop_reason": ("stop" if index < expected_quorum else ""),
                "content": {
                    "text": (f"candidate-{index}" if index < expected_quorum else ""),
                    "chars": (len(f"candidate-{index}") if index < expected_quorum else 0),
                    "truncated": False,
                },
                **(
                    {}
                    if index < expected_quorum
                    else {"error": "failed", "error_code": "test_failure"}
                ),
            }
            for index, member in enumerate(ensemble["proposers"])
        ],
        "final_request_role": "aggregator",
        "final_request": {
            "role": "aggregator",
            "request_started": True,
            "execution": {
                "provider": ensemble["aggregator"]["provider"],
                "requested_provider": ensemble["aggregator"]["provider"],
                "requested_model": ensemble["aggregator"]["model"],
            },
            "usage": {
                "stop_reason": "stop",
                "provider": ensemble["aggregator"]["provider"],
                "model": ensemble["aggregator"]["model"],
                "requested_provider": ensemble["aggregator"]["provider"],
                "requested_model": ensemble["aggregator"]["model"],
            },
            "output": {
                "text": final_answer,
                "chars": len(final_answer),
                "truncated": False,
            },
        },
    }
    row = {
        "group": "B2",
        "provider_spec": dict(resume_runner.GROUP_SPECS["B2"]),
        "routing_trace": {"selection_plan": selection_plan},
        "final_text": final_answer,
        "ensemble_trace": {"calls": [final_trace], "agent_llm_call_count": 1},
    }
    contract = {"experiment_config": experiment}

    assert (
        resume_runner.ensemble_generation_completion_reasons(
            row,
            expected_run_compatibility_contract=contract,
        )
        == []
    )

    metadata_missing = json.loads(json.dumps(row))
    successful_candidate = metadata_missing["ensemble_trace"]["calls"][0]["candidates"][0]
    successful_candidate.pop("provider")
    successful_candidate.pop("model")
    successful_candidate["usage_reported"] = False
    successful_candidate["stop_reason"] = ""
    final_usage = metadata_missing["ensemble_trace"]["calls"][0]["final_request"]["usage"]
    final_usage.pop("provider")
    final_usage.pop("model")
    final_usage.pop("stop_reason")
    metadata_reasons = resume_runner.ensemble_generation_completion_reasons(
        metadata_missing,
        expected_run_compatibility_contract=contract,
    )
    assert "missing_actual_proposer_identity" in metadata_reasons
    assert "missing_actual_aggregator_model" in metadata_reasons
    assert "missing_actual_aggregator_provider" in metadata_reasons
    assert "missing_proposer_usage_metadata" in metadata_reasons
    assert "missing_proposer_stop_reason" in metadata_reasons
    assert "missing_aggregator_stop_reason" in metadata_reasons
    assert "invalid_successful_proposer_evidence" not in metadata_reasons
    assert "successful_proposer_count_mismatch" not in metadata_reasons
    assert "insufficient_actual_proposer_quorum" not in metadata_reasons
    assert not any(reason.startswith("wrong_actual_") for reason in metadata_reasons)
    assert not any(reason.startswith("wrong_b2_actual_") for reason in metadata_reasons)

    metadata_result = resume_runner.RunResult(
        final_text=final_answer,
        done=resume_runner.DoneEvent(
            ensemble_trace=metadata_missing["ensemble_trace"],
        ),
    )
    assert (
        resume_runner.ensemble_generation_retry_reason(
            metadata_result,
            expected_selection_mode=experiment["routing"]["selection_mode"],
            expected_selection_plan=selection_plan,
        )
        == ""
    )

    prompt_hash = resume_runner.text_sha256("same prompt")
    metadata_missing.update(
        {
            "task_id": "task-1",
            "prompt_sha256": prompt_hash,
            "task_input_sha256": "sha256:task-input",
            "run_compatibility_fingerprint": "sha256:run-contract",
            "error": None,
        }
    )
    metadata_state = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(metadata_missing),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        expected_run_compatibility_contract=contract,
        judge_required=False,
    )
    assert metadata_state["generation_valid"] is True, metadata_state
    assert metadata_state["action"] == "metadata_only"
    assert (
        "missing_proposer_usage_metadata_backfill_required"
        in metadata_state["cost_metadata_reasons"]
    )

    repairable = json.loads(json.dumps(metadata_missing))
    generation_receipts = []
    for index, member in enumerate(ensemble["proposers"][:3]):
        generation_receipts.append(
            {
                "role": "proposer",
                "provider": member["provider"],
                "model": member["model"],
                "requested_provider": member["provider"],
                "requested_model": member["model"],
                "input_tokens": 3,
                "output_tokens": 1,
                "billed_cost": 0.01,
                "cost_source": "provider_billed",
                "provider_usage": _openrouter_exact_evidence(
                    0.01,
                    f"metadata-repair-proposer-{index}",
                ),
            }
        )
    generation_receipts.append(
        {
            "role": "aggregator",
            "provider": ensemble["aggregator"]["provider"],
            "model": ensemble["aggregator"]["model"],
            "requested_provider": ensemble["aggregator"]["provider"],
            "requested_model": ensemble["aggregator"]["model"],
            "input_tokens": 5,
            "output_tokens": 2,
            "billed_cost": 0.02,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.02,
                "metadata-repair-aggregator",
            ),
        }
    )
    repairable["llm_request_count"] = len(generation_receipts)
    repairable["usage"] = {
        "input_tokens": 14,
        "output_tokens": 5,
        "billed_cost": 0.05,
        "cost_source": "provider_billed",
        "model_usage_breakdown": generation_receipts,
    }
    original_final_text = repairable["final_text"]
    original_attempts = json.loads(
        json.dumps((repairable.get("execution") or {}).get("generation_attempts"))
    )
    assert resume_runner.repair_ensemble_trace_metadata(repairable) is True
    repaired_candidate = repairable["ensemble_trace"]["calls"][0]["candidates"][0]
    repaired_final_request = repairable["ensemble_trace"]["calls"][0]["final_request"]
    assert repaired_candidate["provider"] == ensemble["proposers"][0]["provider"]
    assert repaired_candidate["model"] == ensemble["proposers"][0]["model"]
    assert repaired_candidate["stop_reason"] == ""
    assert repaired_candidate["metadata_repair"]["stop_reason"]["status"] == ("unavailable")
    assert repaired_final_request["usage"]["provider"] == (ensemble["aggregator"]["provider"])
    assert repaired_final_request["usage"]["model"] == ensemble["aggregator"]["model"]
    assert repaired_final_request["metadata_repair"]["stop_reason"]["status"] == ("unavailable")
    repaired_state = resume_runner.resume_row_completion_state(
        resume_runner.seal_result_row(repairable),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        expected_run_compatibility_contract=contract,
        judge_required=False,
    )
    assert repaired_state["action"] == "complete", repaired_state
    assert repairable["final_text"] == original_final_text
    assert (repairable.get("execution") or {}).get("generation_attempts") == (original_attempts)

    aggregator_usage_missing = json.loads(json.dumps(row))
    aggregator_usage_missing["ensemble_trace"]["calls"][0]["final_request"].pop("usage")
    aggregator_metadata_reasons = resume_runner.ensemble_generation_completion_reasons(
        aggregator_usage_missing,
        expected_run_compatibility_contract=contract,
    )
    assert "missing_aggregator_usage_metadata" in aggregator_metadata_reasons
    assert "missing_aggregator_stop_reason" in aggregator_metadata_reasons
    assert "aggregator_request_incomplete" not in aggregator_metadata_reasons

    missing_candidates = json.loads(json.dumps(row))
    missing_candidates["ensemble_trace"]["calls"][0].pop("candidates")
    missing_candidate_reasons = resume_runner.ensemble_generation_completion_reasons(
        missing_candidates,
        expected_run_compatibility_contract=contract,
    )
    assert "missing_actual_proposer_candidates" in missing_candidate_reasons
    assert (
        resume_runner.ensemble_metadata_only_reason("missing_actual_proposer_candidates") is False
    )

    wrong = json.loads(json.dumps(row))
    wrong_plan = wrong["ensemble_trace"]["calls"][0]["selection_plan"]
    wrong_plan["proposer_models"] = expected_models[:3]
    wrong["routing_trace"]["selection_plan"]["proposer_models"] = expected_models[:3]
    wrong["ensemble_trace"]["calls"][0]["total_candidates"] = 3
    wrong["ensemble_trace"]["calls"][0]["successful_proposers"] = 2
    reasons = resume_runner.ensemble_generation_completion_reasons(
        wrong,
        expected_run_compatibility_contract=contract,
    )

    assert "wrong_b2_proposer_count" in reasons
    assert "insufficient_b2_configured_quorum" in reasons
    assert "wrong_b2_proposer_lineup" in reasons

    missing_plan = json.loads(json.dumps(row))
    missing_plan["ensemble_trace"]["calls"][0].pop("selection_plan")
    missing_plan_reasons = resume_runner.ensemble_generation_completion_reasons(
        missing_plan,
        expected_run_compatibility_contract=contract,
    )
    assert "missing_executed_selection_plan" in missing_plan_reasons

    wrong_actual = json.loads(json.dumps(row))
    wrong_actual["ensemble_trace"]["calls"][0]["final_request"]["usage"]["model"] = (
        "wrong/aggregator"
    )
    wrong_actual_reasons = resume_runner.ensemble_generation_completion_reasons(
        wrong_actual,
        expected_run_compatibility_contract=contract,
    )
    assert "wrong_b2_actual_aggregator_model" in wrong_actual_reasons

    wrong_success = json.loads(json.dumps(row))
    wrong_success["ensemble_trace"]["calls"][0]["candidates"][0]["ok"] = False
    wrong_success_reasons = resume_runner.ensemble_generation_completion_reasons(
        wrong_success,
        expected_run_compatibility_contract=contract,
    )
    assert "successful_proposer_count_mismatch" in wrong_success_reasons
    assert "insufficient_actual_proposer_quorum" in wrong_success_reasons

    request_not_started = json.loads(json.dumps(row))
    request_not_started["ensemble_trace"]["calls"][0]["candidates"][0]["request_started"] = False
    request_not_started_reasons = resume_runner.ensemble_generation_completion_reasons(
        request_not_started,
        expected_run_compatibility_contract=contract,
    )
    assert "invalid_successful_proposer_evidence" in request_not_started_reasons
    assert "successful_proposer_count_mismatch" in request_not_started_reasons

    # The fourth failed proposer is legal under the 3/4 quorum and does not
    # need actual response identity; its requested slot is still audited.
    assert (
        resume_runner.ensemble_generation_completion_reasons(
            row,
            expected_run_compatibility_contract=contract,
        )
        == []
    )

    forged_success = json.loads(json.dumps(row))
    forged_candidate = forged_success["ensemble_trace"]["calls"][0]["candidates"][3]
    forged_candidate.update(
        {
            "ok": True,
            "error": "",
            "stop_reason": "stop",
            "content": {
                "text": "forged",
                "chars": len("forged"),
                "truncated": False,
            },
        }
    )
    forged_success["ensemble_trace"]["calls"][0]["successful_proposers"] = 4
    forged_reasons = resume_runner.ensemble_generation_completion_reasons(
        forged_success,
        expected_run_compatibility_contract=contract,
    )
    assert "missing_proposer_usage_metadata" in forged_reasons
    assert "invalid_successful_proposer_evidence" not in forged_reasons
    assert "successful_proposer_count_mismatch" not in forged_reasons

    failed_receipt_wrong_identity = json.loads(json.dumps(row))
    failed_with_receipt = failed_receipt_wrong_identity["ensemble_trace"]["calls"][0]["candidates"][
        3
    ]
    failed_with_receipt.update(
        {
            "provider": "wrong-provider",
            "model": failed_with_receipt["requested_model"],
            "diagnostic_model_usage_breakdown": [
                {
                    "provider": "wrong-provider",
                    "model": failed_with_receipt["requested_model"],
                }
            ],
        }
    )
    failed_receipt_reasons = resume_runner.ensemble_generation_completion_reasons(
        failed_receipt_wrong_identity,
        expected_run_compatibility_contract=contract,
    )
    assert "wrong_actual_proposer_identity" in failed_receipt_reasons

    two_calls = json.loads(json.dumps(row))
    terminal_call = json.loads(json.dumps(two_calls["ensemble_trace"]["calls"][0]))
    terminal_call["agent_call_index"] = 2
    first_call = json.loads(json.dumps(terminal_call))
    first_call["agent_call_index"] = 1
    intermediate_text = "intermediate answer segment\n"
    first_call["final_request"]["output"] = {
        "text": intermediate_text,
        "chars": len(intermediate_text),
        "truncated": False,
    }
    two_calls["final_text"] = intermediate_text + row["final_text"]
    two_calls["ensemble_trace"]["calls"] = [
        first_call,
        terminal_call,
    ]
    two_calls["ensemble_trace"]["agent_llm_call_count"] = 2
    assert (
        resume_runner.ensemble_generation_completion_reasons(
            two_calls,
            expected_run_compatibility_contract=contract,
        )
        == []
    )

    tampered_intermediate = json.loads(json.dumps(two_calls))
    tampered_intermediate["ensemble_trace"]["calls"][0]["final_request"]["output"]["text"] = (
        "x" * len(intermediate_text)
    )
    assert "wrong_agent_call_output_binding" in (
        resume_runner.ensemble_generation_completion_reasons(
            tampered_intermediate,
            expected_run_compatibility_contract=contract,
        )
    )

    dropped_early = json.loads(json.dumps(two_calls))
    dropped_early["ensemble_trace"]["calls"] = [dropped_early["ensemble_trace"]["calls"][1]]
    dropped_reasons = resume_runner.ensemble_generation_completion_reasons(
        dropped_early,
        expected_run_compatibility_contract=contract,
    )
    assert "wrong_agent_llm_call_count" in dropped_reasons
    assert "invalid_agent_call_index_sequence" in dropped_reasons

    malformed_call = json.loads(json.dumps(row))
    malformed_call["ensemble_trace"]["calls"].insert(0, "not-a-call")
    malformed_reasons = resume_runner.ensemble_generation_completion_reasons(
        malformed_call,
        expected_run_compatibility_contract=contract,
    )
    assert "invalid_ensemble_call_trace" in malformed_reasons


def _terminal_policy_call(
    *,
    index: int,
    plan: dict[str, object],
    successful: int,
    fallback: bool,
    output: str,
) -> dict[str, object]:
    proposer_models = list(plan["proposer_models"])
    candidates = []
    for candidate_index, model in enumerate(proposer_models):
        ok = candidate_index < successful
        text = f"candidate-{candidate_index}" if ok else ""
        candidates.append(
            {
                "provider": "openrouter",
                "requested_provider": "openrouter",
                "model": model,
                "requested_model": model,
                "ok": ok,
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": ok,
                "stop_reason": "stop" if ok else "",
                "error": "" if ok else "test failure",
                "content": {
                    "text": text,
                    "chars": len(text),
                    "truncated": False,
                },
            }
        )
    role = "fallback_single" if fallback else "aggregator"
    model = proposer_models[0] if fallback else str(plan["aggregator_model"])
    return {
        "agent_call_index": index,
        "request_outcome": "llm_response",
        "selection_strategy": plan["selection_mode"],
        "selection_plan": deepcopy(plan),
        "successful_proposers": successful,
        "total_candidates": len(proposer_models),
        "fallback_used": fallback,
        "fallback_reason": "sub-quorum" if fallback else "",
        "candidates": candidates,
        "final_request_role": role,
        "final_request": {
            "role": role,
            "request_started": True,
            "error": "",
            "execution": {
                "role": role,
                "requested_provider": "openrouter",
                "provider": "openrouter",
                "actual_provider": "openrouter",
                "requested_model": model,
                "model": model,
                "actual_model": model,
            },
            "usage": {
                "stop_reason": "stop",
                "provider": "openrouter",
                "model": model,
                "requested_provider": "openrouter",
                "requested_model": model,
            },
            "output": {
                "text": output,
                "chars": len(output),
                "truncated": False,
            },
        },
    }


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_terminal_policy_allows_only_empty_nonterminal_fallback(module) -> None:
    plan = {
        "strategy": "static_openrouter_b5",
        "selection_mode": "static_openrouter_b5",
        "profile": "test",
        "proposer_models": ["p1", "p2", "p3", "p4"],
        "proposer_sample_count": 4,
        "selected_P": [
            "openrouter:p1",
            "openrouter:p2",
            "openrouter:p3",
            "openrouter:p4",
        ],
        "aggregator_model": "agg",
        "selected_A": "openrouter:agg",
    }
    fallback = _terminal_policy_call(
        index=1,
        plan=plan,
        successful=2,
        fallback=True,
        output="",
    )
    terminal = _terminal_policy_call(
        index=2,
        plan=plan,
        successful=4,
        fallback=False,
        output="final answer",
    )

    def retry_reason(calls: list[dict[str, object]], final_text: str) -> str:
        return module.ensemble_generation_retry_reason(
            module.RunResult(
                final_text=final_text,
                done=module.DoneEvent(
                    ensemble_trace={
                        "mode": "agent_loop",
                        "agent_llm_call_count": len(calls),
                        "untraced_agent_llm_call_count": 0,
                        "calls": calls,
                    }
                ),
            ),
            expected_selection_mode="static_openrouter_b5",
            expected_selection_plan=plan,
        )

    assert retry_reason([deepcopy(fallback), deepcopy(terminal)], "final answer") == ""

    visible = deepcopy(fallback)
    visible["final_request"]["output"] = {
        "text": "unsafe text",
        "chars": len("unsafe text"),
        "truncated": False,
    }
    assert (
        retry_reason([visible, deepcopy(terminal)], "unsafe textfinal answer")
        == "intermediate_fallback_visible_output"
    )

    wrong_model = deepcopy(fallback)
    wrong_model["final_request"]["usage"]["model"] = "outside/model"
    wrong_model["final_request"]["usage"]["requested_model"] = "outside/model"
    assert (
        retry_reason([wrong_model, deepcopy(terminal)], "final answer")
        == "wrong_intermediate_fallback_model"
    )

    conflicting_execution = deepcopy(fallback)
    conflicting_execution["final_request"]["execution"]["actual_model"] = "outside/model"
    assert (
        retry_reason([conflicting_execution, deepcopy(terminal)], "final answer")
        == "wrong_intermediate_fallback_model"
    )

    boolean_chars = deepcopy(fallback)
    boolean_chars["final_request"]["output"]["chars"] = False
    assert module.admissible_empty_nonterminal_fallback_reasons(
        boolean_chars,
        expected_selection_plan=plan,
    ) == ["intermediate_fallback_visible_output"]

    formal_plan = deepcopy(plan)
    formal_plan["proposer_models"].append("p5")
    formal_plan["selected_P"].append("openrouter:p5")
    formal_plan["proposer_sample_count"] = 5
    formal_plan["proposer_recovery_policy"] = deepcopy(module.FORMAL_PROPOSER_RECOVERY_POLICY)
    formal_fallback = _terminal_policy_call(
        index=1,
        plan=formal_plan,
        successful=2,
        fallback=True,
        output="",
    )
    assert module.admissible_empty_nonterminal_fallback_reasons(
        formal_fallback,
        expected_selection_plan=formal_plan,
    ) == ["invalid_intermediate_fallback_quorum"]

    assert retry_reason([deepcopy(fallback)], "") == ("aggregator_fallback_used_or_unknown")


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_agent_ensemble_root_summary_uses_terminal_call(module) -> None:
    plan = {
        "selection_mode": "static_openrouter_b5",
        "proposer_models": ["p1", "p2", "p3", "p4"],
        "selected_P": [
            "openrouter:p1",
            "openrouter:p2",
            "openrouter:p3",
            "openrouter:p4",
        ],
        "aggregator_model": "agg",
        "selected_A": "openrouter:agg",
    }
    recorder = module.BenchmarkTurnCallRecorder()
    calls = (
        _terminal_policy_call(
            index=1,
            plan=plan,
            successful=2,
            fallback=True,
            output="",
        ),
        _terminal_policy_call(
            index=2,
            plan=plan,
            successful=4,
            fallback=False,
            output="final answer",
        ),
    )
    for call, seed in zip(calls, (11, 22), strict=True):
        call.update(
            {
                "shuffle_candidates": True,
                "configured_candidate_order_seed": None,
                "candidate_order_seed": seed,
                "candidate_order_seed_source": "system_random",
            }
        )
        recorder.write(
            "llm_response",
            {
                "iteration": call["agent_call_index"],
                "attempt": 1,
                "ensemble_trace": call,
            },
        )

    trace = module.aggregate_agent_ensemble_trace(recorder.records)

    assert trace["fallback_used"] is False
    assert trace["final_request_role"] == "aggregator"
    assert trace["successful_proposers"] == 4
    assert trace["total_candidates"] == 4
    assert trace["shuffle_candidates"] is True
    assert trace["configured_candidate_order_seed"] is None
    assert trace["candidate_order_seed"] == 22
    assert trace["candidate_order_seed_source"] == "system_random"
    assert trace["terminal_call_index"] == 2
    assert trace["any_intermediate_fallback"] is True
    assert trace["intermediate_fallback_call_indexes"] == [1]


def test_resume_reclassifies_one_legacy_terminal_attempt_without_hiding_spend() -> None:
    module = _load_resume_runner()
    plan = {
        "strategy": "static_openrouter_b5",
        "selection_mode": "static_openrouter_b5",
        "profile": "test",
        "proposer_models": ["p1", "p2", "p3", "p4"],
        "proposer_sample_count": 4,
        "selected_P": [
            "openrouter:p1",
            "openrouter:p2",
            "openrouter:p3",
            "openrouter:p4",
        ],
        "aggregator_model": "agg",
        "selected_A": "openrouter:agg",
    }
    calls = [
        _terminal_policy_call(
            index=1,
            plan=plan,
            successful=2,
            fallback=True,
            output="",
        ),
        _terminal_policy_call(
            index=2,
            plan=plan,
            successful=4,
            fallback=False,
            output="final answer",
        ),
    ]

    def usage(response_id: str, cost: float) -> dict[str, object]:
        return {
            "provider": "openrouter",
            "model": "agg",
            "requested_provider": "openrouter",
            "requested_model": "agg",
            "input_tokens": 10,
            "output_tokens": 5,
            "billed_cost": cost,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(cost, response_id),
        }

    selected_usage = usage("selected-generation", 0.1)
    attempts = []
    for index, (answer, attempt_usage, error) in enumerate(
        (
            (
                "final answer",
                selected_usage,
                module.LEGACY_TERMINAL_POLICY_ERROR,
            ),
            ("later answer 2", usage("extra-generation-2", 0.2), "other failure"),
            ("later answer 3", usage("extra-generation-3", 0.3), "other failure"),
        ),
        start=1,
    ):
        attempts.append(
            {
                "attempt_id": f"{index:032x}",
                "attempt_kind": "generation",
                "attempt": index,
                "started_at": float(index),
                "completed_at": float(index + 1),
                "retry_reason": error,
                "run": {
                    "error": error,
                    "final_text_sha256": module.text_sha256(answer),
                    "latency_ms": 100 * index,
                    "stream_tool_call_count": index,
                    "server_tool_call_count": 0,
                    "server_tool_use": {},
                    "total_tool_call_count": index,
                    "trajectory_steps": index + 1,
                    "llm_request_count": 1,
                    "usage_unknown_count": 0,
                    "usage": attempt_usage,
                },
            }
        )
    row = {
        "group": "B2",
        "provider_spec": dict(module.GROUP_SPECS["B2"]),
        "routing_trace": {"selection_plan": plan},
        "final_text": "final answer",
        "final_text_sha256": module.text_sha256("final answer"),
        "final_text_chars": len("final answer"),
        "error": module.LEGACY_TERMINAL_POLICY_ERROR,
        "selected_generation_succeeded": False,
        "usage": selected_usage,
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 2,
            "untraced_agent_llm_call_count": 0,
            "calls": calls,
        },
        "execution": {
            "run_error": module.LEGACY_TERMINAL_POLICY_ERROR,
            "selected_generation_attempt": 0,
            "generation_attempts": attempts,
        },
        "completion_status": {
            "generation_accepted": False,
            "incomplete_reasons": [module.LEGACY_TERMINAL_POLICY_ERROR],
        },
        "generation_attempt_count": 3,
        "actual_spend_metrics": {"generation_attempt_count": 3},
    }

    assert module.apply_terminal_generation_reclassification(
        row,
        expected_run_compatibility_contract=None,
    )
    assert row["selected_generation_succeeded"] is True
    assert row["execution"]["selected_generation_attempt"] == 1
    assert row["execution"]["generation_attempts"][0]["run"]["error"] == (
        module.LEGACY_TERMINAL_POLICY_ERROR
    )
    assert row["selected_attempt_billed_cost_usd"] == pytest.approx(0.1)
    accounting = module.row_cost_accounting(row)
    assert accounting["selected_generation_attempt"]["recorded_cost_usd"] == (pytest.approx(0.1))
    assert accounting["actual_generation_spend"]["recorded_cost_usd"] == (pytest.approx(0.6))
    prompt_hash = module.text_sha256("same prompt")
    row.update(
        {
            "task_id": "task-1",
            "prompt_sha256": prompt_hash,
            "task_input_sha256": "sha256:task-input",
            "run_compatibility_fingerprint": "sha256:run-contract",
        }
    )
    state = module.resume_row_completion_state(
        module.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
    )
    assert state["generation_valid"] is True, state
    assert state["action"] == "judge_only"


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_g1_only_uses_the_same_shared_experiment_profile_as_b2_g1(module) -> None:
    def aligned(groups: str):
        args = module.build_parser().parse_args(
            [
                "--input",
                "tasks.jsonl",
                "--groups",
                groups,
                "--concurrency",
                "9",
                "--agent-max-iterations",
                "1",
            ]
        )
        record = module.apply_b2_g12_argument_alignment(
            args,
            module.parse_groups(groups),
        )
        return args, record

    g1_args, g1_record = aligned("G1")
    joint_args, joint_record = aligned("B2,G1")

    assert g1_record is not None
    assert joint_record is not None
    assert g1_record["effective_args"] == joint_record["effective_args"]
    assert g1_args.agent_max_iterations == joint_args.agent_max_iterations == 20
    assert g1_args.concurrency == joint_args.concurrency == 2
    assert "global_experiment_profile" in g1_args._benchmark_alignments
    assert "B2" not in g1_args._benchmark_alignments
    assert g1_args._effective_argument_sources["agent_max_iterations"] == ("experiment_config")


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_g1_registry_contract_is_manifested_and_fingerprinted(
    module,
    tmp_path: Path,
) -> None:
    args = module.build_parser().parse_args(["--input", "tasks.jsonl", "--groups", "G1"])
    module.apply_b2_g12_argument_alignment(args, ["G1"])
    experiment = args._draco_experiment_config_bundle.config
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={
            "ranking_user_profile_generation_enabled": True,
            "ranking_user_profile_enabled": True,
        },
        sandbox={"sandbox": True, "security_grading": True},
    )
    args._formal_runtime_freeze = module.enforce_formal_draco_runtime_config(
        config,
        experiment,
        ["G1"],
    )
    assert config.llm_ensemble.ranking_user_profile_generation_enabled is False
    assert config.llm_ensemble.ranking_user_profile_enabled is False
    assert config.llm_ensemble.aggregator_recovery_mode == "experiment"
    assert config.llm_ensemble.aggregator_recovery_top_k == 3
    assert config.llm_ensemble.aggregator_max_tokens_cap == 65_536
    assert config.llm_ensemble.aggregator_visible_answer_reserve_tokens == 8_192
    assert config.llm_ensemble.proposer_backup_count == 2
    assert config.llm_ensemble.proposer_recovery_max_additional_calls == 3
    assert config.llm_ensemble.proposer_max_tokens_cap == 65_536
    assert config.llm_ensemble.proposer_visible_answer_reserve_tokens == 4_096
    assert config.sandbox.sandbox is False
    assert config.sandbox.security_grading is False
    args._g1_registry_contract = module.validate_g1_registry_contract(
        experiment,
        config,
    )
    args._source_provenance = {
        "git_head": "a" * 40,
        "source_tree_sha256": "b" * 64,
    }
    policy = module.benchmark_tool_policy(args)
    compatibility = module.build_run_compatibility(
        args=args,
        config=config,
        groups=["G1"],
        group_tool_policies=module.benchmark_tool_policies_for_groups(
            policy,
            ["G1"],
            args=args,
        ),
        generation_policy=module.generation_thinking_policy(args),
    )
    args._run_compatibility = compatibility
    contract = compatibility["contracts"]["G1"]
    assert contract["global_experiment_profile"]["g1_routing"] == (
        experiment.g1_routing.model_dump(mode="json")
    )
    resolved_contract = contract["g1_registry_contract"]
    expected_count = resolved_contract["expected_candidate_count"]
    assert expected_count == resolved_contract["available_registry_candidate_count"]
    assert expected_count == len(resolved_contract["expected_routes"])
    assert expected_count == len(resolved_contract["expected_identities"])
    assert resolved_contract["candidate_scope"] == "registry_all"
    assert resolved_contract["policy"] == "all_registry_models"
    assert resolved_contract["runtime_pin_policy"] == "optional_auto"
    assert set(resolved_contract["expected_routes"].values()) == {"auto"}
    assert contract["formal_runtime_freeze"] == {
        "source": "experiment_config",
        "sandbox_enabled": False,
        "sandbox_security_grading_enabled": False,
        "aggregator_recovery_mode": "experiment",
        "aggregator_recovery_top_k": 3,
        "aggregator_max_tokens_cap": 65_536,
        "aggregator_visible_answer_reserve_tokens": 8_192,
        "proposer_backup_count": 2,
        "proposer_recovery_max_additional_calls": 3,
        "proposer_max_tokens_cap": 65_536,
        "proposer_visible_answer_reserve_tokens": 4_096,
        "g1_user_profile_generation_enabled": False,
        "g1_user_profile_enabled": False,
        "task_analyzer": {
            "protocol_version": "opus-4.8-json-v3",
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "upstream_provider": "anthropic",
            "stream_close_timeout_seconds": 1.0,
            "timeout_seconds": 20.0,
            "max_retries": 3,
        },
    }
    assert (
        contract["gateway_execution"]["llm_ensemble"]["ranking_user_profile_generation_enabled"]
        is False
    )
    assert contract["gateway_execution"]["llm_ensemble"]["ranking_user_profile_enabled"] is False
    assert contract["gateway_execution"]["sandbox"]["sandbox"] is False
    assert contract["gateway_execution"]["sandbox"]["security_grading"] is False

    manifest_path = tmp_path / f"{module.__name__}.manifest.json"
    module.write_manifest(
        manifest_path,
        args=args,
        stamp="test",
        status="running",
        started_at=1.0,
        tasks=[{"id": "task-1"}],
        groups=["G1"],
        artifacts={},
        tool_policy=policy,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["g1_registry_contract"]["profile_id"] == experiment.g1_routing.profile_id
    assert manifest["g1_registry_contract"]["expected_candidate_count"] == expected_count
    assert (
        manifest["g1_registry_contract"]["expected_identities"]
        == (resolved_contract["expected_identities"])
    )
    assert manifest["formal_runtime_freeze"] == contract["formal_runtime_freeze"]


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
async def test_g1_dry_build_records_registry_all_candidate_allowlist(module) -> None:
    experiment = module.load_draco_experiment_config(
        module.DEFAULT_B2_EXPERIMENT_CONFIG_PATH
    ).config
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        }
    )
    contract = _resolved_g1_registry_contract(module, experiment, config)
    result = await module.build_experiment_provider(
        config=config,
        inherited=ProviderConfig(
            provider="openrouter",
            model="deepseek/deepseek-v4-pro",
            api_key="fake",
        ),
        group="G1",
        prompt="dry prompt",
        dry_run=True,
        enable_proposer_tools=False,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        experiment_config=experiment,
        g1_registry_contract=contract,
        generation_policy={},
    )

    plan = result.routing_trace["selection_plan"]
    expected_count = contract["expected_candidate_count"]
    expected_identities = set(contract["expected_identities"])
    assert plan["candidate_pool_size"] == expected_count
    assert len(plan["candidate_pool"]) == expected_count
    assert {row["identity"] for row in plan["candidate_pool"]} == expected_identities
    assert plan["candidate_allowlist"]["candidate_count"] == expected_count
    assert plan["candidate_allowlist"]["candidate_scope"] == "registry_all"
    assert plan["candidate_allowlist"]["policy"] == "all_registry_models"
    assert plan["candidate_allowlist"]["input_candidate_count"] == expected_count
    assert plan["candidate_allowlist"]["excluded_candidate_count"] == 0
    assert plan["user_profile_enabled"] is False
    assert set(plan["candidate_allowlist"]["expected_identities"]) == expected_identities
    assert plan["candidate_allowlist"]["filtered_registry_snapshot_version"].startswith(
        f"{experiment.g1_routing.source_registry_snapshot_version}+"
    )
    assert len(plan["selected_P"]) == 2
    assert plan["N_min"] == 2
    assert plan["N_max"] == 2
    assert plan["proposer_count"] == 2
    assert plan["ranking_config_hash"] == (experiment.g1_routing.expected_ranking_config_sha256)
    assert plan["selected_A"] in plan["candidate_allowlist"]["expected_identities"]
    assert set(plan["selected_P"]) <= set(plan["candidate_allowlist"]["expected_identities"])
    assert plan["aggregator_recovery_mode"] == "experiment"
    assert plan["aggregator_recovery_top_k"] == 3
    assert plan["aggregator_max_tokens_cap"] == 65_536
    assert plan["aggregator_visible_answer_reserve_tokens"] == 8_192


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
async def test_g1_dry_build_applies_cli_timeout_overrides(module) -> None:
    experiment = module.load_draco_experiment_config(
        module.DEFAULT_B2_EXPERIMENT_CONFIG_PATH
    ).config
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        }
    )
    contract = _resolved_g1_registry_contract(module, experiment, config)

    result = await module.build_experiment_provider(
        config=config,
        inherited=ProviderConfig(
            provider="openrouter",
            model="deepseek/deepseek-v4-pro",
            api_key="fake",
        ),
        group="G1",
        prompt="dry timeout projection prompt",
        dry_run=True,
        enable_proposer_tools=False,
        ensemble_proposer_timeout=907.5,
        ensemble_aggregator_timeout=2_662.5,
        experiment_config=experiment,
        g1_registry_contract=contract,
        generation_policy={},
    )

    plan = result.routing_trace["selection_plan"]
    assert plan["effective_proposer_timeout_seconds"] == pytest.approx(907.5)
    assert plan["effective_aggregator_timeout_seconds"] == pytest.approx(2_662.5)
    assert result.provider.selection_plan == plan


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
@pytest.mark.parametrize(
    ("wait_for_all_proposers", "quorum_grace_seconds"),
    [(True, 0.0), (False, 7.5)],
    ids=["wait-all-current", "bounded-grace"],
)
async def test_g1_dry_build_applies_experiment_quorum_wait_policy(
    module,
    wait_for_all_proposers: bool,
    quorum_grace_seconds: float,
) -> None:
    experiment = module.load_draco_experiment_config(
        module.DEFAULT_B2_EXPERIMENT_CONFIG_PATH
    ).config
    payload = experiment.model_dump(mode="json")
    payload["ensemble"]["wait_for_all_proposers"] = wait_for_all_proposers
    payload["ensemble"]["quorum_grace_seconds"] = quorum_grace_seconds
    payload["ensemble"]["shuffle_candidates"] = True
    payload["ensemble"]["candidate_order_seed"] = 0
    experiment = type(experiment).model_validate(payload)
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        }
    )
    contract = _resolved_g1_registry_contract(module, experiment, config)

    result = await module.build_experiment_provider(
        config=config,
        inherited=ProviderConfig(
            provider="openrouter",
            model="deepseek/deepseek-v4-pro",
            api_key="fake",
        ),
        group="G1",
        prompt="dry quorum policy prompt",
        dry_run=True,
        enable_proposer_tools=False,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        experiment_config=experiment,
        g1_registry_contract=contract,
        generation_policy={},
    )

    plan = result.routing_trace["selection_plan"]
    assert result.provider.quorum_grace_seconds == pytest.approx(quorum_grace_seconds)
    assert plan["quorum_grace_seconds"] == pytest.approx(quorum_grace_seconds)
    assert plan["wait_for_all_proposers"] is wait_for_all_proposers
    assert plan["effective_shuffle_candidates"] is True
    assert plan["configured_candidate_order_seed"] == 0
    assert plan["effective_candidate_order_seed"] == 0
    assert result.provider.selection_plan == plan

    events = [
        event
        async for event in result.provider.chat(
            [module.Message(role="user", content="dry candidate order prompt")]
        )
    ]
    done = next(event for event in events if isinstance(event, DoneEvent))
    trace = done.ensemble_trace
    assert trace is not None
    assert trace["selection_plan"] == plan
    assert trace["shuffle_candidates"] is True
    assert trace["configured_candidate_order_seed"] == 0
    assert trace["candidate_order_seed"] == 0
    assert trace["candidate_order_seed_source"] == "configured"
    expected_order = list(range(len(result.provider.proposer_models)))
    random.Random(0).shuffle(expected_order)
    assert trace["candidate_display_order"] == expected_order

    system_random_provider = module.DryEnsembleProvider(
        group="G1",
        profile="router_dynamic",
        proposer_models=["p1", "p2", "p3"],
    )
    system_random_provider.selection_plan = {
        "effective_shuffle_candidates": True,
    }
    system_random_events = [
        event
        async for event in system_random_provider.chat(
            [module.Message(role="user", content="dry random order prompt")]
        )
    ]
    system_random_done = next(
        event for event in system_random_events if isinstance(event, DoneEvent)
    )
    system_random_trace = system_random_done.ensemble_trace
    assert system_random_trace is not None
    assert system_random_trace["configured_candidate_order_seed"] is None
    assert system_random_trace["candidate_order_seed_source"] == "system_random"
    system_random_seed = system_random_trace["candidate_order_seed"]
    assert type(system_random_seed) is int
    expected_random_order = [0, 1, 2]
    random.Random(system_random_seed).shuffle(expected_random_order)
    assert system_random_trace["candidate_display_order"] == expected_random_order


def test_g1_dry_cli_main_exits_zero_with_frozen_registry_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(
        json.dumps({"id": "task-1", "prompt": "dry G1 prompt"}) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    experiment_path = ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"
    experiment = runner.load_draco_experiment_config(experiment_path).config
    assert experiment.g1_routing is not None
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        }
    )
    monkeypatch.setattr(runner.GatewayConfig, "load", lambda _path: config)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--experiment-config",
            str(experiment_path),
            "--groups",
            "G1",
            "--max-tasks",
            "1",
            "--dry-run",
            "--require-openrouter-non-byok",
            "--experiment-config-set",
            "benchmark_input.enforce_reference_input=false",
            "--experiment-config-set",
            "runner.concurrency=1",
            "--experiment-config-set",
            "judge.concurrency=1",
            "--experiment-config-set",
            "generation.max_attempts=1",
        ],
    )

    assert runner.main() == 0

    manifests = list(output_dir.glob("draco_run_*.manifest.json"))
    result_paths = list(output_dir.glob("draco_ensemble_*.jsonl"))
    assert len(manifests) == 1
    assert len(result_paths) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    row = json.loads(result_paths[0].read_text(encoding="utf-8").splitlines()[0])
    plan = row["ensemble_trace"]["calls"][0]["selection_plan"]
    assert manifest["status"] == "complete"
    assert (
        manifest["run_compatibility"]["contracts"]["G1"]["cost_policy"][
            "require_openrouter_non_byok"
        ]
        is True
    )
    assert "openrouter_non_byok_audit" not in row
    contract = manifest["g1_registry_contract"]
    assert contract["candidate_scope"] == "registry_all"
    assert contract["policy"] == "all_registry_models"
    assert len(plan["candidate_pool"]) == contract["expected_candidate_count"]
    assert {item["identity"] for item in plan["candidate_pool"]} == set(
        contract["expected_identities"]
    )
    assert (
        plan["registry_snapshot_version"]
        == (plan["candidate_allowlist"]["filtered_registry_snapshot_version"])
    )
    registry_hash = plan["registry_snapshot_hash"]
    assert len(registry_hash) == 64
    assert all(char in "0123456789abcdef" for char in registry_hash)


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_g1_registry_all_contract_does_not_require_runtime_pins(module) -> None:
    experiment = module.load_draco_experiment_config(
        module.DEFAULT_B2_EXPERIMENT_CONFIG_PATH
    ).config
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "provider_routing": {"x-ai/grok-4.5": "wrong-provider"},
        }
    )

    contract = _resolved_g1_registry_contract(module, experiment, config)

    assert contract["runtime_pin_policy"] == "optional_auto"
    assert contract["expected_routes"]["x-ai/grok-4.5"] == "auto"
    audited = module._openrouter_audit_provider_routing(
        {
            "x-ai/grok-4.5": "wrong-provider",
            "anthropic/claude-opus-4.8": "anthropic",
        },
        contract,
    )
    assert audited["x-ai/grok-4.5"] == "wrong-provider"
    missing_model = next(
        model
        for model in contract["expected_routes"]
        if model not in {"x-ai/grok-4.5", "anthropic/claude-opus-4.8"}
    )
    assert audited[missing_model] == "auto"


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_formal_g1_refreezes_allowlisted_historical_assets(module) -> None:
    experiment = _experiment_config()
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )
    assert "role_reliability" in (
        config.llm_ensemble.ranking_config_effective_snapshot()
    )

    module.enforce_formal_draco_runtime_config(config, experiment, ["G1"])
    resolution = config.llm_ensemble.ranking_config_resolution_snapshot()
    contract = module.validate_g1_registry_contract(experiment, config)

    assert resolution["base_config"]["config_version"] == (
        "step2-ranking-2026-08-02.2"
    )
    assert resolution["base_sha256"] == (
        "71be283f94095bc3ced34d39ae9ed58abbaa7e4d273b0a074e7e8a4a6e4b5fc6"
    )
    assert "role_reliability" not in resolution["base_config"]
    assert contract["source_registry_snapshot_version"] == (
        "curated-openrouter-step2-2026-07-31.1"
    )
    assert contract["expected_source_registry_snapshot_sha256"] == (
        "9f76c7f96e5cb22c05b615f69b71ca633965e5039fbec9673f0a5edf9b45078a"
    )


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_g1_runtime_ranking_override_rebinds_effective_contract(module) -> None:
    experiment = _experiment_with_current_g1_contract(
        module,
        thinking_assignment_enabled=False,
    )
    payload = experiment.model_dump(mode="json")
    payload["g1_routing"]["live_task_analyzer_chain"][0].update(
        {
            "model": "openai/gpt-5.5",
            "upstream_provider": "openai",
        }
    )
    payload["router_dynamic_ranking_override"] = {
        "penalties": {"task_cost_weights": {"medium": 0.17}},
        "proposer_count": {"backup_count": 1},
        "task_analyzer": {
            "model": "openai/gpt-5.5",
            "upstream_provider": "openai",
            "stream_close_timeout_seconds": 2.0,
        },
    }
    experiment = type(experiment).model_validate(payload)
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "provider_routing": {"openai/gpt-5.5": "openai"},
        },
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )

    freeze = module.enforce_formal_draco_runtime_config(config, experiment, ["G1"])
    assert config.llm_ensemble.ranking_config_override == (
        experiment.router_dynamic_ranking_override
    )
    # The public experiment/runtime dictionaries may still be inspected or
    # changed by callers, but execution and audit remain bound to the detached
    # startup resolution.
    experiment.router_dynamic_ranking_override["penalties"]["task_cost_weights"]["medium"] = 0.91
    config.llm_ensemble.ranking_config_override["penalties"]["task_cost_weights"]["medium"] = 0.92
    config.llm_ensemble.ranking_thinking_assignment_enabled = True
    contract = module.validate_g1_registry_contract(experiment, config)
    resolution = contract["ranking_config_resolution"]

    assert freeze["ranking_config_override_present"] is True
    assert freeze["ranking_config_override_sha256"] == resolution["override_sha256"]
    assert freeze["ranking_config_effective_sha256"] == resolution["effective_sha256"]
    assert contract["baseline_expected_ranking_config_sha256"] == (
        experiment.g1_routing.expected_ranking_config_sha256
    )
    assert contract["expected_ranking_config_sha256"] == resolution["effective_sha256"]
    assert contract["expected_ranking_config_version"].endswith(
        f"+override.{resolution['override_sha256'][:12]}"
    )
    assert resolution["effective_config"]["penalties"]["task_cost_weights"][
        "medium"
    ] == pytest.approx(0.17)
    assert contract["task_analyzer"]["model"] == "openai/gpt-5.5"
    assert contract["task_analyzer"]["upstream_provider"] == "openai"
    assert contract["task_analyzer"]["stream_close_timeout_seconds"] == 2.0
    assert resolution["thinking_assignment_enabled"] is False
    provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=ProviderConfig(
            provider="openrouter",
            model="deepseek/deepseek-v4-pro",
            api_key="fake",
        ),
        fallback_provider=None,
        turn_metadata={"routed_tier": "c1", "routing_confidence": 0.9},
        ranking_inputs={"registry_allowlist": contract},
    )
    assert (
        provider._router_dynamic_retry_context.frozen_ranking_inputs["ranking_config"]
        == resolution["effective_config"]
    )
    assert provider.selection_plan.get("ranking_thinking_assignment_enabled") is not True
    assert provider.selection_plan["configured_proposer_backup_count"] == 1
    assert provider.selection_plan["effective_proposer_backup_count"] == 1
    assert len(provider.selection_plan["backup_P"]) == 1
    assert provider.selection_plan["proposer_recovery_policy"]["configured_backup_count"] == 1
    assert provider.selection_plan["proposer_recovery_policy"]["effective_backup_count"] == 1


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_formal_g1_rejects_auto_task_analyzer_upstream_before_model_calls(module) -> None:
    experiment = _experiment_with_current_g1_contract(
        module,
        thinking_assignment_enabled=False,
    )
    payload = experiment.model_dump(mode="json")
    payload["router_dynamic_ranking_override"] = {
        "task_analyzer": {"upstream_provider": "auto"}
    }
    experiment = type(experiment).model_validate(payload)
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "provider_routing": {"anthropic/claude-opus-4.8": "auto"},
        },
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )

    module.enforce_formal_draco_runtime_config(config, experiment, ["G1"])
    with pytest.raises(ValueError, match="must be explicitly pinned"):
        module.validate_g1_registry_contract(experiment, config)


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_formal_runtime_rejects_unpinned_custom_judge_before_model_calls(module) -> None:
    experiment = _experiment_with_current_g1_contract(
        module,
        thinking_assignment_enabled=False,
    )
    payload = experiment.model_dump(mode="json")
    payload["judge"]["model"] = "openai/gpt-5.5"
    experiment = type(experiment).model_validate(payload)
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )

    with pytest.raises(ValueError, match="frozen Gemini Judge model"):
        module.enforce_formal_draco_runtime_config(config, experiment, ["G1"])


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
@pytest.mark.parametrize(
    ("tools_patch", "message"),
    [
        ({"mode": "provider_only"}, "tools.mode=local_web_tools"),
        (
            {"contamination_blocked_domains": ["example.com"]},
            "frozen contamination-blocked domain set",
        ),
    ],
    ids=["tool-mode", "blocked-domains"],
)
def test_formal_runtime_rejects_unsafe_tool_policy_before_model_calls(
    module,
    tools_patch: dict[str, object],
    message: str,
) -> None:
    experiment = _experiment_with_current_g1_contract(
        module,
        thinking_assignment_enabled=False,
    )
    payload = experiment.model_dump(mode="json")
    payload["tools"].update(tools_patch)
    experiment = type(experiment).model_validate(payload)
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )

    with pytest.raises(ValueError, match=message):
        module.enforce_formal_draco_runtime_config(config, experiment, ["G1"])


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
@pytest.mark.parametrize(
    ("overlay", "gateway_patch", "message"),
    [
        (
            {"ensemble": {"aggregator": {"provider": "anthropic"}}},
            {},
            r"ensemble\.aggregator\.provider=openrouter",
        ),
        (
            {
                "ensemble": {
                    "aggregator": {"base_url": "https://attacker.invalid/v1"}
                }
            },
            {},
            r"ensemble\.aggregator\.base_url",
        ),
        (
            {
                "ensemble": {
                    "aggregator": {"api_key_env": "UNRELATED_AMBIENT_SECRET"}
                }
            },
            {},
            r"ensemble\.aggregator\.api_key_env",
        ),
        (
            {"tools": {"web_search": {"api_key_env": "UNRELATED_AMBIENT_SECRET"}}},
            {},
            r"tools\.web_search\.api_key_env=BRAVE_SEARCH_API_KEY",
        ),
        (
            {},
            {"provider": "anthropic"},
            r"config\.llm\.provider=openrouter",
        ),
        (
            {},
            {"base_url": "https://attacker.invalid/v1"},
            r"config\.llm\.base_url",
        ),
        (
            {},
            {"api_key_env": "UNRELATED_AMBIENT_SECRET"},
            r"config\.llm\.api_key_env",
        ),
    ],
    ids=[
        "member-provider",
        "member-base-url",
        "member-api-key-env",
        "search-api-key-env",
        "root-provider",
        "root-base-url",
        "root-api-key-env",
    ],
)
def test_formal_amain_rejects_credential_redirects_before_any_env_or_provider_read(
    module,
    overlay: dict[str, object],
    gateway_patch: dict[str, str],
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(
        json.dumps({"id": "task-1", "prompt": "must fail before providers"}) + "\n",
        encoding="utf-8",
    )
    overlay = deepcopy(overlay)
    overlay["benchmark_input"] = {"enforce_reference_input": False}
    args = module.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(tmp_path / "output"),
            "--groups",
            "B2",
            "--dry-run",
            "--experiment-config",
            str(module.DEFAULT_B2_EXPERIMENT_CONFIG_PATH),
            "--experiment-config-override-json",
            json.dumps(overlay),
        ]
    )
    ambient_secret_env = "UNRELATED_AMBIENT_SECRET"
    gateway_payload = {
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-pro",
        "api_key_env": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        **gateway_patch,
    }
    gateway = GatewayConfig(llm=gateway_payload)

    class GuardedEnvironment(dict[str, str]):
        def get(self, key: str, default=None):
            if key in {ambient_secret_env, "OPENROUTER_API_KEY", "BRAVE_SEARCH_API_KEY"}:
                raise AssertionError("formal validation did not precede credential env access")
            return super().get(key, default)

    monkeypatch.setattr(module, "source_provenance", lambda: {"git_dirty": False})
    monkeypatch.setattr(module.GatewayConfig, "load", lambda _path: gateway)
    monkeypatch.setattr(module.os, "environ", GuardedEnvironment(module.os.environ))

    def unexpected_provider_read(*_args, **_kwargs):
        raise AssertionError("provider/search setup ran before formal credential validation")

    monkeypatch.setattr(module, "configure_local_web_search_runtime", unexpected_provider_read)
    monkeypatch.setattr(module, "_experiment_member_provider_config", unexpected_provider_read)

    with pytest.raises(ValueError, match=message):
        asyncio.run(module.amain(args))


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    ("gateway_patch", "runtime_patch", "environment_patch", "message"),
    [
        (
            {},
            {},
            {"OPENROUTER_BASE_URL": "https://attacker.invalid/v1"},
            "OPENROUTER_BASE_URL",
        ),
        (
            {},
            {},
            {"OPENSQUILLA_LLM_API_KEY_ENV": "UNRELATED_AMBIENT_SECRET"},
            "OPENSQUILLA_LLM_API_KEY_ENV",
        ),
        (
            {},
            {},
            {"OPENSQUILLA_LLM_API_KEY": "generic-secret"},
            "OPENSQUILLA_LLM_API_KEY",
        ),
        (
            {"llm_proxy": "http://proxy.invalid:8080"},
            {},
            {},
            r"config\.llm\.proxy",
        ),
        (
            {},
            {"provider": "anthropic"},
            {},
            "resolved provider must be openrouter",
        ),
        (
            {},
            {"base_url": "https://attacker.invalid/v1"},
            {},
            "base URL must exactly match",
        ),
        (
            {},
            {"base_url_from_env": True},
            {},
            "base URL must not come from the environment",
        ),
        (
            {},
            {"proxy": "http://proxy.invalid:8080"},
            {},
            "resolved OpenRouter proxy must be empty",
        ),
        (
            {},
            {},
            {
                "OPENSQUILLA_TRUST_ENV": "1",
                "HTTPS_PROXY": "http://proxy.invalid:8080",
            },
            "OPENSQUILLA_TRUST_ENV must be disabled",
        ),
        (
            {},
            {"trust_env": True},
            {},
            "resolved OpenRouter transport must not trust ambient environment",
        ),
        (
            {},
            {
                "api_key_from_env": True,
                "api_key_env_name": "UNRELATED_AMBIENT_SECRET",
            },
            {},
            "environment credential must come from OPENROUTER_API_KEY",
        ),
        (
            {"search_proxy": "http://proxy.invalid:8080"},
            {},
            {},
            r"config\.search_proxy",
        ),
        (
            {"search_use_env_proxy": True},
            {},
            {},
            r"config\.search_use_env_proxy",
        ),
    ],
    ids=[
        "openrouter-base-url-env",
        "generic-api-key-env-name",
        "generic-api-key",
        "configured-llm-proxy",
        "resolved-provider",
        "resolved-base-url",
        "resolved-base-url-from-env",
        "resolved-proxy",
        "trusted-ambient-proxy",
        "resolved-trust-env",
        "resolved-credential-source",
        "brave-search-proxy",
        "brave-search-env-proxy",
    ],
)
def test_formal_amain_rejects_unsafe_resolved_transports_before_any_call(
    module,
    gateway_patch: dict[str, object],
    runtime_patch: dict[str, object],
    environment_patch: dict[str, str],
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(
        json.dumps({"id": "task-1", "prompt": "must fail before transport use"}) + "\n",
        encoding="utf-8",
    )
    args = module.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(tmp_path / "output"),
            "--groups",
            "B2",
            "--dry-run",
            "--experiment-config",
            str(module.DEFAULT_B2_EXPERIMENT_CONFIG_PATH),
            "--experiment-config-override-json",
            json.dumps({"benchmark_input": {"enforce_reference_input": False}}),
        ]
    )

    isolated_names = (
        "OPENROUTER_BASE_URL",
        "OPENSQUILLA_LLM_BASE_URL",
        "OPENSQUILLA_LLM_PROXY",
        "OPENSQUILLA_LLM_API_KEY_ENV",
        "OPENSQUILLA_LLM_API_KEY",
        "OPENSQUILLA_SEARCH_PROXY",
        "OPENSQUILLA_SEARCH_USE_ENV_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    )
    for name in isolated_names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "0")

    gateway = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "test-openrouter-key",
            "api_key_env": "",
            "base_url": "https://openrouter.ai/api/v1",
            "proxy": gateway_patch.get("llm_proxy", ""),
        },
        search_proxy=gateway_patch.get("search_proxy", ""),
        search_use_env_proxy=gateway_patch.get("search_use_env_proxy", False),
    )
    monkeypatch.setattr(
        type(gateway.llm_ensemble),
        "freeze_ranking_config",
        lambda _self: {
            "effective_config": {"proposer_count": {"backup_count": 2}},
        },
    )
    for name, value in environment_patch.items():
        monkeypatch.setenv(name, value)

    runtime = SimpleNamespace(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        base_url_from_env=False,
        proxy="",
        api_key_from_env=False,
        api_key_env_name="",
    )
    for name, value in runtime_patch.items():
        setattr(runtime, name, value)

    monkeypatch.setattr(module, "source_provenance", lambda: {"git_dirty": False})
    monkeypatch.setattr(module.GatewayConfig, "load", lambda _path: gateway)
    monkeypatch.setattr(module, "resolve_llm_runtime_config", lambda _config: runtime)
    calls = {"search_setup": 0, "member_provider": 0, "preflight": 0}

    def forbidden_search_setup(*_args, **_kwargs):
        calls["search_setup"] += 1
        raise AssertionError("search setup must not run")

    def forbidden_member_provider(*_args, **_kwargs):
        calls["member_provider"] += 1
        raise AssertionError("member provider resolution must not run")

    async def forbidden_preflight(*_args, **_kwargs):
        calls["preflight"] += 1
        raise AssertionError("web preflight must not run")

    monkeypatch.setattr(module, "configure_local_web_search_runtime", forbidden_search_setup)
    monkeypatch.setattr(module, "_experiment_member_provider_config", forbidden_member_provider)
    monkeypatch.setattr(module, "run_local_web_tools_preflight", forbidden_preflight)

    with pytest.raises(ValueError, match=message):
        asyncio.run(module.amain(args))
    assert calls == {"search_setup": 0, "member_provider": 0, "preflight": 0}


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_formal_runtime_accepts_frozen_proposer_recovery_overrides(module) -> None:
    experiment = _experiment_with_current_g1_contract(
        module,
        thinking_assignment_enabled=False,
    )
    payload = experiment.model_dump(mode="json")
    payload["ensemble"].update(
        {
            "proposer_recovery_max_additional_calls": 2,
            "proposer_max_tokens_cap": 32_768,
            "proposer_visible_answer_reserve_tokens": 2_048,
        }
    )
    experiment = type(experiment).model_validate(payload)
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "min_successful_proposers": 2,
        },
    )

    freeze = module.enforce_formal_draco_runtime_config(config, experiment, ["G1"])

    assert config.llm_ensemble.proposer_recovery_max_additional_calls == 2
    assert config.llm_ensemble.proposer_max_tokens_cap == 32_768
    assert config.llm_ensemble.proposer_visible_answer_reserve_tokens == 2_048
    assert freeze["proposer_recovery_max_additional_calls"] == 2
    assert freeze["proposer_max_tokens_cap"] == 32_768
    assert freeze["proposer_visible_answer_reserve_tokens"] == 2_048

    contract = module.validate_g1_registry_contract(experiment, config)
    provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=ProviderConfig(
            provider="openrouter",
            model="deepseek/deepseek-v4-pro",
            api_key="fake",
        ),
        fallback_provider=None,
        turn_metadata={"routed_tier": "c1", "routing_confidence": 0.9},
        ranking_inputs={"registry_allowlist": contract},
    )
    provider = module.enforce_draco_legal_proposer_quorum(provider)

    assert provider.proposer_recovery_max_additional_calls == 2
    assert provider.proposer_max_tokens_cap == 32_768
    assert provider.proposer_visible_answer_reserve_tokens == 2_048
    assert provider.selection_plan["proposer_recovery_policy"] == {
        **provider.selection_plan["proposer_recovery_policy"],
        "max_additional_physical_requests": 2,
        "max_tokens_cap": 32_768,
        "visible_answer_reserve_tokens": 2_048,
    }
    assert module.formal_proposer_recovery_policy_for_plan(
        provider.selection_plan
    ) == provider.selection_plan["proposer_recovery_policy"]
    assert module.g1_provider_native_recovery_policy_reason(
        provider.selection_plan
    ) == ""


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_formal_runtime_accepts_effective_proposer_backup_shortfall(module) -> None:
    policy = module.formal_proposer_recovery_policy(2)
    policy["effective_backup_count"] = 1
    plan = {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "ranking_parameters": {"proposer_count": {"backup_count": 2}},
        "selected_P": ["openrouter:model-a", "openrouter:model-b"],
        "proposer_models": ["model-a", "model-b"],
        "proposer_sample_count": 2,
        "backup_P": ["openrouter:model-c"],
        "selected_A": "openrouter:model-d",
        "aggregator_candidates": ["openrouter:model-d"],
        "configured_proposer_backup_count": 2,
        "effective_proposer_backup_count": 1,
        "effective_min_successful_proposers": 2,
        "proposer_recovery_policy": policy,
    }

    assert module.formal_proposer_recovery_policy_for_plan(plan) == policy
    assert module.g1_provider_native_recovery_policy_reason(plan) == ""

    plan["proposer_recovery_policy"] = {
        **policy,
        "effective_backup_count": 3,
    }
    assert module.formal_proposer_recovery_policy_for_plan(plan) is None
    assert (
        module.g1_provider_native_recovery_policy_reason(plan)
        == "invalid_g1_proposer_recovery_policy"
    )


def test_gateway_ranking_override_effective_snapshot_is_detached() -> None:
    original = {"penalties": {"task_cost_weights": {"medium": 0.17}}}
    config = GatewayConfig(
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "ranking_config_override": original,
        }
    )
    expected = config.llm_ensemble.ranking_config_effective_snapshot()

    original["penalties"]["task_cost_weights"]["medium"] = 0.81
    config.llm_ensemble.ranking_config_override["penalties"]["task_cost_weights"]["medium"] = 0.82
    config.llm_ensemble.ranking_thinking_assignment_enabled = True

    assert config.llm_ensemble.ranking_config_override_snapshot()["penalties"]["task_cost_weights"][
        "medium"
    ] == pytest.approx(0.17)
    assert config.llm_ensemble.ranking_config_effective_snapshot() == expected


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_g1_exact_routes_contract_rejects_runtime_pin_drift(module) -> None:
    experiment = _experiment_with_exact_g1_routes(module)
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "provider_routing": {"x-ai/grok-4.5": "wrong-provider"},
        }
    )
    assert experiment.g1_routing is not None
    config.llm_ensemble.freeze_ranking_config(
        base_version=experiment.g1_routing.expected_ranking_config_version
    )

    with pytest.raises(ValueError, match="provider pin"):
        module.validate_g1_registry_contract(experiment, config)


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_g1_registry_contract_rejects_allowlist_trace_drift(module) -> None:
    experiment = module.load_draco_experiment_config(
        module.DEFAULT_B2_EXPERIMENT_CONFIG_PATH
    ).config
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )
    contract = _resolved_g1_registry_contract(module, experiment, config)
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
        provider_routing={
            "deepseek/deepseek-v4-pro": "deepseek",
            "x-ai/grok-4.5": "wrong-provider",
        },
    )
    provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata={"routed_tier": "c1", "routing_confidence": 0.9},
        ranking_inputs={"registry_allowlist": contract},
    )
    expected_identities = contract["expected_identities"]
    plan = json.loads(json.dumps(provider.selection_plan))
    trace = {"selection_plan": plan}

    assert module.g1_registry_contract_reasons(trace, contract) == []

    missing_policy = dict(contract)
    missing_policy.pop("policy")
    assert module.g1_registry_contract_reasons(trace, missing_policy) == [
        "invalid_g1_registry_contract"
    ]

    missing_allowlist = json.loads(json.dumps(trace))
    missing_allowlist["selection_plan"].pop("candidate_allowlist")
    assert "missing_g1_candidate_allowlist" in module.g1_registry_contract_reasons(
        missing_allowlist,
        contract,
    )

    wrong_version = json.loads(json.dumps(trace))
    wrong_version["selection_plan"]["candidate_allowlist"]["filtered_registry_snapshot_version"] = (
        "wrong-version"
    )
    assert (
        "wrong_g1_candidate_allowlist_filtered_registry_snapshot_version"
        in module.g1_registry_contract_reasons(wrong_version, contract)
    )

    wrong_count = json.loads(json.dumps(trace))
    wrong_count_value = contract["expected_candidate_count"] - 1
    wrong_count["selection_plan"]["candidate_pool_size"] = wrong_count_value
    wrong_count["selection_plan"]["candidate_allowlist"]["candidate_count"] = wrong_count_value
    wrong_count_reasons = module.g1_registry_contract_reasons(
        wrong_count,
        contract,
    )
    assert "wrong_g1_candidate_pool_size" in wrong_count_reasons
    assert "wrong_g1_candidate_allowlist_candidate_count" in wrong_count_reasons

    wrong_pool = json.loads(json.dumps(trace))
    wrong_pool["selection_plan"]["candidate_pool"][0]["identity"] = "openrouter:evil/model"
    assert "wrong_g1_candidate_pool" in module.g1_registry_contract_reasons(
        wrong_pool,
        contract,
    )

    wrong_registry_version = json.loads(json.dumps(trace))
    wrong_registry_version["selection_plan"]["registry_snapshot_version"] = "wrong-version"
    assert "wrong_g1_registry_snapshot_version" in module.g1_registry_contract_reasons(
        wrong_registry_version, contract
    )

    invalid_registry_hash = json.loads(json.dumps(trace))
    invalid_registry_hash["selection_plan"]["registry_snapshot_hash"] = "not-a-hash"
    assert "invalid_g1_registry_snapshot_hash" in module.g1_registry_contract_reasons(
        invalid_registry_hash, contract
    )

    escaped_route = json.loads(json.dumps(trace))
    escaped_route["selection_plan"]["selected_P"][0] = "openrouter:evil/model"
    assert "wrong_g1_selected_proposers" in module.g1_registry_contract_reasons(
        escaped_route,
        contract,
    )

    wrong_user_profile = json.loads(json.dumps(trace))
    wrong_user_profile["selection_plan"]["user_profile_enabled"] = True
    assert "wrong_g1_user_profile_enabled" in module.g1_registry_contract_reasons(
        wrong_user_profile,
        contract,
    )

    escaped_count = json.loads(json.dumps(trace))
    escaped_count["selection_plan"]["selected_P"] = expected_identities
    escaped_count["selection_plan"]["proposer_count"] = len(expected_identities)
    escaped_count["selection_plan"]["proposer_sample_count"] = len(expected_identities)
    escaped_count["selection_plan"]["N_max"] = len(expected_identities)
    escaped_reasons = module.g1_registry_contract_reasons(
        escaped_count,
        contract,
    )
    assert "wrong_g1_proposer_bounds" in escaped_reasons
    assert "wrong_g1_selected_proposer_count" in escaped_reasons

    ranking_drift = json.loads(json.dumps(trace))
    ranking_drift["selection_plan"]["ranking_parameters"]["proposer_count"]["high_risk"]["max"] = 20
    assert "wrong_g1_ranking_config_hash" in module.g1_registry_contract_reasons(
        ranking_drift, contract
    )

    ranking_trace_drift = json.loads(json.dumps(trace))
    ranking_trace_drift["selection_plan"]["ranking_config_hash"] = "0" * 64
    assert "wrong_g1_ranking_config_trace" in module.g1_registry_contract_reasons(
        ranking_trace_drift, contract
    )

    duplicate_proposer = json.loads(json.dumps(trace))
    duplicate_proposer["selection_plan"]["selected_P"][1] = duplicate_proposer["selection_plan"][
        "selected_P"
    ][0]
    assert "wrong_g1_selected_proposers" in module.g1_registry_contract_reasons(
        duplicate_proposer, contract
    )


def test_resume_reuses_g1_plan_and_analyzer_within_one_provider_lifecycle() -> None:
    module = _load_resume_runner()
    from opensquilla.provider.ranking_router import (
        TASK_ANALYZER_MODEL_ID,
        TASK_ANALYZER_PROVIDER_ID,
    )

    experiment = _experiment_config()
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )
    contract = _resolved_g1_registry_contract(module, experiment, config)
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
    )
    provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata={"routed_tier": "c1", "routing_confidence": 0.9},
        ranking_inputs={"registry_allowlist": contract},
    )
    plan = deepcopy(provider.selection_plan)
    final_text = "answer"
    selected_usage = {
        "provider": "openrouter",
        "model": plan["aggregator_model"],
        "requested_provider": "openrouter",
        "requested_model": plan["aggregator_model"],
        "input_tokens": 3,
        "output_tokens": 1,
    }
    analyzer = {
        "role": "task_analyzer",
        "provider": TASK_ANALYZER_PROVIDER_ID,
        "model": TASK_ANALYZER_MODEL_ID,
        "requested_provider": TASK_ANALYZER_PROVIDER_ID,
        "requested_model": TASK_ANALYZER_MODEL_ID,
        "input_tokens": 3,
        "output_tokens": 1,
    }
    row = {
        "group": "G1",
        "provider_spec": dict(module.GROUP_SPECS["G1"]),
        "routing_trace": {},
        "final_text": final_text,
        "final_text_sha256": module.text_sha256(final_text),
        "usage": selected_usage,
        "ensemble_trace": {
            "mode": "agent_loop",
            "agent_llm_call_count": 1,
            "untraced_agent_llm_call_count": 0,
            "calls": [
                _terminal_policy_call(
                    index=1,
                    plan=plan,
                    successful=len(plan["proposer_models"]),
                    fallback=False,
                    output=final_text,
                )
            ],
        },
        "execution": {
            "selected_generation_attempt": 2,
            "generation_attempts": [
                {
                    "attempt_id": "1" * 32,
                    "attempt": 1,
                    "run": {
                        "error": "retry",
                        "final_text_sha256": module.text_sha256("earlier"),
                        "llm_request_count": 1,
                        "routing_trace": {
                            "kind": "selection_mode",
                            "selection_mode": "router_dynamic",
                            "selection_plan": deepcopy(plan),
                        },
                        "usage": {"model_usage_breakdown": [analyzer]},
                    },
                },
                {
                    "attempt_id": "2" * 32,
                    "attempt": 2,
                    "run": {
                        "error": "",
                        "final_text_sha256": module.text_sha256(final_text),
                        "llm_request_count": 1,
                        "routing_trace": {},
                        "usage": deepcopy(selected_usage),
                    },
                },
            ],
        },
    }
    compatibility = {"g1_registry_contract": contract}

    assert (
        module.ensemble_generation_completion_reasons(
            row,
            expected_run_compatibility_contract=compatibility,
        )
        == []
    )

    conflict = deepcopy(row)
    conflict["ensemble_trace"]["calls"][0]["selection_plan"]["decision_id"] = "conflicting-decision"
    conflict_reasons = module.ensemble_generation_completion_reasons(
        conflict,
        expected_run_compatibility_contract=compatibility,
    )
    assert "g1_lifecycle_plan_differs_from_physical_plan" in conflict_reasons

    missing_analyzer = deepcopy(row)
    missing_analyzer["execution"]["generation_attempts"][0]["run"]["usage"] = {
        "model_usage_breakdown": []
    }
    assert "missing_g1_task_analyzer_request" in module.ensemble_generation_completion_reasons(
        missing_analyzer,
        expected_run_compatibility_contract=compatibility,
    )

    repeated_analyzer = deepcopy(row)
    repeated_analyzer["execution"]["generation_attempts"][1]["run"]["usage"][
        "model_usage_breakdown"
    ] = [deepcopy(analyzer)]
    assert "repeated_g1_task_analyzer_request" in module.ensemble_generation_completion_reasons(
        repeated_analyzer,
        expected_run_compatibility_contract=compatibility,
    )

    prompt_hash = module.text_sha256("same prompt")
    row.update(
        {
            "task_id": "task-1",
            "prompt_sha256": prompt_hash,
            "task_input_sha256": "sha256:task-input",
            "run_compatibility_fingerprint": "sha256:run-contract",
            "quality_total": 80.0,
            "judge": _complete_legacy_judge("g1-lifecycle-judge"),
        }
    )
    state = module.resume_row_completion_state(
        module.seal_result_row(row),
        expected_prompt_sha256=prompt_hash,
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        expected_run_compatibility_contract=compatibility,
    )
    assert state["generation_valid"] is True, state
    assert state["action"] == "metadata_only"


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_g1_runtime_dynamic_plan_satisfies_frozen_ranking_contract(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = _experiment_config()
    assert experiment.g1_routing is not None
    generation_policy = {
        "generation_thinking": "model_max",
        "thinking_enabled": experiment.generation.thinking_enabled,
        "default_thinking_level": str(experiment.generation.default_thinking_level),
        "model_thinking_levels": dict(experiment.generation.model_thinking_levels),
        "require_highest_thinking": experiment.generation.require_highest_thinking,
    }
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        },
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )
    contract = _resolved_g1_registry_contract(module, experiment, config)
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="fake",
        provider_routing={
            "deepseek/deepseek-v4-pro": "deepseek",
            "x-ai/grok-4.5": "wrong-provider",
        },
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "1")

    provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=None,
        turn_metadata={"routed_tier": "c1", "routing_confidence": 0.9},
        ranking_inputs={
            "registry_allowlist": contract,
            "generation_policy": generation_policy,
        },
    )
    provider = module.apply_generation_policy_to_ensemble_provider(
        provider,
        generation_policy,
    )
    module.validate_strict_openrouter_ensemble_members(
        provider,
        generation_policy,
        allow_unpinned_openrouter=True,
    )
    assert provider.begin_provider_retry_scope(
        "post-generation-policy",
        max_additional_physical_requests=3,
    )
    assert provider.end_provider_retry_scope("post-generation-policy")

    assert (
        module.g1_registry_contract_reasons(
            {"selection_plan": provider.selection_plan},
            contract,
        )
        == []
    )
    assert provider.selection_plan["generation_policy_filter"]["excluded_count"] == 0
    assert all(
        member.thinking
        in module._openrouter_supported_thinking_levels(member.provider_config.model)
        for member in [
            *provider.proposers,
            *provider.proposer_backups,
            provider.aggregator,
            *provider.aggregator_fallbacks,
        ]
    )
    assert all(
        member.provider_config.provider_routing.get(member.provider_config.model) == "auto"
        for member in [*provider.proposers, provider.aggregator]
    )


def _compatibility_for(
    module,
    *,
    concurrency: int = 1,
    judge_repeats: int = 3,
    api_key: str = "benchmark-key-secret",
    extra_args: list[str] | None = None,
):
    args = module.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B1",
            "--concurrency",
            str(concurrency),
            "--judge-model",
            "google/gemini-3.1-pro-preview",
            "--judge-repeats",
            str(judge_repeats),
            *(extra_args or []),
        ]
    )
    args._source_provenance = {
        "git_head": "a" * 40,
        "source_tree_sha256": "b" * 64,
    }
    policy = module.benchmark_tool_policy(args)
    generation = module.generation_thinking_policy(args)
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": api_key,
        }
    )
    return module.build_run_compatibility(
        args=args,
        config=config,
        groups=["B1"],
        group_tool_policies=module.benchmark_tool_policies_for_groups(
            policy,
            ["B1"],
            args=args,
        ),
        generation_policy=generation,
    )


def test_run_compatibility_is_shared_by_main_and_resume_and_excludes_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", raising=False)
    resume_runner = _load_resume_runner()
    main_contract = _compatibility_for(runner, concurrency=1)
    resume_contract = _compatibility_for(resume_runner, concurrency=5)

    assert main_contract["fingerprints"] == resume_contract["fingerprints"]
    expected_capability = runner.durable_artifact_capability_contract()
    assert main_contract["contracts"]["B1"]["durable_artifact_capability"] == (
        expected_capability
    )
    assert resume_contract["contracts"]["B1"]["durable_artifact_capability"] == (
        expected_capability
    )
    assert "benchmark-key-secret" not in json.dumps(main_contract)
    changed_key = _compatibility_for(runner, api_key="different-benchmark-key")
    assert changed_key["fingerprints"]["B1"] != main_contract["fingerprints"]["B1"]
    changed_judge = _compatibility_for(runner, judge_repeats=2)
    assert changed_judge["fingerprints"]["B1"] != main_contract["fingerprints"]["B1"]
    changed_finalization = _compatibility_for(
        runner,
        extra_args=[
            "--deadline-wrapup-margin-seconds",
            "600",
            "--deadline-wrapup-disable-tools",
        ],
    )
    assert changed_finalization["fingerprints"]["B1"] != main_contract["fingerprints"]["B1"]
    assert (
        changed_finalization["contracts"]["B1"]["runner"]["finalization_policy"][
            "deadline_wrapup_margin_seconds"
        ]
        == 600
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "1")
    strict = _compatibility_for(runner)
    assert strict["fingerprints"]["B1"] != main_contract["fingerprints"]["B1"]


def _b2_compatibility_for(module, *, runner_concurrency: int, judge_concurrency: int):
    args = module.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B2",
            "--experiment-config",
            str(ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"),
            "--experiment-config-set",
            f"runner.concurrency={runner_concurrency}",
            "--experiment-config-set",
            f"judge.concurrency={judge_concurrency}",
        ]
    )
    module.apply_b2_g12_argument_alignment(args, ["B2"])
    args._source_provenance = {
        "git_head": "a" * 40,
        "source_tree_sha256": "b" * 64,
    }
    policy = module.benchmark_tool_policy(args)
    config = GatewayConfig(llm={"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"})
    return module.build_run_compatibility(
        args=args,
        config=config,
        groups=["B2"],
        group_tool_policies=module.benchmark_tool_policies_for_groups(
            policy,
            ["B2"],
            args=args,
        ),
        generation_policy=module.generation_thinking_policy(args),
    )


def test_b2_compatibility_excludes_effective_scheduling_concurrency() -> None:
    canary = _b2_compatibility_for(
        runner,
        runner_concurrency=1,
        judge_concurrency=1,
    )
    full = _b2_compatibility_for(
        runner,
        runner_concurrency=5,
        judge_concurrency=6,
    )

    assert canary["fingerprints"]["B2"] == full["fingerprints"]["B2"]
    experiment = canary["contracts"]["B2"]["experiment_config"]
    assert set(experiment) == {"sha256"}
    assert experiment["sha256"].startswith("sha256:")
    provider_routing = canary["contracts"]["B2"]["resolved_llm_runtime"]["provider_routing"]
    assert provider_routing["deepseek/deepseek-v4-pro"] == "deepseek"
    assert provider_routing["google/gemini-3.1-pro-preview"] == "google-ai-studio"
    assert provider_routing["moonshotai/kimi-k2.7-code"] == "moonshotai"
    assert provider_routing["qwen/qwen3.7-max"] == "alibaba"
    assert provider_routing["z-ai/glm-5.2"] == "z-ai"


def test_resume_expected_manifest_rejects_incompatible_contract(tmp_path: Path) -> None:
    resume_runner = _load_resume_runner()
    actual = _compatibility_for(resume_runner)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"run_compatibility": {"fingerprints": {"B1": "sha256:different"}}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="incompatible"):
        resume_runner.validate_expected_run_compatibility(
            path=manifest,
            actual=actual,
            groups=["B1"],
        )


def _repair_source_drift_compatibility(
    module,
    *,
    git_head: str,
    source_tree_sha256: str,
    contract_marker: str = "same",
) -> dict[str, object]:
    contract = {
        "schema": module.RUN_COMPATIBILITY_SCHEMA,
        "benchmark": "DRACO",
        "group": "B0",
        "source_identity": {
            "git_head": git_head,
            "source_tree_sha256": source_tree_sha256,
        },
        "contract_marker": contract_marker,
        "resolved_llm_runtime": {"provider": "openrouter"},
    }
    return {
        "schema": module.RUN_COMPATIBILITY_SCHEMA,
        "contracts": {"B0": contract},
        "fingerprints": {"B0": module.canonical_json_sha256(contract)},
    }


def test_repair_only_source_drift_inherits_expected_contract_and_actions(
    tmp_path: Path,
) -> None:
    module = _load_resume_runner()
    expected = _repair_source_drift_compatibility(
        module,
        git_head="a" * 40,
        source_tree_sha256="b" * 64,
    )
    actual = _repair_source_drift_compatibility(
        module,
        git_head="c" * 40,
        source_tree_sha256="d" * 64,
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"run_compatibility": expected}),
        encoding="utf-8",
    )

    inherited, audit = module.validate_repair_only_source_drift_compatibility(
        path=manifest,
        actual=actual,
        groups=["B0"],
    )

    assert inherited == expected
    assert audit["groups"]["B0"]["source_identity_changed"] is True
    assert audit["groups"]["B0"]["non_source_contract_match"] is True
    action_audit = module.repair_only_resume_classification_audit(
        selected_keys={
            ("B2", "task-1"),
            ("G1", "task-1"),
            ("B4", "task-1"),
        },
        resume_states={
            ("B2", "task-1"): {"action": "judge_only"},
            ("G1", "task-1"): {"action": "metadata_only"},
            ("B4", "task-1"): {"action": "audit_only"},
        },
    )
    assert action_audit["status"] == "repair_actions_validated"
    assert action_audit["action_counts"] == {
        "audit_only": 1,
        "judge_only": 1,
        "metadata_only": 1,
    }
    assert action_audit["regenerate_pair_count"] == 0
    assert action_audit["generation_allowed"] is False


@pytest.mark.parametrize(
    ("mutate_expected", "match"),
    [
        (
            lambda expected: expected["contracts"]["B0"].__setitem__(
                "contract_marker",
                "different",
            ),
            "non_source_contract_mismatch",
        ),
        (
            lambda expected: expected["fingerprints"].__setitem__(
                "B0",
                "sha256:not-canonical",
            ),
            "expected_fingerprint_not_canonical",
        ),
    ],
    ids=["non-source-contract", "non-canonical-fingerprint"],
)
def test_repair_only_source_drift_rejects_every_other_difference(
    tmp_path: Path,
    mutate_expected,
    match: str,
) -> None:
    module = _load_resume_runner()
    expected = _repair_source_drift_compatibility(
        module,
        git_head="a" * 40,
        source_tree_sha256="b" * 64,
    )
    actual = _repair_source_drift_compatibility(
        module,
        git_head="c" * 40,
        source_tree_sha256="d" * 64,
    )
    mutate_expected(expected)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"run_compatibility": expected}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=match):
        module.validate_repair_only_source_drift_compatibility(
            path=manifest,
            actual=actual,
            groups=["B0"],
        )


def test_repair_only_source_drift_requires_all_safeguards(tmp_path: Path) -> None:
    module = _load_resume_runner()
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text("", encoding="utf-8")
    args = module.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--groups",
            "B0",
            "--repair-only-source-drift",
        ]
    )

    with pytest.raises(ValueError, match="all repair-only safeguards") as exc:
        module.validate_repair_only_source_drift_prerequisites(args)

    message = str(exc.value)
    assert "--require-clean-source" in message
    assert "--expected-compatibility-manifest" in message
    assert "--resume-from-jsonl" in message
    assert "--only-group-task-keys" in message


def test_repair_only_classification_rejects_regenerate_and_budget_exhaustion() -> None:
    module = _load_resume_runner()
    audit = module.repair_only_resume_classification_audit(
        selected_keys={("B0", "missing"), ("B1", "budget")},
        resume_states={
            ("B1", "budget"): {
                "action": "regenerate",
                "prior_generation_attempts_used": module.GENERATION_MAX_ATTEMPTS,
                "generation_reasons": ["empty_final_text"],
            }
        },
    )

    assert audit["status"] == "rejected_regeneration_required"
    assert audit["regenerate_pair_count"] == 2
    assert {item["reason"] for item in audit["regenerate_pairs"]} == {
        "missing_resume_state",
        "generation_budget_exhausted",
    }


@pytest.mark.asyncio
async def test_repair_only_regenerate_fails_before_provider_or_judge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_resume_runner()
    task = {"id": "task-1", "prompt": "prompt"}
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(json.dumps(task) + "\n", encoding="utf-8")
    resume_path = tmp_path / "resume.jsonl"
    resume_path.write_text("", encoding="utf-8")
    only_keys = tmp_path / "only.jsonl"
    only_keys.write_text(
        json.dumps({"group": "B0", "task_id": "task-1"}) + "\n",
        encoding="utf-8",
    )
    expected = _repair_source_drift_compatibility(
        module,
        git_head="a" * 40,
        source_tree_sha256="b" * 64,
    )
    actual = _repair_source_drift_compatibility(
        module,
        git_head="c" * 40,
        source_tree_sha256="d" * 64,
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"run_compatibility": expected}),
        encoding="utf-8",
    )
    args = module.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(tmp_path / "output"),
            "--groups",
            "B0",
            "--resume-from-jsonl",
            str(resume_path),
            "--only-group-task-keys",
            str(only_keys),
            "--expected-compatibility-manifest",
            str(manifest),
            "--require-clean-source",
            "--repair-only-source-drift",
            "--judge-model",
            "judge-model",
        ]
    )
    monkeypatch.setattr(
        module,
        "source_provenance",
        lambda: {
            "git_head": "c" * 40,
            "source_tree_sha256": "d" * 64,
            "git_dirty": False,
        },
    )
    monkeypatch.setattr(
        module.GatewayConfig,
        "load",
        lambda _path: GatewayConfig(),
    )
    monkeypatch.setattr(
        module,
        "build_run_compatibility",
        lambda **_kwargs: actual,
    )
    calls = {"preflight": 0, "provider": 0, "generation": 0, "judge": 0}

    async def forbidden_preflight(*_args, **_kwargs):
        calls["preflight"] += 1
        raise AssertionError("preflight must not start")

    def forbidden_provider(*_args, **_kwargs):
        calls["provider"] += 1
        raise AssertionError("provider must not be built")

    async def forbidden_generation(*_args, **_kwargs):
        calls["generation"] += 1
        raise AssertionError("generation must not start")

    async def forbidden_judge(*_args, **_kwargs):
        calls["judge"] += 1
        raise AssertionError("Judge must not start")

    monkeypatch.setattr(
        module,
        "run_local_web_tools_preflight",
        forbidden_preflight,
    )
    monkeypatch.setattr(module, "build_single_provider", forbidden_provider)
    monkeypatch.setattr(module, "run_one", forbidden_generation)
    monkeypatch.setattr(module, "judge_text", forbidden_judge)

    with pytest.raises(
        ValueError,
        match="refuses every generation path",
    ):
        await module.amain(args)

    assert calls == {
        "preflight": 0,
        "provider": 0,
        "generation": 0,
        "judge": 0,
    }
    assert args._run_compatibility == expected
    assert args._repair_compatibility_audit["status"] == ("rejected_regeneration_required")
    assert not (tmp_path / "output").exists()


def test_repair_manifest_keeps_current_source_and_inherited_compatibility(
    tmp_path: Path,
) -> None:
    module = _load_resume_runner()
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text("", encoding="utf-8")
    args = module.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--groups",
            "B0",
            "--repair-only-source-drift",
        ]
    )
    inherited = _repair_source_drift_compatibility(
        module,
        git_head="a" * 40,
        source_tree_sha256="b" * 64,
    )
    args._source_provenance = {
        "git_head": "c" * 40,
        "source_tree_sha256": "d" * 64,
        "git_dirty": False,
    }
    args._run_compatibility = inherited
    args._repair_compatibility_audit = {
        "schema": module.REPAIR_ONLY_SOURCE_DRIFT_SCHEMA,
        "status": "repair_actions_validated",
    }
    manifest = tmp_path / "repair.manifest.json"

    module.write_manifest(
        manifest,
        args=args,
        stamp="repair-test",
        status="running",
        started_at=1.0,
        tasks=[],
        groups=["B0"],
        artifacts={},
        tool_policy={"tool_mode": "provider_only"},
    )

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["source_provenance"]["git_head"] == "c" * 40
    assert payload["run_compatibility"] == inherited
    assert payload["repair_compatibility_audit"]["status"] == ("repair_actions_validated")


def test_task_input_hash_covers_rubric_not_only_prompt() -> None:
    first = {"id": "task-1", "prompt": "same", "rubric": {"criteria": ["a"]}}
    second = {"id": "task-1", "prompt": "same", "rubric": {"criteria": ["b"]}}

    assert runner.text_sha256(first["prompt"]) == runner.text_sha256(second["prompt"])
    assert runner.canonical_json_sha256(first) != runner.canonical_json_sha256(second)


def test_openrouter_non_byok_audit_fails_closed() -> None:
    exact = {
        "llm_request_count": 2,
        "usage": {
            "model_usage_breakdown": [
                {
                    "provider": "openrouter",
                    "model": "model-a",
                    "input_tokens": 3,
                    "output_tokens": 1,
                    "billed_cost": 0.01,
                    "cost_source": "provider_billed",
                    "provider_usage": _openrouter_exact_evidence(0.01, "exact-1"),
                },
                {
                    "provider": "openrouter",
                    "model": "model-b",
                    "input_tokens": 4,
                    "output_tokens": 2,
                    "billed_cost": 0.02,
                    "cost_source": "provider_billed",
                    "provider_usage": _openrouter_exact_evidence(0.02, "exact-2"),
                },
            ]
        },
        # A forged/stale summary must not bypass physical receipt validation.
        "cost_accounting": {
            "llm_total": {
                "cost_exact": True,
                "request_count": 2,
                "exact_request_count": 2,
            }
        },
    }
    unverified = {
        **exact,
        "usage": {
            "model_usage_breakdown": [
                exact["usage"]["model_usage_breakdown"][0],
                {
                    **exact["usage"]["model_usage_breakdown"][1],
                    "provider_usage": {
                        "is_byok": True,
                        "provider_reported_cost": 0.02,
                        "response_ids": ["byok-2"],
                        "router_metadata": {"is_byok": True},
                    },
                },
            ]
        },
        "cost_accounting": {
            "llm_total": {
                "cost_exact": True,
                "request_count": 2,
                "exact_request_count": 2,
            }
        },
    }

    assert runner.openrouter_non_byok_audit(exact)["pass"] is True
    audit = runner.openrouter_non_byok_audit(unverified)
    assert audit["pass"] is False
    assert audit["status"] == "policy_violation"
    assert audit["policy_safe_to_continue"] is False
    assert audit["explicit_byok_request_count"] == 1
    assert audit["conflict_request_count"] == 0
    assert audit["unverified_request_count"] == 0
    assert audit["unverified_or_byok_request_count"] == 1


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    ("group", "model", "provider_slug", "provider_name", "serving_model"),
    [
        (
            "B0",
            "anthropic/claude-fable-5",
            "amazonbedrock",
            "Amazon Bedrock",
            "anthropic/claude-5-fable-20260609",
        ),
        (
            "B1",
            "deepseek/deepseek-v4-flash",
            "deepseek",
            "DeepSeek",
            "deepseek/deepseek-v4-flash-20260423",
        ),
        (
            "B2",
            "qwen/qwen3.7-max",
            "alibaba",
            "Alibaba",
            "qwen/qwen3.7-max-20260520",
        ),
        (
            "B4",
            "openai/gpt-5.6-sol",
            "azure",
            "Azure",
            "openai/gpt-5.6-sol-20260709",
        ),
        (
            "G1",
            "x-ai/grok-4.5",
            "xai",
            "xAI",
            "x-ai/grok-4.5-20260708",
        ),
    ],
)
def test_openrouter_non_byok_receipt_requires_formal_serving_route_pin(
    module,
    group: str,
    model: str,
    provider_slug: str,
    provider_name: str,
    serving_model: str,
) -> None:
    del group
    routing = {model: provider_slug}
    unit = {
        "provider": "openrouter",
        "model": serving_model,
        "requested_model": model,
        "input_tokens": 3,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            f"route-{provider_slug}",
            requested_model=model,
            serving_provider=provider_name,
            serving_model=serving_model,
        ),
    }
    row = {"llm_request_count": 1, "usage": unit}

    exact = module.openrouter_non_byok_audit(
        row,
        provider_routing=routing,
    )
    assert exact["pass"] is True
    assert exact["status"] == "exact"

    selected_only = json.loads(json.dumps(row))
    selected_only["usage"]["provider_usage"]["router_metadata"].pop("attempts")
    selected_only_audit = module.openrouter_non_byok_audit(
        selected_only,
        provider_routing=routing,
    )
    assert selected_only_audit["pass"] is True

    unexpected = json.loads(json.dumps(row))
    metadata = unexpected["usage"]["provider_usage"]["router_metadata"]
    metadata["attempts"][0]["provider"] = "UnexpectedProvider"
    metadata["endpoints"]["available"][0]["provider"] = "UnexpectedProvider"
    rejected = module.openrouter_non_byok_audit(
        unexpected,
        provider_routing=routing,
    )
    assert rejected["pass"] is False
    assert rejected["status"] == "policy_violation"
    assert rejected["conflict_request_count"] == 1


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_strict_openrouter_non_byok_environment_contract(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "OPENROUTER_BASE_URL",
        "OPENSQUILLA_LLM_BASE_URL",
        "OPENSQUILLA_LLM_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in (
        "OPENSQUILLA_PROVIDER_ROUTING_STRICT",
        "OPENSQUILLA_PROVIDER_STREAM_ERROR_FRAMES",
        "OPENSQUILLA_OPENROUTER_METADATA_REQUIRED",
        "OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS",
        "OPENSQUILLA_OPENROUTER_DISABLE_RESPONSE_CACHE",
        "DRACO_OPENROUTER_KEY_EXCLUSIVE",
    ):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "0")
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "test-openrouter-key",
        }
    )

    contract = module.validate_strict_openrouter_non_byok_environment(config)

    assert contract["validated"] is True
    assert contract["key_exclusive"] is True
    assert contract["trust_env"] is False
    monkeypatch.setenv("HTTPS_PROXY", "http://unexpected.invalid")
    with pytest.raises(ValueError, match="HTTPS_PROXY"):
        module.validate_strict_openrouter_non_byok_environment(config)


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
async def test_require_non_byok_rejects_unsafe_environment_before_any_call(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_path = tmp_path / "tasks.jsonl"
    task_path.write_text(
        json.dumps({"id": "task-1", "prompt": "prompt"}) + "\n",
        encoding="utf-8",
    )
    args = module.build_parser().parse_args(
        [
            "--input",
            str(task_path),
            "--output-dir",
            str(tmp_path / "output"),
            "--groups",
            "B0",
            "--require-openrouter-non-byok",
        ]
    )
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "api_key": "test-openrouter-key",
        }
    )
    monkeypatch.setattr(module.GatewayConfig, "load", lambda _path: config)
    monkeypatch.delenv("OPENSQUILLA_PROVIDER_ROUTING_STRICT", raising=False)
    calls = {"preflight": 0, "provider": 0, "judge": 0}

    async def forbidden_preflight(*_args, **_kwargs):
        calls["preflight"] += 1
        raise AssertionError("preflight must not start")

    def forbidden_provider(*_args, **_kwargs):
        calls["provider"] += 1
        raise AssertionError("provider must not be built")

    async def forbidden_judge(*_args, **_kwargs):
        calls["judge"] += 1
        raise AssertionError("Judge must not start")

    monkeypatch.setattr(module, "run_local_web_tools_preflight", forbidden_preflight)
    monkeypatch.setattr(module, "build_single_provider", forbidden_provider)
    monkeypatch.setattr(module, "judge_text", forbidden_judge)

    with pytest.raises(
        ValueError,
        match="strict OpenRouter non-BYOK environment validation failed",
    ):
        await module.amain(args)
    assert calls == {"preflight": 0, "provider": 0, "judge": 0}


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_openrouter_non_byok_audit_rejects_zero_request_vacuous_pass(module) -> None:
    audit = module.openrouter_non_byok_audit({"llm_request_count": 0, "usage": {}})

    assert audit["pass"] is False
    assert audit["status"] == "metadata_incomplete"
    assert audit["policy_safe_to_continue"] is True
    assert audit["request_count"] == 0
    assert audit["evidence_unit_count"] == 0


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_missing_cost_is_estimated_cache_aware_with_frozen_price_provenance(
    module,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    snapshot = {
        "schema_version": "test/v1",
        "snapshot_version": "frozen-price-test-v1",
        "models": [
            {
                "registry_facts": {
                    "provider": "openrouter",
                    "model_id": "model-a",
                    "price": {
                        "input_per_million": 2.0,
                        "output_per_million": 4.0,
                        "cache_read_per_million": 0.5,
                        "cache_write_per_million": 3.0,
                    },
                }
            }
        ],
    }
    module._frozen_openrouter_registry_price_index.cache_clear()
    request.addfinalizer(module._frozen_openrouter_registry_price_index.cache_clear)
    monkeypatch.setattr(
        module,
        "_load_frozen_model_registry_snapshot",
        lambda: snapshot,
    )
    monkeypatch.setattr(
        module,
        "resolve_model_price",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("exact frozen registry price must win before layered pricing")
        ),
    )
    usage = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 100,
        "output_tokens": 20,
        # Some legacy adapters populate only ``cached_tokens`` while newer
        # ones also emit a smaller cache_read_tokens bucket.  The normalized
        # estimate must conservatively retain the larger observed count.
        "cache_read_tokens": 10,
        "cached_tokens": 40,
        "cache_write_tokens": 10,
        "cost_source": "none",
    }

    assert module.estimate_missing_usage_costs(usage) is True

    assert usage["estimated_cost_usd"] == pytest.approx(230.0 / 1_000_000)
    assert usage["cost_usd"] == pytest.approx(230.0 / 1_000_000)
    assert usage["cost_source"] == "opensquilla_static_estimate"
    provider_usage = usage["provider_usage"]
    assert provider_usage["cost_repair"] == "token_price_estimate"
    assert provider_usage["estimate_basis"] == "cache_aware"
    assert provider_usage["price_source"] == "frozen_openrouter_model_registry"
    assert provider_usage["estimate_pricing"] == {
        "source": "frozen_openrouter_model_registry",
        "snapshot_version": "frozen-price-test-v1",
        "snapshot_canonical_sha256": module.canonical_json_sha256(snapshot),
        "registry_provider": "openrouter",
        "registry_model_id": "model-a",
        "provider": "openrouter",
        "model": "model-a",
        "input_per_m": 2.0,
        "output_per_m": 4.0,
        "cache_read_per_m": 0.5,
        "cache_write_per_m": 3.0,
    }


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize("provider", ["openrouter", ""], ids=["explicit", "legacy-empty"])
def test_qwen_3_8_registry_miss_uses_frozen_cache_aware_static_price(
    module,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    provider: str,
) -> None:
    snapshot = {
        "schema_version": "test/v1",
        "snapshot_version": "frozen-price-test-v1",
        "models": [],
    }
    module._frozen_openrouter_registry_price_index.cache_clear()
    request.addfinalizer(module._frozen_openrouter_registry_price_index.cache_clear)
    monkeypatch.setattr(
        module,
        "_load_frozen_model_registry_snapshot",
        lambda: snapshot,
    )
    monkeypatch.setattr(
        module,
        "resolve_model_price",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("the frozen Qwen price must not use layered pricing")
        ),
    )
    usage = {
        "provider": provider,
        "model": "qwen/qwen3.8-max",
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cached_tokens": 100_000,
        "cache_write_tokens": 100_000,
        "cost_source": "none",
    }

    assert module.estimate_missing_usage_costs(usage) is True
    assert usage["estimated_cost_usd"] == pytest.approx(7.875)
    assert usage["cost_usd"] == pytest.approx(7.875)
    assert usage["cost_source"] == "opensquilla_static_estimate"
    provider_usage = usage["provider_usage"]
    assert provider_usage["estimate_basis"] == "cache_aware"
    assert provider_usage["price_source"] == "static_table"
    assert provider_usage["estimate_pricing"] == {
        "source": "static_table",
        "snapshot_version": "",
        "snapshot_canonical_sha256": "",
        "registry_provider": "",
        "registry_model_id": "",
        "provider": provider,
        "model": "qwen/qwen3.8-max",
        "input_per_m": 2.0,
        "output_per_m": 6.0,
        "cache_read_per_m": 0.25,
        "cache_write_per_m": 2.5,
    }


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize("provider", ["openrouter", ""], ids=["explicit", "legacy-empty"])
def test_openrouter_frozen_registry_miss_never_calls_layered_price_resolver(
    module,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    provider: str,
) -> None:
    snapshot = {
        "schema_version": "test/v1",
        "snapshot_version": "frozen-price-test-v1",
        "models": [],
    }
    module._frozen_openrouter_registry_price_index.cache_clear()
    request.addfinalizer(module._frozen_openrouter_registry_price_index.cache_clear)
    monkeypatch.setattr(
        module,
        "_load_frozen_model_registry_snapshot",
        lambda: snapshot,
    )
    monkeypatch.setattr(
        module,
        "resolve_model_price",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a frozen OpenRouter registry miss must not perform live lookup")
        ),
    )
    usage = {
        "provider": provider,
        "model": "unknown/model",
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_source": "none",
    }

    assert module.estimate_missing_usage_costs(usage) is True
    assert usage["cost_source"] == "none"
    assert "estimated_cost_usd" not in usage
    assert "cost_usd" not in usage
    assert usage["provider_usage"]["cost_estimate_provenance"] == {
        "status": "unavailable",
        "reason": "frozen_registry_price_unavailable",
        "provider": provider,
        "model": "unknown/model",
        "source": "frozen_openrouter_model_registry",
        "snapshot_version": "frozen-price-test-v1",
        "snapshot_canonical_sha256": module.canonical_json_sha256(snapshot),
        "registry_provider": "openrouter",
        "registry_model_id": "unknown/model",
    }


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize(
    "price_source",
    ["catalog", "live_openrouter", "user_override", "default", "unknown"],
)
def test_missing_cost_rejects_non_frozen_price_sources_without_inventing_zero(
    module,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    price_source: str,
) -> None:
    price = SimpleNamespace(
        input_per_m=2.0,
        output_per_m=4.0,
        cache_read_per_m=0.5,
        cache_write_per_m=3.0,
    )
    module._frozen_openrouter_registry_price_index.cache_clear()
    request.addfinalizer(module._frozen_openrouter_registry_price_index.cache_clear)
    monkeypatch.setattr(
        module,
        "_load_frozen_model_registry_snapshot",
        lambda: {
            "schema_version": "test/v1",
            "snapshot_version": "frozen-price-test-v1",
            "models": [],
        },
    )
    monkeypatch.setattr(
        module,
        "resolve_model_price",
        lambda _model, _provider: SimpleNamespace(
            entry=price,
            source=price_source,
        ),
    )
    usage = {
        "provider": "anthropic",
        "model": "unknown/model",
        "input_tokens": 100,
        "output_tokens": 20,
        "cached_tokens": 40,
        "cache_write_tokens": 10,
        "billed_cost": 0.0,
        "estimated_cost_usd": 99.0,
        "cost_usd": 99.0,
        "cost_source": "opensquilla_static_estimate",
        "provider_usage": {
            "response_ids": ["response-1"],
            "price_source": price_source,
        },
    }

    # Recording why no estimate was possible is a metadata repair, but no
    # dollar value or completeness claim may be synthesized from this source.
    assert module.estimate_missing_usage_costs(usage) is True
    assert usage["billed_cost"] == 0.0
    assert usage["cost_source"] == "none"
    assert "estimated_cost_usd" not in usage
    assert "cost_usd" not in usage
    assert usage["provider_usage"]["response_ids"] == ["response-1"]
    assert usage["provider_usage"]["discarded_cost_estimate_provenance"] == {
        "reason": "non_frozen_price_source",
        "cost_source": "opensquilla_static_estimate",
        "price_source": price_source,
        "estimated_cost_usd": 99.0,
    }
    assert usage["provider_usage"]["cost_estimate_provenance"] == {
        "status": "unavailable",
        "reason": "non_frozen_price_source",
        "provider": "anthropic",
        "model": "unknown/model",
        "source": price_source,
        "snapshot_version": "",
        "snapshot_canonical_sha256": "",
        "registry_provider": "",
        "registry_model_id": "",
    }
    accounting = module.usage_cost_accounting(
        usage,
        expected_requests=1,
        scope="test",
    )
    assert accounting["recorded_cost_usd"] == 0.0
    assert accounting["unknown_request_count"] == 1
    assert accounting["cost_complete"] is False
    assert accounting["cost_exact"] is False


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_unclosed_stream_and_byok_are_degraded_execution_and_audit_warning(module) -> None:
    execution = module.execution_status_payload(
        generation_accepted=True,
        final_text="accepted answer",
        run_error=None,
        ensemble_trace={"candidates": [{"stream_closed": False}]},
    )
    audit = module.row_audit_status(
        {
            "cost_accounting": {
                "actual_llm_total": {
                    "cost_complete": True,
                    "cost_exact": False,
                    "estimated_request_count": 1,
                    "mixed_request_count": 0,
                    "unknown_request_count": 0,
                }
            },
            "ensemble_trace": {"candidates": [{"stream_closed": False}]},
            "usage": {},
        },
        non_byok_audit={
            "status": "policy_violation",
            "pass": False,
        },
    )

    assert execution["status"] == "degraded_success"
    assert execution["success"] is True
    assert execution["degraded_reasons"] == ["unclosed_physical_stream"]
    assert audit["status"] == "warning"
    assert audit["separate_from_execution"] is True
    assert audit["policy"]["compliant"] is False
    assert audit["cost"]["status"] == "estimated"
    assert audit["warnings"] == [
        "openrouter_non_byok_policy_violation",
        "llm_cost_estimated",
        "unclosed_physical_stream",
    ]


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_visible_ensemble_answer_survives_provider_and_identity_audit_errors(module) -> None:
    plan = _k21_router_dynamic_plan()
    trace = _k21_ensemble_trace(plan)
    trace["candidates"][1].update(
        {
            "provider": "openrouter",
            "model": "outside-roster",
            "requested_provider": "openrouter",
            "requested_model": "outside-roster",
        }
    )
    result = module.RunResult(
        final_text="answer",
        done=module.DoneEvent(ensemble_trace=trace),
        error="ensemble post-call identity evidence mismatch",
    )

    assert module.generation_retry_reason(
        result,
        expected_selection_mode="router_dynamic",
        expected_selection_plan=plan,
    ) == ""
    assert "wrong_requested_proposer_identity" in result.audit_warnings
    execution = module.execution_status_payload(
        generation_accepted=True,
        final_text=result.final_text,
        run_error=result.error,
        ensemble_trace=trace,
    )
    assert execution["status"] == "degraded_success"
    assert execution["success"] is True
    assert "provider_error_after_visible_ensemble_output" in execution["degraded_reasons"]


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_ensemble_actual_quorum_shortfall_still_blocks_execution(module) -> None:
    plan = _k21_router_dynamic_plan()
    trace = _k21_ensemble_trace(plan)
    for candidate in trace["candidates"][1:]:
        candidate.update(
            {
                "ok": False,
                "error": "failed",
                "content": {"text": "", "chars": 0, "truncated": False},
            }
        )
    trace["successful_proposers"] = 1
    result = module.RunResult(
        final_text="answer",
        done=module.DoneEvent(ensemble_trace=trace),
    )

    assert module.ensemble_generation_retry_reason(
        result,
        expected_selection_mode="router_dynamic",
        expected_selection_plan=plan,
    ) == "insufficient_actual_proposer_quorum"


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_dynamic_partial_proposer_quorum_is_usable_but_not_strict_success(module) -> None:
    plan = {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "profile": "test",
        "proposer_models": ["p1", "p2", "p3"],
        "proposer_sample_count": 3,
        "selected_P": ["openrouter:p1", "openrouter:p2", "openrouter:p3"],
        "aggregator_model": "agg-primary",
        "selected_A": "openrouter:agg-primary",
        "aggregator_candidates": [
            "openrouter:agg-primary",
            "openrouter:agg-backup",
        ],
        "effective_min_successful_proposers": 2,
    }
    trace = _terminal_policy_call(
        index=1,
        plan=plan,
        successful=1,
        fallback=False,
        output="final answer",
    )
    for index, candidate in enumerate(trace["candidates"]):
        candidate.update(
            {
                "usable_for_aggregation": index < 2,
                "completion_outcome": (
                    "complete" if index == 0 else "partial_usable" if index == 1 else "failed"
                ),
                "selected_for_aggregation": index < 2,
                "error_code": "" if index == 0 else "stream_interrupted",
            }
        )
    partial = trace["candidates"][1]
    partial_text = "Partial but meaningful proposer draft that can be fused."
    partial["content"] = {
        "text": partial_text,
        "chars": len(partial_text),
        "truncated": False,
    }
    trace.update(
        {
            "usable_proposers": 2,
            "partial_proposers": 1,
            "selected_candidate_count": 2,
            "execution_quorum_required": 2,
            "execution_quorum_met": True,
        }
    )

    assert (
        module.ensemble_call_core_reasons(
            trace,
            expected_selection_mode="router_dynamic",
            expected_selection_plan=plan,
            final_text="final answer",
            require_output_binding=True,
        )
        == []
    )
    if module is resume_runner:
        assert module.ensemble_generation_completion_reasons(
            {
                "group": "G1",
                "provider_spec": dict(module.GROUP_SPECS["G1"]),
                "routing_trace": {"selection_plan": deepcopy(plan)},
                "final_text": "final answer",
                "ensemble_trace": deepcopy(trace),
            }
        ) == []

    tampered = deepcopy(trace)
    tampered["usable_proposers"] = 1
    assert "invalid_proposer_execution_quorum_evidence" in (
        module.ensemble_call_core_reasons(
            tampered,
            expected_selection_mode="router_dynamic",
            expected_selection_plan=plan,
            final_text="final answer",
            require_output_binding=True,
        )
    )

    insufficient = deepcopy(trace)
    failed = insufficient["candidates"][1]
    failed.update(
        {
            "usable_for_aggregation": False,
            "completion_outcome": "failed",
            "selected_for_aggregation": False,
            "content": {"text": "", "chars": 0, "truncated": False},
        }
    )
    insufficient.update(
        {
            "usable_proposers": 1,
            "partial_proposers": 0,
            "selected_candidate_count": 1,
            "execution_quorum_required": 2,
            "execution_quorum_met": False,
        }
    )
    assert "insufficient_actual_proposer_quorum" in (
        module.ensemble_call_core_reasons(
            insufficient,
            expected_selection_mode="router_dynamic",
            expected_selection_plan=plan,
            final_text="final answer",
            require_output_binding=True,
        )
    )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_dynamic_aggregator_fallback_requires_frozen_physical_identity(module) -> None:
    plan = {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "profile": "test",
        "proposer_models": ["p1", "p2"],
        "proposer_sample_count": 2,
        "selected_P": ["openrouter:p1", "openrouter:p2"],
        "aggregator_model": "agg-primary",
        "selected_A": "openrouter:agg-primary",
        "aggregator_candidates": [
            "openrouter:agg-primary",
            "openrouter:agg-backup",
        ],
        "effective_min_successful_proposers": 2,
    }
    trace = _terminal_policy_call(
        index=1,
        plan=plan,
        successful=2,
        fallback=False,
        output="fallback answer",
    )
    for candidate in trace["candidates"]:
        candidate.update(
            {
                "usable_for_aggregation": True,
                "completion_outcome": "complete",
                "selected_for_aggregation": True,
            }
        )
    trace.update(
        {
            "usable_proposers": 2,
            "partial_proposers": 0,
            "selected_candidate_count": 2,
            "execution_quorum_required": 2,
            "execution_quorum_met": True,
            "fallback_used": True,
            "fallback_reason": "primary_failed",
            "executed_A": "openrouter:agg-backup",
        }
    )
    final_request = trace["final_request"]
    for key in ("role",):
        final_request[key] = "aggregator"
    trace["final_request_role"] = "aggregator"
    final_request["execution"].update(
        {
            "role": "aggregator",
            "provider": "openrouter",
            "requested_provider": "openrouter",
            "actual_provider": "openrouter",
            "model": "agg-backup",
            "requested_model": "agg-backup",
            "actual_model": "agg-backup",
        }
    )
    final_request["usage"].update(
        {
            "provider": "openrouter",
            "requested_provider": "openrouter",
            "model": "agg-backup",
            "requested_model": "agg-backup",
            "physical_attempt_id": "2" * 32,
        }
    )
    trace["aggregator_recovery"] = {
        "schema": "opensquilla.ensemble-aggregator-recovery/v1",
        "candidate_ids": list(plan["aggregator_candidates"]),
        "attempts": [
            {
                "attempt": 1,
                "fallback_index": 0,
                "request_started": True,
                "physical_request_count": 1,
                "physical_attempt_id": "1" * 32,
                "outcome": "failed",
                "requested_provider": "openrouter",
                "requested_model": "agg-primary",
            },
            {
                "attempt": 2,
                "fallback_index": 1,
                "request_started": True,
                "physical_request_count": 1,
                "physical_attempt_id": "2" * 32,
                "outcome": "succeeded",
                "requested_provider": "openrouter",
                "requested_model": "agg-backup",
            },
        ],
        "selected_attempt": 2,
        "fallback_index": 1,
        "executed_A": "openrouter:agg-backup",
        "success": True,
        "degraded": False,
    }

    assert (
        module.ensemble_call_core_reasons(
            trace,
            expected_selection_mode="router_dynamic",
            expected_selection_plan=plan,
            final_text="fallback answer",
            require_output_binding=True,
        )
        == []
    )
    status = module.execution_status_payload(
        generation_accepted=True,
        final_text="fallback answer",
        run_error=None,
        ensemble_trace=trace,
    )
    assert status["status"] == "degraded_success"
    assert "aggregator_fallback_used" in status["degraded_reasons"]

    unknown_usage_degraded = deepcopy(trace)
    unknown_final_request = unknown_usage_degraded["final_request"]
    unknown_final_request["usage"].update(
        {
            "provider": "",
            "model": "",
            "usage_unknown": True,
            "cost_source": "none",
        }
    )
    unknown_final_request["usage"].pop("stop_reason", None)
    unknown_final_request["execution"].pop("actual_provider", None)
    unknown_final_request["execution"].pop("actual_model", None)
    unknown_usage_degraded.update(
        {
            "execution_outcome": "degraded_success",
            "delivery_outcome": "degraded_success",
        }
    )
    unknown_recovery = unknown_usage_degraded["aggregator_recovery"]
    unknown_recovery.update(
        {
            "degraded": True,
            "success": False,
            "delivery_success": True,
            "delivery_outcome": "degraded_success",
            "audit_outcome": "incomplete",
        }
    )
    unknown_recovery["attempts"][1]["outcome"] = "failed"
    unknown_reasons = module.ensemble_call_core_reasons(
        unknown_usage_degraded,
        expected_selection_mode="router_dynamic",
        expected_selection_plan=plan,
        final_text="fallback answer",
        require_output_binding=True,
    )
    assert set(unknown_reasons) == {
        "missing_actual_aggregator_model",
        "missing_actual_aggregator_provider",
        "missing_aggregator_stop_reason",
    }
    assert all(module.ensemble_metadata_only_reason(reason) for reason in unknown_reasons)
    assert unknown_final_request["usage"]["physical_attempt_id"] == "2" * 32
    assert (
        module.ensemble_generation_retry_reason(
            module.RunResult(
                final_text="fallback answer",
                done=module.DoneEvent(ensemble_trace=unknown_usage_degraded),
            ),
            expected_selection_mode="router_dynamic",
            expected_selection_plan=plan,
        )
        == ""
    )

    missing_usage_physical = deepcopy(trace)
    missing_usage_physical["final_request"]["usage"].pop("physical_attempt_id")
    assert "invalid_aggregator_fallback_physical_evidence" in (
        module.ensemble_call_core_reasons(
            missing_usage_physical,
            expected_selection_mode="router_dynamic",
            expected_selection_plan=plan,
        )
    )

    wrong_identity = deepcopy(trace)
    wrong_identity["final_request"]["usage"]["model"] = "outside-roster"
    assert "unauthorized_aggregator_fallback_identity" in (
        module.ensemble_call_core_reasons(
            wrong_identity,
            expected_selection_mode="router_dynamic",
            expected_selection_plan=plan,
        )
    )
    conflicting_actual_identity = deepcopy(trace)
    conflicting_actual_identity["final_request"]["execution"]["actual_model"] = "outside-roster"
    assert "unauthorized_aggregator_fallback_identity" in (
        module.ensemble_call_core_reasons(
            conflicting_actual_identity,
            expected_selection_mode="router_dynamic",
            expected_selection_plan=plan,
        )
    )
    bad_physical = deepcopy(trace)
    bad_physical["aggregator_recovery"]["attempts"][1]["physical_attempt_id"] = "bad"
    assert "invalid_aggregator_fallback_physical_evidence" in (
        module.ensemble_call_core_reasons(
            bad_physical,
            expected_selection_mode="router_dynamic",
            expected_selection_plan=plan,
        )
    )

    mismatched_physical = deepcopy(trace)
    mismatched_physical["aggregator_recovery"]["attempts"][1]["physical_attempt_id"] = "3" * 32
    assert "invalid_aggregator_fallback_physical_evidence" in (
        module.ensemble_call_core_reasons(
            mismatched_physical,
            expected_selection_mode="router_dynamic",
            expected_selection_plan=plan,
        )
    )

    wrong_selected_index = deepcopy(trace)
    wrong_selected_index["aggregator_recovery"]["attempts"][1]["fallback_index"] = 2
    assert "invalid_aggregator_fallback_physical_evidence" in (
        module.ensemble_call_core_reasons(
            wrong_selected_index,
            expected_selection_mode="router_dynamic",
            expected_selection_plan=plan,
        )
    )

    wrong_recovery_index = deepcopy(trace)
    wrong_recovery_index["aggregator_recovery"]["fallback_index"] = 2
    assert "invalid_aggregator_fallback_physical_evidence" in (
        module.ensemble_call_core_reasons(
            wrong_recovery_index,
            expected_selection_mode="router_dynamic",
            expected_selection_plan=plan,
        )
    )

    duplicate_selected_attempt = deepcopy(trace)
    duplicate_selected_attempt["aggregator_recovery"]["attempts"].append(
        deepcopy(duplicate_selected_attempt["aggregator_recovery"]["attempts"][1])
    )
    assert "invalid_aggregator_fallback_physical_evidence" in (
        module.ensemble_call_core_reasons(
            duplicate_selected_attempt,
            expected_selection_mode="router_dynamic",
            expected_selection_plan=plan,
        )
    )


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_closed_partial_aggregator_delivery_is_degraded_execution(module) -> None:
    trace = {
        "execution_outcome": "degraded_success",
        "delivery_outcome": "partial_usable",
        "aggregator_recovery": {
            "schema": "opensquilla.ensemble-aggregator-recovery/v1",
            "degraded": True,
            "success": False,
            "attempts": [{"outcome": "failed", "stream_closed": True}],
        },
    }
    execution = module.execution_status_payload(
        generation_accepted=True,
        final_text="usable visible answer",
        run_error=None,
        ensemble_trace=trace,
    )

    assert execution["status"] == "degraded_success"
    assert execution["success"] is True
    assert execution["degraded_reasons"] == [
        "aggregator_partial_usable",
        "aggregator_recovery_degraded",
    ]


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
@pytest.mark.parametrize("extra_kind", ["explicit_byok", "conflict"])
def test_openrouter_non_byok_audit_does_not_hide_extra_policy_evidence(
    module,
    extra_kind: str,
) -> None:
    exact = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 3,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.01, "exact-1"),
    }
    extra = {
        **exact,
        "provider_usage": {
            "is_byok": True,
            "provider_reported_cost": 0.01,
            "response_ids": [f"{extra_kind}-1"],
            "router_metadata": {
                **_openrouter_exact_evidence(0.01, "unused")["router_metadata"],
                "is_byok": extra_kind == "explicit_byok",
            },
        },
    }
    audit = module.openrouter_non_byok_audit(
        {
            "llm_request_count": 1,
            "usage": {"model_usage_breakdown": [exact, extra]},
        }
    )

    assert audit["pass"] is False
    assert audit["status"] == "policy_violation"
    assert audit["policy_safe_to_continue"] is False
    assert audit["request_count"] == 2
    assert audit["evidence_unit_count"] == 2
    assert audit["evidence_overflow_count"] == 0
    if extra_kind == "explicit_byok":
        assert audit["explicit_byok_request_count"] == 1
        assert audit["conflict_request_count"] == 0
    else:
        assert audit["explicit_byok_request_count"] == 0
        assert audit["conflict_request_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "strict",
        "expected_requests",
        "explicit_byok",
    ),
    [
        (True, 5, False),
        (True, 4, False),
        (True, 4, True),
        (False, 5, False),
    ],
    ids=[
        "strict-missing-receipt",
        "strict-all-exact",
        "strict-explicit-byok",
        "non-strict-unchanged",
    ],
)
async def test_generation_non_byok_audit_never_blocks_judge_or_task_success(
    monkeypatch: pytest.MonkeyPatch,
    strict: bool,
    expected_requests: int,
    explicit_byok: bool,
) -> None:
    config, inherited = _openrouter_config()
    rows = [
        {
            "provider": "openrouter",
            "model": f"model-{index}",
            "input_tokens": 3,
            "output_tokens": 1,
            "billed_cost": 0.01,
            "cost_source": "provider_billed",
            "provider_usage": _openrouter_exact_evidence(
                0.01,
                f"generation-{index}",
            ),
        }
        for index in range(4)
    ]
    if explicit_byok:
        rows[-1]["provider_usage"] = {
            "is_byok": True,
            "provider_reported_cost": 0.01,
            "response_ids": ["generation-explicit-byok"],
            "router_metadata": {"is_byok": True},
        }
    done = DoneEvent(
        input_tokens=sum(int(row["input_tokens"]) for row in rows),
        output_tokens=sum(int(row["output_tokens"]) for row in rows),
        billed_cost=sum(float(row["billed_cost"]) for row in rows),
        cost_source="provider_billed",
        model="model-final",
        provider="openrouter",
        model_usage_breakdown=rows,
        ensemble_trace=_valid_ensemble_trace(
            selection_mode="router_tree_baseline",
            llm_request_count=expected_requests,
        ),
    )
    result = runner.RunResult(final_text="answer", done=done)
    attempts = [
        {
            "attempt": 1,
            "retryable": False,
            "retry_reason": "",
            "will_retry": False,
            "retry_backoff_s": 0.0,
            "run": runner.run_result_summary(result),
        }
    ]

    async def fake_build_experiment_provider(**_kwargs):
        return runner.ProviderBuildResult(provider=object(), prompt="prompt")

    async def fake_collect_generation_with_retries(*_args, **_kwargs):
        return result, attempts, 1

    judge_calls = 0

    async def fake_judge_text(**_kwargs):
        nonlocal judge_calls
        judge_calls += 1
        return _complete_legacy_judge("generation-gate-judge")

    monkeypatch.setattr(runner, "build_experiment_provider", fake_build_experiment_provider)
    monkeypatch.setattr(
        runner,
        "collect_generation_with_retries",
        fake_collect_generation_with_retries,
    )
    monkeypatch.setattr(runner, "judge_text", fake_judge_text)

    row = await runner.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="B3",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=object(),
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=10.0,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={"tools_enabled": False, "tool_mode": "provider_only"},
        generation_policy={},
        require_openrouter_non_byok=strict,
    )

    assert judge_calls == 1
    assert row["error"] is None
    assert row.get("judge") is not None
    assert row["completion_status"]["status"] == "complete"
    assert row["execution_status"]["success"] is True
    if strict:
        assert row["openrouter_non_byok_audit"]["pass"] is (
            expected_requests == 4 and not explicit_byok
        )
        assert row["openrouter_non_byok_audit"]["policy_safe_to_continue"] is (not explicit_byok)
        assert row["audit_status"]["status"] == (
            "pass" if expected_requests == 4 and not explicit_byok else "warning"
        )
        assert row["audit_status"]["separate_from_execution"] is True
    else:
        assert "openrouter_non_byok_audit" not in row


@pytest.mark.asyncio
async def test_provider_build_failure_preserves_already_billed_setup_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, inherited = _openrouter_config()
    setup_usage = {
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "input_tokens": 12,
        "output_tokens": 3,
        "billed_cost": 0.25,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.25, "setup-receipt-1"),
    }

    async def fake_build_experiment_provider(**_kwargs):
        raise runner.ProviderBuildError(
            RuntimeError("strict dynamic selection failed"),
            setup_latency_ms=123,
            setup_usage=[setup_usage],
            routing_trace={"task_analyzer": {"model": setup_usage["model"]}},
        )

    judge_calls = 0

    async def fake_judge_text(**_kwargs):
        nonlocal judge_calls
        judge_calls += 1
        return {"total": 10.0}

    monkeypatch.setattr(runner, "build_experiment_provider", fake_build_experiment_provider)
    monkeypatch.setattr(runner, "judge_text", fake_judge_text)

    row = await runner.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="G1",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=object(),
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=10.0,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={"tools_enabled": False, "tool_mode": "provider_only"},
        generation_policy={},
        require_openrouter_non_byok=True,
    )

    assert row["error"] == ("provider_build_failed_after_setup:RuntimeError")
    assert "strict dynamic selection failed" not in row["error"]
    assert row["selected_generation_succeeded"] is False
    assert row["llm_request_count"] == 0
    assert row["actual_spend_metrics"]["llm_request_count"] == 1
    assert row["generation_attempt_count"] == 1
    assert row["generation_attempt_budget_used"] == 1
    assert row["generation_attempt_evidence_schema"] == (runner.GENERATION_ATTEMPT_EVIDENCE_SCHEMA)
    assert row["selected_attempt_billed_cost_usd"] == 0.0
    assert row["actual_spend_billed_cost_usd"] == pytest.approx(0.25)
    assert row["execution"]["routing_setup_latency_ms"] == 123
    failed_attempt = row["execution"]["generation_attempts"][0]
    assert failed_attempt["attempt_kind"] == "provider_build_after_paid_setup"
    assert len(failed_attempt["attempt_id"]) == 32
    assert failed_attempt["run"]["llm_request_count"] == 1
    assert failed_attempt["run"]["usage"]["model_usage_breakdown"] == [setup_usage]
    assert row["usage"]["model_usage_breakdown"] == [setup_usage]
    assert runner.openrouter_non_byok_audit(row)["pass"] is True
    assert judge_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module",
    [runner, resume_runner],
    ids=["main", "resume"],
)
@pytest.mark.parametrize(
    "failure_stage",
    ["routing_serialization", "model_capabilities"],
)
async def test_run_one_post_build_failure_preserves_paid_setup_and_blocks_generation(
    module,
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, inherited = _openrouter_config()
    setup_usage = {
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "input_tokens": 12,
        "output_tokens": 3,
        "billed_cost": 0.25,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.25,
            f"post-build-{failure_stage}",
        ),
    }
    routing_trace = {
        "kind": "selection_mode",
        "model": "post-build-model",
    }

    async def fake_build_experiment_provider(**_kwargs):
        return module.ProviderBuildResult(
            provider=object(),
            prompt="built prompt",
            setup_latency_ms=123,
            setup_usage=[setup_usage],
            routing_trace=routing_trace,
        )

    collector_calls = 0

    async def forbidden_collect_generation(*_args, **_kwargs):
        nonlocal collector_calls
        collector_calls += 1
        raise AssertionError("post-build failure must block generation")

    monkeypatch.setattr(
        module,
        "build_experiment_provider",
        fake_build_experiment_provider,
    )
    monkeypatch.setattr(
        module,
        "collect_generation_with_retries",
        forbidden_collect_generation,
    )
    private_detail = "private post-build diagnostic must not persist"
    if failure_stage == "routing_serialization":
        original_json_safe = module.json_safe

        def fail_build_routing_only(value):
            if value is routing_trace:
                raise RuntimeError(private_detail)
            return original_json_safe(value)

        monkeypatch.setattr(module, "json_safe", fail_build_routing_only)
    else:
        original_capabilities = module.with_openrouter_model_capabilities

        def fail_post_build_capabilities(config_value, model):
            if model == "post-build-model":
                raise RuntimeError(private_detail)
            return original_capabilities(config_value, model)

        monkeypatch.setattr(
            module,
            "with_openrouter_model_capabilities",
            fail_post_build_capabilities,
        )

    row = await module.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="B1",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=object(),
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=10.0,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={
            "tools_enabled": False,
            "tool_mode": "provider_only",
        },
        generation_policy={},
    )

    assert collector_calls == 0
    assert row["execution"]["provider_error"] == ("provider_build_failed_after_setup:RuntimeError")
    assert private_detail not in json.dumps(row)
    assert row["generation_attempt_count"] == 1
    assert row["generation_attempt_budget_used"] == 1
    failed_attempt = row["execution"]["generation_attempts"][0]
    assert failed_attempt["attempt_kind"] == "provider_build_after_paid_setup"
    assert failed_attempt["run"]["usage"]["model_usage_breakdown"] == [setup_usage]
    assert row["actual_spend_metrics"]["llm_request_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module",
    [runner, _load_resume_runner()],
    ids=["main", "resume"],
)
@pytest.mark.parametrize(
    "failure_stage",
    [
        "requested_identity_backfill",
        "generation_retry_reason",
        "run_result_summary",
    ],
)
async def test_paid_generation_postprocess_failure_commits_once_and_blocks_more_paid_work(
    module,
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, inherited = _openrouter_config()
    model = str(module.GROUP_SPECS["B0"]["model"])
    usage_row = {
        "provider": "openrouter",
        "model": model,
        "requested_provider": "openrouter",
        "requested_model": model,
        "input_tokens": 7,
        "output_tokens": 2,
        "billed_cost": 0.02,
        "cost_source": "provider_billed",
        "physical_attempt_id": "d" * 32,
        "provider_usage": {
            **_openrouter_exact_evidence(
                0.02,
                f"postprocess-{module.__name__}-{failure_stage}",
            ),
            "physical_attempt_id": "d" * 32,
        },
    }
    paid_result = module.RunResult(
        final_text="answer that must not reach Judge",
        done=DoneEvent(
            provider="openrouter",
            model=model,
            requested_provider="openrouter",
            requested_model=model,
            input_tokens=7,
            output_tokens=2,
            billed_cost=0.02,
            cost_source="provider_billed",
            model_usage_breakdown=[usage_row],
        ),
    )

    async def fake_build_experiment_provider(**_kwargs):
        return module.ProviderBuildResult(
            provider=object(),
            prompt="built prompt",
            routing_trace={"kind": "single", "model": model},
        )

    paid_calls = 0

    async def fake_collect_run(*_args, **_kwargs):
        nonlocal paid_calls
        paid_calls += 1
        if paid_calls > 1:
            raise AssertionError("postprocessing failure must not trigger another paid call")
        return paid_result

    judge_calls = 0

    async def forbidden_judge(**_kwargs):
        nonlocal judge_calls
        judge_calls += 1
        raise AssertionError("postprocessing failure must not reach Judge")

    private_detail = "private paid postprocess diagnostic"
    monkeypatch.setattr(
        module,
        "build_experiment_provider",
        fake_build_experiment_provider,
    )
    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    monkeypatch.setattr(module, "judge_text", forbidden_judge)
    if failure_stage == "requested_identity_backfill":
        monkeypatch.setattr(
            module,
            "backfill_result_requested_identity",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(private_detail)),
        )
    elif failure_stage == "generation_retry_reason":
        monkeypatch.setattr(
            module,
            "generation_retry_reason",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(private_detail)),
        )
    else:
        monkeypatch.setattr(
            module,
            "run_result_summary",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(private_detail)),
        )

    row = await module.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="B0",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=object(),
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=10.0,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={
            "tools_enabled": False,
            "tool_mode": "provider_only",
        },
        generation_policy={},
        generation_max_attempts=3,
    )

    expected_reason = f"generation_postprocessing_failed:{failure_stage}:RuntimeError"
    assert paid_calls == 1
    assert judge_calls == 0
    assert row["error"] == expected_reason
    assert private_detail not in json.dumps(row)
    assert row["selected_generation_succeeded"] is False
    assert row["generation_attempt_count"] == 1
    assert row["selected_attempt_metrics"]["generation_attempt"] == 0
    assert row["execution"]["selected_generation_attempt"] == 0
    attempt = row["execution"]["generation_attempts"][0]
    assert attempt["attempt"] == 1
    assert len(attempt["attempt_id"]) == 32
    assert attempt["retryable"] is False
    assert attempt["retry_reason"] == expected_reason
    assert attempt["retry_suppressed_reason"] == expected_reason
    assert attempt["will_retry"] is False
    assert attempt["run"]["llm_request_count"] == 1
    assert len(attempt["run"]["usage"]["model_usage_breakdown"]) == 1
    assert attempt["generation_postprocessing_failure"] == {
        "stage": failure_stage,
        "exception_type": "RuntimeError",
    }
    terminal_evidence = resume_runner.generation_postprocessing_terminal_evidence(row)
    assert terminal_evidence is not None
    assert terminal_evidence["attempt_id"] == attempt["attempt_id"]
    assert terminal_evidence["automatic_generation_retry_allowed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module",
    [runner, _load_resume_runner()],
    ids=["main", "resume"],
)
async def test_terminal_generation_validation_failure_rewrites_existing_attempt_without_duplication(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, inherited = _openrouter_config()
    model = str(module.GROUP_SPECS["B0"]["model"])
    paid_result = module.RunResult(
        final_text="answer",
        done=DoneEvent(
            provider="openrouter",
            model=model,
            requested_provider="openrouter",
            requested_model=model,
            input_tokens=2,
            output_tokens=1,
            model_usage_breakdown=[
                {
                    "provider": "openrouter",
                    "model": model,
                    "requested_provider": "openrouter",
                    "requested_model": model,
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "physical_attempt_id": "e" * 32,
                    "provider_usage": {
                        "physical_attempt_id": "e" * 32,
                    },
                }
            ],
        ),
    )

    async def fake_build_experiment_provider(**_kwargs):
        return module.ProviderBuildResult(
            provider=object(),
            prompt="prompt",
            routing_trace={"kind": "single", "model": model},
        )

    paid_calls = 0

    async def fake_collect_run(*_args, **_kwargs):
        nonlocal paid_calls
        paid_calls += 1
        return paid_result

    reason_calls = 0

    def fail_second_validation(*_args, **_kwargs):
        nonlocal reason_calls
        reason_calls += 1
        if reason_calls == 1:
            return ""
        raise RuntimeError("private terminal validation detail")

    judge_calls = 0

    async def forbidden_judge(**_kwargs):
        nonlocal judge_calls
        judge_calls += 1
        raise AssertionError("terminal validation failure must block Judge")

    monkeypatch.setattr(
        module,
        "build_experiment_provider",
        fake_build_experiment_provider,
    )
    monkeypatch.setattr(module, "collect_run", fake_collect_run)
    monkeypatch.setattr(
        module,
        "generation_retry_reason",
        fail_second_validation,
    )
    monkeypatch.setattr(module, "judge_text", forbidden_judge)

    row = await module.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="B0",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=object(),
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=10.0,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={
            "tools_enabled": False,
            "tool_mode": "provider_only",
        },
        generation_policy={},
    )

    expected_reason = "generation_postprocessing_failed:terminal_generation_validation:RuntimeError"
    assert paid_calls == 1
    assert judge_calls == 0
    assert reason_calls == 2
    assert row["error"] == expected_reason
    assert row["generation_attempt_count"] == 1
    attempt = row["execution"]["generation_attempts"][0]
    assert attempt["retry_reason"] == expected_reason
    assert attempt["will_retry"] is False
    assert attempt["generation_postprocessing_failure"]["stage"] == "terminal_generation_validation"


@pytest.mark.parametrize(
    "module",
    [runner, _load_resume_runner()],
    ids=["main", "resume"],
)
def test_emergency_generation_summary_counts_setup_and_generation_once(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_id = "1" * 32
    analyzer_id = "2" * 32
    model = str(module.GROUP_SPECS["B0"]["model"])
    primary = {
        "role": "generation",
        "physical_attempt_id": primary_id,
        "provider": "openrouter",
        "model": model,
        "requested_provider": "openrouter",
        "requested_model": model,
        "input_tokens": 2,
        "output_tokens": 1,
        "provider_usage": {
            "physical_attempt_id": primary_id,
        },
    }
    analyzer = {
        "role": "task_analyzer",
        "label": "task_analyzer",
        "request_count": 1,
        "physical_attempt_id": analyzer_id,
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "requested_provider": "openrouter",
        "requested_model": "anthropic/claude-opus-4.8",
        "input_tokens": 3,
        "output_tokens": 1,
        "provider_usage": {
            "physical_attempt_id": analyzer_id,
        },
    }
    result = module.RunResult(
        final_text="answer",
        done=DoneEvent(
            provider="openrouter",
            model=model,
            requested_provider="openrouter",
            requested_model=model,
            model_usage_breakdown=[primary],
        ),
        setup_usage=[analyzer],
    )
    monkeypatch.setattr(
        module,
        "run_result_summary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("summary failed")),
    )

    summary = module.emergency_generation_run_summary(
        result,
        reason=("generation_postprocessing_failed:run_result_summary:RuntimeError"),
        stage="run_result_summary",
        exception_type="RuntimeError",
        identity_seed="generation-attempt:" + "3" * 32,
        expected_provider="openrouter",
        expected_model=model,
    )

    assert summary["llm_request_count"] == 2
    units = summary["usage"]["model_usage_breakdown"]
    assert len(units) == 2
    assert {str(unit.get("physical_attempt_id") or "") for unit in units} == {
        primary_id,
        analyzer_id,
    }


@pytest.mark.parametrize(
    "module",
    [runner, _load_resume_runner()],
    ids=["main", "resume"],
)
def test_paid_generation_recovery_survives_broken_summary_and_canonicalizer(
    module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = module.RunResult(
        final_text="",
        done=None,
        error="HTTP 503",
        trace_events=[
            {
                "kind": "error",
                "code": "503",
                "request_started": True,
                "physical_request_count": 1,
            }
        ],
    )
    monkeypatch.setattr(
        module,
        "run_result_summary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("summary boom")),
    )
    monkeypatch.setattr(
        module,
        "canonicalize_run_usage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("canonicalizer boom")),
    )
    monkeypatch.setattr(
        module,
        "json_safe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("serializer boom")),
    )
    pending = {
        "result": result,
        "attempts": [],
        "attempt_id": "4" * 32,
        "attempt_index": 1,
        "attempt_started_at": 1.0,
        "expected_provider": "openrouter",
        "expected_model": "model-a",
        "stage": "run_result_summary",
    }

    recovered, attempts, selected, reason = module.recover_paid_generation_postprocessing_failure(
        pending,
        RuntimeError("outer boom"),
    )

    assert recovered is result
    assert selected == 0
    assert reason == ("generation_postprocessing_failed:run_result_summary:RuntimeError")
    assert len(attempts) == 1
    run = attempts[0]["run"]
    assert run["llm_request_count"] == 1
    assert run["usage_unknown_count"] == 1
    assert (
        run["generation_postprocessing_failure"]["evidence_precision"]
        == "unvalidated_raw_plus_primitive_unknown"
    )
    units = run["usage"]["model_usage_breakdown"]
    assert len(units) == 1
    assert units[0]["role"] == "usage_missing"
    assert units[0]["requested_provider"] == "openrouter"
    assert units[0]["requested_model"] == "model-a"
    assert units[0]["usage_unknown"] is True
    assert run["trace_events"] == [
        {
            "kind": "error",
            "code": "trace_evidence_capture_failed",
            "exception_type": "RuntimeError",
        }
    ]


@pytest.mark.parametrize(
    "module",
    [runner, _load_resume_runner()],
    ids=["main", "resume"],
)
def test_provider_native_paid_postprocessing_recovery_preserves_plan_and_owner(
    module,
) -> None:
    selection_plan = {
        "strategy": "router_dynamic",
        "selected_P": [
            "openrouter:model-a",
            "openrouter:model-b",
        ],
        "backup_P": [
            "openrouter:model-c",
            "openrouter:model-d",
        ],
        "aggregator_candidates": ["openrouter:model-e"],
        "effective_min_successful_proposers": 2,
        "proposer_recovery_policy": deepcopy(module.FORMAL_PROPOSER_RECOVERY_POLICY),
    }
    result = module.RunResult(
        final_text="",
        done=None,
        error="HTTP 503",
        trace_events=[
            {
                "kind": "error",
                "code": "503",
                "request_started": True,
                "physical_request_count": 1,
            }
        ],
    )
    pending = {
        "result": result,
        "attempts": [],
        "attempt_id": "5" * 32,
        "attempt_index": 1,
        "attempt_started_at": 1.0,
        "selection_plan": selection_plan,
        "adaptive_g1": False,
        "provider_native_g1_recovery": True,
        "excluded_proposer_identities": (),
        "stage": "run_result_summary",
    }

    _, attempts, selected, _ = module.recover_paid_generation_postprocessing_failure(
        pending,
        RuntimeError("postprocessing failed"),
    )

    assert selected == 0
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["selection_plan"] == selection_plan
    assert attempt["proposer_recovery_owner"] == "provider"
    assert attempt["deterministic_proposer_failures"] == []
    assert attempt["excluded_proposer_identities"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
async def test_run_one_freezes_dataclass_routing_receipt(
    monkeypatch: pytest.MonkeyPatch,
    module,
) -> None:
    config, inherited = _openrouter_config()
    receipt = ProviderBillingReceipt(
        currency="USD",
        status="confirmed",
        amount_nanos=10_000_000,
        usd_equivalent_nanos=10_000_000,
        fx_native_per_usd_nanos=1_000_000_000,
    )
    model = str(module.GROUP_SPECS["B0"]["model"])
    usage_row = {
        "provider": "openrouter",
        "model": model,
        "requested_provider": "openrouter",
        "requested_model": model,
        "input_tokens": 3,
        "output_tokens": 1,
        "billed_cost": 0.01,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(
            0.01,
            f"dataclass-routing-{module.__name__}",
        ),
    }
    done = DoneEvent(
        input_tokens=3,
        output_tokens=1,
        billed_cost=0.01,
        cost_source="provider_billed",
        provider="openrouter",
        model=model,
        requested_provider="openrouter",
        requested_model=model,
        model_usage_breakdown=[usage_row],
    )
    result = module.RunResult(final_text="answer", done=done)
    attempts = [
        {
            "attempt_id": "1" * 32,
            "attempt_kind": "generation",
            "attempt": 1,
            "started_at": 1.0,
            "completed_at": 2.0,
            "retryable": False,
            "retry_reason": "",
            "will_retry": False,
            "retry_backoff_s": 0.0,
            "run": module.run_result_summary(result),
        }
    ]

    async def fake_build_experiment_provider(**_kwargs):
        return module.ProviderBuildResult(
            provider=object(),
            prompt="prompt",
            routing_trace={
                "kind": "single",
                "model": model,
                "billing_receipt": receipt,
            },
        )

    async def fake_collect_generation_with_retries(*_args, **_kwargs):
        return result, attempts, 1

    async def fake_judge_text(**_kwargs):
        return _complete_legacy_judge(f"dataclass-routing-judge-{module.__name__}")

    monkeypatch.setattr(
        module,
        "build_experiment_provider",
        fake_build_experiment_provider,
    )
    monkeypatch.setattr(
        module,
        "collect_generation_with_retries",
        fake_collect_generation_with_retries,
    )
    monkeypatch.setattr(module, "judge_text", fake_judge_text)

    row = await module.run_one(
        task={"id": "task-1", "prompt": "prompt"},
        group="B0",
        config=config,
        inherited=inherited,
        dry_run=False,
        judge_provider=object(),
        judge_candidates=False,
        judge_repeats=1,
        judge_concurrency=1,
        judge_max_attempts=1,
        judge_semaphore=None,
        timeout=10.0,
        ensemble_proposer_timeout=None,
        ensemble_aggregator_timeout=None,
        ensemble_proposer_early_stop_success_count=None,
        ensemble_proposer_early_stop_after=None,
        expand_ensemble_timeouts_to_task_timeout=False,
        tool_policy={"tools_enabled": False, "tool_mode": "provider_only"},
        generation_policy={},
    )

    assert row["selected_generation_succeeded"] is True
    assert row["execution"]["provider_error"] == ""
    assert row["routing_trace"]["billing_receipt"]["status"] == "confirmed"
    assert isinstance(row["routing_trace"]["billing_receipt"], dict)


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_openrouter_exact_receipt_requires_billed_and_reported_cost_match(module) -> None:
    usage = {
        "provider": "openrouter",
        "model": "model-a",
        "input_tokens": 3,
        "output_tokens": 1,
        "billed_cost": 0.0,
        "cost_source": "provider_billed",
        "provider_usage": _openrouter_exact_evidence(0.25, "mismatch-1"),
    }

    accounting = module.usage_cost_accounting(
        usage,
        expected_requests=1,
        scope="generation",
    )

    assert accounting["exact_request_count"] == 0
    assert accounting["unknown_request_count"] == 1
    assert accounting["cost_exact"] is False


def test_done_payload_keeps_provider_for_single_exact_receipt() -> None:
    done = runner.DoneEvent(
        provider="openrouter",
        model="model-a",
        requested_provider="openrouter",
        requested_model="requested-model-a",
        input_tokens=3,
        output_tokens=1,
        billed_cost=0.25,
        cost_source="provider_billed",
        provider_usage=_openrouter_exact_evidence(0.25, "single-1"),
    )

    payload = runner.done_payload(done)

    assert payload["provider"] == "openrouter"
    assert payload["model"] == "model-a"
    assert payload["requested_provider"] == "openrouter"
    assert payload["requested_model"] == "requested-model-a"
    assert (
        runner.usage_cost_accounting(
            payload,
            expected_requests=1,
            scope="generation",
        )["cost_exact"]
        is True
    )


def test_b2_provider_alignment_pins_effective_member_configuration() -> None:
    config, inherited = _openrouter_config()
    provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=None,
    )
    assert provider.min_successful_proposers == 3
    assert provider.quorum_grace_seconds == 10.0

    provider = runner.align_b2_provider_to_g12(provider, _experiment_config())

    assert provider.profile_name == "g12_k2_replace_gemini"
    assert [member.provider_config.model for member in provider.proposers] == [
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.7-code",
        "qwen/qwen3.7-max",
    ]
    assert provider.aggregator.provider_config.model == "z-ai/glm-5.2"
    assert provider.min_successful_proposers == 3
    assert provider.proposer_timeout_seconds == pytest.approx(907.5)
    assert provider.aggregator_timeout_seconds == pytest.approx(2662.5)
    assert provider.quorum_grace_seconds == 0.0
    assert provider.candidate_max_chars == 24_000
    assert provider.shuffle_candidates is False
    assert provider.record_candidates is True
    assert provider.proposer_tools is False
    assert provider.aggregator_tools is True
    assert provider.aggregator_recovery_mode == "experiment"
    assert provider.aggregator_recovery_top_k == 3
    assert provider.aggregator_max_tokens_cap == 65_536
    assert provider.aggregator_visible_answer_reserve_tokens == 8_192

    members = [*provider.proposers, provider.aggregator]
    assert all(member.max_tokens == 16_384 for member in members)
    assert all(member.temperature == 0.0 for member in members)
    assert all(member.k == 1 for member in members)
    assert [member.thinking for member in provider.proposers] == [
        "xhigh",
        "xhigh",
        "high",
        "high",
    ]
    base = ChatConfig(max_tokens=999, temperature=0.9, thinking=False)
    effective = [_member_chat_config(base, member) for member in members]
    assert all(config.max_tokens == 16_384 for config in effective)
    assert all(config.temperature == 0.0 for config in effective)
    assert all(config.thinking is True for config in effective)
    assert [config.thinking_level for config in effective] == [
        "xhigh",
        "xhigh",
        "high",
        "high",
        "xhigh",
    ]

    plan = provider.selection_plan
    assert plan["benchmark_alignment"]["id"] == "opensquilla_b2_quality_first_v2"
    assert plan["pre_alignment"]["min_successful_proposers"] == 3
    assert plan["pre_alignment"]["selection_plan"]["profile"] == "static_openrouter_b5"
    assert plan["proposer_models"] == [
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.7-code",
        "qwen/qwen3.7-max",
    ]
    assert plan["aggregator_model"] == "z-ai/glm-5.2"
    assert plan["proposer_count"] == 4
    assert plan["proposer_sample_count"] == 4
    assert plan["selected_P"] == [
        "openrouter:deepseek/deepseek-v4-pro",
        "openrouter:z-ai/glm-5.2",
        "openrouter:moonshotai/kimi-k2.7-code",
        "openrouter:qwen/qwen3.7-max",
    ]
    assert plan["selected_A"] == "openrouter:z-ai/glm-5.2"
    assert plan["wait_for_all_proposers"] is True
    assert plan["member_generation"][2]["model"] == "moonshotai/kimi-k2.7-code"
    assert plan["member_generation"][2]["max_tokens"] == 16_384
    assert plan["member_generation"][2]["thinking"] == "high"
    assert plan["proposer_tools"] is False
    assert plan["aggregator_tools"] is True
    assert plan["aggregator_recovery_mode"] == "experiment"
    assert plan["aggregator_recovery_top_k"] == 3
    assert plan["aggregator_max_tokens_cap"] == 65_536
    assert plan["aggregator_visible_answer_reserve_tokens"] == 8_192

    provider = runner.enforce_draco_legal_proposer_quorum(provider)
    assert provider.min_successful_proposers == 3
    assert provider.selection_plan["effective_min_successful_proposers"] == 3
    assert provider.selection_plan["legal_min_successful_proposers"] == 3


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_provider_native_formal_quorum_stays_two_for_five_proposers(
    module,
) -> None:
    class Member:
        k = 1

    class Provider:
        proposers = [Member() for _ in range(5)]
        min_successful_proposers = 4
        selection_plan = {
            "proposer_recovery_policy": deepcopy(module.FORMAL_PROPOSER_RECOVERY_POLICY),
            "configured_min_successful_proposers": 4,
        }

    provider = module.enforce_draco_legal_proposer_quorum(Provider())

    assert provider.min_successful_proposers == 2
    assert provider.selection_plan["effective_min_successful_proposers"] == 2
    assert provider.selection_plan["legal_min_successful_proposers"] == 2
    assert provider.selection_plan["legal_quorum_policy"] == "fixed_2_provider_native"

    legacy = Provider()
    legacy.selection_plan = {}
    legacy.min_successful_proposers = 4
    legacy = module.enforce_draco_legal_proposer_quorum(legacy)
    assert legacy.min_successful_proposers == 4


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
@pytest.mark.parametrize(
    "policy_quorum",
    [0, True, "2", 3],
    ids=["zero", "bool", "string", "mismatch"],
)
def test_proposer_recovery_policy_quorum_mismatch_fails_before_mutation(
    module,
    policy_quorum: object,
) -> None:
    class Member:
        k = 1

    class Provider:
        pass

    policy = deepcopy(module.FORMAL_PROPOSER_RECOVERY_POLICY)
    policy["quorum_required"] = policy_quorum
    provider = Provider()
    provider.proposers = [Member() for _ in range(5)]
    provider.min_successful_proposers = 4
    provider.selection_plan = {
        "proposer_recovery_policy": policy,
        "configured_min_successful_proposers": 4,
        "sentinel": {"unchanged": True},
    }
    original_plan = provider.selection_plan
    original_snapshot = deepcopy(original_plan)

    with pytest.raises(
        ValueError,
        match=r"proposer_recovery_policy\.quorum_required",
    ):
        module.enforce_draco_legal_proposer_quorum(provider)

    assert provider.min_successful_proposers == 4
    assert provider.selection_plan is original_plan
    assert provider.selection_plan == original_snapshot


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_enforced_proposer_quorum_above_sample_count_fails_before_mutation(
    module,
) -> None:
    class Member:
        k = 1

    class Provider:
        pass

    provider = Provider()
    provider.proposers = [Member()]
    provider.min_successful_proposers = 1
    provider.selection_plan = {
        "proposer_recovery_policy": deepcopy(module.FORMAL_PROPOSER_RECOVERY_POLICY),
        "configured_min_successful_proposers": 1,
        "sentinel": {"unchanged": True},
    }
    original_plan = provider.selection_plan
    original_snapshot = deepcopy(original_plan)

    with pytest.raises(
        ValueError,
        match="enforced proposer quorum 2 exceeds proposer sample count 1",
    ):
        module.enforce_draco_legal_proposer_quorum(provider)

    assert provider.min_successful_proposers == 1
    assert provider.selection_plan is original_plan
    assert provider.selection_plan == original_snapshot


@pytest.mark.parametrize("module", [runner, _load_resume_runner()], ids=["main", "resume"])
def test_formal_g1_requires_legacy_proposer_backup_count_to_match_ranking(
    module,
) -> None:
    with pytest.raises(
        ValueError,
        match="legacy compatibility input.*must match",
    ):
        module.load_draco_experiment_config(
            module.DEFAULT_B2_EXPERIMENT_CONFIG_PATH,
            inline_overlay_json='{"ensemble":{"proposer_backup_count":1}}',
        )

    experiment = module.load_draco_experiment_config(
        module.DEFAULT_B2_EXPERIMENT_CONFIG_PATH,
        inline_overlay_json=(
            '{"ensemble":{"proposer_backup_count":1},'
            '"router_dynamic_ranking_override":'
            '{"proposer_count":{"backup_count":1}}}'
        ),
    ).config
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "model-a",
            "api_key": "fake",
        }
    )

    freeze = module.enforce_formal_draco_runtime_config(
        config,
        experiment,
        ["G1"],
    )

    assert experiment.ensemble.proposer_backup_count == 1
    assert config.llm_ensemble.proposer_backup_count == 1
    assert freeze["proposer_backup_count"] == 1


def test_b2_provider_alignment_rebuilds_trace_after_lineup_override() -> None:
    config, inherited = _openrouter_config()
    provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=None,
    )
    experiment = runner.load_draco_experiment_config(
        runner.DEFAULT_B2_EXPERIMENT_CONFIG_PATH,
        inline_sets=[
            'ensemble.proposers.0.model="openai/gpt-5.5-pro"',
            'ensemble.proposers.0.thinking="xhigh"',
            'ensemble.aggregator.model="deepseek/deepseek-v4-pro"',
            'ensemble.aggregator.thinking="xhigh"',
        ],
    ).config

    provider = runner.align_b2_provider_to_g12(provider, experiment)

    assert provider.proposers[0].provider_config.model == "openai/gpt-5.5-pro"
    assert provider.aggregator.provider_config.model == "deepseek/deepseek-v4-pro"
    assert provider.selection_plan["proposer_models"][0] == "openai/gpt-5.5-pro"
    assert provider.selection_plan["aggregator_model"] == "deepseek/deepseek-v4-pro"
    assert provider.selection_plan["selected_P"][0] == "openrouter:openai/gpt-5.5-pro"
    assert provider.selection_plan["selected_A"] == ("openrouter:deepseek/deepseek-v4-pro")
    assert (
        provider.selection_plan["pre_alignment"]["selection_plan"]["aggregator_model"]
        == "z-ai/glm-5.2"
    )


@pytest.mark.asyncio
async def test_b2_quality_first_build_skips_single_model_router(monkeypatch) -> None:
    config, inherited = _openrouter_config()

    async def _unexpected_router(*_args, **_kwargs):
        raise AssertionError("fixed B2 must not run SquillaRouter")

    monkeypatch.setattr(runner, "run_pipeline", _unexpected_router)
    result = await runner.build_experiment_provider(
        config=config,
        inherited=inherited,
        group="B2",
        prompt="test prompt",
        dry_run=False,
        enable_proposer_tools=True,
        ensemble_proposer_timeout=1.0,
        ensemble_aggregator_timeout=2.0,
        experiment_config=_experiment_config(),
    )

    assert result.routing_trace["routing_applied"] is False
    assert result.routing_trace["routing_source"] == "b2_quality_first_profile"
    assert result.routing_trace["selection_plan"]["wait_for_all_proposers"] is True
    assert result.provider.proposer_tools is False
    assert result.provider.aggregator_tools is True
    assert result.provider.proposer_timeout_seconds == pytest.approx(907.5)
    assert result.provider.aggregator_timeout_seconds == pytest.approx(2662.5)


@pytest.mark.asyncio
async def test_b2_dry_build_records_canonical_selection_plan() -> None:
    config, inherited = _openrouter_config()

    result = await runner.build_experiment_provider(
        config=config,
        inherited=inherited,
        group="B2",
        prompt="test prompt",
        dry_run=True,
        enable_proposer_tools=True,
        ensemble_proposer_timeout=1.0,
        ensemble_aggregator_timeout=2.0,
        experiment_config=_experiment_config(),
    )

    plan = result.routing_trace["selection_plan"]
    assert plan["proposer_models"] == [
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
        "moonshotai/kimi-k2.7-code",
        "qwen/qwen3.7-max",
    ]
    assert plan["aggregator_model"] == "z-ai/glm-5.2"
    assert plan["selected_P"][2] == "openrouter:moonshotai/kimi-k2.7-code"
    assert plan["selected_A"] == "openrouter:z-ai/glm-5.2"
    assert plan["proposer_count"] == 4
    assert plan["proposer_sample_count"] == 4
    assert plan["member_generation"][-1]["role"] == "aggregator"
    assert plan["member_generation"][-1]["thinking"] == "xhigh"
    assert plan["aggregator_recovery_mode"] == "experiment"
    assert plan["aggregator_recovery_top_k"] == 3
    assert plan["aggregator_max_tokens_cap"] == 65_536
    assert plan["aggregator_visible_answer_reserve_tokens"] == 8_192
    assert plan["aggregator_candidates"] == ["openrouter:z-ai/glm-5.2"]
    assert "aggregator_recovery" not in result.routing_trace
    assert result.routing_trace["aggregator_recovery_policy"] == {
        "schema": "opensquilla.ensemble-aggregator-recovery-policy/v1",
        "evidence_kind": "dry_run_policy_only",
        "aggregator_recovery_mode": "experiment",
        "aggregator_recovery_top_k": 3,
        "aggregator_max_tokens_cap": 65_536,
        "aggregator_visible_answer_reserve_tokens": 8_192,
    }

    events = [
        event
        async for event in result.provider.chat([runner.Message(role="user", content="dry prompt")])
    ]
    trace = events[-1].ensemble_trace
    recovery = trace["aggregator_recovery"]
    assert recovery["schema"] == "opensquilla.ensemble-aggregator-recovery/v1"
    assert recovery["mode"] == "experiment"
    assert recovery["candidate_ids"] == ["openrouter:z-ai/glm-5.2"]
    assert recovery["candidate_count"] == 1
    assert recovery["success"] is True
    assert recovery["degraded"] is False
    assert recovery["selected_attempt"] == 1
    assert recovery["selected_kind"] == "primary"
    assert recovery["fallback_index"] == 0
    assert recovery["executed_A"] == "openrouter:z-ai/glm-5.2"
    assert recovery["attempts"] == [
        {
            **recovery["attempts"][0],
            "attempt": 1,
            "physical_attempt_index": 1,
            "request_started": True,
            "outcome": "succeeded",
        }
    ]
    assert trace["run_outcome"] == "success"
    assert trace["delivery_outcome"] == "complete"
    assert trace["llm_request_count"] == 5
    assert trace["physical_request_count"] == 5


def test_manifest_records_effective_and_requested_b2_alignment(tmp_path: Path) -> None:
    args = runner.build_parser().parse_args(
        ["--input", "tasks.jsonl", "--groups", "B2", "--concurrency", "8"]
    )
    runner.apply_b2_g12_argument_alignment(args, ["B2"])
    path = tmp_path / "manifest.json"

    runner.write_manifest(
        path,
        args=args,
        stamp="test",
        status="running",
        started_at=1.0,
        tasks=[{"id": "task-1"}],
        groups=["B2"],
        artifacts={},
        tool_policy={"tool_mode": "local_web_tools", "tools_enabled": True},
    )

    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["runner"] == "scripts/run_draco_routing_experiment.py"
    assert manifest["args"]["concurrency"] == 2
    assert manifest["agent_finalization_policy"] == {
        "deadline_wrapup_margin_seconds": 300,
        "deadline_wrapup_disable_tools": True,
        "deadline_thinking_off_margin_seconds": 0,
        "max_iterations_includes_finalization": False,
        "retrieval_loop_finalization_threshold": 0,
        "finalization_aggregator_only": False,
        "finalization_disable_thinking": False,
    }
    alignment = manifest["benchmark_alignments"]["B2"]
    assert alignment["requested_args"]["concurrency"] == 8
    assert alignment["effective_args"]["concurrency"] == 2
    assert "effective_config" not in alignment
    assert "reference" not in alignment
    assert alignment["effective_config_sha256"].startswith("sha256:")
    assert alignment["reference_sha256"].startswith("sha256:")
    assert len(manifest["source_provenance"]["runner_sha256"]) == 64
    assert "git_head" in manifest["source_provenance"]


def test_markdown_records_agent_finalization_policy(tmp_path: Path) -> None:
    policy = {
        **runner.DEFAULT_AGENT_FINALIZATION_POLICY,
        "deadline_wrapup_margin_seconds": 600,
        "finalization_disable_thinking": True,
    }

    markdown = runner.render_markdown(
        {"groups": {}},
        tmp_path / "draco_ensemble_test.jsonl",
        agent_finalization_policy=policy,
    )

    assert "Agent finalization policy:" in markdown
    assert '"deadline_wrapup_margin_seconds": 600' in markdown
    assert '"finalization_disable_thinking": true' in markdown
    assert "Avg Selected LLM $" in markdown
    assert "Avg Actual LLM $" in markdown
    assert "selected columns contain only the accepted generation attempt" in markdown
    assert "actual-spend columns contain every generation attempt" in markdown


def test_manifest_reuses_source_provenance_frozen_at_process_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = runner.build_parser().parse_args(["--input", "tasks.jsonl", "--groups", "B1"])
    args._source_provenance = {
        "runner_path": "/frozen/runner.py",
        "runner_sha256": "a" * 64,
        "git_head": "b" * 40,
        "git_dirty": False,
        "source_tree_sha256": "c" * 64,
    }
    monkeypatch.setattr(
        runner,
        "source_provenance",
        lambda: {"runner_sha256": "changed-after-start"},
    )
    path = tmp_path / "manifest.json"

    runner.write_manifest(
        path,
        args=args,
        stamp="test",
        status="complete",
        started_at=1.0,
        tasks=[{"id": "task-1"}],
        groups=["B1"],
        artifacts={},
    )

    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["source_provenance"] == args._source_provenance


def test_source_provenance_detects_tracked_changes_against_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            stdout = "a" * 40 + "\n"
        elif command[:3] == ["git", "diff", "--binary"]:
            stdout = "tracked diff\n"
        else:
            stdout = ""
        return runner.subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    provenance = runner.source_provenance()

    diff_command = next(command for command in commands if command[1] == "diff")
    assert diff_command.count("HEAD") == 1
    assert provenance["git_tracked_dirty"] is True
    assert provenance["git_dirty"] is True


def test_experiment_config_artifacts_publish_only_private_effective_and_safe_resolution(
    tmp_path: Path,
) -> None:
    override_path = tmp_path / "override.json"
    override_path.write_text('{"judge":{"repeats":2}}\n', encoding="utf-8")
    args = runner.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B2",
            "--experiment-config-override",
            str(override_path),
            "--experiment-config-set",
            "runner.concurrency=4",
        ]
    )
    runner.apply_b2_g12_argument_alignment(args, ["B2"])
    args._draco_input_validation = {"status": "matched"}

    artifacts = runner.write_experiment_config_artifacts(
        tmp_path,
        args=args,
        stamp="test",
    )

    assert set(artifacts) == {
        "experiment_config_effective_json",
        "experiment_config_resolution_json",
    }
    effective_path = Path(artifacts["experiment_config_effective_json"])
    resolution_path = Path(artifacts["experiment_config_resolution_json"])
    effective = json.loads(effective_path.read_text(encoding="utf-8"))
    resolution = json.loads(
        resolution_path.read_text(encoding="utf-8")
    )
    assert effective["runner"]["concurrency"] == 4
    assert effective["judge"]["repeats"] == 2
    assert effective_path.stat().st_mode & 0o777 == 0o600
    assert resolution_path.stat().st_mode & 0o777 == 0o600
    assert args._effective_experiment_config_path == effective_path.resolve()
    inline = resolution["provenance"]["inline_overrides"]
    assert inline == {"count": 1, "paths": ["runner.concurrency"]}
    assert hashlib.sha256(b"4").hexdigest() not in resolution_path.read_text(
        encoding="utf-8"
    )
    assert resolution["input_validation"]["status"] == "matched"
    assert resolution["artifact_keys"] == sorted(artifacts)
    assert resolution["replay_validation"] == runner.gateway_replay_validation_contract()
    assert not list(tmp_path.glob("*.experiment-config.base.json"))
    assert not list(tmp_path.glob("*.experiment-config.override-*.json"))
    assert not list(tmp_path.glob("*.experiment-config.inline-*.json"))


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_experiment_config_artifacts_refuse_to_overwrite_existing_path(
    module,
    tmp_path: Path,
) -> None:
    args = module.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B2",
        ]
    )
    module.apply_b2_g12_argument_alignment(args, ["B2"])
    stamp = "collision"
    effective_path = (
        tmp_path / f"draco_run_{stamp}.experiment-config.effective.json"
    )
    sentinel = b"preexisting-sentinel\n"
    effective_path.write_bytes(sentinel)

    with pytest.raises(FileExistsError):
        module.write_experiment_config_artifacts(
            tmp_path,
            args=args,
            stamp=stamp,
        )

    assert effective_path.read_bytes() == sentinel
    assert not (tmp_path / f"draco_run_{stamp}.experiment-config.resolution.json").exists()


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_experiment_config_artifacts_roll_back_when_later_publish_fails(
    module,
    tmp_path: Path,
) -> None:
    args = module.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B2",
        ]
    )
    module.apply_b2_g12_argument_alignment(args, ["B2"])
    stamp = "rollback"
    effective_path = (
        tmp_path / f"draco_run_{stamp}.experiment-config.effective.json"
    )
    resolution_path = (
        tmp_path / f"draco_run_{stamp}.experiment-config.resolution.json"
    )
    sentinel = b"preexisting-resolution\n"
    resolution_path.write_bytes(sentinel)

    with pytest.raises(FileExistsError):
        module.write_experiment_config_artifacts(
            tmp_path,
            args=args,
            stamp=stamp,
        )

    assert not effective_path.exists()
    assert resolution_path.read_bytes() == sentinel
    assert not list(tmp_path.glob(".*.tmp"))
    assert not hasattr(args, "_effective_experiment_config_path")


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_recorded_command_replays_private_effective_config_without_overlay_values(
    module,
    tmp_path: Path,
) -> None:
    inline_marker = "inline-secret-marker"
    dotted_marker = "dotted-secret-marker"
    args = module.build_parser().parse_args(
        [
            "--input",
            "tasks.jsonl",
            "--groups",
            "B2",
            "--experiment-config-override-json",
            json.dumps(
                {
                    "reference": {"repository": inline_marker},
                    "generation": {"max_attempts": 2},
                }
            ),
            "--experiment-config-set",
            f'reference.run_directory="{dotted_marker}"',
            "--experiment-config-set",
            "runner.concurrency=1",
        ]
    )
    module.apply_b2_g12_argument_alignment(args, ["B2"])
    original_effective = args._draco_experiment_config_bundle.config.model_dump(mode="json")
    args._source_provenance = {
        "git_head": "a" * 40,
        "source_tree_sha256": "b" * 64,
    }

    def compatibility(current_args: object) -> dict[str, object]:
        config = GatewayConfig(
            llm={
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-pro",
                "api_key": "fake",
            }
        )
        current_args._formal_runtime_freeze = {}
        policy = module.benchmark_tool_policy(current_args)
        return module.build_run_compatibility(
            args=current_args,
            config=config,
            groups=["B2"],
            group_tool_policies=module.benchmark_tool_policies_for_groups(
                policy,
                ["B2"],
                args=current_args,
            ),
            generation_policy=module.generation_thinking_policy(current_args),
        )

    original_compatibility = compatibility(args)
    args._run_compatibility = original_compatibility
    artifacts = module.write_experiment_config_artifacts(
        tmp_path,
        args=args,
        stamp=module.__name__.rsplit("_", 1)[-1],
    )
    command_path = tmp_path / f"{module.__name__}.command.txt"
    command = module.write_command_file(command_path, args=args, stamp="test")
    manifest_path = tmp_path / f"{module.__name__}.manifest.json"
    module.write_manifest(
        manifest_path,
        args=args,
        stamp="test",
        status="complete",
        started_at=1.0,
        tasks=[{"id": "task-1"}],
        groups=["B2"],
        artifacts=artifacts,
        command=command,
    )

    effective_path = Path(artifacts["experiment_config_effective_json"])
    assert effective_path.stat().st_mode & 0o777 == 0o600
    assert inline_marker in effective_path.read_text(encoding="utf-8")
    assert dotted_marker in effective_path.read_text(encoding="utf-8")
    public_payload = "\n".join(
        [
            command_path.read_text(encoding="utf-8"),
            manifest_path.read_text(encoding="utf-8"),
            Path(artifacts["experiment_config_resolution_json"]).read_text(
                encoding="utf-8"
            ),
            json.dumps(args._benchmark_alignments, ensure_ascii=False),
        ]
    )
    assert inline_marker not in public_payload
    assert dotted_marker not in public_payload
    assert args._draco_experiment_config_bundle.inline_overlay_sha256 not in public_payload
    assert _canonical_digest(dotted_marker) not in public_payload
    assert "--experiment-config-override-json" not in command["argv"]
    assert "--experiment-config-set" not in command["argv"]
    assert command["parsed_args"]["experiment_config_override"] == []
    assert command["parsed_args"]["experiment_config_set"] == []
    assert command["parsed_args"]["experiment_config"] == str(effective_path.resolve())
    assert command["replay_validation"] == module.gateway_replay_validation_contract()
    assert "# Replay validation" in command_path.read_text(encoding="utf-8")

    replayed_args = module.build_parser().parse_args(command["argv"][2:])
    module.apply_b2_g12_argument_alignment(replayed_args, ["B2"])
    replayed_args._source_provenance = dict(args._source_provenance)
    replayed_effective = (
        replayed_args._draco_experiment_config_bundle.config.model_dump(mode="json")
    )
    replayed_compatibility = compatibility(replayed_args)

    assert replayed_effective == original_effective
    assert replayed_compatibility["fingerprints"] == original_compatibility["fingerprints"]


@pytest.mark.parametrize(
    "module",
    [runner, resume_runner],
    ids=["main", "resume"],
)
def test_expanded_proposer_slot_identities_supports_k21_and_fails_closed(
    module,
) -> None:
    plan = _k21_router_dynamic_plan()

    assert module.expanded_proposer_slot_identities(plan) == (
        "openrouter:p0",
        "openrouter:p0",
        "openrouter:p1",
    )

    noncontiguous = deepcopy(plan)
    noncontiguous["proposer_models"] = ["p0", "p1", "p0"]
    assert module.expanded_proposer_slot_identities(noncontiguous) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module",
    [runner, resume_runner],
    ids=["main", "resume"],
)
async def test_dry_ensemble_emits_expanded_k21_proposer_identity_roster(
    module,
) -> None:
    plan = _k21_router_dynamic_plan()
    provider = module.DryEnsembleProvider(
        group="G1",
        profile="router_dynamic/c2",
        proposer_models=list(plan["proposer_models"]),
        model="agg",
        selection_mode="router_dynamic",
    )
    provider.selection_plan = deepcopy(plan)

    events = [event async for event in provider.chat([module.Message(role="user", content="task")])]
    done = next(event for event in events if isinstance(event, DoneEvent))
    trace = done.ensemble_trace
    assert isinstance(trace, dict)
    candidates = trace["candidates"]
    assert [
        (
            candidate["requested_provider"],
            candidate["requested_model"],
            candidate["sample_index"],
        )
        for candidate in candidates
    ] == [
        ("openrouter", "p0", 0),
        ("openrouter", "p0", 1),
        ("openrouter", "p1", 0),
    ]
    recovery = trace["proposer_recovery"]
    assert recovery["executed_proposer_roster_before"] == [
        "openrouter:p0",
        "openrouter:p0",
        "openrouter:p1",
    ]
    assert recovery["executed_proposer_roster_after"] == [
        "openrouter:p0",
        "openrouter:p0",
        "openrouter:p1",
    ]


@pytest.mark.parametrize(
    "module",
    [runner, resume_runner],
    ids=["main", "resume"],
)
def test_ensemble_call_core_audits_expanded_k21_candidate_slots(
    module,
) -> None:
    plan = _k21_router_dynamic_plan()
    trace = _k21_ensemble_trace(plan)

    assert (
        module.ensemble_call_core_reasons(
            trace,
            expected_selection_mode="router_dynamic",
            expected_selection_plan=plan,
        )
        == []
    )

    # Post-call validation is roster based.  A frozen proposer or backup may
    # occupy a different runtime slot after provider-native recovery.
    moved_selected = deepcopy(trace)
    moved_selected["candidates"][1].update(
        {
            "provider": "openrouter",
            "model": "p1",
            "requested_provider": "openrouter",
            "requested_model": "p1",
        }
    )
    assert module.ensemble_call_core_reasons(
        moved_selected,
        expected_selection_mode="router_dynamic",
        expected_selection_plan=plan,
    ) == []

    recovered_backup = deepcopy(trace)
    recovered_backup["candidates"][1].update(
        {
            "provider": "openrouter",
            "model": "b0",
            "requested_provider": "openrouter",
            "requested_model": "b0",
        }
    )
    recovered_backup["proposer_recovery"] = {
        "schema": "opensquilla.router-dynamic-proposer-recovery/v1",
        "executed_proposer_roster_before": [
            "openrouter:p0",
            "openrouter:p0",
            "openrouter:p1",
        ],
        "executed_proposer_roster_after": [
            "openrouter:p0",
            "openrouter:b0",
            "openrouter:p1",
        ],
        "attempts": [
            {
                "sequence": 1,
                "slot_index": 1,
                "kind": "backup_replacement",
                "source_identity": "openrouter:p0",
                "target_identity": "openrouter:b0",
                "reason": "frozen_backup_order",
                "request_started": True,
                "physical_request_count": 1,
                "physical_attempt_id": "a" * 32,
                "stream_closed": True,
                "usage_reported": True,
                "usage_missing_count": 0,
                "outcome": "succeeded",
            }
        ],
    }
    assert module.ensemble_call_core_reasons(
        recovered_backup,
        expected_selection_mode="router_dynamic",
        expected_selection_plan=plan,
    ) == []
    result = module.RunResult(
        final_text="answer",
        done=module.DoneEvent(ensemble_trace=recovered_backup),
    )
    assert module.ensemble_generation_retry_reason(
        result,
        expected_selection_mode="router_dynamic",
        expected_selection_plan=plan,
    ) == ""
    assert result.audit_warnings == []

    outside_roster = deepcopy(trace)
    outside_roster["candidates"][1].update(
        {
            "provider": "openrouter",
            "model": "outside-roster",
            "requested_provider": "openrouter",
            "requested_model": "outside-roster",
        }
    )
    audit_reasons = module.ensemble_call_core_reasons(
        outside_roster,
        expected_selection_mode="router_dynamic",
        expected_selection_plan=plan,
    )
    assert "wrong_requested_proposer_identity" in audit_reasons
    assert "wrong_actual_proposer_identity" in audit_reasons
    result = module.RunResult(
        final_text="answer",
        done=module.DoneEvent(ensemble_trace=outside_roster),
    )
    assert module.ensemble_generation_retry_reason(
        result,
        expected_selection_mode="router_dynamic",
        expected_selection_plan=plan,
    ) == ""
    assert "wrong_requested_proposer_identity" in result.audit_warnings
    assert "wrong_actual_proposer_identity" in result.audit_warnings


def test_resume_completion_accepts_k21_plan_and_rejects_ambiguous_slots() -> None:
    plan = _k21_router_dynamic_plan()
    trace = _k21_ensemble_trace(plan)
    row = {
        "group": "G1",
        "provider_spec": dict(resume_runner.GROUP_SPECS["G1"]),
        "routing_trace": {"selection_plan": deepcopy(plan)},
        "final_text": "answer",
        "ensemble_trace": {
            "calls": [trace],
            "agent_llm_call_count": 1,
        },
    }

    assert resume_runner.ensemble_generation_completion_reasons(row) == []

    ambiguous = deepcopy(row)
    ambiguous_plan = ambiguous["routing_trace"]["selection_plan"]
    ambiguous_plan["proposer_models"] = ["p0", "p1", "p0"]
    ambiguous["ensemble_trace"]["calls"][0]["selection_plan"] = deepcopy(ambiguous_plan)
    reasons = resume_runner.ensemble_generation_completion_reasons(ambiguous)
    assert "invalid_expected_selection_plan" in reasons
    assert "invalid_expected_proposer_slot_roster" in reasons


@pytest.mark.parametrize(
    "module",
    [runner, resume_runner],
    ids=["main", "resume"],
)
def test_requested_identity_backfill_uses_expanded_k21_slots(module) -> None:
    plan = _k21_router_dynamic_plan()
    trace = _k21_ensemble_trace(
        plan,
        include_requested_identity=False,
    )
    done = module.DoneEvent(
        model="agg",
        provider="openrouter",
        requested_model="agg",
        requested_provider="openrouter",
        ensemble_trace={"calls": [trace]},
    )
    result = module.RunResult(final_text="answer", done=done)

    assert module.backfill_result_requested_identity(
        result,
        expected_model="agg",
        expected_provider="openrouter",
        expected_selection_plan=plan,
    )
    assert [
        (
            candidate["requested_provider"],
            candidate["requested_model"],
        )
        for candidate in trace["candidates"]
    ] == [
        ("openrouter", "p0"),
        ("openrouter", "p0"),
        ("openrouter", "p1"),
    ]

    malformed_plan = deepcopy(plan)
    malformed_plan["proposer_models"] = ["p0", "p1", "p0"]
    malformed_trace = _k21_ensemble_trace(
        malformed_plan,
        include_requested_identity=False,
    )
    malformed_result = module.RunResult(
        final_text="answer",
        done=module.DoneEvent(
            model="agg",
            provider="openrouter",
            requested_model="agg",
            requested_provider="openrouter",
            ensemble_trace={"calls": [malformed_trace]},
        ),
    )
    assert not module.backfill_result_requested_identity(
        malformed_result,
        expected_model="agg",
        expected_provider="openrouter",
        expected_selection_plan=malformed_plan,
    )


@pytest.mark.parametrize(
    "module",
    [runner, resume_runner],
    ids=["main", "resume"],
)
def test_g1_registry_audit_distinguishes_member_and_sample_counts(
    module,
) -> None:
    from opensquilla.provider.ranking_router import (
        load_model_registry_snapshot,
        ranking_config_snapshot,
    )

    registry = load_model_registry_snapshot()
    ranking = ranking_config_snapshot(thinking_assignment_enabled=True)

    def _sha256(value) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    routes = {
        str(row["registry_facts"]["model_id"]).strip().lower(): "auto" for row in registry["models"]
    }
    contract = {
        "profile_id": "test-k21-registry",
        "selection_mode": "router_dynamic",
        "candidate_scope": "registry_all",
        "policy": "all_registry_models",
        "user_profile_enabled": False,
        "source_registry_snapshot_version": registry["snapshot_version"],
        "expected_source_registry_snapshot_sha256": _sha256(registry),
        "expected_ranking_config_schema_version": ranking["schema_version"],
        "expected_ranking_config_version": ranking["config_version"],
        "expected_ranking_config_sha256": _sha256(ranking),
        "expected_proposer_count_max": 5,
        "expected_candidate_count": len(routes),
        "expected_routes_sha256": _sha256(routes),
        "expected_routes": routes,
    }
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
            "provider_routing": routes,
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "ranking_thinking_assignment_enabled": True,
        },
    )
    provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=ProviderConfig(
            provider="openrouter",
            model="deepseek/deepseek-v4-pro",
            api_key="fake",
            provider_routing=routes,
        ),
        fallback_provider=None,
        turn_metadata={
            "routed_tier": "c1",
            "routing_confidence": 0.9,
        },
        ranking_inputs={
            "registry_allowlist": contract,
        },
    )
    plan = deepcopy(provider.selection_plan)
    proposer_models = list(plan["proposer_models"])
    proposer_models.insert(1, proposer_models[0])
    plan["proposer_models"] = proposer_models
    plan["proposer_sample_count"] = len(proposer_models)

    reasons = module.g1_registry_contract_reasons(
        {"selection_plan": plan},
        contract,
    )
    assert "invalid_g1_expanded_proposer_roster" not in reasons
    assert "wrong_g1_selected_proposer_count" not in reasons

    noncontiguous = deepcopy(plan)
    noncontiguous_models = list(noncontiguous["proposer_models"])
    noncontiguous_models[1:3] = [
        noncontiguous_models[2],
        noncontiguous_models[1],
    ]
    noncontiguous["proposer_models"] = noncontiguous_models
    reasons = module.g1_registry_contract_reasons(
        {"selection_plan": noncontiguous},
        contract,
    )
    assert "invalid_g1_expanded_proposer_roster" in reasons
    assert "wrong_g1_selected_proposer_count" in reasons


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_ensemble_call_core_uses_frozen_provider_quorum(module) -> None:
    plan = {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "proposer_models": [f"model-{index}" for index in range(5)],
        "proposer_sample_count": 5,
        "effective_min_successful_proposers": 2,
        "proposer_recovery_policy": {"quorum_required": 2},
        "aggregator_model": "aggregator",
        "selected_A": "openrouter:aggregator",
    }

    def candidate(index: int, *, ok: bool) -> dict[str, object]:
        text = f"candidate-{index}" if ok else ""
        return {
            "ok": ok,
            "request_started": True,
            "physical_request_count": 1,
            "usage_reported": ok,
            "stop_reason": "end_turn" if ok else "",
            "error": "" if ok else "failed",
            "content": {
                "text": text,
                "chars": len(text),
            },
        }

    trace: dict[str, object] = {
        "request_outcome": "llm_response",
        "fallback_used": False,
        "final_request_role": "aggregator",
        "selection_plan": deepcopy(plan),
        "total_candidates": 5,
        "successful_proposers": 2,
        "candidates": [candidate(index, ok=index < 2) for index in range(5)],
        "final_request": {
            "request_started": True,
            "role": "aggregator",
            "error": "",
            "usage": {
                "provider": "openrouter",
                "requested_provider": "openrouter",
                "model": "aggregator",
                "requested_model": "aggregator",
                "stop_reason": "end_turn",
            },
        },
    }

    reasons = module.ensemble_call_core_reasons(
        trace,
        expected_selection_plan=plan,
    )
    assert reasons == []

    trace["successful_proposers"] = 1
    candidates = trace["candidates"]
    assert isinstance(candidates, list)
    candidates[1] = candidate(1, ok=False)
    reasons = module.ensemble_call_core_reasons(
        trace,
        expected_selection_plan=plan,
    )
    assert "insufficient_proposer_quorum" in reasons
    assert "insufficient_configured_proposer_quorum" in reasons
    assert "insufficient_actual_proposer_quorum" in reasons

    invalid_policy = deepcopy(plan)
    invalid_policy["proposer_recovery_policy"] = {
        "quorum_required": True,
    }
    assert module.frozen_proposer_quorum(invalid_policy, 5) == 2
    invalid_policy.pop("effective_min_successful_proposers")
    assert module.frozen_proposer_quorum(invalid_policy, 5) == module.legal_proposer_quorum(5) == 4


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_ensemble_call_core_accepts_one_complete_analyzer_fallback_proposer(
    module,
) -> None:
    plan = {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "analyzer_failure_fallback": True,
        "complete_proposers_only": True,
        "proposer_models": [f"model-{index}" for index in range(4)],
        "proposer_sample_count": 4,
        "effective_min_successful_proposers": 1,
        "proposer_recovery_policy": {"quorum_required": 1},
        "aggregator_model": "aggregator",
        "selected_A": "openrouter:aggregator",
    }

    def candidate(index: int, outcome: str) -> dict[str, object]:
        has_text = outcome != "failed"
        text = f"candidate-{index}" if has_text else ""
        complete = outcome == "complete"
        return {
            "ok": has_text,
            "request_started": True,
            "physical_request_count": 1,
            "usage_reported": has_text,
            "stop_reason": "end_turn" if complete else "length" if has_text else "",
            "error": "" if has_text else "failed",
            "completion_outcome": outcome,
            "usable_for_aggregation": complete,
            "selected_for_aggregation": complete,
            "content": {"text": text, "chars": len(text)},
        }

    trace: dict[str, object] = {
        "request_outcome": "llm_response",
        "fallback_used": False,
        "final_request_role": "aggregator",
        "selection_plan": deepcopy(plan),
        "total_candidates": 4,
        "successful_proposers": 1,
        "usable_proposers": 2,
        "partial_proposers": 1,
        "selected_candidate_count": 1,
        "execution_quorum_required": 1,
        "execution_quorum_met": True,
        "strict_quorum_met": True,
        "candidates": [
            candidate(0, "complete"),
            candidate(1, "partial_usable"),
            candidate(2, "failed"),
            candidate(3, "failed"),
        ],
        "final_request": {
            "request_started": True,
            "role": "aggregator",
            "error": "",
            "usage": {
                "provider": "openrouter",
                "requested_provider": "openrouter",
                "model": "aggregator",
                "requested_model": "aggregator",
                "stop_reason": "end_turn",
            },
        },
    }

    assert module.ensemble_call_core_reasons(
        trace,
        expected_selection_mode="router_dynamic",
        expected_selection_plan=plan,
    ) == []

    partials_only = deepcopy(trace)
    partials_only["successful_proposers"] = 0
    partials_only["strict_quorum_met"] = False
    partials_only["execution_quorum_met"] = False
    candidates = partials_only["candidates"]
    assert isinstance(candidates, list)
    candidates[0] = candidate(0, "partial_usable")
    reasons = module.ensemble_call_core_reasons(
        partials_only,
        expected_selection_mode="router_dynamic",
        expected_selection_plan=plan,
    )
    assert "insufficient_proposer_quorum" in reasons
    assert "insufficient_configured_proposer_quorum" in reasons
    assert "insufficient_actual_proposer_quorum" in reasons


@pytest.mark.parametrize("module", [runner, resume_runner], ids=["main", "resume"])
def test_analyzer_failure_fallback_uses_zero_recovery_quorum_one_policy(
    module,
) -> None:
    policy = module.formal_proposer_recovery_policy(
        0,
        max_additional_physical_requests=0,
    )
    policy["quorum_required"] = 1
    plan = {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "analyzer_failure_fallback": True,
        "complete_proposers_only": True,
        "ranking_parameters": {"proposer_count": {"backup_count": 2}},
        "selected_P": [f"openrouter:model-{index}" for index in range(4)],
        "proposer_models": [f"model-{index}" for index in range(4)],
        "proposer_sample_count": 4,
        "backup_P": [],
        "configured_proposer_backup_count": 0,
        "effective_proposer_backup_count": 0,
        "effective_min_successful_proposers": 1,
        "selected_A": "openrouter:aggregator",
        "aggregator_candidates": ["openrouter:aggregator"],
        "proposer_recovery_policy": policy,
    }

    assert module.formal_proposer_recovery_policy_for_plan(plan) == policy
    assert module.g1_provider_native_recovery_policy_reason(plan) == ""


def _fixed_fallback_provider_native_row(
    *,
    selected_generation_succeeded: bool = True,
    retry_reason: str = "",
    retry_suppressed_reason: str = "",
) -> dict[str, object]:
    policy = resume_runner.formal_proposer_recovery_policy(
        0,
        max_additional_physical_requests=0,
    )
    policy["quorum_required"] = 1
    selected = [f"openrouter:model-{index}" for index in range(4)]
    plan = {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "analyzer_failure_fallback": True,
        "complete_proposers_only": True,
        "ranking_parameters": {"proposer_count": {"backup_count": 2}},
        "selected_P": selected,
        "proposer_models": [f"model-{index}" for index in range(4)],
        "proposer_sample_count": 4,
        "backup_P": [],
        "configured_proposer_backup_count": 0,
        "effective_proposer_backup_count": 0,
        "effective_min_successful_proposers": 1,
        "selected_A": "openrouter:aggregator",
        "aggregator_candidates": ["openrouter:aggregator"],
        "proposer_recovery_policy": policy,
    }
    from opensquilla.provider.protocol import provider_retry_roster_fingerprint

    fingerprint = provider_retry_roster_fingerprint(plan)
    candidates = []
    for index, identity in enumerate(selected):
        complete = index == 0
        attempt_id = f"{index + 1:x}" * 32
        candidates.append(
            {
                "ok": complete,
                "error": "" if complete else "failed",
                "usable_for_aggregation": complete,
                "completion_outcome": "complete" if complete else "failed",
                "request_started": True,
                "physical_request_count": 1,
                "content": {
                    "text": "complete draft" if complete else "",
                    "chars": len("complete draft") if complete else 0,
                },
                "execution": {
                    "physical_attempts": [
                        {
                            "attempt": 1,
                            "physical_attempt_id": attempt_id,
                            "identity": identity,
                            "request_started": True,
                            "stream_closed": True,
                            "outcome": "succeeded" if complete else "failed",
                        }
                    ]
                },
            }
        )
    call = {
        "selection_plan": deepcopy(plan),
        "successful_proposers": 1,
        "candidates": candidates,
        "proposer_recovery": {
            "schema": resume_runner.PROPOSER_RECOVERY_SCHEMA,
            "selection_plan_fingerprint": fingerprint,
            "scope": "run_turn",
            "scope_id": "fixed-fallback-scope",
            "max_additional_physical_requests": 0,
            "external_physical_requests_reserved": 0,
            "additional_physical_requests_started": 0,
            "remaining_additional_physical_requests": 0,
            "quorum_required": 1,
            "quorum_reached": True,
            "cumulative_excluded_identities": [],
            "visited_identities": [],
            "executed_proposer_roster_before": selected,
            "executed_proposer_roster_after": selected,
            "attempts": [],
        },
    }
    row = {
        "group": "G1",
        "error": "" if selected_generation_succeeded else retry_reason,
        "selected_generation_succeeded": selected_generation_succeeded,
        "execution": {
            "generation_attempts": [
                {
                    "attempt_id": "f" * 32,
                    "attempt_kind": "generation",
                    "attempt": 1,
                    "retry_reason": retry_reason,
                    "retry_suppressed_reason": retry_suppressed_reason,
                    "will_retry": False,
                    "proposer_recovery_owner": "provider",
                    "selection_plan": deepcopy(plan),
                    "run": {
                        "error": (
                            "" if selected_generation_succeeded else retry_reason
                        ),
                        "ensemble_trace": call,
                    },
                }
            ]
        },
    }
    return row


def test_resume_accepts_terminal_fixed_fallback_with_one_complete_proposer() -> None:
    row = _fixed_fallback_provider_native_row()

    evidence = resume_runner.provider_native_proposer_recovery_terminal_evidence(row)

    assert evidence is not None
    assert evidence["status"] == "provider_scope_terminal"
    assert evidence["receipt_valid"] is True
    assert evidence["quorum_reached"] is True


def test_resume_fixed_fallback_non_proposer_failure_is_terminal_across_waves() -> None:
    retry_reason = "invalid_agent_call_output_binding"
    row = _fixed_fallback_provider_native_row(
        selected_generation_succeeded=False,
        retry_reason=retry_reason,
    )

    evidence = resume_runner.provider_native_proposer_recovery_terminal_evidence(row)

    assert evidence is not None
    assert evidence["automatic_generation_retry_allowed"] is False
    assert evidence["status"] == "receipt_invalid"
    assert "provider_native_outer_retry_not_suppressed" in evidence[
        "receipt_reasons"
    ]

    resumable = _strict_attempt_resume_row(
        attempt_id="f" * 32,
        cumulative_budget=1,
        generation_completed_at=1.0,
    )
    resumable["error"] = retry_reason
    resumable["selected_generation_succeeded"] = False
    resumable["execution"]["generation_attempts"] = deepcopy(
        row["execution"]["generation_attempts"]
    )
    resumable = resume_runner.seal_result_row(resumable)
    state = resume_runner.resume_row_completion_state(
        resumable,
        expected_prompt_sha256=resume_runner.text_sha256("same prompt"),
        expected_task_input_sha256="sha256:task-input",
        expected_run_compatibility_fingerprint="sha256:run-contract",
        judge_required=False,
    )

    assert state["action"] == "regenerate"
    assert state["generation_auto_retry_blocked"] is True
    assert state["provider_native_proposer_recovery_terminal"] is not None
    assert not resume_runner.g1_cross_wave_lifecycle_requires_reconstruction(
        group="G1",
        prior_attempts_used=1,
        state=state,
        current_run_compatibility_contract=(
            _enabled_g1_frozen_lifecycle_contract()
        ),
    )


@pytest.mark.asyncio
async def test_resume_amain_never_reruns_blocked_fixed_fallback_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {"id": "task-1", "prompt": "blocked fixed fallback"}
    input_path = tmp_path / "tasks.jsonl"
    input_path.write_text(json.dumps(task) + "\n", encoding="utf-8")
    output_dir = tmp_path / "output"
    experiment_path = ROOT / "configs" / "benchmarks" / "draco_b2_g12.json"
    retry_reason = "invalid_agent_call_output_binding"
    row = _fixed_fallback_provider_native_row(
        selected_generation_succeeded=False,
        retry_reason=retry_reason,
    )
    row.update(
        {
            "provider_spec": dict(resume_runner.GROUP_SPECS["G1"]),
            "task_id": task["id"],
            "prompt_sha256": resume_runner.text_sha256(task["prompt"]),
            "task_input_sha256": resume_runner.canonical_json_sha256(task),
            "run_compatibility_fingerprint": "sha256:prior-g1-contract",
            "final_text": "",
            "generation_attempt_count": 1,
            "generation_attempt_budget_used": 1,
            "generation_completed_at": 1.0,
            "completed_at": 1.0,
        }
    )
    terminal = resume_runner.provider_native_proposer_recovery_terminal_evidence(row)
    assert terminal is not None
    assert terminal["automatic_generation_retry_allowed"] is False
    state = {
        "action": "regenerate",
        "row": resume_runner.seal_result_row(row),
        "prior_generation_attempts_used": 1,
        "generation_auto_retry_blocked": True,
        "generation_reasons": [retry_reason],
        "generation_postprocessing_terminal": None,
        "provider_native_proposer_recovery_terminal": terminal,
    }
    args = resume_runner.build_parser().parse_args(
        [
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--experiment-config",
            str(experiment_path),
            "--groups",
            "G1",
            "--max-tasks",
            "1",
            "--dry-run",
            "--experiment-config-set",
            "benchmark_input.enforce_reference_input=false",
            "--experiment-config-set",
            "runner.concurrency=1",
            "--experiment-config-set",
            "judge.concurrency=1",
        ]
    )
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "fake",
        }
    )
    monkeypatch.setattr(resume_runner.GatewayConfig, "load", lambda _path: config)

    def fake_resume_states(**kwargs):
        assert kwargs["selected_keys"] == {("G1", "task-1")}
        return {("G1", "task-1"): state}, {"source_row_count": 1}

    calls = {"preflight": 0, "provider": 0, "run_one": 0}

    async def forbidden_preflight(*_args, **_kwargs):
        calls["preflight"] += 1
        raise AssertionError("blocked fallback must not run model preflight")

    def forbidden_provider(*_args, **_kwargs):
        calls["provider"] += 1
        raise AssertionError("blocked fallback must not construct a provider")

    async def forbidden_run_one(*_args, **_kwargs):
        calls["run_one"] += 1
        raise AssertionError("blocked fallback must not call run_one")

    monkeypatch.setattr(
        resume_runner,
        "load_resume_group_task_states",
        fake_resume_states,
    )
    monkeypatch.setattr(
        resume_runner,
        "run_local_web_tools_preflight",
        forbidden_preflight,
    )
    monkeypatch.setattr(resume_runner, "build_single_provider", forbidden_provider)
    monkeypatch.setattr(resume_runner, "run_one", forbidden_run_one)

    status = await resume_runner.amain(args)

    assert status == 2
    assert calls == {"preflight": 0, "provider": 0, "run_one": 0}
    result_path = next(output_dir.glob("draco_ensemble_*.jsonl"))
    result = json.loads(result_path.read_text(encoding="utf-8").splitlines()[0])
    assert result["group"] == "G1"
    assert result["execution"]["generation_model_started"] is False
    assert result["execution"]["generation_auto_retry_blocked"] is True
    manifest_path = next(output_dir.glob("draco_run_*.manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["resume_selection"]["model_regenerate_pair_count"] == 0
    assert manifest["resume_selection"]["generation_auto_retry_blocked_pair_count"] == 1


def _exhausted_live_analyzer_chain_lifecycle_row() -> tuple[
    dict[str, object],
    dict[str, object],
]:
    routes = [
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
    configured_routes = [
        {
            "provider": route["provider"],
            "model": route["model"],
            "upstream_provider": route["upstream_provider"],
        }
        for route in routes
    ]
    plan = {
        "task_analyzer": {
            "source": "router_fallback",
            "schema_valid": False,
            "provider": routes[-1]["provider"],
            "model": routes[-1]["model"],
            "fallback_reason": "TimeoutError",
            "chain": {
                "protocol": "opensquilla.task-analyzer-fallback-chain/v1",
                "configured_routes": configured_routes,
                "attempt_outcomes": [
                    {
                        "candidate_index": index,
                        **configured_route,
                        "outcome": "failed",
                        "reason": "TimeoutError",
                        "physical_request_count": 1,
                    }
                    for index, configured_route in enumerate(configured_routes)
                ],
                "selected_index": None,
                "exhausted": True,
            }
        }
    }
    analyzer_units = []
    for index, route in enumerate(routes, start=1):
        unit = _task_analyzer_unit(
            attempt=index,
            physical_attempt_id=f"{index:x}" * 32,
        )
        unit["model"] = route["model"]
        unit["requested_model"] = route["model"]
        unit["provider_usage"]["model"] = route["model"]
        analyzer_units.append(unit)
    plan["task_analyzer"]["usage"] = _task_analyzer_usage(analyzer_units)
    run = {
        "llm_request_count": len(analyzer_units),
        "routing_trace": {"selection_plan": deepcopy(plan)},
        "setup_usage": deepcopy(analyzer_units),
        "usage": {"model_usage_breakdown": deepcopy(analyzer_units)},
    }
    row = {
        "group": "G1",
        "task_id": "task-1",
        "execution": {
            "generation_attempts": [
                {
                    "attempt_id": "e" * 32,
                    "attempt_kind": "generation",
                    "attempt": 1,
                    "run": run,
                }
            ]
        },
    }
    contract = {
        "g1_registry_contract": {
            "live_task_analyzer_chain": routes,
        }
    }
    return row, contract


def test_resume_provider_lifecycle_accepts_exhausted_live_analyzer_chain() -> None:
    row, contract = _exhausted_live_analyzer_chain_lifecycle_row()

    assert resume_runner.g1_provider_lifecycle_analyzer_reasons(
        row,
        contract=contract,
    ) == []


@pytest.mark.parametrize(
    "selected_index",
    [1, 2],
    ids=["gpt", "gemini"],
)
def test_resume_provider_lifecycle_accepts_later_live_analyzer_success(
    selected_index: int,
) -> None:
    row, contract = _exhausted_live_analyzer_chain_lifecycle_row()
    attempt = row["execution"]["generation_attempts"][0]
    run = attempt["run"]
    plan = run["routing_trace"]["selection_plan"]
    chain = plan["task_analyzer"]["chain"]
    outcomes = chain["attempt_outcomes"][: selected_index + 1]
    outcomes[-1]["outcome"] = "success"
    outcomes[-1]["reason"] = ""
    chain["attempt_outcomes"] = outcomes
    chain["selected_index"] = selected_index
    chain["exhausted"] = False
    plan["task_analyzer"].update(
        {
            "source": "llm_provider",
            "schema_valid": True,
            "provider": chain["configured_routes"][selected_index]["provider"],
            "model": chain["configured_routes"][selected_index]["model"],
            "fallback_reason": "",
        }
    )
    run["setup_usage"] = run["setup_usage"][: selected_index + 1]
    run["usage"]["model_usage_breakdown"] = run["usage"][
        "model_usage_breakdown"
    ][: selected_index + 1]
    plan["task_analyzer"]["usage"] = _task_analyzer_usage(
        run["setup_usage"]
    )
    run["llm_request_count"] = selected_index + 1

    assert resume_runner.g1_provider_lifecycle_analyzer_reasons(
        row,
        contract=contract,
    ) == []


def test_resume_provider_lifecycle_rejects_boolean_analyzer_physical_count() -> None:
    row, contract = _exhausted_live_analyzer_chain_lifecycle_row()
    attempt = row["execution"]["generation_attempts"][0]
    chain = attempt["run"]["routing_trace"]["selection_plan"]["task_analyzer"][
        "chain"
    ]
    chain["attempt_outcomes"][0]["physical_request_count"] = True

    assert resume_runner.g1_provider_lifecycle_analyzer_reasons(
        row,
        contract=contract,
    ) == ["invalid_g1_task_analyzer_chain_trace"]


def test_resume_provider_lifecycle_rejects_repeated_live_analyzer_chain() -> None:
    row, contract = _exhausted_live_analyzer_chain_lifecycle_row()
    attempts = row["execution"]["generation_attempts"]
    assert isinstance(attempts, list)
    first_run = attempts[0]["run"]
    repeated_unit = deepcopy(first_run["setup_usage"][0])
    attempts.append(
        {
            "attempt_id": "f" * 32,
            "attempt_kind": "generation",
            "attempt": 2,
            "run": {
                "llm_request_count": 1,
                "routing_trace": deepcopy(first_run["routing_trace"]),
                "setup_usage": [deepcopy(repeated_unit)],
                "usage": {"model_usage_breakdown": [repeated_unit]},
            },
        }
    )

    assert resume_runner.g1_provider_lifecycle_analyzer_reasons(
        row,
        contract=contract,
    ) == ["repeated_g1_task_analyzer_request"]
