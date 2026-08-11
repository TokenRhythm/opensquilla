#!/usr/bin/env python3
"""Run the DRACO single-model and ensemble-routing experiment matrix."""

# ruff: noqa: E402 - the standalone runner adds the repository root to sys.path.

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import hashlib
import json
import math
import os
import random
import shlex
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opensquilla.engine.agent import Agent
from opensquilla.engine.pipeline import TurnContext, run_pipeline
from opensquilla.engine.pricing import (
    PriceEntry,
    ResolvedModelPrice,
    estimate_cost,
    resolve_model_price,
)
from opensquilla.engine.selector_override import apply_model_override
from opensquilla.engine.steps.squilla_router import apply_squilla_router
from opensquilla.engine.types import (
    THINKING_BUDGETS,
    AgentConfig,
    ThinkingLevel,
    done_text_snapshot,
)
from opensquilla.engine.types import (
    DoneEvent as AgentDoneEvent,
)
from opensquilla.engine.types import (
    ErrorEvent as AgentErrorEvent,
)
from opensquilla.engine.types import (
    RunHeartbeatEvent as AgentRunHeartbeatEvent,
)
from opensquilla.engine.types import (
    StateChangeEvent as AgentStateChangeEvent,
)
from opensquilla.engine.types import (
    TextDeltaEvent as AgentTextDeltaEvent,
)
from opensquilla.engine.types import (
    ThinkingEvent as AgentThinkingEvent,
)
from opensquilla.engine.types import (
    ToolResultEvent as AgentToolResultEvent,
)
from opensquilla.engine.types import (
    ToolUseDeltaEvent as AgentToolUseDeltaEvent,
)
from opensquilla.engine.types import (
    ToolUseStartEvent as AgentToolUseStartEvent,
)
from opensquilla.engine.types import (
    WarningEvent as AgentWarningEvent,
)
from opensquilla.eval.draco_artifact_index import (
    load_tasks,
    parse_maybe_json,
    result_key_coverage,  # noqa: F401 - compatibility re-export
)
from opensquilla.eval.draco_artifact_integrity import (
    compact_tool_result_diagnostic,
    seal_result_row,
    trace_row_from_result,
)
from opensquilla.eval.draco_artifact_io import (
    DRACO_DURABLE_RESULT_ROW_FIELD,
    DRACO_RUN_MANIFEST_SCHEMA_V2,
    AsyncDurableDracoArtifactWriter,
    DracoArtifactRunLock,
    DurableDracoArtifactWriter,
    atomic_write_text,
    durable_artifact_capability_contract,
    iter_verified_result_rows,
    verify_durable_artifact_path_snapshots,
    verify_durable_draco_artifacts,
)
from opensquilla.eval.draco_experiment_artifacts import (
    gateway_replay_validation_contract,
    publish_experiment_config_artifacts,
)
from opensquilla.eval.draco_experiment_config import (
    DracoEnsembleMemberConfig,
    DracoExperimentConfig,
    DracoExperimentConfigBundle,
    load_draco_experiment_config,
    validate_formal_draco_credential_bindings,
    validate_formal_draco_ensemble_member_binding,
    validate_formal_draco_gateway_credential_binding,
    validate_reference_input,
)
from opensquilla.eval.draco_judge_scoring import (
    clamp_percent,
    coerce_weight,
    score_criterion_judgments,
)
from opensquilla.eval.draco_result_summary import (
    DracoResultSummaryFact,
    build_result_summary_fact,
    result_failures_and_coverage,
    summarize_result_facts,
)
from opensquilla.eval.draco_runtime_contract import (
    _sanitize_fingerprint_config,  # noqa: F401 - compatibility re-export
    _sanitize_url_for_fingerprint,  # noqa: F401 - compatibility re-export
    canonical_json_sha256,
    gateway_execution_contract,
    validate_formal_web_search_transport,
)
from opensquilla.eval.draco_runtime_contract import (
    resolved_llm_runtime_contract as _shared_runtime_contract,
)
from opensquilla.eval.draco_runtime_contract import (
    validate_formal_openrouter_runtime_transport as _shared_openrouter_transport,
)
from opensquilla.eval.draco_runtime_contract import (
    validate_strict_openrouter_non_byok_environment as _shared_strict_non_byok_environment,
)
from opensquilla.eval.draco_selection_plan_evidence import (
    SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD,
    SELECTION_PLAN_EVIDENCE_ROW_FIELD,
    SELECTION_PLAN_PACK_ARTIFACT_FIELD,
    SelectionPlanPackAppender,
    SelectionPlanPackReader,
    compact_selection_plan_evidence_row,
    selection_plan_evidence_capability_contract,
    selection_plan_evidence_manifest_binding,
    validate_compact_selection_plan_evidence_row,
)
from opensquilla.eval.draco_task_supervisor import DracoRollingTaskWindow
from opensquilla.eval.draco_usage_evidence import (
    STABLE_RECEIPT_EVIDENCE_KEY,
    aggregate_agent_ensemble_trace,
    aggregate_agent_model_usage,
    build_stable_receipt_evidence,
    coerce_metric_int,
    deduplicate_stable_usage_receipts,
    llm_response_records,
    merge_usage_receipt_provenance,
    payload_physical_request_count,  # noqa: F401 - compatibility re-export
    usage_receipt_fingerprint,  # noqa: F401 - compatibility re-export
    usage_row_is_missing_placeholder,
    usage_row_match_priority,
    usage_row_response_ids,
)
from opensquilla.eval.draco_usage_evidence import (
    build_task_analyzer_usage_row as _shared_task_analyzer_usage_row,
)
from opensquilla.eval.draco_usage_evidence import (
    expand_task_analyzer_usage_rows as _shared_task_analyzer_usage_rows,
)
from opensquilla.eval.draco_usage_evidence import (
    recover_task_analyzer_usage_rows as _shared_conservative_task_analyzer_usage_rows,
)
from opensquilla.execution_status import compact_provider_status
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.llm_runtime import (
    OPENROUTER_DEFAULT_PROVIDER_ROUTING,
    resolve_llm_runtime_config,
)
from opensquilla.provider.ensemble import (
    EnsembleMemberConfig,
    EnsembleProvider,
    build_ensemble_provider_from_config,
    openrouter_static_capabilities,
    resolve_effective_generation_request_parameters,
)
from opensquilla.provider.protocol import (
    provider_retry_expanded_proposer_identities,
)
from opensquilla.provider.registry import get_provider_spec
from opensquilla.provider.selector import (
    ModelSelector,
    ProviderConfig,
    SelectorConfig,
)
from opensquilla.provider.types import (
    REASONING_ONLY_LENGTH_STOP_REASONS,
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ProviderBillingReceipt,
    ProviderHeartbeatEvent,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from opensquilla.result_budget import ToolRunBudgetPolicy
from opensquilla.tool_boundary import ToolCall
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import CallerKind, InteractionMode, ToolContext, ToolSpec
from opensquilla.usage_evidence import (
    MISSING_USAGE_PLACEHOLDER_ROLES,
    USAGE_EVIDENCE_SCHEMA,
    canonical_run_usage_units,
    canonicalize_run_usage,
    derive_physical_request_count,
    is_missing_usage_placeholder,  # noqa: F401 - legacy compatibility re-export
)

DEFAULT_B2_EXPERIMENT_CONFIG_PATH = ROOT / "configs/benchmarks/draco_b2_g12.json"
TASK_ANALYZER_MODEL = "anthropic/claude-opus-4.8"


GROUP_SPECS: dict[str, dict[str, Any]] = {
    # B0/B1 remain the summary baselines, so the reference report's delta
    # columns compare every multi-model strategy against fixed Claude Fable 5 and
    # SquillaRouter-selected single-model execution respectively.
    "B0": {
        "kind": "single",
        "model": "anthropic/claude-fable-5",
        "label": "fixed_claude_fable5",
    },
    "B1": {"kind": "router_single", "label": "single_model_routing"},
    "B2": {
        "kind": "selection_mode",
        "selection_mode": "static_openrouter_b5",
        "label": "b2_quality_first_static_openrouter_b5",
        "experiment_config": "draco_b2_quality_first_v1",
    },
    "B3": {
        "kind": "selection_mode",
        "selection_mode": "router_tree_baseline",
        "label": "router_tree_baseline",
    },
    "B4": {
        "kind": "single",
        "model": "openai/gpt-5.6-sol",
        "label": "fixed_gpt56_sol",
    },
    "G1": {
        "kind": "selection_mode",
        "selection_mode": "router_dynamic",
        "label": "ranking_router_dynamic",
    },
}

TOOL_MODE_PROVIDER_ONLY = "provider_only"
TOOL_MODE_OPENROUTER_SERVER_TOOLS = "openrouter_server_tools"
TOOL_MODE_LOCAL_WEB_TOOLS = "local_web_tools"
RUNNER_MODE = TOOL_MODE_PROVIDER_ONLY
RUNNER_MODE_PROVIDER = "provider"
RUNNER_MODE_AGENT_LOOP = "agent_loop"
DEFAULT_DRACO_RUNNER_MODE = RUNNER_MODE_AGENT_LOOP
SELECTION_PLAN_EVIDENCE_MODE_INLINE = "inline"
SELECTION_PLAN_EVIDENCE_MODE_CONTENT_ADDRESSED = "content-addressed"
SUPPORTED_SELECTION_PLAN_EVIDENCE_MODES = (
    SELECTION_PLAN_EVIDENCE_MODE_INLINE,
    SELECTION_PLAN_EVIDENCE_MODE_CONTENT_ADDRESSED,
)
DEFAULT_SELECTION_PLAN_EVIDENCE_MODE = (
    SELECTION_PLAN_EVIDENCE_MODE_CONTENT_ADDRESSED
)
DEFAULT_AGENT_MAX_ITERATIONS = 12
DEFAULT_DEADLINE_WRAPUP_MARGIN_SECONDS = 0
DEFAULT_DEADLINE_WRAPUP_DISABLE_TOOLS = False
DEFAULT_DEADLINE_THINKING_OFF_MARGIN_SECONDS = 0
DEFAULT_MAX_ITERATIONS_INCLUDES_FINALIZATION = False
DEFAULT_RETRIEVAL_LOOP_FINALIZATION_THRESHOLD = 0
DEFAULT_FINALIZATION_AGGREGATOR_ONLY = False
DEFAULT_FINALIZATION_DISABLE_THINKING = False
AGENT_FINALIZATION_POLICY_FIELDS = (
    "deadline_wrapup_margin_seconds",
    "deadline_wrapup_disable_tools",
    "deadline_thinking_off_margin_seconds",
    "max_iterations_includes_finalization",
    "retrieval_loop_finalization_threshold",
    "finalization_aggregator_only",
    "finalization_disable_thinking",
)
AGENT_FINALIZATION_BOOLEAN_FIELDS = frozenset(
    {
        "deadline_wrapup_disable_tools",
        "max_iterations_includes_finalization",
        "finalization_aggregator_only",
        "finalization_disable_thinking",
    }
)
DEFAULT_AGENT_FINALIZATION_POLICY: dict[str, int | bool] = {
    "deadline_wrapup_margin_seconds": DEFAULT_DEADLINE_WRAPUP_MARGIN_SECONDS,
    "deadline_wrapup_disable_tools": DEFAULT_DEADLINE_WRAPUP_DISABLE_TOOLS,
    "deadline_thinking_off_margin_seconds": DEFAULT_DEADLINE_THINKING_OFF_MARGIN_SECONDS,
    "max_iterations_includes_finalization": DEFAULT_MAX_ITERATIONS_INCLUDES_FINALIZATION,
    "retrieval_loop_finalization_threshold": (DEFAULT_RETRIEVAL_LOOP_FINALIZATION_THRESHOLD),
    "finalization_aggregator_only": DEFAULT_FINALIZATION_AGGREGATOR_ONLY,
    "finalization_disable_thinking": DEFAULT_FINALIZATION_DISABLE_THINKING,
}
# B2 and G1 are compared in the same DRACO campaign.  Their task envelope
# (Agent policy, tools, Judge, generation, and timeouts) must therefore come
# from the same profile even when either group is launched by itself.
GLOBAL_EXPERIMENT_PROFILE_GROUPS = frozenset({"B2", "G1"})
SUPPORTED_RUNNER_MODES = (RUNNER_MODE_PROVIDER, RUNNER_MODE_AGENT_LOOP)
SUPPORTED_TOOL_MODES = (
    TOOL_MODE_PROVIDER_ONLY,
    TOOL_MODE_OPENROUTER_SERVER_TOOLS,
    TOOL_MODE_LOCAL_WEB_TOOLS,
)
BENCHMARK_WEB_PREFLIGHT_QUERY = "OpenAI official website"
BENCHMARK_WEB_FETCH_PREFLIGHT_URL = "https://example.com/"
DEFAULT_OPENROUTER_WEB_SEARCH_ENGINE = "exa"
DEFAULT_OPENROUTER_WEB_SEARCH_MAX_RESULTS = 5
DEFAULT_OPENROUTER_WEB_SEARCH_MAX_TOTAL_RESULTS = 10
DEFAULT_OPENROUTER_WEB_SEARCH_CONTEXT_SIZE = "medium"
DEFAULT_LOCAL_WEB_SEARCH_PROVIDER = "duckduckgo"
SUPPORTED_LOCAL_WEB_SEARCH_PROVIDERS = ("duckduckgo", "brave")
DEFAULT_OPENROUTER_WEB_FETCH_ENGINE = "openrouter"
DEFAULT_OPENROUTER_WEB_FETCH_MAX_USES = 5
DEFAULT_OPENROUTER_WEB_FETCH_MAX_CONTENT_TOKENS = 50_000
DEFAULT_OPENROUTER_FUSION_ANALYSIS_MODELS = (
    "deepseek/deepseek-v4-pro",
    "z-ai/glm-5.2",
    "google/gemini-3.1-pro-preview",
    "qwen/qwen3.7-max",
)
DEFAULT_OPENROUTER_FUSION_MODEL = "z-ai/glm-5.2"
DEFAULT_OPENROUTER_FUSION_MAX_TOOL_CALLS = 12
DEFAULT_OPENROUTER_FUSION_MAX_COMPLETION_TOKENS = 16_384
DEFAULT_OPENROUTER_FUSION_REASONING_EFFORT = "xhigh"
DEFAULT_OPENROUTER_FUSION_TEMPERATURE = 0.0
GENERATION_THINKING_MODEL_MAX = "model_max"
DEFAULT_GENERATION_THINKING = GENERATION_THINKING_MODEL_MAX
DEFAULT_GENERATION_THINKING_FALLBACK = "xhigh"
DEFAULT_GENERATION_MAX_TOKENS_OVERRIDE = 0
DEFAULT_MODEL_MAX_GENERATION_THINKING: dict[str, str] = {
    "anthropic/claude-fable-5": "max",
    "anthropic/claude-opus-4.8": "max",
    "deepseek/deepseek-v4-pro": "xhigh",
    "google/gemini-3.1-pro-preview": "high",
    "moonshotai/kimi-k2.7-code": "high",
    "openai/gpt-5.5": "xhigh",
    "openai/gpt-5.5-pro": "xhigh",
    "openai/gpt-5.6-sol": "max",
    "qwen/qwen3.7-max": "high",
    "sakana/fugu-ultra": "max",
    "z-ai/glm-5.2": "xhigh",
}
DEFAULT_GENERATION_TEMPERATURE = 0.0
DEFAULT_CONTAMINATION_BLOCKED_DOMAINS = (
    "hf.co",
    "huggingface.co",
    "datasets-server.huggingface.co",
    "github.com",
    "raw.githubusercontent.com",
    "openrouter.ai",
    "perplexity.ai",
    "research.perplexity.ai",
)
PROFILE_TIMEOUT_MARGIN_SECONDS = 30.0
DEFAULT_PROFILE_PROPOSER_TIMEOUT_SECONDS = 120.0
DEFAULT_PROFILE_AGGREGATOR_TIMEOUT_SECONDS = 300.0
JUDGE_MAX_ATTEMPTS = 3
GENERATION_MAX_ATTEMPTS = 3
GENERATION_ATTEMPT_EVIDENCE_SCHEMA = "opensquilla.draco-generation-attempt/v1"
JUDGE_ATTEMPT_EVIDENCE_SCHEMA = "opensquilla.draco-judge-attempt/v1"
JUDGE_ATTEMPT_BUDGET_SCOPE = "criterion_repeat_campaign"
JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR = "judge_attempt_budget_exhausted"
DEFAULT_GENERATION_RETRY_BACKOFF_SECONDS = 2.0
GENERATION_EMPTY_OUTPUT_ERROR = "empty_generation_output"
GENERATION_MISSING_DONE_ERROR = "generation_missing_done"
RUNNER_STREAM_CLEANUP_TIMEOUT_SECONDS = 1.0
RUN_COMPATIBILITY_SCHEMA = "opensquilla.draco.run-compatibility/v1"
PROPOSER_RECOVERY_SCHEMA = "opensquilla.router-dynamic-proposer-recovery/v1"


def formal_proposer_recovery_policy(
    backup_count: int,
    *,
    max_additional_physical_requests: int = 3,
    max_tokens_cap: int = 65_536,
    visible_answer_reserve_tokens: int = 4_096,
) -> dict[str, Any]:
    """Return the execution policy bound to a frozen ranking roster size."""

    if (
        isinstance(backup_count, bool)
        or not isinstance(backup_count, int)
        or not 0 <= backup_count <= 2
    ):
        raise ValueError("ranking proposer backup_count must be between 0 and 2")
    if (
        isinstance(max_additional_physical_requests, bool)
        or not isinstance(max_additional_physical_requests, int)
        or not 0 <= max_additional_physical_requests <= 3
    ):
        raise ValueError(
            "proposer max_additional_physical_requests must be between 0 and 3"
        )
    if (
        isinstance(max_tokens_cap, bool)
        or not isinstance(max_tokens_cap, int)
        or max_tokens_cap < 2
    ):
        raise ValueError("proposer max_tokens_cap must be at least 2")
    if (
        isinstance(visible_answer_reserve_tokens, bool)
        or not isinstance(visible_answer_reserve_tokens, int)
        or not 1 <= visible_answer_reserve_tokens < max_tokens_cap
    ):
        raise ValueError(
            "proposer visible_answer_reserve_tokens must be between 1 and "
            "max_tokens_cap - 1"
        )
    return {
        "schema": PROPOSER_RECOVERY_SCHEMA,
        "configured_backup_count": backup_count,
        "effective_backup_count": backup_count,
        "max_additional_physical_requests": max_additional_physical_requests,
        "quorum_required": 2,
        "max_tokens_cap": max_tokens_cap,
        "visible_answer_reserve_tokens": visible_answer_reserve_tokens,
        "thinking_downgrade_order": ["one_strictly_lower"],
        "transient_same_model_retries": 1,
        "backup_reasoning_downgrades": 1,
    }


FORMAL_PROPOSER_RECOVERY_POLICY: dict[str, Any] = formal_proposer_recovery_policy(2)


def ranking_proposer_backup_count(ranking_config: Mapping[str, Any]) -> int:
    """Read the sole authoritative proposer backup roster size."""

    proposer_count = ranking_config.get("proposer_count")
    raw = proposer_count.get("backup_count") if isinstance(proposer_count, Mapping) else None
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 2:
        raise ValueError("frozen ranking proposer_count.backup_count must be between 0 and 2")
    return raw


def formal_proposer_recovery_policy_for_plan(
    plan: Mapping[str, Any],
) -> dict[str, Any] | None:
    policy = plan.get("proposer_recovery_policy")
    if not isinstance(policy, Mapping):
        return None
    analyzer_failure_fallback = bool(
        plan.get("analyzer_failure_fallback") is True
        and plan.get("complete_proposers_only") is True
        and plan.get("effective_min_successful_proposers") == 1
    )
    ranking_config = plan.get("ranking_parameters")
    if analyzer_failure_fallback:
        backup_count = 0
    elif isinstance(ranking_config, Mapping):
        try:
            backup_count = ranking_proposer_backup_count(ranking_config)
        except ValueError:
            return None
    else:
        # Archived plans predate embedded ranking_parameters. Their signed
        # recovery policy remains replayable, but it still has to normalize to
        # the exact formal schema below.
        backup_count = policy.get("configured_backup_count")
    try:
        expected = formal_proposer_recovery_policy(
            backup_count,
            max_additional_physical_requests=policy.get(
                "max_additional_physical_requests"
            ),
            max_tokens_cap=policy.get("max_tokens_cap"),
            visible_answer_reserve_tokens=policy.get(
                "visible_answer_reserve_tokens"
            ),
        )
    except ValueError:
        return None
    if analyzer_failure_fallback:
        expected["quorum_required"] = 1
    effective_backup_count = policy.get("effective_backup_count")
    if (
        isinstance(effective_backup_count, bool)
        or not isinstance(effective_backup_count, int)
        or not 0 <= effective_backup_count <= expected["configured_backup_count"]
    ):
        return None
    expected["effective_backup_count"] = effective_backup_count
    return expected


def normalized_agent_finalization_policy(
    value: Mapping[str, Any] | None = None,
) -> dict[str, int | bool]:
    raw = dict(value or {})
    policy: dict[str, int | bool] = {}
    for field_name in AGENT_FINALIZATION_POLICY_FIELDS:
        default = DEFAULT_AGENT_FINALIZATION_POLICY[field_name]
        candidate = raw.get(field_name, default)
        if field_name in AGENT_FINALIZATION_BOOLEAN_FIELDS:
            if not isinstance(candidate, bool):
                raise ValueError(f"{field_name} must be a boolean")
            policy[field_name] = candidate
            continue
        if isinstance(candidate, bool) or not isinstance(candidate, int):
            raise ValueError(f"{field_name} must be an integer >= 0")
        if candidate < 0:
            raise ValueError(f"{field_name} must be an integer >= 0")
        policy[field_name] = candidate
    return policy


def legal_proposer_quorum(proposer_count: int) -> int:
    """Return the DRACO two-thirds quorum (4 -> 3, 3 -> 2)."""

    if isinstance(proposer_count, bool) or not isinstance(proposer_count, int):
        raise ValueError("proposer_count must be a non-negative integer")
    if proposer_count < 0:
        raise ValueError("proposer_count must be a non-negative integer")
    return (2 * proposer_count + 2) // 3 if proposer_count else 0


def frozen_proposer_quorum(
    plan: Mapping[str, Any] | None,
    proposer_count: int,
) -> int:
    """Resolve the provider-native quorum frozen into a dynamic route plan."""

    if isinstance(plan, Mapping):
        policy = plan.get("proposer_recovery_policy")
        for raw_quorum in (
            (
                policy.get("quorum_required")
                if isinstance(policy, Mapping)
                else None
            ),
            plan.get("effective_min_successful_proposers"),
        ):
            if (
                isinstance(raw_quorum, int)
                and not isinstance(raw_quorum, bool)
                and 0 < raw_quorum <= proposer_count
            ):
                return raw_quorum
    return legal_proposer_quorum(proposer_count)


def expanded_proposer_slot_identities(
    plan: object,
) -> tuple[str, ...]:
    """Return the exact provider identity bound to every proposer sample."""

    expanded = provider_retry_expanded_proposer_identities(plan)
    if expanded:
        return expanded
    if not isinstance(plan, Mapping):
        return ()
    recovery_policy = plan.get("proposer_recovery_policy")
    if (
        isinstance(recovery_policy, Mapping)
        and recovery_policy.get("schema") == PROPOSER_RECOVERY_SCHEMA
    ):
        return ()
    selected_p = plan.get("selected_P")
    proposer_models = plan.get("proposer_models")
    if (
        not isinstance(selected_p, list)
        or not selected_p
        or not isinstance(proposer_models, list)
        or len(selected_p) != len(proposer_models)
    ):
        return ()
    normalized: list[str] = []
    for identity, expected_model in zip(
        selected_p,
        proposer_models,
        strict=True,
    ):
        if (
            not isinstance(identity, str)
            or identity != identity.strip()
            or any(character.isspace() for character in identity)
            or not isinstance(expected_model, str)
            or expected_model != expected_model.strip()
            or any(character.isspace() for character in expected_model)
        ):
            return ()
        provider, separator, model = identity.partition(":")
        if (
            separator != ":"
            or not provider
            or provider != provider.casefold()
            or not model
            or any(not segment for segment in model.split(":"))
            or model != expected_model
        ):
            return ()
        normalized.append(identity)
    return tuple(normalized)


def validate_g1_registry_contract(
    experiment: DracoExperimentConfig,
    config: GatewayConfig,
) -> dict[str, Any]:
    """Validate the formal G1 pool and upstream pins before any model call."""

    contract = experiment.g1_routing
    if contract is None:
        raise ValueError("G1 requires a versioned g1_routing experiment contract")
    from opensquilla.provider.ranking_router import (
        _legacy_registry_snapshot_projection,
        load_model_registry_snapshot,
        ranking_config_resolution,
        task_analyzer_policy,
    )

    frozen_resolution = getattr(
        config.llm_ensemble,
        "ranking_config_resolution_snapshot",
        None,
    )
    if callable(frozen_resolution):
        ranking_resolution = frozen_resolution()
        thinking_assignment_enabled = (
            ranking_resolution.get("thinking_assignment_enabled") is True
        )
    else:
        thinking_assignment_enabled = (
            config.llm_ensemble.ranking_thinking_assignment_enabled is True
        )
        ranking_resolution = ranking_config_resolution(
            thinking_assignment_enabled=thinking_assignment_enabled,
            override=(config.llm_ensemble.ranking_config_override or None),
            base_version=contract.expected_ranking_config_version,
        )
    snapshot = load_model_registry_snapshot(
        base_version=contract.source_registry_snapshot_version
    )
    if not thinking_assignment_enabled:
        snapshot = _legacy_registry_snapshot_projection(snapshot)
    actual_version = str(snapshot.get("snapshot_version") or "").strip()
    if actual_version != contract.source_registry_snapshot_version:
        raise ValueError("G1 registry snapshot version differs from the experiment contract")
    actual_registry_hash = canonical_json_sha256(snapshot).removeprefix("sha256:")
    if actual_registry_hash != contract.expected_source_registry_snapshot_sha256:
        raise ValueError("G1 registry snapshot content differs from the experiment contract")
    base_ranking_config = ranking_resolution["base_config"]
    ranking_config = ranking_resolution["effective_config"]
    analyzer_policy = task_analyzer_policy(ranking_config)
    base_ranking_schema = str(base_ranking_config.get("schema_version") or "").strip()
    base_ranking_version = str(base_ranking_config.get("config_version") or "").strip()
    base_ranking_hash = str(ranking_resolution["base_sha256"])
    if (
        base_ranking_schema != contract.expected_ranking_config_schema_version
        or base_ranking_version != contract.expected_ranking_config_version
        or base_ranking_hash != contract.expected_ranking_config_sha256
    ):
        raise ValueError("G1 baseline ranking configuration differs from the experiment contract")
    actual_ranking_schema = str(ranking_config.get("schema_version") or "").strip()
    actual_ranking_version = str(ranking_config.get("config_version") or "").strip()
    actual_ranking_hash = str(ranking_resolution["effective_sha256"])

    def _proposer_max(ranking: Mapping[str, Any]) -> int:
        proposer_count = ranking.get("proposer_count")
        by_tier = proposer_count.get("by_tier") if isinstance(proposer_count, Mapping) else None
        high_risk = (
            proposer_count.get("high_risk") if isinstance(proposer_count, Mapping) else None
        )
        try:
            return max(
                *(int(row["max"]) for row in by_tier.values() if isinstance(row, Mapping)),
                int(high_risk["max"]),
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("G1 ranking proposer bounds are malformed") from exc

    baseline_proposer_max = _proposer_max(base_ranking_config)
    if baseline_proposer_max != contract.expected_proposer_count_max:
        raise ValueError(
            "G1 baseline ranking proposer maximum differs from the experiment contract"
        )
    actual_proposer_max = _proposer_max(ranking_config)
    rows = snapshot.get("models")
    if not isinstance(rows, list):
        raise ValueError("G1 registry snapshot has no model rows")
    available: set[str] = set()
    for row in rows:
        facts = row.get("registry_facts") if isinstance(row, Mapping) else None
        if not isinstance(facts, Mapping):
            raise ValueError("G1 registry snapshot contains a malformed model row")
        if str(facts.get("provider") or "").strip().lower() != "openrouter":
            raise ValueError("G1 formal registry contains a non-OpenRouter model")
        model = str(facts.get("model_id") or "").strip().lower()
        if not model or model in available:
            raise ValueError("G1 registry snapshot contains a missing or duplicate model id")
        available.add(model)
    replay_contract_configured = contract.task_analysis_execution is not None
    analyzer_routes = (
        [route.model_dump(mode="json") for route in contract.live_task_analyzer_chain]
        if contract.live_task_analyzer_chain and not replay_contract_configured
        else []
    )
    if not analyzer_routes:
        analyzer_routes = [
            {
                "provider": str(analyzer_policy["provider"]),
                "model": str(analyzer_policy["model"]),
                "upstream_provider": str(
                    analyzer_policy["upstream_provider"]
                ),
            }
        ]
    primary_route = analyzer_routes[0]
    if str(analyzer_policy["upstream_provider"]) == "auto":
        raise ValueError(
            "formal G1 task analyzer upstream provider must be explicitly pinned"
        )
    if (
        str(primary_route["provider"]) != str(analyzer_policy["provider"])
        or str(primary_route["model"]) != str(analyzer_policy["model"])
        or str(primary_route["upstream_provider"])
        != str(analyzer_policy["upstream_provider"])
    ):
        raise ValueError(
            "G1 live task analyzer chain primary differs from effective ranking config"
        )
    runtime = resolve_llm_runtime_config(config)
    for route in analyzer_routes:
        route_provider = str(route["provider"])
        route_model = str(route["model"])
        route_upstream = str(route["upstream_provider"])
        if route_upstream == "auto":
            raise ValueError(
                "formal G1 task analyzer upstream provider must be explicitly pinned"
            )
        if route_model not in available:
            raise ValueError(
                "G1 task analyzer model is missing from the frozen registry"
            )
        if runtime.provider != route_provider:
            raise ValueError(
                "G1 task analyzer provider differs from the resolved runtime"
            )
        if runtime.provider_routing.get(route_model) != route_upstream:
            raise ValueError(
                "G1 task analyzer upstream provider pin differs from the runtime"
            )
    analyzer_failure_fallback = (
        None
        if replay_contract_configured
        else contract.analyzer_failure_fallback_ensemble
    )
    if analyzer_failure_fallback is not None:
        fallback_routes = [
            *analyzer_failure_fallback.proposers,
            analyzer_failure_fallback.aggregator,
        ]
        for route in fallback_routes:
            if route.provider != runtime.provider:
                raise ValueError(
                    "G1 analyzer-failure fallback provider differs from runtime"
                )
            if route.upstream_provider == "auto" or (
                runtime.provider_routing.get(route.model)
                != route.upstream_provider
            ):
                raise ValueError(
                    "G1 analyzer-failure fallback route is not explicitly pinned"
                )
    if contract.candidate_scope == "exact_routes":
        assert contract.expected_routes is not None
        assert contract.expected_candidate_count is not None
        assert contract.expected_routes_sha256 is not None
        expected_routes = dict(contract.expected_routes)
        expected_count = contract.expected_candidate_count
        expected_hash = contract.expected_routes_sha256
        missing_models = sorted(set(expected_routes) - available)
        if missing_models:
            raise ValueError(
                "G1 expected route model(s) missing from registry: " + ", ".join(missing_models)
            )
        pin_mismatches = {
            model: {
                "expected": expected_provider,
                "actual": runtime.provider_routing.get(model),
            }
            for model, expected_provider in expected_routes.items()
            if runtime.provider_routing.get(model) != expected_provider
        }
        if pin_mismatches:
            raise ValueError(
                "G1 expected route provider pin(s) differ: " + ", ".join(sorted(pin_mismatches))
            )
        policy = "exact_openrouter_routes"
        runtime_pin_policy = "required_exact"
        runtime_pins_match: bool | None = True
    else:
        expected_routes = {model: "auto" for model in sorted(available)}
        expected_count = len(expected_routes)
        expected_hash = canonical_json_sha256(expected_routes).removeprefix("sha256:")
        policy = "all_registry_models"
        runtime_pin_policy = "optional_auto"
        runtime_pins_match = None
    if expected_count < actual_proposer_max:
        raise ValueError("G1 registry has fewer candidates than the proposer maximum")
    serialized_contract = contract.model_dump(mode="json", exclude_none=True)
    if replay_contract_configured:
        serialized_contract.pop("live_task_analyzer_chain", None)
        serialized_contract.pop("analyzer_failure_fallback_ensemble", None)
    resolved_contract = {
        **serialized_contract,
        "candidate_scope": contract.candidate_scope,
        "policy": policy,
        "expected_candidate_count": expected_count,
        "expected_routes": expected_routes,
        "expected_routes_sha256": expected_hash,
        "expected_identities": sorted(f"openrouter:{model}" for model in expected_routes),
        "validated": True,
        "available_registry_candidate_count": len(available),
        "runtime_pin_policy": runtime_pin_policy,
        "runtime_pins_match": runtime_pins_match,
        "task_analyzer": dict(analyzer_policy),
    }
    if ranking_resolution["override"] is not None:
        resolved_contract.update(
            {
                "baseline_expected_ranking_config_schema_version": (
                    contract.expected_ranking_config_schema_version
                ),
                "baseline_expected_ranking_config_version": (
                    contract.expected_ranking_config_version
                ),
                "baseline_expected_ranking_config_sha256": (
                    contract.expected_ranking_config_sha256
                ),
                "baseline_expected_proposer_count_max": (
                    contract.expected_proposer_count_max
                ),
                "expected_ranking_config_schema_version": actual_ranking_schema,
                "expected_ranking_config_version": actual_ranking_version,
                "expected_ranking_config_sha256": actual_ranking_hash,
                "expected_proposer_count_max": actual_proposer_max,
                "ranking_config_resolution": ranking_resolution,
            }
        )
    return resolved_contract


def aggregator_recovery_policy(
    experiment: DracoExperimentConfig,
) -> dict[str, Any]:
    """Return the frozen aggregator recovery settings from the experiment profile."""

    ensemble = experiment.ensemble
    return {
        "aggregator_recovery_mode": ensemble.aggregator_recovery_mode,
        "aggregator_recovery_top_k": ensemble.aggregator_recovery_top_k,
        "aggregator_max_tokens_cap": ensemble.aggregator_max_tokens_cap,
        "aggregator_visible_answer_reserve_tokens": (
            ensemble.aggregator_visible_answer_reserve_tokens
        ),
    }


def proposer_recovery_policy(
    experiment: DracoExperimentConfig,
    *,
    ranking_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the frozen, provider-owned proposer recovery settings."""

    ensemble = experiment.ensemble
    return {
        "proposer_backup_count": ranking_proposer_backup_count(ranking_config),
        "proposer_recovery_max_additional_calls": (
            ensemble.proposer_recovery_max_additional_calls
        ),
        "proposer_max_tokens_cap": ensemble.proposer_max_tokens_cap,
        "proposer_visible_answer_reserve_tokens": (
            ensemble.proposer_visible_answer_reserve_tokens
        ),
    }


def proposer_recovery_plan_policy(
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Project runtime settings into immutable selection-plan evidence."""

    configured_backup_count = int(policy["proposer_backup_count"])
    return {
        "schema": PROPOSER_RECOVERY_SCHEMA,
        "configured_backup_count": configured_backup_count,
        "effective_backup_count": configured_backup_count,
        "max_additional_physical_requests": int(
            policy["proposer_recovery_max_additional_calls"]
        ),
        "quorum_required": 2,
        "max_tokens_cap": int(policy["proposer_max_tokens_cap"]),
        "visible_answer_reserve_tokens": int(
            policy["proposer_visible_answer_reserve_tokens"]
        ),
        "thinking_downgrade_order": ["one_strictly_lower"],
        "transient_same_model_retries": 1,
        "backup_reasoning_downgrades": 1,
    }


def apply_aggregator_recovery_policy(target: Any, policy: Mapping[str, Any]) -> None:
    """Apply one normalized recovery policy to a runtime or dry provider config."""

    for field_name, value in policy.items():
        setattr(target, field_name, value)


def enforce_formal_draco_runtime_config(
    config: GatewayConfig,
    experiment: DracoExperimentConfig | None,
    groups: list[str],
) -> dict[str, Any]:
    """Apply formal runtime switches independently of the operator base config."""

    if experiment is None:
        return {}
    validate_formal_draco_credential_bindings(experiment)
    validate_formal_draco_gateway_credential_binding(
        provider=str(getattr(config.llm, "provider", "") or ""),
        base_url=str(getattr(config.llm, "base_url", "") or ""),
        api_key_env=str(getattr(config.llm, "api_key_env", "") or ""),
    )
    if experiment.judge.model != "google/gemini-3.1-pro-preview":
        raise ValueError(
            "formal DRACO currently requires the frozen Gemini Judge model"
        )
    if experiment.tools.mode != TOOL_MODE_LOCAL_WEB_TOOLS:
        raise ValueError("formal DRACO requires tools.mode=local_web_tools")
    if tuple(experiment.tools.contamination_blocked_domains) != tuple(
        DEFAULT_CONTAMINATION_BLOCKED_DOMAINS
    ):
        raise ValueError(
            "formal DRACO requires the frozen contamination-blocked domain set"
        )
    if experiment.tools.sandbox_enabled is not False:
        raise ValueError("formal DRACO requires tools.sandbox_enabled=false")
    config.sandbox.sandbox = False
    config.sandbox.security_grading = False
    recovery_policy = aggregator_recovery_policy(experiment)
    apply_aggregator_recovery_policy(config.llm_ensemble, recovery_policy)
    config.llm_ensemble.ranking_config_override = copy.deepcopy(
        experiment.router_dynamic_ranking_override
    )
    if "G1" in groups:
        g1_routing = experiment.g1_routing
        if g1_routing is None:
            raise ValueError("G1 requires a versioned g1_routing experiment contract")
        ranking_resolution = config.llm_ensemble.freeze_ranking_config(
            base_version=g1_routing.expected_ranking_config_version
        )
    else:
        ranking_resolution = config.llm_ensemble.freeze_ranking_config()
    proposer_policy = proposer_recovery_policy(
        experiment,
        ranking_config=ranking_resolution["effective_config"],
    )
    apply_aggregator_recovery_policy(config.llm_ensemble, proposer_policy)
    freeze: dict[str, Any] = {
        "source": "experiment_config",
        "sandbox_enabled": False,
        "sandbox_security_grading_enabled": False,
        **recovery_policy,
        **proposer_policy,
    }
    if experiment.router_dynamic_ranking_override:
        freeze["ranking_config_override_present"] = True
        freeze["ranking_config_override_sha256"] = ranking_resolution[
            "override_sha256"
        ]
        freeze["ranking_config_effective_sha256"] = ranking_resolution[
            "effective_sha256"
        ]
    if "G1" in groups:
        from opensquilla.provider.ranking_router import task_analyzer_policy

        g1_routing = experiment.g1_routing
        if g1_routing is None or g1_routing.user_profile_enabled is not False:
            raise ValueError("formal G1 requires g1_routing.user_profile_enabled=false")
        config.llm_ensemble.ranking_user_profile_generation_enabled = False
        config.llm_ensemble.ranking_user_profile_enabled = False
        analyzer_policy = task_analyzer_policy(
            ranking_resolution["effective_config"]
        )
        config.llm.provider_routing[str(analyzer_policy["model"])] = str(
            analyzer_policy["upstream_provider"]
        )
        freeze.update(
            {
                "g1_user_profile_generation_enabled": False,
                "g1_user_profile_enabled": False,
                "task_analyzer": dict(analyzer_policy),
            }
        )
    return freeze


def agent_finalization_policy_from_args(
    args: argparse.Namespace,
) -> dict[str, int | bool]:
    return normalized_agent_finalization_policy(
        {
            field_name: getattr(
                args,
                field_name,
                DEFAULT_AGENT_FINALIZATION_POLICY[field_name],
            )
            for field_name in AGENT_FINALIZATION_POLICY_FIELDS
        }
    )


def normalize_agent_runner_args(
    args: argparse.Namespace,
) -> dict[str, int | bool]:
    raw_max_iterations = getattr(
        args,
        "agent_max_iterations",
        DEFAULT_AGENT_MAX_ITERATIONS,
    )
    if raw_max_iterations is None:
        raw_max_iterations = DEFAULT_AGENT_MAX_ITERATIONS
    if isinstance(raw_max_iterations, bool) or not isinstance(raw_max_iterations, int):
        raise ValueError("agent_max_iterations must be an integer >= 0")
    if raw_max_iterations < 0:
        raise ValueError("agent_max_iterations must be an integer >= 0")
    args.agent_max_iterations = raw_max_iterations
    policy = agent_finalization_policy_from_args(args)
    for field_name, value in policy.items():
        setattr(args, field_name, value)
    return policy


def apply_b2_g12_argument_alignment(
    args: argparse.Namespace,
    groups: list[str],
) -> dict[str, Any] | None:
    """Load the composable JSON profile and apply its run-wide settings."""

    config_requested = bool(
        getattr(args, "experiment_config", None)
        or getattr(args, "experiment_config_override", None)
        or getattr(args, "experiment_config_override_json", None) is not None
        or getattr(args, "experiment_config_set", None)
    )
    uses_global_profile = bool(GLOBAL_EXPERIMENT_PROFILE_GROUPS.intersection(groups))
    if not uses_global_profile and not config_requested:
        return None

    base_path = Path(getattr(args, "experiment_config", None) or DEFAULT_B2_EXPERIMENT_CONFIG_PATH)
    bundle = load_draco_experiment_config(
        base_path,
        override_paths=list(getattr(args, "experiment_config_override", []) or []),
        inline_overlay_json=getattr(args, "experiment_config_override_json", None),
        inline_sets=list(getattr(args, "experiment_config_set", []) or []),
    )
    config = bundle.config
    overrides = {
        "concurrency": config.runner.concurrency,
        "timeout": config.timeouts.task_seconds,
        "ensemble_proposer_timeout": config.timeouts.proposer_seconds,
        "ensemble_aggregator_timeout": config.timeouts.aggregator_seconds,
        "ensemble_proposer_early_stop_success_count": 0,
        "ensemble_proposer_early_stop_after": 0.0,
        "expand_ensemble_timeouts_to_task_timeout": False,
        "runner_mode": config.runner.mode,
        "agent_max_iterations": config.runner.agent_max_iterations,
        **{
            field_name: getattr(
                config.runner,
                field_name,
                DEFAULT_AGENT_FINALIZATION_POLICY[field_name],
            )
            for field_name in AGENT_FINALIZATION_POLICY_FIELDS
        },
        "judge_model": config.judge.model,
        "judge_repeats": config.judge.repeats,
        "judge_concurrency": config.judge.concurrency,
        "judge_max_attempts": config.judge.max_attempts,
        "judge_candidates": config.judge.judge_candidates,
        "generation_max_attempts": config.generation.max_attempts,
        "generation_max_tokens": config.generation.max_tokens,
        "generation_retry_backoff": config.generation.retry_backoff_seconds,
        "tool_mode": config.tools.mode,
        "contamination_blocked_domains": ",".join(config.tools.contamination_blocked_domains),
        "local_web_search_provider": config.tools.web_search.provider,
        "local_web_search_api_key_env": config.tools.web_search.api_key_env,
        "openrouter_web_search_max_results": config.tools.web_search.max_results,
        "openrouter_web_fetch_max_content_tokens": (config.tools.web_fetch.max_content_tokens),
    }
    requested = {key: getattr(args, key, None) for key in overrides}
    overridden = {
        key: {"requested": requested[key], "effective": value}
        for key, value in overrides.items()
        if requested[key] != value
    }
    for key, value in overrides.items():
        setattr(args, key, value)
    effective_sources = dict(getattr(args, "_effective_argument_sources", {}) or {})
    effective_sources.update({key: "experiment_config" for key in overrides})
    args._effective_argument_sources = effective_sources
    args.experiment_config = bundle.base_path
    args._draco_experiment_config_bundle = bundle
    record = {
        "id": config.profile_id,
        "reference_sha256": canonical_json_sha256(
            config.reference.model_dump(mode="json")
        ),
        "scope": "Shared DRACO execution profile; B2 ensemble mapping remains B2-only",
        "config_provenance": bundle.provenance(),
        "effective_config_sha256": canonical_json_sha256(
            config.model_dump(mode="json")
        ),
        "requested_args": requested,
        "effective_args": dict(overrides),
        "effective_arg_sources": {key: "experiment_config" for key in overrides},
        "overridden_args": overridden,
    }
    alignments = dict(getattr(args, "_benchmark_alignments", {}) or {})
    alignments["global_experiment_profile"] = record
    if "B2" in groups:
        alignments["B2"] = record
    args._benchmark_alignments = alignments
    return record


def build_webresearch_tool_run_budget_policy(
    *,
    max_single_fetch_chars: int | None,
    max_web_search_results: int | None,
) -> ToolRunBudgetPolicy:
    """Preserve the reference DRACO policy on the current extended budget type."""

    return ToolRunBudgetPolicy(
        max_single_fetch_chars=max_single_fetch_chars,
        max_web_search_results=max_web_search_results,
        max_web_search_fetch_top_k=None,
        max_web_search_chars_per_source=None,
        max_repeated_retrievals_per_turn=None,
    )


class _BenchmarkApprovalQueue:
    """Non-interactive approval queue for unattended benchmark runs."""

    def request(self, namespace: str = "exec", params: dict | None = None) -> str:
        return "draco-benchmark:auto-deny"

    async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
        return False

    def resolve(self, approval_id: str, approved: bool) -> None:
        return None


@dataclass
class RunResult:
    final_text: str
    done: DoneEvent | None
    error: str = ""
    latency_ms: int = 0
    ttft_ms: int | None = None
    tool_call_count: int = 0
    trace_events: list[dict[str, Any]] = field(default_factory=list)
    setup_latency_ms: int = 0
    setup_usage: list[dict[str, Any]] = field(default_factory=list)
    routing_trace: dict[str, Any] = field(default_factory=dict)
    audit_warnings: list[str] = field(default_factory=list)


@dataclass
class ProviderBuildResult:
    provider: Any
    prompt: str
    setup_latency_ms: int = 0
    setup_usage: list[dict[str, Any]] = field(default_factory=list)
    routing_trace: dict[str, Any] = field(default_factory=dict)


class ProviderBuildError(RuntimeError):
    """Preserve already-billed setup receipts when provider construction fails."""

    def __init__(
        self,
        cause: Exception,
        *,
        setup_latency_ms: int,
        setup_usage: list[dict[str, Any]],
        routing_trace: dict[str, Any],
    ) -> None:
        super().__init__(
            "provider_build_failed_after_setup:"
            + type(cause).__name__
        )
        self.setup_latency_ms = setup_latency_ms
        self.setup_usage = list(setup_usage)
        self.routing_trace = dict(routing_trace)


def safe_provider_build_routing_trace(value: Any) -> dict[str, Any]:
    """Return only JSON-safe routing evidence for a failed provider build."""

    try:
        normalized = json_safe(value)
    except Exception:
        return {}
    return dict(normalized) if isinstance(normalized, Mapping) else {}


def _provider_selection_plan_execution_snapshot(
    provider: Any,
) -> Mapping[str, Any] | None:
    """Read the provider's immutable route plus its persisted receipt prefix."""

    snapshot = getattr(provider, "selection_plan_execution_snapshot", None)
    plan = (
        snapshot()
        if callable(snapshot)
        else getattr(provider, "selection_plan", None)
    )
    return plan if isinstance(plan, Mapping) else None


def attach_provider_setup(provider: Any, build: ProviderBuildResult) -> Any:
    """Attach one-shot setup accounting plus a reusable frozen routing receipt."""

    try:
        selection_plan = getattr(provider, "selection_plan", None)
        managed_thinking = bool(
            isinstance(selection_plan, Mapping)
            and selection_plan.get("ranking_thinking_assignment_enabled")
            is True
        )
        if not managed_thinking:
            setattr(provider, "_draco_frozen_routing_trace", None)
            setattr(
                provider,
                "_draco_setup_metrics",
                {
                    "latency_ms": build.setup_latency_ms,
                    "usage": list(build.setup_usage),
                    "routing": dict(build.routing_trace),
                },
            )
            return provider
        execution_plan = _provider_selection_plan_execution_snapshot(
            provider
        )
        if not (
            isinstance(execution_plan, Mapping)
            and execution_plan.get(
                "ranking_thinking_assignment_enabled"
            )
            is True
        ):
            raise ValueError(
                "managed provider selection plan snapshot is invalid"
            )
        frozen_routing = json_safe(build.routing_trace)
        if not isinstance(frozen_routing, Mapping):
            raise TypeError(
                "provider routing trace must serialize to an object"
            )
        if execution_plan is not None:
            serialized_plan = json_safe(
                copy.deepcopy(dict(execution_plan))
            )
            if not isinstance(serialized_plan, Mapping):
                raise TypeError(
                    "provider selection plan must serialize to an object"
                )
            frozen_routing = {
                **dict(frozen_routing),
                "selection_plan": copy.deepcopy(
                    dict(serialized_plan)
                ),
            }
        setattr(
            provider,
            "_draco_setup_metrics",
            {
                "latency_ms": build.setup_latency_ms,
                "usage": copy.deepcopy(list(build.setup_usage)),
            },
        )
        setattr(
            provider,
            "_draco_frozen_routing_trace",
            copy.deepcopy(dict(frozen_routing)),
        )
        return provider
    except ProviderBuildError:
        raise
    except Exception as exc:
        if build.setup_usage:
            raise ProviderBuildError(
                exc,
                setup_latency_ms=build.setup_latency_ms,
                setup_usage=build.setup_usage,
                routing_trace=safe_provider_build_routing_trace(
                    build.routing_trace
                ),
            ) from exc
        raise


def consume_provider_setup(provider: Any) -> dict[str, Any]:
    """Consume setup once and bind the frozen route to the executed plan state."""

    setup = getattr(provider, "_draco_setup_metrics", None)
    routing = getattr(provider, "_draco_frozen_routing_trace", None)
    if not isinstance(routing, Mapping):
        if not isinstance(setup, dict):
            return {}
        setattr(provider, "_draco_setup_metrics", None)
        return setup
    if not isinstance(setup, Mapping) and not isinstance(routing, Mapping):
        return {}
    if isinstance(setup, Mapping):
        setattr(provider, "_draco_setup_metrics", None)
    routing_receipt = copy.deepcopy(dict(routing)) if isinstance(routing, Mapping) else {}
    executed_plan = _provider_selection_plan_execution_snapshot(provider)
    if routing_receipt and isinstance(executed_plan, Mapping):
        serialized_plan = json_safe(copy.deepcopy(dict(executed_plan)))
        if not isinstance(serialized_plan, Mapping):
            raise TypeError("provider selection plan must serialize to an object")
        routing_receipt["selection_plan"] = copy.deepcopy(dict(serialized_plan))
    return {
        "latency_ms": coerce_metric_int(
            setup.get("latency_ms") if isinstance(setup, Mapping) else 0
        ),
        "usage": copy.deepcopy(
            setup.get("usage")
            if isinstance(setup, Mapping) and isinstance(setup.get("usage"), list)
            else []
        ),
        "routing": routing_receipt,
    }


class DryProvider:
    provider_name = "dry"

    def __init__(self, model: str, group: str) -> None:
        self.model = model
        self.group = group

    async def chat(self, messages: list[Message], tools=None, config=None):  # noqa: ANN001
        prompt = str(messages[-1].content if messages else "")
        text = f"[dry:{self.group}:{self.model}] {prompt[:160]}"
        yield TextDeltaEvent(text=text)
        yield DoneEvent(
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=max(1, len(text) // 4),
            model=self.model,
            provider="dry",
            cost_source="none",
        )

    async def list_models(self) -> list[Any]:
        return []


class DryEnsembleProvider:
    provider_name = "dry_ensemble"

    def __init__(
        self,
        *,
        group: str,
        profile: str,
        proposer_models: list[str] | None = None,
        model: str = "dry-aggregator",
        selection_mode: str = "",
    ) -> None:
        self.group = group
        self.profile = profile
        self.model = model
        self.proposer_models = proposer_models or ["dry-proposer-a", "dry-proposer-b"]
        self.selection_mode = selection_mode or profile
        self.selection_plan: dict[str, Any] = {}

    async def chat(self, messages: list[Message], tools=None, config=None):  # noqa: ANN001
        prompt = str(messages[-1].content if messages else "")
        sample_indexes: dict[str, int] = {}
        candidates: list[dict[str, Any]] = []
        selected_p = self.selection_plan.get("selected_P")
        selected_identities = selected_p if isinstance(selected_p, list) else []
        recovery_policy = self.selection_plan.get(
            "proposer_recovery_policy"
        )
        expected_recovery_policy = formal_proposer_recovery_policy_for_plan(
            self.selection_plan
        )
        formal_recovery = (
            isinstance(recovery_policy, Mapping)
            and expected_recovery_policy is not None
            and dict(recovery_policy) == expected_recovery_policy
        )
        expanded_selected_identities = list(
            expanded_proposer_slot_identities(self.selection_plan)
        )
        if formal_recovery and (
            len(expanded_selected_identities) != len(self.proposer_models)
        ):
            raise ValueError(
                "dry router_dynamic proposer sample roster is invalid"
            )
        slot_identities = (
            expanded_selected_identities
            if expanded_selected_identities
            else selected_identities
        )
        chat_sequence = int(
            getattr(self, "_draco_dry_chat_sequence", 0)
        ) + 1
        self._draco_dry_chat_sequence = chat_sequence
        recovery_scope_id = str(
            getattr(self, "_draco_dry_recovery_scope_id", "")
        )
        if formal_recovery and not recovery_scope_id:
            recovery_scope_id = hashlib.sha256(
                (
                    f"dry-run-turn:{self.group}:"
                    f"{self.selection_plan.get('decision_id')}"
                ).encode()
            ).hexdigest()[:32]
            self._draco_dry_recovery_scope_id = recovery_scope_id

        def physical_attempt_id(role: str, index: int, identity: str) -> str:
            return hashlib.sha256(
                (
                    f"{recovery_scope_id}:{chat_sequence}:"
                    f"{role}:{index}:{identity}"
                ).encode()
            ).hexdigest()[:32]

        for index, model in enumerate(self.proposer_models):
            sample_index = sample_indexes.get(model, 0)
            sample_indexes[model] = sample_index + 1
            identity = (
                slot_identities[index]
                if index < len(slot_identities)
                and isinstance(slot_identities[index], str)
                else f"dry:{model}"
            )
            provider, separator, selected_model = identity.partition(":")
            provider = provider.strip() if separator else "dry"
            selected_model = selected_model.strip() if separator else model
            candidate_text = f"Candidate {index + 1} for {prompt[:80]}"
            candidate = {
                "index": index,
                "sample_index": sample_index,
                "label": f"proposer_{index + 1}",
                "provider": provider,
                "requested_provider": provider,
                "model": selected_model,
                "requested_model": selected_model,
                "ok": True,
                "request_started": True,
                "physical_request_count": 1,
                "usage_reported": True,
                "usage_missing_count": 0,
                "stop_reason": "stop",
                "content": {
                    "text": candidate_text,
                    "chars": len(candidate_text),
                    "truncated": False,
                },
                "text": candidate_text,
                "input_tokens": 10 + index,
                "output_tokens": 8,
                "billed_cost": 0.0,
                "cost_source": "none",
            }
            if formal_recovery:
                candidate["execution"] = {
                    "actual_provider": provider,
                    "actual_model": selected_model,
                    "requested_provider": provider,
                    "requested_model": selected_model,
                    "physical_attempts": [
                        {
                            "attempt": 1,
                            "request_started": True,
                            "stream_closed": True,
                            "physical_attempt_id": physical_attempt_id(
                                "proposer",
                                index,
                                identity,
                            ),
                            "identity": identity,
                            "outcome": "succeeded",
                        }
                    ],
                }
            candidates.append(candidate)
        shuffle_candidates = (
            self.selection_plan.get("effective_shuffle_candidates") is True
        )
        configured_candidate_order_seed = self.selection_plan.get(
            "configured_candidate_order_seed"
        )
        candidate_order_seed = None
        candidate_display_order: list[int] = []
        if shuffle_candidates:
            candidate_order_seed = (
                configured_candidate_order_seed
                if configured_candidate_order_seed is not None
                else random.SystemRandom().getrandbits(64)
            )
            candidate_display_order = [
                int(candidate["index"]) for candidate in candidates
            ]
            random.Random(candidate_order_seed).shuffle(candidate_display_order)
        text = f"[dry:{self.group}:{self.profile}] fused answer for {prompt[:120]}"
        proposer_usage = []
        for candidate in candidates:
            unit = {
                "role": "proposer",
                "provider": candidate["provider"],
                "model": candidate["model"],
                "requested_provider": candidate["requested_provider"],
                "requested_model": candidate["requested_model"],
                "request_count": 1,
                "input_tokens": candidate["input_tokens"],
                "output_tokens": candidate["output_tokens"],
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "billed_cost": 0.0,
                "cost_source": "none",
            }
            if formal_recovery:
                attempt_id = candidate["execution"][
                    "physical_attempts"
                ][0]["physical_attempt_id"]
                unit["physical_attempt_id"] = attempt_id
                unit["provider_usage"] = {
                    "physical_attempt_id": attempt_id,
                }
            proposer_usage.append(unit)
        yield TextDeltaEvent(text=text)
        aggregator_provider = (
            str(self.selection_plan.get("selected_A") or "dry:").partition(":")[0].strip() or "dry"
        )
        aggregator_identity = f"{aggregator_provider}:{self.model}"
        raw_aggregator_candidates = self.selection_plan.get("aggregator_candidates")
        aggregator_candidates = (
            [
                str(identity).strip()
                for identity in raw_aggregator_candidates
                if str(identity).strip()
            ]
            if isinstance(raw_aggregator_candidates, list)
            else []
        )
        if not aggregator_candidates or aggregator_candidates[0] != aggregator_identity:
            aggregator_candidates = [aggregator_identity]
        self.selection_plan["aggregator_candidates"] = list(aggregator_candidates)
        aggregator_attempt_id = physical_attempt_id(
            "aggregator",
            0,
            aggregator_identity,
        )
        selection_plan_fingerprint = ""
        if formal_recovery:
            from opensquilla.provider.protocol import (
                provider_retry_roster_fingerprint,
            )

            selection_plan_fingerprint = (
                provider_retry_roster_fingerprint(self.selection_plan)
            )
        yield DoneEvent(
            input_tokens=sum(int(candidate["input_tokens"]) for candidate in candidates) + 21,
            output_tokens=max(1, len(text) // 4),
            model=self.model,
            provider=aggregator_provider,
            model_usage_breakdown=[
                *proposer_usage,
                {
                    "role": "aggregator",
                    "provider": aggregator_provider,
                    "model": self.model,
                    "requested_provider": aggregator_provider,
                    "requested_model": self.model,
                    "request_count": 1,
                    "input_tokens": 21,
                    "output_tokens": max(1, len(text) // 4),
                    "cached_tokens": 0,
                    "cache_write_tokens": 0,
                    "billed_cost": 0.0,
                    "cost_source": "none",
                    **(
                        {
                            "physical_attempt_id": (
                                aggregator_attempt_id
                            ),
                            "provider_usage": {
                                "physical_attempt_id": (
                                    aggregator_attempt_id
                                ),
                            },
                        }
                        if formal_recovery
                        else {}
                    ),
                },
            ],
            ensemble_trace={
                "mode": "b5_fusion",
                "profile": self.profile,
                "selection_strategy": self.selection_mode,
                "selection_plan": dict(self.selection_plan),
                "successful_proposers": len(candidates),
                "total_candidates": len(candidates),
                "fallback_used": False,
                "candidates": candidates,
                "shuffle_candidates": shuffle_candidates,
                "configured_candidate_order_seed": configured_candidate_order_seed,
                "candidate_order_seed": candidate_order_seed,
                "candidate_order_seed_source": (
                    "disabled"
                    if not shuffle_candidates
                    else "configured"
                    if configured_candidate_order_seed is not None
                    else "system_random"
                ),
                "candidate_display_order": candidate_display_order,
                "proposer_tools": getattr(self, "proposer_tools", False),
                "aggregator_tools": getattr(self, "aggregator_tools", True),
                "aggregator_recovery": {
                    "schema": "opensquilla.ensemble-aggregator-recovery/v1",
                    "mode": getattr(
                        self,
                        "aggregator_recovery_mode",
                        "serving",
                    ),
                    "candidate_count": len(aggregator_candidates),
                    "candidate_ids": list(aggregator_candidates),
                    "max_tokens_cap": getattr(
                        self,
                        "aggregator_max_tokens_cap",
                        65_536,
                    ),
                    "visible_answer_reserve_tokens": getattr(
                        self,
                        "aggregator_visible_answer_reserve_tokens",
                        8_192,
                    ),
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
                            "requested_provider": aggregator_provider,
                            "requested_model": self.model,
                            "actual_provider": aggregator_provider,
                            "actual_model": self.model,
                            **(
                                {
                                    "physical_attempt_id": (
                                        aggregator_attempt_id
                                    ),
                                    "usage_reported": True,
                                    "usage_missing_count": 0,
                                }
                                if formal_recovery
                                else {}
                            ),
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
                **(
                    {
                        "proposer_recovery": {
                            "schema": (
                                PROPOSER_RECOVERY_SCHEMA
                            ),
                            "selection_plan_fingerprint": (
                                selection_plan_fingerprint
                            ),
                            "scope": "run_turn",
                            "scope_id": recovery_scope_id,
                            "max_additional_physical_requests": 3,
                            "external_physical_requests_reserved": 0,
                            "additional_physical_requests_started": 0,
                            "remaining_additional_physical_requests": 3,
                            "quorum_required": 2,
                            "quorum_reached": len(candidates) >= 2,
                            "cumulative_excluded_identities": [],
                            "visited_identities": [],
                            "executed_proposer_roster_before": list(
                                slot_identities
                            ),
                            "executed_proposer_roster_after": list(
                                slot_identities
                            ),
                            "attempts": [],
                        }
                    }
                    if formal_recovery
                    else {}
                ),
                "executed_A": aggregator_identity,
                "fallback_reason": "",
                "run_outcome": "success",
                "delivery_outcome": "complete",
                "final_request_role": "aggregator",
                "final_request": {
                    "role": "aggregator",
                    "request_started": True,
                    "execution": {
                        "provider": aggregator_provider,
                        "actual_provider": aggregator_provider,
                        "model": self.model,
                        "actual_model": self.model,
                        "requested_provider": aggregator_provider,
                        "requested_model": self.model,
                    },
                    "usage": {
                        "provider": aggregator_provider,
                        "model": self.model,
                        "requested_provider": aggregator_provider,
                        "requested_model": self.model,
                        "stop_reason": "stop",
                        **(
                            {
                                "physical_attempt_id": (
                                    aggregator_attempt_id
                                ),
                                "provider_usage": {
                                    "physical_attempt_id": (
                                        aggregator_attempt_id
                                    ),
                                },
                            }
                            if formal_recovery
                            else {}
                        ),
                    },
                    "output": {
                        "text": text,
                        "chars": len(text),
                        "truncated": False,
                    },
                },
                "llm_request_count": len(candidates) + 1,
                "physical_request_count": len(candidates) + 1,
            },
        )

    async def list_models(self) -> list[Any]:
        return []


def select_tasks_by_ids(
    tasks: list[dict[str, Any]],
    requested_task_ids: list[str] | None,
) -> list[dict[str, Any]]:
    """Select an exact task subset while preserving reference-input order."""

    if not requested_task_ids:
        return tasks
    normalized = [str(task_id).strip() for task_id in requested_task_ids]
    if any(not task_id for task_id in normalized):
        raise ValueError("--task-ids values must be non-empty")
    duplicates = sorted(task_id for task_id in set(normalized) if normalized.count(task_id) > 1)
    if duplicates:
        raise ValueError(f"duplicate --task-ids value(s): {', '.join(duplicates)}")

    source_counts: dict[str, int] = {}
    for task in tasks:
        task_id = str(task["id"])
        source_counts[task_id] = source_counts.get(task_id, 0) + 1
    missing = sorted(task_id for task_id in normalized if task_id not in source_counts)
    if missing:
        raise ValueError(f"unknown --task-ids value(s): {', '.join(missing)}")
    repeated_source_ids = sorted(
        task_id for task_id in normalized if source_counts.get(task_id, 0) != 1
    )
    if repeated_source_ids:
        raise ValueError(
            "selected task id(s) are not unique in the input: " + ", ".join(repeated_source_ids)
        )
    requested = set(normalized)
    return [task for task in tasks if str(task["id"]) in requested]


def parse_groups(raw: str) -> list[str]:
    groups = [item.strip().upper() for item in raw.split(",") if item.strip()]
    if not groups:
        raise ValueError("--groups must contain at least one experiment group")
    unknown = [group for group in groups if group not in GROUP_SPECS]
    if unknown:
        raise ValueError(f"unknown group(s): {', '.join(unknown)}")
    duplicates = sorted(group for group in set(groups) if groups.count(group) > 1)
    if duplicates:
        raise ValueError(f"duplicate group(s): {', '.join(duplicates)}")
    return groups


def normalize_domain(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urlparse(raw)
        host = parsed.hostname or parsed.netloc
    else:
        host = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        host = urlparse(f"//{host}").hostname or host
    return host.strip().lstrip("*.").strip(".")


def parse_domain_list(raw: Any) -> list[str]:
    if raw is None:
        values: list[Any] = list(DEFAULT_CONTAMINATION_BLOCKED_DOMAINS)
    elif isinstance(raw, str):
        values = raw.split(",")
    else:
        values = list(raw)
    domains: list[str] = []
    for value in values:
        domain = normalize_domain(value)
        if domain and domain not in domains:
            domains.append(domain)
    return domains


def positive_int_value(raw: Any, *, default: int, field: str) -> int:
    value = default if raw is None else raw
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def bounded_int_value(
    raw: Any,
    *,
    default: int,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    value = positive_int_value(raw, default=default, field=field)
    if value < minimum or value > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def parse_csv_values(raw: Any, *, default: tuple[str, ...] = ()) -> list[str]:
    value = raw if raw is not None else ",".join(default)
    if isinstance(value, str):
        candidates = value.split(",")
    elif isinstance(value, tuple | list):
        candidates = list(value)
    else:
        candidates = [str(value)]
    items: list[str] = []
    for candidate in candidates:
        item = str(candidate).strip()
        if item and item not in items:
            items.append(item)
    return items


def float_range_value(
    raw: Any,
    *,
    default: float,
    field: str,
    minimum: float,
    maximum: float,
) -> float:
    value = default if raw is None else raw
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return parsed


def approx_chars_for_content_tokens(tokens: int) -> int:
    return max(100, int(tokens) * 4)


def openrouter_server_tool_settings(
    args: argparse.Namespace | None,
    *,
    blocked_domains: list[str],
) -> dict[str, Any]:
    search_context_size = (
        str(
            getattr(
                args,
                "openrouter_web_search_context_size",
                DEFAULT_OPENROUTER_WEB_SEARCH_CONTEXT_SIZE,
            )
            or DEFAULT_OPENROUTER_WEB_SEARCH_CONTEXT_SIZE
        )
        .strip()
        .lower()
    )
    if search_context_size not in {"low", "medium", "high"}:
        raise ValueError("openrouter_web_search_context_size must be one of: low, medium, high")
    web_search = {
        "type": "openrouter:web_search",
        "parameters": {
            "engine": str(
                getattr(
                    args,
                    "openrouter_web_search_engine",
                    DEFAULT_OPENROUTER_WEB_SEARCH_ENGINE,
                )
                or DEFAULT_OPENROUTER_WEB_SEARCH_ENGINE
            ).strip(),
            "max_results": positive_int_value(
                getattr(args, "openrouter_web_search_max_results", None),
                default=DEFAULT_OPENROUTER_WEB_SEARCH_MAX_RESULTS,
                field="openrouter_web_search_max_results",
            ),
            "max_total_results": positive_int_value(
                getattr(args, "openrouter_web_search_max_total_results", None),
                default=DEFAULT_OPENROUTER_WEB_SEARCH_MAX_TOTAL_RESULTS,
                field="openrouter_web_search_max_total_results",
            ),
            "search_context_size": search_context_size,
            "excluded_domains": blocked_domains,
        },
    }
    web_fetch = {
        "type": "openrouter:web_fetch",
        "parameters": {
            "engine": str(
                getattr(
                    args,
                    "openrouter_web_fetch_engine",
                    DEFAULT_OPENROUTER_WEB_FETCH_ENGINE,
                )
                or DEFAULT_OPENROUTER_WEB_FETCH_ENGINE
            ).strip(),
            "max_uses": positive_int_value(
                getattr(args, "openrouter_web_fetch_max_uses", None),
                default=DEFAULT_OPENROUTER_WEB_FETCH_MAX_USES,
                field="openrouter_web_fetch_max_uses",
            ),
            "max_content_tokens": positive_int_value(
                getattr(args, "openrouter_web_fetch_max_content_tokens", None),
                default=DEFAULT_OPENROUTER_WEB_FETCH_MAX_CONTENT_TOKENS,
                field="openrouter_web_fetch_max_content_tokens",
            ),
            "blocked_domains": blocked_domains,
        },
    }
    return {
        "web_search": web_search,
        "web_fetch": web_fetch,
    }


def openrouter_fusion_tool_settings(args: argparse.Namespace | None) -> dict[str, Any]:
    analysis_models = parse_csv_values(
        getattr(args, "openrouter_fusion_analysis_models", None),
        default=DEFAULT_OPENROUTER_FUSION_ANALYSIS_MODELS,
    )
    if not 1 <= len(analysis_models) <= 8:
        raise ValueError("openrouter_fusion_analysis_models must contain 1 to 8 models")
    judge_model = str(
        getattr(args, "openrouter_fusion_model", DEFAULT_OPENROUTER_FUSION_MODEL)
        or DEFAULT_OPENROUTER_FUSION_MODEL
    ).strip()
    if not judge_model:
        raise ValueError("openrouter_fusion_model must not be empty")
    reasoning_effort = (
        str(
            getattr(
                args,
                "openrouter_fusion_reasoning_effort",
                DEFAULT_OPENROUTER_FUSION_REASONING_EFFORT,
            )
            or DEFAULT_OPENROUTER_FUSION_REASONING_EFFORT
        )
        .strip()
        .lower()
    )
    if reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh", "max"}:
        raise ValueError(
            "openrouter_fusion_reasoning_effort must be one of: "
            "minimal, low, medium, high, xhigh, max"
        )
    return {
        "type": "openrouter:fusion",
        "parameters": {
            "analysis_models": analysis_models,
            "model": judge_model,
            "max_tool_calls": bounded_int_value(
                getattr(args, "openrouter_fusion_max_tool_calls", None),
                default=DEFAULT_OPENROUTER_FUSION_MAX_TOOL_CALLS,
                field="openrouter_fusion_max_tool_calls",
                minimum=1,
                maximum=16,
            ),
            "max_completion_tokens": positive_int_value(
                getattr(args, "openrouter_fusion_max_completion_tokens", None),
                default=DEFAULT_OPENROUTER_FUSION_MAX_COMPLETION_TOKENS,
                field="openrouter_fusion_max_completion_tokens",
            ),
            "reasoning": {"effort": reasoning_effort},
            "temperature": float_range_value(
                getattr(args, "openrouter_fusion_temperature", None),
                default=DEFAULT_OPENROUTER_FUSION_TEMPERATURE,
                field="openrouter_fusion_temperature",
                minimum=0.0,
                maximum=2.0,
            ),
        },
    }


def benchmark_tool_policy(args: argparse.Namespace | None = None) -> dict[str, Any]:
    mode = str(getattr(args, "tool_mode", RUNNER_MODE) or RUNNER_MODE).strip()
    blocked_domains = parse_domain_list(getattr(args, "contamination_blocked_domains", None))
    if mode not in SUPPORTED_TOOL_MODES:
        raise ValueError(f"unknown tool mode: {mode}")
    if mode == TOOL_MODE_LOCAL_WEB_TOOLS:
        if not blocked_domains:
            raise ValueError("DRACO research-tool runs require contamination-blocked domains")
        local_search_max_results = positive_int_value(
            getattr(args, "openrouter_web_search_max_results", None),
            default=DEFAULT_OPENROUTER_WEB_SEARCH_MAX_RESULTS,
            field="openrouter_web_search_max_results",
        )
        local_search_provider = (
            str(
                getattr(args, "local_web_search_provider", DEFAULT_LOCAL_WEB_SEARCH_PROVIDER)
                or DEFAULT_LOCAL_WEB_SEARCH_PROVIDER
            ).strip()
            or DEFAULT_LOCAL_WEB_SEARCH_PROVIDER
        )
        if local_search_provider not in SUPPORTED_LOCAL_WEB_SEARCH_PROVIDERS:
            raise ValueError(
                "local_web_search_provider must be one of: "
                f"{', '.join(SUPPORTED_LOCAL_WEB_SEARCH_PROVIDERS)}"
            )
        local_search_api_key_env = str(
            getattr(args, "local_web_search_api_key_env", "") or ""
        ).strip()
        local_fetch_max_content_tokens = positive_int_value(
            getattr(args, "openrouter_web_fetch_max_content_tokens", None),
            default=DEFAULT_OPENROUTER_WEB_FETCH_MAX_CONTENT_TOKENS,
            field="openrouter_web_fetch_max_content_tokens",
        )
        return {
            "tool_mode": mode,
            "tools_enabled": True,
            "tool_names": ["web_search", "web_fetch"],
            "local_web_tools": {
                "web_search": {
                    "excluded_domains": blocked_domains,
                    "max_results": local_search_max_results,
                    "provider": local_search_provider,
                    "api_key_env": local_search_api_key_env,
                },
                "web_fetch": {
                    "blocked_domains": blocked_domains,
                    "max_content_tokens": local_fetch_max_content_tokens,
                    "max_content_chars": approx_chars_for_content_tokens(
                        local_fetch_max_content_tokens
                    ),
                    "allow_firecrawl": bool(getattr(args, "allow_firecrawl_web_fetch", False)),
                },
            },
            "contamination_blocked_domains": blocked_domains,
            "contamination_controls": {
                "status": "enforced_by_local_web_tools",
                "web_search_field": "excluded_domains_query_and_result_filter",
                "web_fetch_field": "blocked_domains",
            },
        }
    if mode == TOOL_MODE_OPENROUTER_SERVER_TOOLS:
        if not blocked_domains:
            raise ValueError("DRACO research-tool runs require contamination-blocked domains")
        server_tools = openrouter_server_tool_settings(
            args,
            blocked_domains=blocked_domains,
        )
        return {
            "tool_mode": mode,
            "tools_enabled": True,
            "tool_names": [
                server_tools["web_search"]["type"],
                server_tools["web_fetch"]["type"],
            ],
            "openrouter_server_tools": server_tools,
            "contamination_blocked_domains": blocked_domains,
            "contamination_controls": {
                "status": "enforced_by_openrouter_server_tools",
                "web_search_field": "excluded_domains",
                "web_fetch_field": "blocked_domains",
            },
        }
    return {
        "tool_mode": mode,
        "tools_enabled": False,
        "tool_names": [],
        "contamination_blocked_domains": blocked_domains,
        "contamination_controls": {
            "status": "not_applicable_no_external_tools",
            "web_search_field": "excluded_domains",
            "web_fetch_field": "blocked_domains",
        },
    }


def group_uses_openrouter_fusion(group: str) -> bool:
    spec = GROUP_SPECS[group]
    return spec.get("server_tool_profile") == "openrouter_fusion"


def validate_runner_mode_for_groups(runner_mode: str, groups: list[str]) -> None:
    if runner_mode != RUNNER_MODE_AGENT_LOOP:
        return
    fusion_groups = [group for group in groups if group_uses_openrouter_fusion(group)]
    if fusion_groups:
        raise ValueError(
            "OpenRouter Fusion experiment groups are provider-level server-side "
            "agent baselines; run "
            f"{','.join(fusion_groups)} with --runner-mode=provider"
        )


def validate_tool_mode_for_runner(
    runner_mode: str,
    tool_mode: str,
    *,
    smoke_only: bool = False,
) -> None:
    if smoke_only:
        return
    if tool_mode == TOOL_MODE_LOCAL_WEB_TOOLS and runner_mode != RUNNER_MODE_AGENT_LOOP:
        raise ValueError(
            "--tool-mode=local_web_tools requires --runner-mode=agent_loop so local "
            "tool calls are executed rather than only forwarded to the provider."
        )
    if tool_mode == TOOL_MODE_OPENROUTER_SERVER_TOOLS and runner_mode != RUNNER_MODE_PROVIDER:
        raise ValueError("--tool-mode=openrouter_server_tools requires --runner-mode=provider.")


def benchmark_tool_policy_for_group(
    tool_policy: dict[str, Any],
    group: str,
    *,
    args: argparse.Namespace | None = None,
) -> dict[str, Any]:
    if not group_uses_openrouter_fusion(group):
        return tool_policy
    fusion_tool = openrouter_fusion_tool_settings(args)
    fusion_tool_name = str(fusion_tool.get("type") or "openrouter:fusion")
    return {
        **tool_policy,
        "tools_enabled": True,
        "tool_names": [fusion_tool_name],
        "openrouter_fusion_enabled": True,
        "openrouter_fusion_only": True,
        "openrouter_fusion_tool": fusion_tool,
        "openrouter_fusion_tool_choice": "required",
        "contamination_controls": {
            **dict(tool_policy.get("contamination_controls") or {}),
            "fusion_status": "internal_web_domain_controls_not_exposed",
            "fusion_internal_web_tools": (
                "openrouter_fusion_enables_internal_web_search_and_fetch; "
                "domain exclusion is not exposed in the documented fusion parameters"
            ),
        },
    }


def benchmark_tool_policies_for_groups(
    tool_policy: dict[str, Any],
    groups: list[str],
    *,
    args: argparse.Namespace | None = None,
) -> dict[str, dict[str, Any]]:
    return {
        group: benchmark_tool_policy_for_group(tool_policy, group, args=args) for group in groups
    }


def _local_web_tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="web_search",
            description=(
                "Search the web and return result titles, URLs, and snippets. "
                "Benchmark leakage domains are excluded and filtered."
            ),
            input_schema=ToolInputSchema(
                properties={
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return.",
                    },
                },
                required=["query"],
            ),
        ),
        ToolDefinition(
            name="web_fetch",
            description=(
                "Fetch a URL and extract readable content. Benchmark leakage domains are blocked."
            ),
            input_schema=ToolInputSchema(
                properties={
                    "url": {"type": "string", "description": "URL to fetch."},
                    "extract_mode": {
                        "type": "string",
                        "description": "Extraction mode, usually markdown.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters to return.",
                    },
                },
                required=["url"],
            ),
        ),
    ]


def _provider_tool_definition(
    provider_tool: dict[str, Any],
    *,
    description: str,
) -> ToolDefinition:
    tool_type = str(provider_tool.get("type") or "")
    return ToolDefinition(
        name=tool_type,
        description=description,
        input_schema=ToolInputSchema(),
        provider_tool=provider_tool,
    )


def benchmark_tools_for_policy(tool_policy: dict[str, Any]) -> list[ToolDefinition] | None:
    fusion_enabled = bool(tool_policy.get("openrouter_fusion_enabled"))
    if not tool_policy.get("tools_enabled") and not fusion_enabled:
        return None
    tools: list[ToolDefinition] = []
    if tool_policy.get("tool_mode") == TOOL_MODE_LOCAL_WEB_TOOLS and not tool_policy.get(
        "openrouter_fusion_only"
    ):
        tools.extend(_local_web_tool_definitions())
    server_tools = tool_policy.get("openrouter_server_tools") or {}
    if not tool_policy.get("openrouter_fusion_only"):
        for key, description in (
            ("web_search", "OpenRouter server-side web search."),
            ("web_fetch", "OpenRouter server-side web fetch."),
        ):
            provider_tool = server_tools.get(key)
            if not isinstance(provider_tool, dict):
                continue
            tools.append(_provider_tool_definition(provider_tool, description=description))
    fusion_tool = tool_policy.get("openrouter_fusion_tool")
    if fusion_enabled and isinstance(fusion_tool, dict):
        tools.append(
            _provider_tool_definition(
                fusion_tool,
                description=("OpenRouter Fusion server-side multi-model deliberation."),
            )
        )
    return tools or None


def local_web_search_max_results(tool_policy: dict[str, Any]) -> int:
    local_policy = tool_policy.get("local_web_tools") or {}
    search_defaults = local_policy.get("web_search") or {}
    return positive_int_value(
        search_defaults.get("max_results"),
        default=DEFAULT_OPENROUTER_WEB_SEARCH_MAX_RESULTS,
        field="local_web_search_max_results",
    )


def local_web_search_provider(tool_policy: dict[str, Any]) -> str:
    local_policy = tool_policy.get("local_web_tools") or {}
    search_defaults = local_policy.get("web_search") or {}
    provider = (
        str(search_defaults.get("provider") or DEFAULT_LOCAL_WEB_SEARCH_PROVIDER).strip()
        or DEFAULT_LOCAL_WEB_SEARCH_PROVIDER
    )
    if provider not in SUPPORTED_LOCAL_WEB_SEARCH_PROVIDERS:
        raise ValueError(
            "local_web_search_provider must be one of: "
            f"{', '.join(SUPPORTED_LOCAL_WEB_SEARCH_PROVIDERS)}"
        )
    return provider


def local_web_search_api_key_env(tool_policy: dict[str, Any]) -> str:
    local_policy = tool_policy.get("local_web_tools") or {}
    search_defaults = local_policy.get("web_search") or {}
    return str(search_defaults.get("api_key_env") or "").strip()


def local_web_fetch_max_chars(tool_policy: dict[str, Any]) -> int:
    local_policy = tool_policy.get("local_web_tools") or {}
    fetch_defaults = local_policy.get("web_fetch") or {}
    chars = fetch_defaults.get("max_content_chars")
    if isinstance(chars, int | float) and not isinstance(chars, bool):
        return max(100, int(chars))
    tokens = positive_int_value(
        fetch_defaults.get("max_content_tokens"),
        default=DEFAULT_OPENROUTER_WEB_FETCH_MAX_CONTENT_TOKENS,
        field="local_web_fetch_max_content_tokens",
    )
    return approx_chars_for_content_tokens(tokens)


def bounded_tool_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    try:
        if isinstance(value, bool):
            parsed = default
        elif isinstance(value, int | float):
            parsed = int(value)
        else:
            parsed = int(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


def configure_local_web_search_runtime(
    config: GatewayConfig,
    tool_policy: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if tool_policy.get("tool_mode") != TOOL_MODE_LOCAL_WEB_TOOLS:
        return {}

    import opensquilla.search.providers.brave  # noqa: F401
    import opensquilla.search.providers.duckduckgo  # noqa: F401
    from opensquilla.search.registry import get_provider_spec
    from opensquilla.tools.builtin.web import configure_search

    configured_provider = local_web_search_provider(tool_policy)
    provider = configured_provider
    env_key = local_web_search_api_key_env(tool_policy)
    if not env_key:
        env_key = str(getattr(config, "search_api_key_env", "") or "").strip()
    try:
        spec = get_provider_spec(provider)
    except Exception as exc:
        raise ValueError(f"unknown local web search provider: {provider}") from exc
    if not spec.runtime_supported:
        raise ValueError(f"local web search provider is not runtime-supported: {provider}")
    api_key = ""
    api_key_source = ""
    credential_status = "not_required"
    if spec.requires_api_key:
        credential_status = "configured"
        if not env_key:
            env_key = str(getattr(spec, "env_key", "") or "").strip()
        if env_key and os.environ.get(env_key):
            api_key = str(os.environ.get(env_key) or "")
            api_key_source = f"env:{env_key}"
        if not api_key:
            api_key = str(getattr(config, "search_api_key", "") or "").strip()
            api_key_source = "config" if api_key else ""
        if not api_key:
            env_hint = env_key or getattr(spec, "env_key", "") or "the provider API key env var"
            if not dry_run:
                raise ValueError(
                    f"local web search provider '{provider}' requires an API key; "
                    f"set {env_hint} or choose --local-web-search-provider duckduckgo"
                )
            credential_status = "missing_allowed_dry_run"
    else:
        env_key = ""

    runtime_max_results = local_web_search_max_results(tool_policy)
    proxy = str(getattr(config, "search_proxy", "") or "").strip()
    fallback_policy = str(getattr(config, "search_fallback_policy", "off") or "off")
    diagnostics = bool(getattr(config, "search_diagnostics", False))
    use_env_proxy = bool(getattr(config, "search_use_env_proxy", False))
    runtime_configured = bool(api_key) or not spec.requires_api_key
    if runtime_configured:
        configure_search(
            provider_name=provider,
            max_results=runtime_max_results,
            api_key=api_key,
            proxy=proxy,
            use_env_proxy=use_env_proxy,
            fallback_policy=fallback_policy,
            diagnostics=diagnostics,
        )
    return {
        "configured_provider": configured_provider,
        "provider": provider,
        "max_results": runtime_max_results,
        "api_key_configured": bool(api_key),
        "api_key_source": api_key_source,
        "api_key_env": env_key,
        "credential_status": credential_status,
        "runtime_configured": runtime_configured,
        "proxy_configured": bool(proxy),
        "use_env_proxy": use_env_proxy,
        "fallback_policy": fallback_policy,
        "diagnostics": diagnostics,
    }


def configure_benchmark_sandbox_runtime(
    config: GatewayConfig,
    tool_policy: dict[str, Any],
) -> dict[str, Any]:
    if tool_policy.get("tool_mode") != TOOL_MODE_LOCAL_WEB_TOOLS:
        return {}

    from opensquilla.sandbox.integration import configure_runtime

    workspace = Path(config.workspace_dir) if config.workspace_dir else ROOT
    runtime = configure_runtime(
        config.sandbox,
        approval_queue=_BenchmarkApprovalQueue(),
        workspace=workspace,
    )
    return {
        "configured": True,
        "backend": runtime.backend.name,
        "workspace": str(runtime.workspace),
        "approval_queue": "auto_deny_unattended",
        "effective": runtime.effective.as_dict(),
    }


def configure_local_web_fetch_runtime(tool_policy: dict[str, Any]) -> dict[str, Any]:
    if tool_policy.get("tool_mode") != TOOL_MODE_LOCAL_WEB_TOOLS:
        return {}
    local_policy = tool_policy.get("local_web_tools") or {}
    fetch_policy = local_policy.get("web_fetch") or {}
    allow_firecrawl = bool(fetch_policy.get("allow_firecrawl"))
    firecrawl_was_configured = bool(os.environ.get("FIRECRAWL_API_KEY", "").strip())
    if firecrawl_was_configured and not allow_firecrawl:
        os.environ.pop("FIRECRAWL_API_KEY", None)
    return {
        "extractor_mode": "auto_local_first",
        "firecrawl_allowed": allow_firecrawl,
        "firecrawl_api_key_active": firecrawl_was_configured and allow_firecrawl,
        "firecrawl_disabled_for_reproducibility": (
            firecrawl_was_configured and not allow_firecrawl
        ),
        "external_fetch_cost_tracking": (
            "required_when_firecrawl_used" if allow_firecrawl else "not_applicable"
        ),
    }


def blocked_domain_match(url: str, blocked_domains: list[str]) -> str:
    domain = normalize_domain(url)
    if not domain:
        return ""
    for blocked in blocked_domains:
        if domain == blocked or domain.endswith(f".{blocked}"):
            return blocked
    return ""


def append_search_exclusions(query: str, blocked_domains: list[str]) -> str:
    clean_query = str(query or "").strip()
    exclusions = " ".join(f"-site:{domain}" for domain in blocked_domains if domain)
    return f"{clean_query} {exclusions}".strip() if exclusions else clean_query


def filter_blocked_search_results(
    payload: dict[str, Any],
    *,
    blocked_domains: list[str],
    original_query: str,
    executed_query: str,
) -> dict[str, Any]:
    filtered = dict(payload)
    removed_by_field: dict[str, list[dict[str, str]]] = {}
    for field_name in ("results", "sources"):
        items = filtered.get(field_name)
        removed: list[dict[str, str]] = []
        kept: list[Any] = []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            match = blocked_domain_match(str(item.get("url") or ""), blocked_domains)
            if match:
                removed.append(
                    {
                        "url": str(item.get("url") or ""),
                        "blocked_domain": match,
                    }
                )
            else:
                kept.append(item)
        filtered[field_name] = kept
        removed_by_field[field_name] = removed
    filtered["query"] = original_query
    filtered["executed_query"] = executed_query
    filtered["blocked_domains"] = blocked_domains
    blocked_results = removed_by_field.get("results", [])
    blocked_sources = removed_by_field.get("sources", [])
    filtered["blocked_result_count"] = len(blocked_results)
    filtered["blocked_source_count"] = len(blocked_sources)
    if blocked_results:
        filtered["blocked_results"] = blocked_results
    if blocked_sources:
        filtered["blocked_sources"] = blocked_sources
    return filtered


def build_local_web_tool_registry(tool_policy: dict[str, Any]) -> ToolRegistry:
    registry = ToolRegistry()
    blocked_domains = parse_domain_list(tool_policy.get("contamination_blocked_domains") or [])
    default_max_results = local_web_search_max_results(tool_policy)
    configured_search_provider = local_web_search_provider(tool_policy)
    default_fetch_max_chars = local_web_fetch_max_chars(tool_policy)

    async def _web_search(query: str, max_results: int | None = None) -> str:
        from opensquilla.tools.builtin.web import run_web_search_payload

        original_query = str(query or "")
        executed_query = (
            append_search_exclusions(original_query, blocked_domains)
            if configured_search_provider == "duckduckgo"
            else original_query
        )
        limit = bounded_tool_int(
            max_results,
            default=default_max_results,
            minimum=1,
            maximum=default_max_results,
        )
        payload = await run_web_search_payload(
            executed_query,
            limit,
            exclude_domains=blocked_domains,
            provider=configured_search_provider,
        )
        filtered = filter_blocked_search_results(
            payload,
            blocked_domains=blocked_domains,
            original_query=original_query,
            executed_query=executed_query,
        )
        return json.dumps(filtered, ensure_ascii=False, indent=2)

    async def _web_fetch(
        url: str,
        extract_mode: str = "markdown",
        max_chars: int | None = None,
    ) -> str:
        from opensquilla.tools.builtin.web_fetch import web_fetch

        match = blocked_domain_match(str(url or ""), blocked_domains)
        if match:
            return json.dumps(
                {
                    "url": url,
                    "error_class": "BlockedDomain",
                    "error": (
                        "This URL belongs to a DRACO contamination-blocked domain "
                        f"({match}) and was not fetched."
                    ),
                    "blocked_domain": match,
                    "blocked_domains": blocked_domains,
                },
                ensure_ascii=False,
                indent=2,
            )
        effective_max_chars = bounded_tool_int(
            max_chars,
            default=default_fetch_max_chars,
            minimum=100,
            maximum=default_fetch_max_chars,
        )
        content = await web_fetch(
            url,
            extract_mode=extract_mode,
            max_chars=effective_max_chars,
        )
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return content
        final_url = str(payload.get("final_url") or payload.get("url") or "")
        final_match = blocked_domain_match(final_url, blocked_domains)
        if not final_match:
            return content
        return json.dumps(
            {
                "url": url,
                "final_url": final_url,
                "error_class": "BlockedDomain",
                "error": (
                    "This request redirected to a DRACO contamination-blocked domain "
                    f"({final_match}); fetched content was discarded."
                ),
                "blocked_domain": final_match,
                "blocked_domains": blocked_domains,
            },
            ensure_ascii=False,
            indent=2,
        )

    registry.register(
        ToolSpec(
            name="web_search",
            description=(
                "Search the web and return result titles, URLs, and snippets. "
                "Benchmark leakage domains are excluded and filtered."
            ),
            parameters={
                "query": {"type": "string", "description": "Search query."},
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return.",
                },
            },
            required=["query"],
            result_budget_class="external",
        ),
        _web_search,
    )
    registry.register(
        ToolSpec(
            name="web_fetch",
            description=(
                "Fetch a URL and extract readable content. Benchmark leakage domains are blocked."
            ),
            parameters={
                "url": {"type": "string", "description": "URL to fetch."},
                "extract_mode": {
                    "type": "string",
                    "description": "Extraction mode, usually markdown.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return.",
                },
            },
            required=["url"],
            result_budget_class="external",
        ),
        _web_fetch,
    )
    return registry


def build_benchmark_tool_context(
    *,
    task_id: str,
    group: str,
    tool_policy: dict[str, Any],
    output_dir: Path | None = None,
) -> ToolContext:
    scratch_dir = None
    if output_dir is not None:
        scratch_dir = str(output_dir / "scratch" / group / task_id)
    return ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        interaction_mode=InteractionMode.UNATTENDED,
        agent_id=f"draco-{group}",
        session_key=f"draco:{group}:{task_id}",
        task_id=task_id,
        allowed_tools={"web_search", "web_fetch"},
        workspace_dir=str(ROOT),
        workspace_strict=True,
        scratch_dir=scratch_dir,
        tool_run_budget_policy=build_webresearch_tool_run_budget_policy(
            max_single_fetch_chars=local_web_fetch_max_chars(tool_policy),
            max_web_search_results=local_web_search_max_results(tool_policy),
        ),
    )


async def run_local_web_tools_preflight(
    tool_policy: dict[str, Any],
    *,
    dry_run: bool = False,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 1.0,
    call_timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    """Fail fast unless the benchmark's real web tool path can search and fetch."""

    if tool_policy.get("tool_mode") != TOOL_MODE_LOCAL_WEB_TOOLS:
        return {}
    if dry_run:
        return {
            "status": "skipped_dry_run",
            "web_fetch_url": BENCHMARK_WEB_FETCH_PREFLIGHT_URL,
            "preflight_calls": {"web_search": 0, "web_fetch": 0},
            "cost_attribution": "no_preflight_calls_dry_run",
        }

    attempt_limit = max(1, int(max_attempts))
    timeout = max(1.0, float(call_timeout_seconds))
    call_counts = {"web_search": 0, "web_fetch": 0}

    async def _attempt(attempt: int) -> dict[str, Any]:
        registry = build_local_web_tool_registry(tool_policy)
        context = build_benchmark_tool_context(
            task_id=f"__local_web_tools_preflight_{attempt}__",
            group="preflight",
            tool_policy=tool_policy,
        )
        handler = build_tool_handler(registry, context)

        async def _call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            call_counts[tool_name] += 1
            try:
                result = await asyncio.wait_for(
                    handler(
                        ToolCall(
                            tool_use_id=f"draco-preflight-{attempt}-{tool_name}",
                            tool_name=tool_name,
                            arguments=arguments,
                        )
                    ),
                    timeout=timeout,
                )
            except TimeoutError as exc:
                raise RuntimeError(
                    f"DRACO local web tool {tool_name} preflight timed out after {timeout:.0f}s"
                ) from exc
            detail = str(result.content or "")[:500]
            if result.is_error:
                raise RuntimeError(f"DRACO local web tool {tool_name} preflight failed: {detail}")
            try:
                payload = json.loads(result.content)
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"DRACO local web tool {tool_name} preflight returned non-JSON: {detail}"
                ) from exc
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"DRACO local web tool {tool_name} preflight returned a non-object payload"
                )
            status = str(payload.get("status") or "").strip().casefold()
            reason = str(payload.get("reason") or "").strip().casefold()
            if (
                payload.get("error")
                or payload.get("error_class")
                or status in {"error", "failed", "denied", "approval_denied"}
                or "denied" in reason
                or "policy" in reason
            ):
                raise RuntimeError(f"DRACO local web tool {tool_name} preflight failed: {detail}")
            return payload

        search_payload = await _call(
            "web_search",
            {"query": BENCHMARK_WEB_PREFLIGHT_QUERY, "max_results": 1},
        )
        search_results = search_payload.get("results")
        if (
            search_payload.get("ok") is False
            or not isinstance(search_results, list)
            or not search_results
        ):
            raise RuntimeError("DRACO local web tool web_search preflight returned invalid results")
        if not any(
            isinstance(item, dict)
            and urlparse(str(item.get("url") or "")).scheme in {"http", "https"}
            and bool(urlparse(str(item.get("url") or "")).netloc)
            for item in search_results
        ):
            raise RuntimeError(
                "DRACO local web tool web_search preflight returned no valid HTTP(S) result"
            )

        fetch_payload = await _call(
            "web_fetch",
            {
                "url": BENCHMARK_WEB_FETCH_PREFLIGHT_URL,
                "extract_mode": "text",
                "max_chars": 1_000,
            },
        )
        fetched_text = fetch_payload.get("text")
        if not isinstance(fetched_text, str) or not fetched_text.strip():
            raise RuntimeError("DRACO local web tool web_fetch preflight returned no text")
        try:
            fetch_status = int(fetch_payload.get("status"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "DRACO local web tool web_fetch preflight returned no HTTP status"
            ) from exc
        if not 200 <= fetch_status < 400:
            raise RuntimeError(
                f"DRACO local web tool web_fetch preflight returned HTTP {fetch_status}"
            )

        return {
            "status": "passed",
            "attempts_used": attempt,
            "web_search_query": BENCHMARK_WEB_PREFLIGHT_QUERY,
            "web_search_result_count": len(search_results),
            "web_fetch_url": BENCHMARK_WEB_FETCH_PREFLIGHT_URL,
            "web_fetch_http_status": fetch_status,
            "web_fetch_text_chars": len(fetched_text),
            "preflight_calls": dict(call_counts),
            "cost_attribution": "setup_preflight_excluded_from_benchmark_row_metrics",
        }

    last_error: RuntimeError | None = None
    for attempt in range(1, attempt_limit + 1):
        try:
            return await _attempt(attempt)
        except RuntimeError as exc:
            last_error = exc
            if attempt < attempt_limit and retry_backoff_seconds > 0:
                await asyncio.sleep(float(retry_backoff_seconds) * (2 ** (attempt - 1)))
    assert last_error is not None
    raise RuntimeError(
        f"DRACO local web tools preflight failed after {attempt_limit} attempt(s): {last_error}"
    ) from last_error


def generation_thinking_policy(args: argparse.Namespace | None = None) -> dict[str, Any]:
    mode = DEFAULT_GENERATION_THINKING
    bundle = getattr(args, "_draco_experiment_config_bundle", None)
    experiment = bundle.config if isinstance(bundle, DracoExperimentConfigBundle) else None
    generation = experiment.generation if experiment is not None else None
    fallback_level = str(
        generation.default_thinking_level
        if generation is not None
        else DEFAULT_GENERATION_THINKING_FALLBACK
    )
    max_tokens_override = max(
        0,
        int(
            getattr(
                args,
                "generation_max_tokens",
                DEFAULT_GENERATION_MAX_TOKENS_OVERRIDE,
            )
            or DEFAULT_GENERATION_MAX_TOKENS_OVERRIDE
        ),
    )
    return {
        "generation_thinking": mode,
        "temperature": (
            generation.temperature if generation is not None else DEFAULT_GENERATION_TEMPERATURE
        ),
        "thinking_enabled": (generation.thinking_enabled if generation is not None else True),
        "thinking_level": "model-specific",
        "default_thinking_level": fallback_level,
        "thinking_budget_tokens": (
            generation.thinking_budget_tokens if generation is not None else "model-specific"
        ),
        "max_thinking_budget_tokens": (
            generation.thinking_budget_tokens
            if generation is not None
            else THINKING_BUDGETS[ThinkingLevel.XHIGH]
        ),
        "max_tokens": max_tokens_override or ChatConfig().max_tokens,
        "max_tokens_overridden": max_tokens_override > 0,
        "model_thinking_levels": (
            dict(generation.model_thinking_levels)
            if generation is not None
            else dict(DEFAULT_MODEL_MAX_GENERATION_THINKING)
        ),
        "require_highest_thinking": bool(
            generation.require_highest_thinking if generation is not None else False
        ),
        "applies_to": "single baselines and ensemble members",
    }


def _normalized_model_id(model: str | None) -> str:
    return str(model or "").strip().lower()


@cache
def _packaged_openrouter_thinking_levels() -> dict[str, tuple[str, ...]]:
    """Return the validated model-specific OpenRouter thinking ladder."""

    from opensquilla.provider.ranking_router import load_model_registry_snapshot

    levels_by_model: dict[str, tuple[str, ...]] = {}
    for row in load_model_registry_snapshot().get("models", []):
        facts = row.get("registry_facts") if isinstance(row, Mapping) else None
        if not isinstance(facts, Mapping):
            continue
        if str(facts.get("provider") or "").strip().lower() != "openrouter":
            continue
        model = _normalized_model_id(str(facts.get("model_id") or ""))
        raw_levels = facts.get("supported_thinking_levels")
        if (
            not model
            or not isinstance(raw_levels, Sequence)
            or isinstance(raw_levels, (str, bytes))
        ):
            continue
        levels = tuple(str(level).strip().lower() for level in raw_levels if str(level).strip())
        if levels:
            levels_by_model[model] = levels
    return levels_by_model


def _openrouter_supported_thinking_levels(model: str | None) -> tuple[str, ...]:
    return _packaged_openrouter_thinking_levels().get(_normalized_model_id(model), ())


def generation_thinking_for_model(
    model: str | None,
    policy: dict[str, Any] | None = None,
) -> str:
    policy = policy or generation_thinking_policy()
    mode = str(policy.get("generation_thinking") or DEFAULT_GENERATION_THINKING)
    if mode != GENERATION_THINKING_MODEL_MAX:
        return mode
    raw_mapping = policy.get("model_thinking_levels")
    mapping = raw_mapping if isinstance(raw_mapping, dict) else {}
    normalized_mapping = {
        _normalized_model_id(str(key)): str(value).strip().lower() for key, value in mapping.items()
    }
    normalized_model = _normalized_model_id(model)
    configured = normalized_mapping.get(normalized_model)
    supported = _openrouter_supported_thinking_levels(normalized_model)
    if supported:
        highest = supported[0]
        if configured is None:
            return highest
        if configured != highest and bool(policy.get("require_highest_thinking")):
            raise ValueError(
                f"OpenRouter model {model!r} freezes thinking={configured!r}, "
                f"but the packaged registry highest supported level is {highest!r}"
            )
        return highest
    return configured or str(
        policy.get("default_thinking_level") or DEFAULT_GENERATION_THINKING_FALLBACK
    )


def with_openrouter_model_capabilities(
    config: ChatConfig,
    model: str | None,
) -> ChatConfig:
    if config.model_capabilities is not None:
        return config
    capabilities = openrouter_static_capabilities(model or "")
    if capabilities is None:
        return config
    return config.model_copy(update={"model_capabilities": capabilities})


def generation_chat_config(
    policy: dict[str, Any],
    *,
    model: str | None = None,
    tool_choice: Any | None = None,
) -> ChatConfig:
    mode = generation_thinking_for_model(model, policy)
    budget_level = ThinkingLevel.MAX if mode == "max" else ThinkingLevel(mode)
    thinking_level: ThinkingLevel | str = "max" if mode == "max" else budget_level
    raw_budget = policy.get("thinking_budget_tokens")
    thinking_budget = (
        int(raw_budget)
        if isinstance(raw_budget, int | float) and not isinstance(raw_budget, bool)
        else THINKING_BUDGETS.get(budget_level, THINKING_BUDGETS[ThinkingLevel.XHIGH])
    )
    return with_openrouter_model_capabilities(
        ChatConfig(
            max_tokens=int(policy.get("max_tokens") or ChatConfig().max_tokens),
            temperature=policy.get("temperature"),
            thinking=bool(policy.get("thinking_enabled", True)),
            thinking_level=thinking_level,
            thinking_budget_tokens=thinking_budget,
            tool_choice=tool_choice,
        ),
        model,
    )


def _experiment_member_provider_config(
    member: DracoEnsembleMemberConfig,
    *,
    templates: list[ProviderConfig],
) -> ProviderConfig:
    validate_formal_draco_ensemble_member_binding(member)
    provider_id = member.provider.strip().lower()
    spec = get_provider_spec(provider_id)
    template = next(
        (item for item in templates if str(item.provider or "").strip().lower() == provider_id),
        None,
    )
    api_key = (
        str(os.environ.get(member.api_key_env, "") or "").strip()
        if member.api_key_env
        else ""
    )
    if template is not None:
        return replace(
            template,
            provider=provider_id,
            model=member.model,
            api_key=api_key or template.api_key,
            base_url=member.base_url or spec.default_base_url,
        )
    env_name = member.api_key_env or spec.env_key
    api_key = api_key or str(os.environ.get(env_name, "") or "").strip()
    return ProviderConfig(
        provider=provider_id,
        model=member.model,
        api_key=api_key,
        base_url=member.base_url or spec.default_base_url,
    )


def align_b2_provider_to_g12(
    provider: EnsembleProvider,
    experiment: DracoExperimentConfig,
) -> EnsembleProvider:
    """Apply the effective member settings observed in the reference G12 run."""

    ensemble = experiment.ensemble
    templates = [
        *(member.provider_config for member in provider.proposers),
        provider.aggregator.provider_config,
    ]

    def _member(member: DracoEnsembleMemberConfig) -> EnsembleMemberConfig:
        return EnsembleMemberConfig(
            provider_config=_experiment_member_provider_config(
                member,
                templates=templates,
            ),
            label=member.label,
            temperature=member.temperature,
            max_tokens=member.max_tokens,
            thinking=member.thinking,
            k=member.k,
        )

    pre_alignment = {
        "profile": provider.profile_name,
        "min_successful_proposers": provider.min_successful_proposers,
        "proposer_timeout_seconds": provider.proposer_timeout_seconds,
        "aggregator_timeout_seconds": provider.aggregator_timeout_seconds,
        "quorum_grace_seconds": provider.quorum_grace_seconds,
        "selection_plan": dict(provider.selection_plan),
    }
    provider.proposers = [_member(member) for member in ensemble.proposers]
    provider.aggregator = _member(ensemble.aggregator)
    provider.profile_name = ensemble.profile_name
    provider.min_successful_proposers = legal_proposer_quorum(
        sum(max(1, int(member.k)) for member in ensemble.proposers)
    )
    provider.proposer_timeout_seconds = experiment.timeouts.proposer_seconds
    provider.aggregator_timeout_seconds = experiment.timeouts.aggregator_seconds
    provider.candidate_max_chars = ensemble.candidate_max_chars
    provider.shuffle_candidates = ensemble.shuffle_candidates
    provider.candidate_order_seed = ensemble.candidate_order_seed
    provider.record_candidates = ensemble.record_candidates
    provider.proposer_tools = ensemble.proposer_tools
    provider.aggregator_tools = ensemble.aggregator_tools
    recovery_policy = aggregator_recovery_policy(experiment)
    apply_aggregator_recovery_policy(provider, recovery_policy)
    provider.quorum_grace_seconds = ensemble.quorum_grace_seconds
    provider.all_failed_policy = ensemble.all_failed_policy
    provider._member_request_budget_bindings = {}

    member_generation = [
        {
            "role": "proposer",
            "provider": member.provider_config.provider,
            "model": member.provider_config.model,
            "temperature": member.temperature,
            "max_tokens": member.max_tokens,
            "thinking": member.thinking,
            "thinking_budget_tokens": experiment.generation.thinking_budget_tokens,
            "k": member.k,
        }
        for member in provider.proposers
    ]
    member_generation.append(
        {
            "role": "aggregator",
            "provider": provider.aggregator.provider_config.provider,
            "model": provider.aggregator.provider_config.model,
            "temperature": provider.aggregator.temperature,
            "max_tokens": provider.aggregator.max_tokens,
            "thinking": provider.aggregator.thinking,
            "thinking_budget_tokens": experiment.generation.thinking_budget_tokens,
            "k": provider.aggregator.k,
        }
    )
    proposer_models = [
        member.provider_config.model
        for member in provider.proposers
        for _ in range(max(1, int(member.k or 1)))
    ]
    selected_proposers = [
        f"{member.provider_config.provider}:{member.provider_config.model}"
        for member in provider.proposers
        for _ in range(max(1, int(member.k or 1)))
    ]
    aggregator_model = provider.aggregator.provider_config.model
    selected_aggregator = f"{provider.aggregator.provider_config.provider}:{aggregator_model}"
    provider.selection_plan = {
        "strategy": experiment.routing.selection_mode,
        "selection_mode": experiment.routing.selection_mode,
        "profile": provider.profile_name,
        "proposer_models": proposer_models,
        "aggregator_model": aggregator_model,
        "proposer_count": len(provider.proposers),
        "proposer_sample_count": sum(member.k for member in provider.proposers),
        "selected_P": selected_proposers,
        "selected_A": selected_aggregator,
        "benchmark_alignment": {
            "id": experiment.profile_id,
            "reference_commit": experiment.reference.source_commit,
            "reference_group": experiment.reference.group,
            "reference_profile": experiment.reference.profile,
        },
        "pre_alignment": pre_alignment,
        "configured_min_successful_proposers": provider.min_successful_proposers,
        "effective_min_successful_proposers": provider.min_successful_proposers,
        "configured_proposer_timeout_seconds": provider.proposer_timeout_seconds,
        "effective_proposer_timeout_seconds": provider.proposer_timeout_seconds,
        "configured_aggregator_timeout_seconds": provider.aggregator_timeout_seconds,
        "effective_aggregator_timeout_seconds": provider.aggregator_timeout_seconds,
        "configured_shuffle_candidates": provider.shuffle_candidates,
        "effective_shuffle_candidates": provider.shuffle_candidates,
        "quorum_grace_seconds": provider.quorum_grace_seconds,
        "wait_for_all_proposers": ensemble.wait_for_all_proposers,
        "all_failed_policy": provider.all_failed_policy,
        "candidate_max_chars": provider.candidate_max_chars,
        "proposer_tools": provider.proposer_tools,
        "aggregator_tools": provider.aggregator_tools,
        **recovery_policy,
        "record_candidates": provider.record_candidates,
        "require_highest_thinking": experiment.generation.require_highest_thinking,
        "member_generation": member_generation,
    }
    if ensemble.candidate_order_seed is not None:
        provider.selection_plan["configured_candidate_order_seed"] = (
            ensemble.candidate_order_seed
        )
        provider.selection_plan["effective_candidate_order_seed"] = (
            ensemble.candidate_order_seed if provider.shuffle_candidates else None
        )
    return provider


def enforce_draco_legal_proposer_quorum(provider: Any) -> Any:
    """Apply the experiment's two-thirds proposer quorum to a live ensemble."""

    proposers = list(getattr(provider, "proposers", []) or [])
    proposer_count = sum(max(1, int(getattr(member, "k", 1) or 1)) for member in proposers)
    if proposer_count <= 0:
        return provider
    selection_plan = dict(getattr(provider, "selection_plan", {}) or {})
    if selection_plan.get("analyzer_failure_fallback") is True:
        if (
            selection_plan.get("complete_proposers_only") is not True
            or coerce_metric_int(
                selection_plan.get("effective_min_successful_proposers")
            )
            != 1
            or int(getattr(provider, "min_successful_proposers", 0) or 0)
            != 1
        ):
            raise ValueError(
                "DRACO analyzer-failure fallback requires one complete proposer"
            )
        selection_plan["legal_min_successful_proposers"] = 1
        selection_plan["legal_quorum_policy"] = (
            "analyzer_failure_fallback_complete_1"
        )
        provider.selection_plan = selection_plan
        return provider
    provider_native_policy = selection_plan.get(
        "proposer_recovery_policy"
    )
    expected_provider_native_policy = formal_proposer_recovery_policy_for_plan(
        selection_plan
    )
    formal_provider_native = bool(
        isinstance(provider_native_policy, Mapping)
        and expected_provider_native_policy is not None
        and dict(provider_native_policy) == expected_provider_native_policy
    )
    required = (
        2
        if formal_provider_native
        else legal_proposer_quorum(proposer_count)
    )
    if required > proposer_count:
        raise ValueError(
            f"DRACO enforced proposer quorum {required} exceeds proposer "
            f"sample count {proposer_count}"
        )
    if "proposer_recovery_policy" in selection_plan:
        if not isinstance(provider_native_policy, Mapping):
            raise ValueError(
                "DRACO proposer_recovery_policy must be a mapping"
            )
        policy_quorum = provider_native_policy.get("quorum_required")
        if (
            isinstance(policy_quorum, bool)
            or not isinstance(policy_quorum, int)
            or policy_quorum <= 0
        ):
            raise ValueError(
                "DRACO proposer_recovery_policy.quorum_required must be "
                "a positive integer"
            )
        if policy_quorum != required:
            raise ValueError(
                "DRACO proposer_recovery_policy.quorum_required="
                f"{policy_quorum} does not match enforced proposer quorum "
                f"{required}"
            )
    configured = int(getattr(provider, "min_successful_proposers", 1) or 1)
    provider.min_successful_proposers = required
    selection_plan.setdefault("configured_min_successful_proposers", configured)
    selection_plan["effective_min_successful_proposers"] = required
    selection_plan["legal_min_successful_proposers"] = required
    selection_plan["legal_quorum_policy"] = (
        "fixed_2_provider_native"
        if formal_provider_native
        else "ceil(2*n/3)"
    )
    provider.selection_plan = selection_plan
    return provider


def apply_generation_policy_to_profile(profile: Any, policy: dict[str, Any]) -> Any:
    preserve_temperature = bool(getattr(profile, "preserve_member_temperature", False))

    def _apply_member_policy(member: Any) -> Any:
        thinking = (
            generation_thinking_for_model(
                str(getattr(member, "model", "") or ""),
                policy,
            )
            if bool(policy.get("thinking_enabled", True))
            else "off"
        )
        update: dict[str, Any] = {
            "temperature": policy.get("temperature"),
            "thinking": thinking,
        }
        if preserve_temperature and getattr(member, "temperature", None) is not None:
            update.pop("temperature", None)
        if policy.get("max_tokens_overridden"):
            update["max_tokens"] = int(policy["max_tokens"])
        return member.model_copy(update=update)

    proposers = [_apply_member_policy(proposer) for proposer in profile.proposers]
    aggregator = _apply_member_policy(profile.aggregator)
    update: dict[str, Any] = {"proposers": proposers, "aggregator": aggregator}
    if getattr(profile, "candidate_scorer", None) is not None:
        update["candidate_scorer"] = _apply_member_policy(profile.candidate_scorer)
    return profile.model_copy(update=update)


def apply_generation_policy_to_ensemble_provider(
    provider: EnsembleProvider,
    policy: dict[str, Any],
) -> EnsembleProvider:
    """Apply the run-wide frozen generation policy to realized ensemble members."""

    def _apply(member: EnsembleMemberConfig) -> EnsembleMemberConfig:
        model = member.provider_config.model
        thinking = (
            generation_thinking_for_model(model, policy)
            if bool(policy.get("thinking_enabled", True))
            else "off"
        )
        updates: dict[str, Any] = {
            "temperature": policy.get("temperature"),
            "thinking": thinking,
        }
        if policy.get("max_tokens_overridden"):
            updates["max_tokens"] = int(policy["max_tokens"])
        return replace(member, **updates)

    provider.proposers = [_apply(member) for member in provider.proposers]
    provider.proposer_backups = [
        _apply(member) for member in provider.proposer_backups
    ]
    provider.aggregator = _apply(provider.aggregator)
    provider.aggregator_fallbacks = [
        _apply(member) for member in provider.aggregator_fallbacks
    ]
    provider._member_request_budget_bindings = {}
    member_generation = [
        {
            "role": role,
            "label": member.label,
            "provider": member.provider_config.provider,
            "model": member.provider_config.model,
            "temperature": member.temperature,
            "max_tokens": member.max_tokens,
            "thinking": member.thinking,
            "thinking_budget_tokens": generation_chat_config(
                policy,
                model=member.provider_config.model,
            ).thinking_budget_tokens,
            "k": member.k,
        }
        for role, member in [
            *(("proposer", member) for member in provider.proposers),
            ("aggregator", provider.aggregator),
        ]
    ]
    provider.selection_plan = {
        **dict(provider.selection_plan),
        "generation_policy_applied": True,
        "member_generation": member_generation,
        "recovery_member_generation": [
            {
                "role": role,
                "label": member.label,
                "provider": member.provider_config.provider,
                "model": member.provider_config.model,
                "temperature": member.temperature,
                "max_tokens": member.max_tokens,
                "thinking": member.thinking,
                "thinking_budget_tokens": generation_chat_config(
                    policy,
                    model=member.provider_config.model,
                ).thinking_budget_tokens,
                "k": member.k,
            }
            for role, member in [
                *(
                    ("proposer_backup", member)
                    for member in provider.proposer_backups
                ),
                *(
                    ("aggregator_fallback", member)
                    for member in provider.aggregator_fallbacks
                ),
            ]
        ],
    }
    provider.seal_proposer_recovery_runtime_guard()
    return provider


def validate_strict_openrouter_ensemble_members(
    provider: EnsembleProvider,
    policy: dict[str, Any],
    *,
    fallback_config: ProviderConfig | None = None,
    allow_unpinned_openrouter: bool = False,
) -> None:
    """Fail before billing when a realized member cannot honor the frozen request."""

    strict_routing = os.environ.get("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "").strip().lower()
    truthy = {"1", "true", "yes", "on", "enabled"}
    strict_routing_enabled = strict_routing in truthy
    require_parameters = (
        os.environ.get("OPENSQUILLA_OPENROUTER_REQUIRE_PARAMETERS", "").strip().lower()
    )
    require_parameters_enabled = require_parameters in truthy
    requests: list[tuple[ProviderConfig, str]] = [
        (member.provider_config, str(member.thinking or ""))
        for member in [
            *provider.proposers,
            *provider.proposer_backups,
            provider.aggregator,
            *provider.aggregator_fallbacks,
        ]
    ]
    if fallback_config is not None:
        fallback_thinking = (
            generation_thinking_for_model(fallback_config.model, policy)
            if bool(policy.get("thinking_enabled", True))
            else "off"
        )
        requests.append((fallback_config, fallback_thinking))
    for cfg, raw_thinking in requests:
        if cfg.provider.strip().lower() != "openrouter":
            continue
        model = cfg.model.strip()
        thinking = raw_thinking.strip().lower()
        thinking_enabled = bool(policy.get("thinking_enabled", True))
        if thinking_enabled:
            supported = _openrouter_supported_thinking_levels(model)
            if strict_routing_enabled and supported and thinking not in supported:
                raise ValueError(
                    f"OpenRouter model {model!r} does not support "
                    f"frozen thinking={thinking!r}; supported levels are "
                    f"{list(supported)!r}"
                )
            if (
                strict_routing_enabled
                and supported
                and bool(policy.get("require_highest_thinking"))
                and thinking != supported[0]
            ):
                raise ValueError(
                    f"OpenRouter model {model!r} requires highest thinking="
                    f"{supported[0]!r}, not {thinking!r}"
                )
        if thinking_enabled and thinking not in {"", "off"}:
            capabilities = openrouter_static_capabilities(model)
            if capabilities is None or not capabilities.supports_reasoning:
                raise ValueError(
                    f"OpenRouter model {model!r} cannot prove support for "
                    f"frozen thinking={thinking!r}"
                )
        if (
            strict_routing_enabled
            and not allow_unpinned_openrouter
            and model not in cfg.provider_routing
        ):
            raise ValueError(f"OpenRouter model {model!r} has no strict upstream provider pin")
        if strict_routing_enabled and not require_parameters_enabled:
            raise ValueError(
                "strict OpenRouter routing requires parameter compatibility enforcement"
            )


def compact_chat_config(
    config: ChatConfig | None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ = policy
    if config is None:
        config = generation_chat_config(generation_thinking_policy())
    level = config.thinking_level
    return {
        "thinking": config.thinking,
        "thinking_level": level.value if isinstance(level, ThinkingLevel) else level,
        "thinking_budget_tokens": config.thinking_budget_tokens,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }


def profile_timeout_seconds(
    profile: Any,
    *,
    requested_timeout: float | None = None,
    proposer_timeout_override: float | None = None,
    aggregator_timeout_override: float | None = None,
    expand_to_requested_timeout: bool = False,
) -> tuple[float, float]:
    moa_layers = max(1, int(getattr(profile, "moa_layers", 1) or 1))
    proposer_timeout = max(
        DEFAULT_PROFILE_PROPOSER_TIMEOUT_SECONDS,
        float(getattr(profile, "proposer_timeout_seconds", 0) or 0),
    )
    aggregator_timeout = max(
        DEFAULT_PROFILE_AGGREGATOR_TIMEOUT_SECONDS,
        float(getattr(profile, "aggregator_timeout_seconds", 0) or 0),
    )
    if proposer_timeout_override is not None and proposer_timeout_override > 0:
        proposer_timeout = float(proposer_timeout_override)
    if aggregator_timeout_override is not None and aggregator_timeout_override > 0:
        aggregator_timeout = float(aggregator_timeout_override)
    if not expand_to_requested_timeout or requested_timeout is None or requested_timeout <= 0:
        return proposer_timeout, aggregator_timeout
    available = max(0.0, float(requested_timeout) - PROFILE_TIMEOUT_MARGIN_SECONDS)
    base_budget = proposer_timeout + aggregator_timeout * moa_layers
    if available <= base_budget:
        return proposer_timeout, aggregator_timeout
    extra = available - base_budget
    return (
        proposer_timeout + extra * 0.25,
        aggregator_timeout + (extra * 0.75 / moa_layers),
    )


def profile_aggregator_timeout_seconds(
    profile: Any,
    *,
    requested_timeout: float | None = None,
    aggregator_timeout_override: float | None = None,
    expand_to_requested_timeout: bool = False,
) -> float:
    return profile_timeout_seconds(
        profile,
        requested_timeout=requested_timeout,
        aggregator_timeout_override=aggregator_timeout_override,
        expand_to_requested_timeout=expand_to_requested_timeout,
    )[1]


def extract_json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def rubric_criteria(task: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = task.get("rubric_items") or task.get("criteria")
    if isinstance(raw_items, list):
        return [
            item
            for index, raw in enumerate(raw_items, start=1)
            if (item := normalize_criterion(raw, index=index)) is not None
        ]
    rubric = parse_maybe_json(task.get("rubric"))
    if not isinstance(rubric, dict):
        return []
    items: list[dict[str, Any]] = []
    for section in rubric.get("sections") or []:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("id") or "").strip()
        section_title = str(section.get("title") or section_id).strip()
        for raw in section.get("criteria") or []:
            item = normalize_criterion(
                raw,
                index=len(items) + 1,
                section_id=section_id,
                section_title=section_title,
            )
            if item is not None:
                items.append(item)
    return items


def normalize_criterion(
    raw: Any,
    *,
    index: int,
    section_id: str = "",
    section_title: str = "",
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    requirement = str(
        raw.get("requirement")
        or raw.get("criterion")
        or raw.get("description")
        or raw.get("text")
        or ""
    ).strip()
    if not requirement:
        return None
    return {
        "id": str(raw.get("id") or f"criterion-{index}"),
        "section_id": str(raw.get("section_id") or section_id or "rubric"),
        "section_title": str(raw.get("section_title") or section_title or section_id or "Rubric"),
        "weight": coerce_weight(raw.get("weight")),
        "requirement": requirement,
    }


def parse_verdict(value: Any) -> bool | None:
    verdict = str(value or "").strip().upper()
    if verdict in {"MET", "TRUE", "YES", "PASS", "PASSED", "1"}:
        return True
    if verdict in {"UNMET", "FALSE", "NO", "FAIL", "FAILED", "0"}:
        return False
    return None


LEGACY_JUDGE_SCORE_KEYS = (
    "accuracy",
    "completeness",
    "objectivity",
    "citation",
)


def valid_legacy_judge_scores(result: Mapping[str, Any]) -> dict[str, float] | None:
    scores = result.get("scores")
    if not isinstance(scores, Mapping) or set(scores) != set(LEGACY_JUDGE_SCORE_KEYS):
        return None
    validated: dict[str, float] = {}
    for key in LEGACY_JUDGE_SCORE_KEYS:
        value = scores.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        score = float(value)
        if not math.isfinite(score) or score < 1.0 or score > 5.0:
            return None
        validated[key] = score
    return validated


def normalize_legacy_judge_result(result: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(result)
    normalized.setdefault("mode", "legacy_dimension_score")
    normalized["score_scale"] = "0-100"

    scores = valid_legacy_judge_scores(normalized)
    raw_total = normalized.get("total")
    if isinstance(raw_total, int | float) and not isinstance(raw_total, bool):
        normalized["raw_total"] = float(raw_total)

    if scores is not None:
        normalized["scores"] = scores
        normalized_score = sum(scores.values()) / 20.0 * 100.0
        normalized["normalized_score"] = clamp_percent(normalized_score)
        normalized["total"] = normalized["normalized_score"]
        normalized["score_status"] = "complete"
        normalized["judge_error_count"] = 0
        normalized.pop("error", None)
    else:
        normalized["normalized_score"] = None
        normalized["total"] = None
        normalized["score_status"] = "incomplete"
        normalized["judge_error_count"] = max(
            1,
            coerce_metric_int(normalized.get("judge_error_count")),
        )
        normalized["error"] = "judge_json_schema_invalid"
    return normalized


def judge_completion_reasons(
    row: Mapping[str, Any],
    *,
    judge_required: bool,
) -> list[str]:
    """Return Judge-only incompleteness reasons for a stored result row."""

    if not judge_required:
        return []
    judge = row.get("judge")
    reasons: list[str] = []
    if not isinstance(judge, Mapping):
        return ["judge_incomplete", "judge_errors", "missing_quality_total"]

    if judge.get("score_status") != "complete" or quality_total(dict(judge)) is None:
        reasons.append("judge_incomplete")
    if judge.get("error") or coerce_metric_int(judge.get("judge_error_count")) != 0:
        reasons.append("judge_errors")
    expected_quality = quality_total(dict(judge))
    stored_quality = row.get("quality_total")
    if isinstance(stored_quality, bool) or not isinstance(stored_quality, int | float):
        reasons.append("missing_quality_total")
    elif expected_quality is None or not math.isclose(
        float(stored_quality),
        float(expected_quality),
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        reasons.append("quality_total_mismatch")
    return reasons


def inherited_provider_config(config: GatewayConfig) -> ProviderConfig:
    runtime = resolve_llm_runtime_config(config)
    base_url = runtime.base_url[:-3] if runtime.base_url.endswith("/v1") else runtime.base_url
    return ProviderConfig(
        provider=runtime.provider,
        model=runtime.model,
        api_key=runtime.api_key,
        base_url=base_url,
        proxy=runtime.proxy,
        provider_routing=runtime.provider_routing,
    )


def build_single_provider(
    *,
    inherited: ProviderConfig,
    group: str,
    model: str,
    dry_run: bool,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
):
    if dry_run:
        return DryProvider(model=model, group=group)
    resolved_api_key = api_key if api_key is not None else inherited.api_key
    if api_key_env:
        resolved_api_key = os.environ.get(api_key_env, resolved_api_key)
    cfg = ProviderConfig(
        provider=provider or inherited.provider,
        model=model,
        api_key=resolved_api_key,
        base_url=base_url or inherited.base_url,
        proxy=inherited.proxy,
        provider_routing=inherited.provider_routing if provider is None else {},
    )
    return ModelSelector(SelectorConfig(primary=cfg)).resolve()


def build_task_analyzer_provider(
    routed_config: ProviderConfig,
    *,
    provider_id: str,
    model_id: str,
    upstream_provider: str,
):
    """Resolve the analyzer without dropping routing or replay-safety fields."""

    normalized_provider = provider_id.strip().casefold()
    if normalized_provider != "openrouter":
        raise ValueError("task analyzer provider currently must be openrouter")
    if str(routed_config.provider or "").strip().casefold() != normalized_provider:
        raise ValueError("task analyzer cannot reuse a different provider credential")
    provider_routing = dict(routed_config.provider_routing)
    provider_routing[model_id] = upstream_provider
    analyzer_cfg = replace(
        routed_config,
        provider=normalized_provider,
        model=model_id,
        provider_routing=provider_routing,
        replay_provider_state=False,
    )
    return ModelSelector(SelectorConfig(primary=analyzer_cfg)).resolve()


def task_analyzer_usage_row(
    usage: Mapping[str, Any],
    *,
    provider_id: str,
    model_id: str,
    source: str,
    fallback_reason: str,
) -> dict[str, Any]:
    return _shared_task_analyzer_usage_row(
        usage,
        provider_id=provider_id,
        model_id=model_id,
        source=source,
        fallback_reason=fallback_reason,
        coerce_metric_int=coerce_metric_int,
        trusted_provider_billed_cost=trusted_provider_billed_cost,
        exact_provider_usage_cost=exact_provider_usage_cost,
        new_physical_attempt_id=lambda: uuid.uuid4().hex,
    )


def task_analyzer_usage_rows(
    usage: Mapping[str, Any],
    *,
    provider_id: str,
    model_id: str,
    source: str,
    fallback_reason: str,
) -> list[dict[str, Any]]:
    return _shared_task_analyzer_usage_rows(
        usage,
        provider_id=provider_id,
        model_id=model_id,
        source=source,
        fallback_reason=fallback_reason,
        coerce_metric_int=coerce_metric_int,
        usage_row_builder=task_analyzer_usage_row,
        new_physical_attempt_id=lambda: uuid.uuid4().hex,
    )


def conservative_task_analyzer_usage_rows(
    usage: Any,
    *,
    provider_id: str,
    model_id: str,
    source: str,
    fallback_reason: str,
) -> list[dict[str, Any]]:
    return _shared_conservative_task_analyzer_usage_rows(
        usage,
        provider_id=provider_id,
        model_id=model_id,
        source=source,
        fallback_reason=fallback_reason,
        usage_row_builder=task_analyzer_usage_row,
        new_physical_attempt_id=lambda: uuid.uuid4().hex,
    )


def _bind_frozen_g1_retry_provenance(
    plan: dict[str, Any],
    *,
    initial_plan: Mapping[str, Any],
    excluded_proposer_identities: Sequence[str],
) -> None:
    """Bind a materialized reroute to the original paid analyzer decision."""

    from opensquilla.provider.ranking_router import (
        ROUTER_DYNAMIC_RETRY_ROUTING_SCHEMA,
        build_router_dynamic_task_analysis_reuse_binding,
    )

    exclusions = sorted(
        {
            str(identity or "").strip().lower()
            for identity in excluded_proposer_identities
            if str(identity or "").strip()
        }
    )
    initial_decision_id = str(initial_plan.get("decision_id") or "")
    if not initial_decision_id or not exclusions:
        raise ValueError("frozen G1 reroute lacks parent decision or exclusions")
    binding = build_router_dynamic_task_analysis_reuse_binding(initial_plan)
    plan["retry_parent_decision_id"] = initial_decision_id
    plan["retry_excluded_proposer_identities"] = exclusions
    plan["task_analysis_reused"] = True
    plan["task_analysis_reuse"] = binding
    retry_routing = dict(plan.get("retry_routing") or {})
    retry_routing.update(
        {
            "schema": ROUTER_DYNAMIC_RETRY_ROUTING_SCHEMA,
            "reason": "prior_attempt_reasoning_only_length",
            "parent_decision_id": initial_decision_id,
            "excluded_proposer_identities": exclusions,
            "task_analysis_reused": True,
            "task_analysis_source_decision_id": initial_decision_id,
            "task_analysis_reuse_sha256": binding["projection_sha256"],
        }
    )
    plan["retry_routing"] = retry_routing


async def build_experiment_provider(
    *,
    config: GatewayConfig,
    inherited: ProviderConfig,
    group: str,
    prompt: str,
    task_id: str = "",
    task_input_sha256: str = "",
    prompt_sha256: str = "",
    dry_run: bool,
    enable_proposer_tools: bool,
    ensemble_proposer_timeout: float | None,
    ensemble_aggregator_timeout: float | None,
    experiment_config: DracoExperimentConfig | None = None,
    g1_registry_contract: Mapping[str, Any] | None = None,
    task_analyzer_execution_contract: Mapping[str, Any] | None = None,
    generation_policy: dict[str, Any] | None = None,
    tools: Sequence[ToolDefinition] | None = None,
    frozen_g1_lifecycle: Mapping[str, Any] | None = None,
    turn_absolute_deadline: float | None = None,
) -> ProviderBuildResult:
    """Build one DRACO provider through the same routing primitives as runtime.py."""

    if frozen_g1_lifecycle is not None:
        raise ValueError(
            "frozen G1 lifecycle provider construction is resume-only"
        )
    spec = GROUP_SPECS[group]
    started = time.monotonic()
    kind = spec["kind"]
    b2_experiment = experiment_config if group == "B2" else None
    g1_routing = (
        experiment_config.g1_routing if group == "G1" and experiment_config is not None else None
    )
    if group == "G1" and g1_routing is None:
        raise ValueError("G1 requires a versioned g1_routing experiment contract")
    g1_ensemble = (
        experiment_config.ensemble
        if g1_routing is not None and experiment_config is not None
        else None
    )
    resolved_g1_registry_contract = (
        dict(g1_registry_contract)
        if isinstance(g1_registry_contract, Mapping)
        else g1_routing.model_dump(mode="json", exclude_none=True)
        if g1_routing is not None
        else None
    )
    if (
        group == "G1"
        and task_analyzer_execution_contract is None
        and isinstance(resolved_g1_registry_contract, Mapping)
    ):
        from opensquilla.eval.draco_task_analyzer_execution import (
            task_analyzer_execution_contract_from_g1_registry,
            task_analyzer_execution_contract_from_ranking_config,
        )

        live_chain = resolved_g1_registry_contract.get(
            "live_task_analyzer_chain"
        )
        task_analyzer_execution_contract = (
            task_analyzer_execution_contract_from_g1_registry(
                resolved_g1_registry_contract
            )
            if isinstance(live_chain, list) and live_chain
            else task_analyzer_execution_contract_from_ranking_config(
                config.llm_ensemble.ranking_config_resolution_snapshot()[
                    "effective_config"
                ]
            )
        )
    recovery_policy = (
        aggregator_recovery_policy(experiment_config)
        if experiment_config is not None
        else {
            "aggregator_recovery_mode": config.llm_ensemble.aggregator_recovery_mode,
            "aggregator_recovery_top_k": config.llm_ensemble.aggregator_recovery_top_k,
            "aggregator_max_tokens_cap": config.llm_ensemble.aggregator_max_tokens_cap,
            "aggregator_visible_answer_reserve_tokens": (
                config.llm_ensemble.aggregator_visible_answer_reserve_tokens
            ),
        }
    )
    proposer_policy = (
        proposer_recovery_policy(
            experiment_config,
            ranking_config=config.llm_ensemble.ranking_config_resolution_snapshot()[
                "effective_config"
            ],
        )
        if experiment_config is not None
        else {
            "proposer_backup_count": config.llm_ensemble.proposer_backup_count,
            "proposer_recovery_max_additional_calls": (
                config.llm_ensemble.proposer_recovery_max_additional_calls
            ),
            "proposer_max_tokens_cap": (
                config.llm_ensemble.proposer_max_tokens_cap
            ),
            "proposer_visible_answer_reserve_tokens": (
                config.llm_ensemble.proposer_visible_answer_reserve_tokens
            ),
        }
    )
    if dry_run:
        if kind in {"single", "router_single"}:
            model = spec.get("model") or "dry-routed-single"
            dry_provider: Any = DryProvider(model=model, group=group)
        else:
            dry_provider = DryEnsembleProvider(
                group=group,
                profile=(
                    b2_experiment.ensemble.profile_name
                    if b2_experiment is not None
                    else spec["selection_mode"]
                ),
                proposer_models=(
                    [
                        member.model
                        for member in b2_experiment.ensemble.proposers
                        for _ in range(max(1, int(member.k or 1)))
                    ]
                    if b2_experiment is not None
                    else None
                ),
                model=(
                    b2_experiment.ensemble.aggregator.model
                    if b2_experiment is not None
                    else "dry-aggregator"
                ),
                selection_mode=str(spec.get("selection_mode") or ""),
            )
            apply_aggregator_recovery_policy(dry_provider, recovery_policy)
            apply_aggregator_recovery_policy(dry_provider, proposer_policy)
            if b2_experiment is not None:
                dry_provider.min_successful_proposers = legal_proposer_quorum(
                    len(dry_provider.proposer_models)
                )
                dry_provider.proposer_timeout_seconds = b2_experiment.timeouts.proposer_seconds
                dry_provider.aggregator_timeout_seconds = b2_experiment.timeouts.aggregator_seconds
                dry_provider.quorum_grace_seconds = b2_experiment.ensemble.quorum_grace_seconds
                dry_provider.candidate_max_chars = b2_experiment.ensemble.candidate_max_chars
                dry_provider.proposer_tools = b2_experiment.ensemble.proposer_tools
                dry_provider.aggregator_tools = b2_experiment.ensemble.aggregator_tools
        dry_routing_trace: dict[str, Any] = {
            "dry_run": True,
            "kind": kind,
            **dict(spec),
        }
        if b2_experiment is not None:
            proposer_models = [
                member.model
                for member in b2_experiment.ensemble.proposers
                for _ in range(max(1, int(member.k or 1)))
            ]
            selected_proposers = [
                f"{member.provider.strip().lower()}:{member.model}"
                for member in b2_experiment.ensemble.proposers
                for _ in range(max(1, int(member.k or 1)))
            ]
            aggregator = b2_experiment.ensemble.aggregator
            member_generation = [
                {"role": "proposer", **member.model_dump(mode="json")}
                for member in b2_experiment.ensemble.proposers
            ]
            member_generation.append({"role": "aggregator", **aggregator.model_dump(mode="json")})
            dry_routing_trace.update(
                {
                    "benchmark_alignment": b2_experiment.profile_id,
                    "profile": b2_experiment.ensemble.profile_name,
                    "selection_plan": {
                        "strategy": b2_experiment.routing.selection_mode,
                        "selection_mode": b2_experiment.routing.selection_mode,
                        "profile": b2_experiment.ensemble.profile_name,
                        "proposer_models": proposer_models,
                        "aggregator_model": aggregator.model,
                        "proposer_count": len(b2_experiment.ensemble.proposers),
                        "proposer_sample_count": sum(
                            member.k for member in b2_experiment.ensemble.proposers
                        ),
                        "selected_P": selected_proposers,
                        "selected_A": f"{aggregator.provider.strip().lower()}:{aggregator.model}",
                        "configured_min_successful_proposers": (
                            b2_experiment.ensemble.min_successful_proposers
                        ),
                        "effective_min_successful_proposers": (
                            legal_proposer_quorum(len(proposer_models))
                        ),
                        "effective_proposer_timeout_seconds": (
                            b2_experiment.timeouts.proposer_seconds
                        ),
                        "effective_aggregator_timeout_seconds": (
                            b2_experiment.timeouts.aggregator_seconds
                        ),
                        "effective_shuffle_candidates": (b2_experiment.ensemble.shuffle_candidates),
                        "proposer_tools": b2_experiment.ensemble.proposer_tools,
                        "aggregator_tools": b2_experiment.ensemble.aggregator_tools,
                        "wait_for_all_proposers": (b2_experiment.ensemble.wait_for_all_proposers),
                        "member_generation": member_generation,
                    },
                }
            )
            if b2_experiment.ensemble.candidate_order_seed is not None:
                dry_routing_trace["selection_plan"].update(
                    {
                        "configured_candidate_order_seed": (
                            b2_experiment.ensemble.candidate_order_seed
                        ),
                        "effective_candidate_order_seed": (
                            b2_experiment.ensemble.candidate_order_seed
                            if b2_experiment.ensemble.shuffle_candidates
                            else None
                        ),
                    }
                )
            dry_provider.selection_plan = dict(dry_routing_trace["selection_plan"])
        elif g1_routing is not None:
            from opensquilla.provider.ranking_router import (
                TaskAnalysisResult,
                build_request_context,
                dynamic_output_token_budgets,
                fallback_task_profile,
                frozen_task_analysis_result,
            )

            ranking_resolution = (
                config.llm_ensemble.ranking_config_resolution_snapshot()
            )
            thinking_assignment_enabled = (
                ranking_resolution.get("thinking_assignment_enabled") is True
            )
            ranking_config = ranking_resolution["effective_config"]
            dry_config = config.model_copy(deep=True)
            dry_ensemble = dry_config.llm_ensemble
            dry_ensemble.enabled = True
            dry_ensemble.selection_mode = "router_dynamic"
            dry_ensemble.ranking_user_profile_generation_enabled = False
            dry_ensemble.ranking_user_profile_enabled = False
            if g1_ensemble is not None:
                dry_ensemble.shuffle_candidates = g1_ensemble.shuffle_candidates
                dry_ensemble.candidate_order_seed = g1_ensemble.candidate_order_seed
            if (
                ensemble_proposer_timeout is not None
                and ensemble_proposer_timeout > 0
            ):
                dry_ensemble.proposer_timeout_seconds = float(ensemble_proposer_timeout)
            if (
                ensemble_aggregator_timeout is not None
                and ensemble_aggregator_timeout > 0
            ):
                dry_ensemble.aggregator_timeout_seconds = float(ensemble_aggregator_timeout)
            apply_aggregator_recovery_policy(dry_ensemble, recovery_policy)
            apply_aggregator_recovery_policy(dry_ensemble, proposer_policy)
            dry_ensemble.min_successful_proposers = (
                FORMAL_PROPOSER_RECOVERY_POLICY["quorum_required"]
            )
            if thinking_assignment_enabled:
                configured_output_tokens, _ = (
                    resolve_effective_generation_request_parameters(
                        llm_config=dry_config.llm,
                        generation_policy=generation_policy,
                    )
                )
            else:
                configured_output_tokens = int(
                    getattr(dry_config.llm, "max_tokens", 0) or 0
                )
            candidate_output_tokens, aggregator_output_tokens = dynamic_output_token_budgets(
                configured_output_tokens=configured_output_tokens,
                candidate_max_chars=int(dry_ensemble.candidate_max_chars or 0),
                ranking_config=ranking_config,
            )
            request_context = build_request_context(
                message=prompt,
                turn_metadata={},
                attachments=[],
                candidate_output_tokens=candidate_output_tokens,
                aggregator_output_tokens=aggregator_output_tokens,
                ranking_config=ranking_config,
            )
            replay = g1_routing.task_analysis_execution
            if replay is not None:
                if not task_id:
                    raise ValueError("frozen task analysis replay requires task_id")
                task_analysis = frozen_task_analysis_result(
                    replay.model_dump(mode="json"),
                    task_id=task_id,
                    task_input_sha256=task_input_sha256,
                    prompt_sha256=prompt_sha256,
                    routed_tier="c1",
                    request_context=request_context,
                    ranking_config=ranking_config,
                )
            else:
                task_profile = fallback_task_profile(
                    routed_tier="c1",
                    request_context=request_context,
                    ranking_config=ranking_config,
                )
                dry_routes = [
                    {
                        "provider": str(route["provider"]),
                        "model": str(route["model"]),
                        "upstream_provider": str(route["upstream_provider"]),
                    }
                    for route in task_analyzer_execution_contract["routes"]
                ]
                dry_timeout = float(
                    task_analyzer_execution_contract["total_timeout_seconds"]
                )
                task_analysis = TaskAnalysisResult(
                    profile=task_profile,
                    source="router_fallback",
                    schema_valid=False,
                    confidence=0.0,
                    fallback_reason="dry_run_no_analyzer_call",
                    usage={"physical_attempts": [], "attempt_count": 0},
                    provider_id=dry_routes[-1]["provider"],
                    model_id=dry_routes[-1]["model"],
                    chain_trace={
                        "protocol": (
                            "opensquilla.task-analyzer-fallback-chain/v1"
                        ),
                        "configured_routes": dry_routes,
                        "attempt_outcomes": [
                            {
                                "candidate_index": index,
                                **route,
                                "outcome": "failed",
                                "reason": "dry_run_no_analyzer_call",
                                "physical_request_count": 0,
                            }
                            for index, route in enumerate(dry_routes)
                        ],
                        "selected_index": None,
                        "exhausted": True,
                        "schema_repair_max_retries": (
                            task_analyzer_execution_contract[
                                "schema_repair_max_retries"
                            ]
                        ),
                        "deadline": {
                            "configured_seconds": dry_timeout,
                            "elapsed_seconds": 0.0,
                            "remaining_seconds": dry_timeout,
                            "expired": False,
                        },
                    },
                )
            dry_ranking_inputs = {
                "decision_id": ("dry-" + hashlib.sha256(prompt.encode()).hexdigest()[:24]),
                "task_analysis": task_analysis,
                "user_profile": None,
                "request_context": request_context,
                "ranking_config": ranking_config,
                "generation_policy": (
                    dict(generation_policy)
                    if generation_policy is not None
                    else None
                    if thinking_assignment_enabled
                    else {}
                ),
                "registry_allowlist": resolved_g1_registry_contract,
                "task_analyzer_execution_contract": (
                    task_analyzer_execution_contract
                ),
            }
            if thinking_assignment_enabled:
                dry_ranking_inputs["request_tools_present"] = bool(tools)
            dry_dynamic_provider = build_ensemble_provider_from_config(
                config=dry_config,
                inherited_provider_config=inherited,
                fallback_provider=None,
                turn_metadata={
                    "routed_tier": "c1",
                    "routing_confidence": 0.0,
                    "router_dynamic_task_text": prompt,
                },
                ranking_inputs=dry_ranking_inputs,
            )
            plan = copy.deepcopy(dry_dynamic_provider.selection_plan)
            if g1_ensemble is not None:
                dry_provider.quorum_grace_seconds = g1_ensemble.quorum_grace_seconds
                plan["quorum_grace_seconds"] = g1_ensemble.quorum_grace_seconds
                plan["wait_for_all_proposers"] = (
                    g1_ensemble.wait_for_all_proposers
                )
            proposer_models = list(plan.get("proposer_models") or [])
            aggregator_model = str(plan.get("aggregator_model") or "")
            dry_provider.proposer_models = proposer_models
            dry_provider.model = aggregator_model
            provider_native_policy = plan.get(
                "proposer_recovery_policy"
            )
            expected_provider_native_policy = (
                formal_proposer_recovery_policy_for_plan(plan)
            )
            dry_provider.min_successful_proposers = (
                2
                if isinstance(provider_native_policy, Mapping)
                and expected_provider_native_policy is not None
                and dict(provider_native_policy) == expected_provider_native_policy
                else legal_proposer_quorum(len(proposer_models))
            )
            dry_routing_trace.update(
                {
                    "benchmark_alignment": g1_routing.profile_id,
                    "profile": "router_dynamic",
                    "selection_plan": plan,
                }
            )
            dry_provider.selection_plan = copy.deepcopy(plan)
        if isinstance(dry_provider, DryEnsembleProvider):
            dry_provider.selection_plan.update(recovery_policy)
            if g1_routing is not None:
                expected_proposer_policy = proposer_recovery_plan_policy(
                    proposer_policy
                )
                actual_proposer_policy = dry_provider.selection_plan.get(
                    "proposer_recovery_policy"
                )
                if isinstance(actual_proposer_policy, Mapping):
                    effective_backup_count = actual_proposer_policy.get(
                        "effective_backup_count"
                    )
                    configured_backup_count = expected_proposer_policy[
                        "configured_backup_count"
                    ]
                    if (
                        isinstance(effective_backup_count, int)
                        and not isinstance(effective_backup_count, bool)
                        and 0 <= effective_backup_count <= configured_backup_count
                    ):
                        expected_proposer_policy["effective_backup_count"] = (
                            effective_backup_count
                        )
                if (
                    actual_proposer_policy != expected_proposer_policy
                ):
                    raise ValueError(
                        "dry G1 proposer recovery policy differs from the "
                        "formal experiment contract"
                    )
            selected_aggregator = str(dry_provider.selection_plan.get("selected_A") or "").strip()
            if selected_aggregator:
                dry_provider.selection_plan.setdefault(
                    "aggregator_candidates", [selected_aggregator]
                )
            dry_routing_trace["aggregator_recovery_policy"] = {
                "schema": "opensquilla.ensemble-aggregator-recovery-policy/v1",
                "evidence_kind": "dry_run_policy_only",
                **dict(recovery_policy),
            }
            if g1_routing is not None:
                dry_routing_trace["proposer_recovery_policy"] = {
                    "evidence_kind": "dry_run_policy_only",
                    **dict(
                        dry_provider.selection_plan[
                            "proposer_recovery_policy"
                        ]
                    ),
                }
            dry_routing_trace["selection_plan"] = copy.deepcopy(dry_provider.selection_plan)
        result = ProviderBuildResult(
            provider=dry_provider,
            prompt=prompt,
            setup_latency_ms=int((time.monotonic() - started) * 1000),
            routing_trace=dry_routing_trace,
        )
        attach_provider_setup(dry_provider, result)
        return result

    if kind == "single":
        provider = build_single_provider(
            inherited=inherited,
            group=group,
            model=spec["model"],
            dry_run=False,
            provider=spec.get("provider"),
            base_url=spec.get("base_url"),
            api_key=spec.get("api_key"),
            api_key_env=spec.get("api_key_env"),
        )
        result = ProviderBuildResult(
            provider=provider,
            prompt=prompt,
            setup_latency_ms=int((time.monotonic() - started) * 1000),
            routing_trace={
                "kind": kind,
                "selection": "fixed",
                "model": spec["model"],
            },
        )
        attach_provider_setup(provider, result)
        return result

    group_config = config.model_copy(deep=True)
    selector = ModelSelector(SelectorConfig(primary=inherited))
    fallback_provider = selector.resolve()
    session_key = f"draco:{group}:{hashlib.sha256(prompt.encode()).hexdigest()[:16]}"
    turn = TurnContext(
        message=prompt,
        session_key=session_key,
        config=group_config,
        provider=fallback_provider,
        model=inherited.model,
        tool_defs=[],
        system_prompt="",
        attachments=[],
        metadata={},
        raw_message=prompt,
    )
    if b2_experiment is not None and b2_experiment.routing.skip_single_model_router:
        # This quality-first profile retains G12's fixed B2 model mapping and
        # skips the single-model router. Keep the inherited provider as fallback.
        routed_provider = fallback_provider
        routed_config = inherited
        routing_trace: dict[str, Any] = {
            "kind": kind,
            "routing_applied": False,
            "routing_source": "b2_quality_first_profile",
            "routed_model": None,
            "applied_model": None,
            "benchmark_alignment": b2_experiment.profile_id,
        }
    else:
        turn = await run_pipeline(turn, [apply_squilla_router])
        routed_provider = apply_model_override(
            selector,
            turn.model or inherited.model,
            turn_metadata=turn.metadata,
            realign_routed_model=False,
        )
        routed_config = selector.current_config
        routing_trace = {
            "kind": kind,
            "routed_tier": turn.metadata.get("routed_tier"),
            "routed_model": turn.metadata.get("routed_model") or turn.model,
            "applied_model": turn.model,
            "routing_source": turn.metadata.get("routing_source"),
            "routing_confidence": turn.metadata.get("routing_confidence"),
            "routing_applied": turn.metadata.get("routing_applied"),
            "rollout_phase": turn.metadata.get("rollout_phase"),
        }
    routing_trace["fallback_model"] = routed_config.model
    if kind == "router_single":
        result = ProviderBuildResult(
            provider=routed_provider,
            prompt=turn.message,
            setup_latency_ms=int((time.monotonic() - started) * 1000),
            routing_trace=routing_trace,
        )
        attach_provider_setup(routed_provider, result)
        return result

    selection_mode = (
        b2_experiment.routing.selection_mode
        if b2_experiment is not None
        else spec["selection_mode"]
    )
    ensemble_cfg = group_config.llm_ensemble
    ensemble_cfg.enabled = True
    ensemble_cfg.selection_mode = selection_mode
    apply_aggregator_recovery_policy(ensemble_cfg, recovery_policy)
    if g1_routing is not None:
        apply_aggregator_recovery_policy(ensemble_cfg, proposer_policy)
        ensemble_cfg.min_successful_proposers = (
            FORMAL_PROPOSER_RECOVERY_POLICY["quorum_required"]
        )
        ensemble_cfg.ranking_user_profile_generation_enabled = False
        ensemble_cfg.ranking_user_profile_enabled = bool(g1_routing.user_profile_enabled)
    ensemble_cfg.proposer_tools = (
        b2_experiment.ensemble.proposer_tools
        if b2_experiment is not None
        else bool(enable_proposer_tools)
    )
    ensemble_cfg.aggregator_tools = (
        b2_experiment.ensemble.aggregator_tools if b2_experiment is not None else True
    )
    ensemble_cfg.record_candidates = (
        b2_experiment.ensemble.record_candidates if b2_experiment is not None else True
    )
    experiment_ensemble = (
        b2_experiment.ensemble
        if b2_experiment is not None
        else g1_ensemble
    )
    ensemble_cfg.shuffle_candidates = (
        experiment_ensemble.shuffle_candidates
        if experiment_ensemble is not None
        else False
    )
    ensemble_cfg.candidate_order_seed = (
        experiment_ensemble.candidate_order_seed
        if experiment_ensemble is not None
        else None
    )
    if ensemble_proposer_timeout is not None:
        ensemble_cfg.proposer_timeout_seconds = float(ensemble_proposer_timeout)
    if ensemble_aggregator_timeout is not None:
        ensemble_cfg.aggregator_timeout_seconds = float(ensemble_aggregator_timeout)

    decision_id = uuid.uuid4().hex
    turn.metadata["ensemble_decision_id"] = decision_id
    turn.metadata["ensemble_enabled"] = True
    turn.metadata["routed_model_before_ensemble"] = turn.model or routed_config.model
    ranking_inputs: dict[str, Any] | None = None
    setup_usage: list[dict[str, Any]] = []
    if selection_mode == "router_dynamic":
        from opensquilla.provider.ranking_router import (
            TaskAnalyzerCandidate,
            TaskAnalyzerPhysicalEvidenceError,
            TaskAnalyzerStreamCleanupError,
            analyze_task_with_fallback_chain,
            analyze_task_with_provider,
            build_request_context,
            dynamic_output_token_budgets,
            frozen_task_analysis_result,
            mock_user_profile,
            task_analyzer_chain_policy,
            task_analyzer_policy,
        )

        ranking_resolution = (
            group_config.llm_ensemble.ranking_config_resolution_snapshot()
        )
        thinking_assignment_enabled = (
            ranking_resolution.get("thinking_assignment_enabled") is True
        )
        ranking_config = ranking_resolution["effective_config"]
        analyzer_policy = task_analyzer_policy(ranking_config)
        analyzer_provider_id = str(analyzer_policy["provider"])
        analyzer_model_id = str(analyzer_policy["model"])
        common_analyzer_chain = task_analyzer_chain_policy(ranking_config)
        resolved_analyzer_chain = (
            task_analyzer_execution_contract.get("routes")
            if isinstance(task_analyzer_execution_contract, Mapping)
            and str(
                task_analyzer_execution_contract.get("route_source") or ""
            ).startswith("g1_registry_contract.")
            else None
        )
        configured_analyzer_chain = (
            [dict(route) for route in resolved_analyzer_chain]
            if isinstance(resolved_analyzer_chain, list)
            and resolved_analyzer_chain
            and all(isinstance(route, Mapping) for route in resolved_analyzer_chain)
            else [
                route.model_dump(mode="json")
                for route in g1_routing.live_task_analyzer_chain
            ]
            if g1_routing is not None and g1_routing.live_task_analyzer_chain
            else []
        )
        analyzer_routes = (
            [
                {
                    "provider": str(route["provider"]),
                    "model": str(route["model"]),
                    "upstream_provider": str(route["upstream_provider"]),
                }
                for route in configured_analyzer_chain
            ]
            or list(common_analyzer_chain["routes"])
        )
        routing_extra = turn.metadata.get("routing_extra")
        routing_extra_map = routing_extra if isinstance(routing_extra, Mapping) else {}
        routed_tier = str(
            turn.metadata.get("routed_tier")
            or routing_extra_map.get("final_tier")
            or routing_extra_map.get("base_tier")
            or "c1"
        )
        try:
            routing_confidence = float(turn.metadata.get("routing_confidence") or 0.0)
        except (TypeError, ValueError):
            routing_confidence = 0.0
        if thinking_assignment_enabled:
            configured_output_tokens, _ = (
                resolve_effective_generation_request_parameters(
                    llm_config=getattr(group_config, "llm", None),
                    generation_policy=generation_policy,
                )
            )
        else:
            configured_output_tokens = int(
                getattr(getattr(group_config, "llm", None), "max_tokens", 0)
                or 0
            )
        candidate_output_tokens, aggregator_output_tokens = dynamic_output_token_budgets(
            configured_output_tokens=configured_output_tokens,
            candidate_max_chars=int(ensemble_cfg.candidate_max_chars or 0),
            ranking_config=ranking_config,
        )
        request_context = build_request_context(
            message=turn.semantic_message,
            turn_metadata=turn.metadata,
            attachments=turn.attachments,
            candidate_output_tokens=candidate_output_tokens,
            aggregator_output_tokens=aggregator_output_tokens,
            ranking_config=ranking_config,
        )
        user_profile_enabled = (
            bool(g1_routing.user_profile_enabled)
            if g1_routing is not None
            else bool(ensemble_cfg.ranking_user_profile_enabled)
        )
        user_profile = mock_user_profile(ranking_config) if user_profile_enabled else None
        replay = g1_routing.task_analysis_execution if g1_routing is not None else None
        replay_contract = replay.model_dump(mode="json") if replay is not None else None
        analyzer_candidates = (
            []
            if replay_contract is not None
            else [
                TaskAnalyzerCandidate(
                    provider=build_task_analyzer_provider(
                        routed_config,
                        provider_id=str(route["provider"]),
                        model_id=str(route["model"]),
                        upstream_provider=str(route["upstream_provider"]),
                    ),
                    provider_id=str(route["provider"]),
                    model_id=str(route["model"]),
                    upstream_provider=str(route["upstream_provider"]),
                )
                for route in analyzer_routes
            ]
        )
        try:
            if replay_contract is not None:
                if not task_id:
                    raise ValueError("frozen task analysis replay requires task_id")
                task_analysis = frozen_task_analysis_result(
                    replay_contract,
                    task_id=task_id,
                    task_input_sha256=task_input_sha256,
                    prompt_sha256=prompt_sha256,
                    routed_tier=routed_tier,
                    request_context=request_context,
                    ranking_config=ranking_config,
                )
            elif configured_analyzer_chain or common_analyzer_chain["configured"]:
                task_analysis = await analyze_task_with_fallback_chain(
                    candidates=analyzer_candidates,
                    message=turn.semantic_message,
                    user_profile_enabled=user_profile_enabled,
                    request_context=request_context,
                    routed_tier=routed_tier,
                    routing_confidence=routing_confidence,
                    ranking_config=ranking_config,
                    schema_repair_max_retries=(
                        int(
                            task_analyzer_execution_contract[
                                "schema_repair_max_retries"
                            ]
                        )
                        if configured_analyzer_chain
                        and isinstance(task_analyzer_execution_contract, Mapping)
                        and isinstance(
                            task_analyzer_execution_contract.get(
                                "schema_repair_max_retries"
                            ),
                            int,
                        )
                        else None
                    ),
                    absolute_deadline=turn_absolute_deadline,
                    decision_id=decision_id,
                )
            else:
                task_analysis = await analyze_task_with_provider(
                    provider=analyzer_candidates[0].provider,
                    message=turn.semantic_message,
                    user_profile_enabled=user_profile_enabled,
                    request_context=request_context,
                    routed_tier=routed_tier,
                    routing_confidence=routing_confidence,
                    analyzer_provider_id=analyzer_provider_id,
                    analyzer_model_id=analyzer_model_id,
                    ranking_config=ranking_config,
                    decision_id=decision_id,
                    _absolute_deadline=turn_absolute_deadline,
                )
        except (
            TaskAnalyzerPhysicalEvidenceError,
            TaskAnalyzerStreamCleanupError,
        ) as exc:
            # Cleanup failure is raised only after the analyzer's provider
            # stream was opened.  Preserve a conservative unknown-usage
            # receipt so the paid generation attempt cannot disappear.
            setup_usage.extend(
                task_analyzer_usage_rows(
                    exc.usage or {"attempt_count": 1},
                    provider_id=analyzer_provider_id,
                    model_id=analyzer_model_id,
                    source="analyzer_stream_cleanup_failed",
                    fallback_reason=type(exc).__name__,
                )
            )
            routing_trace["task_analyzer"] = {
                "provider": analyzer_provider_id,
                "model": analyzer_model_id,
                "configured_chain": analyzer_routes,
                "source": "analyzer_stream_cleanup_failed",
                "schema_valid": False,
                "fallback_reason": type(exc).__name__,
                "request_context_hash": request_context.get("snapshot_hash"),
                "user_profile_enabled": user_profile_enabled,
            }
            raise ProviderBuildError(
                exc,
                setup_latency_ms=int((time.monotonic() - started) * 1000),
                setup_usage=setup_usage,
                routing_trace=safe_provider_build_routing_trace(
                    routing_trace
                ),
            ) from exc
        analyzer_postprocess_error: Exception | None = None
        analyzer_usage_materialization_failed = False
        raw_analyzer_usage: Any = None
        try:
            raw_analyzer_usage = task_analysis.usage
            analyzer_usage = dict(raw_analyzer_usage or {})
        except Exception as exc:  # noqa: BLE001 - preserve raw retry evidence
            analyzer_postprocess_error = exc
            analyzer_usage_materialization_failed = True
            analyzer_usage = {
                "attempt_count": 1,
                "usage_unknown": True,
            }
        try:
            analyzer_source = str(task_analysis.source or "")
            analyzer_fallback_reason = str(
                task_analysis.fallback_reason or ""
            )
        except Exception as exc:  # noqa: BLE001 - usage was already captured
            analyzer_postprocess_error = (
                analyzer_postprocess_error or exc
            )
            analyzer_source = "analyzer_postprocess_failed"
            analyzer_fallback_reason = type(exc).__name__
        try:
            if replay_contract is not None:
                # A frozen replay is evidence-only.  It must never synthesize
                # the one-request fallback that task_analyzer_usage_rows uses
                # for an otherwise empty live Analyzer usage mapping.
                analyzer_usage_rows = []
            elif analyzer_usage_materialization_failed:
                analyzer_usage_rows = conservative_task_analyzer_usage_rows(
                    (
                        raw_analyzer_usage
                        if isinstance(raw_analyzer_usage, Mapping)
                        else analyzer_usage
                    ),
                    provider_id=analyzer_provider_id,
                    model_id=analyzer_model_id,
                    source="analyzer_postprocess_failed",
                    fallback_reason=type(
                        analyzer_postprocess_error
                    ).__name__,
                )
            else:
                analyzer_usage_rows = task_analyzer_usage_rows(
                    analyzer_usage,
                    provider_id=analyzer_provider_id,
                    model_id=analyzer_model_id,
                    source=analyzer_source,
                    fallback_reason=analyzer_fallback_reason,
                )
        except Exception as exc:  # noqa: BLE001 - retain one unknown receipt
            analyzer_postprocess_error = (
                analyzer_postprocess_error or exc
            )
            analyzer_usage_rows = conservative_task_analyzer_usage_rows(
                (
                    raw_analyzer_usage
                    if isinstance(raw_analyzer_usage, Mapping)
                    else analyzer_usage
                ),
                provider_id=analyzer_provider_id,
                model_id=analyzer_model_id,
                source="analyzer_postprocess_failed",
                fallback_reason=type(exc).__name__,
            )
        setup_usage.extend(analyzer_usage_rows)
        if analyzer_postprocess_error is not None:
            routing_trace["task_analyzer"] = {
                "provider": analyzer_provider_id,
                "model": analyzer_model_id,
                "source": "analyzer_postprocess_failed",
                "schema_valid": False,
                "fallback_reason": type(
                    analyzer_postprocess_error
                ).__name__,
                "request_context_hash": request_context.get("snapshot_hash"),
                "user_profile_enabled": user_profile_enabled,
            }
            raise ProviderBuildError(
                analyzer_postprocess_error,
                setup_latency_ms=int((time.monotonic() - started) * 1000),
                setup_usage=setup_usage,
                routing_trace=safe_provider_build_routing_trace(
                    routing_trace
                ),
            ) from analyzer_postprocess_error
        try:
            ranking_inputs = {
                "decision_id": decision_id,
                "task_analysis": task_analysis,
                "user_profile": user_profile,
                "request_context": request_context,
                "ranking_config": ranking_config,
                "generation_policy": (
                    dict(generation_policy)
                    if generation_policy is not None
                    else None
                    if thinking_assignment_enabled
                    else {}
                ),
                "registry_allowlist": resolved_g1_registry_contract,
                "task_analyzer_execution_contract": (
                    task_analyzer_execution_contract
                ),
            }
            analyzer_failure_fallback = (
                g1_routing.analyzer_failure_fallback_ensemble
                if g1_routing is not None
                and replay_contract is None
                and not task_analysis.schema_valid
                else None
            )
            if analyzer_failure_fallback is not None:
                fallback_payload = analyzer_failure_fallback.model_dump(
                    mode="json"
                )
                ranking_inputs["analyzer_failure_fallback"] = {
                    "schema": (
                        "opensquilla.router-dynamic-"
                        "analyzer-failure-fallback/v1"
                    ),
                    **fallback_payload,
                }
                ensemble_cfg.min_successful_proposers = 1
                ensemble_cfg.all_failed_policy = "error"
            if thinking_assignment_enabled:
                ranking_inputs["request_tools_present"] = bool(tools)
            turn.metadata["router_dynamic_task_profile"] = task_analysis.profile
            turn.metadata["router_dynamic_task_analyzer"] = task_analysis.trace(
                ranking_config
            )
            turn.metadata["router_dynamic_request_context_hash"] = (
                request_context.get("snapshot_hash")
            )
            routing_trace["task_analyzer"] = {
                **task_analysis.trace(ranking_config),
                "request_context_hash": request_context.get("snapshot_hash"),
                "user_profile_enabled": user_profile_enabled,
            }
        except Exception as exc:  # noqa: BLE001 - preserve a paid analyzer call
            if not setup_usage:
                raise
            routing_trace["task_analyzer"] = {
                "provider": analyzer_provider_id,
                "model": analyzer_model_id,
                "source": "analyzer_postprocess_failed",
                "schema_valid": False,
                "fallback_reason": type(exc).__name__,
                "request_context_hash": request_context.get("snapshot_hash"),
                "user_profile_enabled": user_profile_enabled,
            }
            raise ProviderBuildError(
                exc,
                setup_latency_ms=int((time.monotonic() - started) * 1000),
                setup_usage=setup_usage,
                routing_trace=safe_provider_build_routing_trace(
                    routing_trace
                ),
            ) from exc

    def materialize_ensemble_provider(
        active_ranking_inputs: Mapping[str, Any] | None,
    ) -> Any:
        materialized = build_ensemble_provider_from_config(
            config=group_config,
            inherited_provider_config=routed_config,
            fallback_provider=routed_provider,
            turn_metadata=turn.metadata,
            ranking_inputs=active_ranking_inputs,
            _absolute_deadline=turn_absolute_deadline,
        )
        if b2_experiment is not None:
            materialized = align_b2_provider_to_g12(materialized, b2_experiment)
        if g1_routing is not None:
            materialized.selection_plan["user_profile_enabled"] = user_profile_enabled
            if g1_ensemble is not None:
                materialized.quorum_grace_seconds = g1_ensemble.quorum_grace_seconds
                materialized.selection_plan["quorum_grace_seconds"] = (
                    g1_ensemble.quorum_grace_seconds
                )
                materialized.selection_plan["wait_for_all_proposers"] = (
                    g1_ensemble.wait_for_all_proposers
                )
        materialized = enforce_draco_legal_proposer_quorum(materialized)
        if generation_policy is not None:
            materialized = apply_generation_policy_to_ensemble_provider(
                materialized,
                generation_policy,
            )
            validate_strict_openrouter_ensemble_members(
                materialized,
                generation_policy,
                fallback_config=routed_config,
                allow_unpinned_openrouter=bool(
                    isinstance(resolved_g1_registry_contract, Mapping)
                    and resolved_g1_registry_contract.get("candidate_scope") == "registry_all"
                ),
            )
        materialized.seal_managed_thinking_execution_guard()
        return materialized

    try:
        provider = materialize_ensemble_provider(ranking_inputs)
        routing_trace.update(
            {
                "selection_mode": selection_mode,
                "profile": provider.profile_name,
                "selection_plan": provider.selection_plan,
                "user_profile_enabled": user_profile_enabled if g1_routing is not None else None,
            }
        )
    except Exception as exc:
        if setup_usage:
            raise ProviderBuildError(
                exc,
                setup_latency_ms=int((time.monotonic() - started) * 1000),
                setup_usage=setup_usage,
                routing_trace=safe_provider_build_routing_trace(
                    routing_trace
                ),
            ) from exc
        raise
    try:
        result = ProviderBuildResult(
            provider=provider,
            prompt=turn.message,
            setup_latency_ms=int((time.monotonic() - started) * 1000),
            setup_usage=setup_usage,
            routing_trace=routing_trace,
        )
        attach_provider_setup(provider, result)
        return result
    except ProviderBuildError:
        raise
    except Exception as exc:
        if setup_usage:
            raise ProviderBuildError(
                exc,
                setup_latency_ms=int(
                    (time.monotonic() - started) * 1000
                ),
                setup_usage=setup_usage,
                routing_trace=safe_provider_build_routing_trace(
                    routing_trace
                ),
            ) from exc
        raise


def build_profile_provider(
    *,
    config: GatewayConfig,
    inherited: ProviderConfig,
    group: str,
    profile: str,
    dry_run: bool,
    generation_policy: dict[str, Any] | None = None,
    requested_timeout: float | None = None,
    enable_proposer_tools: bool = False,
    ensemble_proposer_timeout: float | None = None,
    ensemble_aggregator_timeout: float | None = None,
    ensemble_proposer_early_stop_success_count: int | None = None,
    ensemble_proposer_early_stop_after: float | None = None,
    expand_ensemble_timeouts_to_task_timeout: bool = False,
):
    if dry_run:
        return DryEnsembleProvider(group=group, profile=profile)
    if profile not in config.llm_ensemble.profiles:
        raise ValueError(f"profile {profile!r} for group {group} is not configured")
    config.llm_ensemble.enabled = True
    config.llm_ensemble.active_profile = profile
    config.llm_ensemble.proposer_tools = bool(enable_proposer_tools)
    profile_config = config.llm_ensemble.profiles[profile]
    if generation_policy is not None:
        profile_config = apply_generation_policy_to_profile(
            profile_config,
            generation_policy,
        )
    proposer_timeout_s, aggregator_timeout_s = profile_timeout_seconds(
        profile_config,
        requested_timeout=requested_timeout,
        proposer_timeout_override=ensemble_proposer_timeout,
        aggregator_timeout_override=ensemble_aggregator_timeout,
        expand_to_requested_timeout=expand_ensemble_timeouts_to_task_timeout,
    )
    profile_updates: dict[str, Any] = {
        "record_candidates": True,
        "shuffle_candidates": False,
        "proposer_timeout_seconds": proposer_timeout_s,
        "aggregator_timeout_seconds": aggregator_timeout_s,
    }
    if ensemble_proposer_early_stop_success_count is not None:
        profile_updates["proposer_early_stop_success_count"] = max(
            0,
            int(ensemble_proposer_early_stop_success_count or 0),
        )
    if ensemble_proposer_early_stop_after is not None:
        profile_updates["proposer_early_stop_after_seconds"] = max(
            0.0,
            float(ensemble_proposer_early_stop_after or 0.0),
        )
    config.llm_ensemble.profiles[profile] = profile_config.model_copy(update=profile_updates)
    fallback = build_single_provider(
        inherited=inherited,
        group=f"{group}-fallback",
        model=inherited.model,
        dry_run=False,
    )
    return build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=fallback,
    )


def _consume_background_task(task: asyncio.Future[Any]) -> None:
    with contextlib.suppress(BaseException):
        task.result()


_BENCHMARK_OWNER_CLEANUP_STATE: dict[int, dict[str, Any]] = {}


def _benchmark_owner_state(owner: Any) -> dict[str, Any]:
    key = id(owner)
    state = _BENCHMARK_OWNER_CLEANUP_STATE.get(key)
    if state is None or state.get("owner") is not owner:
        state = {"owner": owner, "pending": set(), "poisoned_reason": ""}
        _BENCHMARK_OWNER_CLEANUP_STATE[key] = state
    return state


def _benchmark_owner_cleanup_reason(owner: Any) -> str:
    state = _benchmark_owner_state(owner)
    poisoned = str(state.get("poisoned_reason") or "")
    if poisoned:
        return poisoned
    pending = state.get("pending")
    if pending:
        return "owned_cleanup_in_progress"
    _BENCHMARK_OWNER_CLEANUP_STATE.pop(id(owner), None)
    return ""


def _poison_benchmark_owner(owner: Any, reason: str) -> None:
    _benchmark_owner_state(owner)["poisoned_reason"] = reason


def _track_benchmark_owner_cleanup(
    owner: Any,
    task: asyncio.Future[Any],
    *,
    reason: str,
) -> None:
    pending = _benchmark_owner_state(owner)["pending"]
    pending.add(task)

    def _finished(done: asyncio.Future[Any]) -> None:
        pending.discard(done)
        _observe_benchmark_cleanup_result(owner, done, reason=reason)
        state = _BENCHMARK_OWNER_CLEANUP_STATE.get(id(owner))
        if state is not None and not state.get("pending") and not state.get("poisoned_reason"):
            _BENCHMARK_OWNER_CLEANUP_STATE.pop(id(owner), None)

    task.add_done_callback(_finished)


class _RunnerStreamCloseError(RuntimeError):
    pass


def _observe_benchmark_cleanup_result(
    owner: Any,
    task: asyncio.Future[Any],
    *,
    reason: str,
) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        # Expected after timeout: cancellation is safe only because the
        # consumer's owned close path has now completed.
        return
    except _RunnerStreamCloseError:
        _poison_benchmark_owner(owner, reason)
    except BaseException:
        # The owned stream wrapper already ran its close path before exposing
        # any unrelated consumer exception.
        return


async def _close_runner_stream(stream_iter: Any, stream: Any) -> None:
    lookup_errors: list[BaseException] = []
    candidates = [stream_iter]
    if stream is not stream_iter:
        candidates.append(stream)
    for candidate in candidates:
        try:
            aclose = getattr(candidate, "aclose", None)
        except BaseException as exc:
            lookup_errors.append(exc)
            continue
        if not callable(aclose):
            continue
        try:
            await aclose()
        except BaseException as exc:
            raise _RunnerStreamCloseError(
                "provider_stream_close_failed: iterator aclose failed"
            ) from exc
        return
    cause = lookup_errors[-1] if lookup_errors else None
    error = _RunnerStreamCloseError(
        "provider_stream_close_unavailable: iterator has no usable aclose"
    )
    if cause is not None:
        raise error from cause
    raise error


async def _aclosing_events(stream: Any) -> Any:
    try:
        stream_iter = stream.__aiter__()
    except BaseException as exc:
        try:
            await _close_runner_stream(stream, stream)
        except _RunnerStreamCloseError:
            raise
        raise exc
    exhausted = False
    try:
        while True:
            try:
                yield await stream_iter.__anext__()
            except StopAsyncIteration:
                exhausted = True
                return
    finally:
        if exhausted:
            return
        await _close_runner_stream(stream_iter, stream)


async def _await_benchmark_consumer(
    consumer: Any,
    *,
    timeout: float,
    owner: Any,
) -> str:
    """Own one stream consumer and bound cancellation cleanup.

    Returns ``"timeout"`` when cancellation closed the stream, or
    ``"cleanup_timeout"`` when the consumer remained alive after the bounded
    close window.  The latter must suppress generation retries.
    """

    task = asyncio.create_task(consumer)
    try:
        if not timeout or timeout <= 0:
            await task
            return ""
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except BaseException:
        if not task.done():
            task.cancel()
            try:
                cleaned, _ = await asyncio.wait(
                    {task},
                    timeout=max(0.0, RUNNER_STREAM_CLEANUP_TIMEOUT_SECONDS),
                )
            except BaseException:
                _track_benchmark_owner_cleanup(
                    owner,
                    task,
                    reason="external_cancel_cleanup_failed",
                )
                task.add_done_callback(_consume_background_task)
                raise
            if not cleaned:
                _track_benchmark_owner_cleanup(
                    owner,
                    task,
                    reason="external_cancel_cleanup_timeout",
                )
                task.add_done_callback(_consume_background_task)
            else:
                _observe_benchmark_cleanup_result(
                    owner,
                    task,
                    reason="external_cancel_cleanup_failed",
                )
        else:
            _observe_benchmark_cleanup_result(
                owner,
                task,
                reason="external_cancel_cleanup_failed",
            )
        raise
    if done:
        try:
            await task
        except _RunnerStreamCloseError:
            _poison_benchmark_owner(owner, "stream_close_failed")
            raise
        return ""
    task.cancel()
    cleaned, _ = await asyncio.wait(
        {task},
        timeout=max(0.0, RUNNER_STREAM_CLEANUP_TIMEOUT_SECONDS),
    )
    if not cleaned:
        _track_benchmark_owner_cleanup(
            owner,
            task,
            reason="stream_close_timeout",
        )
        task.add_done_callback(_consume_background_task)
        return "cleanup_timeout"
    try:
        await task
    except asyncio.CancelledError:
        pass
    except _RunnerStreamCloseError:
        _poison_benchmark_owner(owner, "stream_close_failed")
        raise
    return "timeout"


async def collect_run(
    provider: Any,
    prompt: str,
    *,
    timeout: float,
    config: ChatConfig | None = None,
    tools: list[ToolDefinition] | None = None,
) -> RunResult:
    messages = [Message(role="user", content=prompt)]
    text_parts: list[str] = []
    done: DoneEvent | None = None
    error = ""
    ttft_ms: int | None = None
    tool_call_count = 0
    trace_events: list[dict[str, Any]] = []
    started = time.monotonic()

    def _trace(kind: str, **payload: Any) -> None:
        trace_events.append(
            {
                "seq": len(trace_events) + 1,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "kind": kind,
                **payload,
            }
        )

    owner_cleanup_reason = _benchmark_owner_cleanup_reason(provider)
    if owner_cleanup_reason:
        error = (
            "benchmark_owner_cleanup_in_progress: a prior stream owned by this "
            "provider has not proved closure"
        )
        _trace(
            "cleanup_in_progress",
            code="benchmark_owner_cleanup_in_progress",
            reason=owner_cleanup_reason,
        )
        return RunResult(
            final_text="",
            done=None,
            error=error,
            latency_ms=int((time.monotonic() - started) * 1000),
            trace_events=trace_events,
        )

    try:
        chat_config = (
            config.model_copy(update={"timeout": timeout})
            if config is not None
            else ChatConfig(timeout=timeout)
        )

        async def _consume() -> None:
            nonlocal done, error, ttft_ms, tool_call_count
            stream = provider.chat(
                messages,
                tools=tools,
                config=chat_config,
            )
            async with contextlib.aclosing(_aclosing_events(stream)) as owned_stream:
                async for event in owned_stream:
                    if isinstance(event, TextDeltaEvent):
                        if ttft_ms is None and event.text:
                            ttft_ms = int((time.monotonic() - started) * 1000)
                            _trace("first_text_delta", text_chars=len(event.text))
                        else:
                            _trace("text_delta", text_chars=len(event.text))
                        text_parts.append(event.text)
                    elif isinstance(event, ReasoningDeltaEvent):
                        _trace("reasoning_delta", text_chars=len(event.text))
                    elif isinstance(event, ToolUseStartEvent):
                        tool_call_count += 1
                        _trace(
                            "tool_use_start",
                            tool_use_id=event.tool_use_id,
                            tool_name=event.tool_name,
                            synthetic_from_text=event.synthetic_from_text,
                        )
                    elif isinstance(event, ToolUseDeltaEvent):
                        _trace(
                            "tool_use_delta",
                            tool_use_id=event.tool_use_id,
                            json_fragment_chars=len(event.json_fragment),
                        )
                    elif isinstance(event, ToolUseEndEvent):
                        _trace(
                            "tool_use_end",
                            tool_use_id=event.tool_use_id,
                            tool_name=event.tool_name,
                            argument_keys=sorted(event.arguments.keys()),
                            synthetic_from_text=event.synthetic_from_text,
                        )
                    elif isinstance(event, ProviderHeartbeatEvent):
                        _trace(
                            "provider_heartbeat",
                            phase=event.phase,
                            message=event.message,
                        )
                    elif isinstance(event, DoneEvent):
                        done = event
                        _trace(
                            "done",
                            stop_reason=event.stop_reason,
                            usage=done_payload(event),
                            has_ensemble_trace=bool(event.ensemble_trace),
                        )
                    elif isinstance(event, ErrorEvent):
                        diagnostic_done = diagnostic_done_from_error_event(event)
                        if done is None and diagnostic_done is not None:
                            done = diagnostic_done
                            _trace(
                                "diagnostic_done",
                                stop_reason=done.stop_reason,
                                usage=done_payload(done),
                                has_ensemble_trace=bool(done.ensemble_trace),
                            )
                        error = event.message
                        _trace(
                            "error",
                            message=event.message,
                            code=event.code,
                            request_started=event.request_started,
                            physical_request_count=event.physical_request_count,
                        )
                        break
                    else:
                        _trace("stream_event", event_type=type(event).__name__)

        consume_outcome = await _await_benchmark_consumer(
            _consume(),
            timeout=timeout,
            owner=provider,
        )
        if consume_outcome == "cleanup_timeout":
            error = (
                "provider_stream_close_timeout: provider stream did not close "
                "within the bounded cleanup window"
            )
            _trace(
                "cleanup_timeout",
                code="provider_stream_close_timeout",
                timeout_s=RUNNER_STREAM_CLEANUP_TIMEOUT_SECONDS,
            )
        elif consume_outcome == "timeout":
            error = f"TimeoutError: run timed out after {timeout:g}s"
            _trace("timeout", timeout_s=timeout)
    except Exception as exc:  # noqa: BLE001 - benchmark rows should keep going
        error = type(exc).__name__
        _trace("exception", error=error)
    setup = consume_provider_setup(provider)
    setup_latency_ms = coerce_metric_int(setup.get("latency_ms"))
    setup_usage = setup.get("usage") if isinstance(setup.get("usage"), list) else []
    routing_trace = setup.get("routing") if isinstance(setup.get("routing"), dict) else {}
    if setup:
        trace_events.insert(
            0,
            {
                "seq": 0,
                "elapsed_ms": setup_latency_ms,
                "kind": "routing_setup",
                "routing": routing_trace,
                "usage": setup_usage,
            },
        )
    return RunResult(
        final_text="".join(text_parts),
        done=done,
        error=error,
        latency_ms=int((time.monotonic() - started) * 1000) + setup_latency_ms,
        ttft_ms=(ttft_ms + setup_latency_ms if ttft_ms is not None else None),
        tool_call_count=tool_call_count,
        trace_events=copy.deepcopy(trace_events),
        setup_latency_ms=setup_latency_ms,
        setup_usage=setup_usage,
        routing_trace=routing_trace,
    )


class BenchmarkTurnCallRecorder:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        self.records.append(
            {
                "seq": len(self.records) + 1,
                "kind": kind,
                "payload": json_safe(payload),
            }
        )


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return json_safe(asdict(value))
    if isinstance(value, Path):
        return str(value)
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


def agent_thinking_from_chat_config(config: ChatConfig | None) -> bool | ThinkingLevel:
    if config is None or not config.thinking:
        return False
    if config.thinking_level is not None:
        try:
            return ThinkingLevel(str(config.thinking_level))
        except ValueError:
            return True
    return True


def agent_config_from_chat_config(
    config: ChatConfig | None,
    *,
    timeout: float,
    model_id: str,
    max_iterations: int,
    finalization_policy: Mapping[str, Any] | None = None,
) -> AgentConfig:
    policy = normalized_agent_finalization_policy(finalization_policy)
    return AgentConfig(
        max_iterations=max(0, int(max_iterations)),
        timeout=timeout,
        iteration_timeout=timeout,
        request_timeout=(
            float(config.timeout)
            if config is not None and getattr(config, "timeout", 0)
            else timeout
        ),
        max_tokens=(
            int(config.max_tokens)
            if config is not None and getattr(config, "max_tokens", 0)
            else AgentConfig().max_tokens
        ),
        temperature=config.temperature if config is not None else None,
        thinking=agent_thinking_from_chat_config(config),
        thinking_budget_tokens=(
            int(config.thinking_budget_tokens)
            if config is not None and getattr(config, "thinking_budget_tokens", 0)
            else AgentConfig().thinking_budget_tokens
        ),
        stop_sequences=list(config.stop_sequences) if config is not None else [],
        model_capabilities=config.model_capabilities if config is not None else None,
        model_id=model_id,
        workspace_dir=str(ROOT),
        tool_result_external_keep_recent=3,
        deadline_wrapup_margin_seconds=int(policy["deadline_wrapup_margin_seconds"]),
        deadline_wrapup_disable_tools=bool(policy["deadline_wrapup_disable_tools"]),
        deadline_thinking_off_margin_seconds=int(policy["deadline_thinking_off_margin_seconds"]),
        max_iterations_includes_finalization=bool(policy["max_iterations_includes_finalization"]),
        retrieval_loop_finalization_threshold=int(policy["retrieval_loop_finalization_threshold"]),
        finalization_aggregator_only=bool(policy["finalization_aggregator_only"]),
        finalization_disable_thinking=bool(policy["finalization_disable_thinking"]),
        metadata={
            "benchmark": "DRACO",
            "provider_retry_owner": "caller",
            "runner_mode": RUNNER_MODE_AGENT_LOOP,
            "tool_activity_heartbeat_interval": 30.0,
            "agent_finalization_policy": dict(policy),
        },
    )


def provider_done_from_agent_done(
    done: AgentDoneEvent | None,
    *,
    recorder: BenchmarkTurnCallRecorder,
    fallback_model: str,
) -> DoneEvent | None:
    call_records = llm_response_records(recorder.records)
    breakdown = aggregate_agent_model_usage(recorder.records)
    trace = aggregate_agent_ensemble_trace(recorder.records)
    ignored_agent_done_summary_rows = 0
    ignored_agent_done_policy_evidence: list[dict[str, Any]] = []
    if done is not None:
        done_rows = [
            dict(row)
            for row in getattr(done, "model_usage_breakdown", [])
            if isinstance(row, Mapping)
        ]
        if (
            not done_rows
            and not call_records
            and not breakdown
            and (
                getattr(done, "billing_receipt", None) is not None
                or getattr(done, "provider_usage", None)
                or done.input_tokens
                or done.output_tokens
                or done.reasoning_tokens
                or done.cached_tokens
                or done.cache_write_tokens
                or done.billed_cost
            )
        ):
            done_rows = [
                {
                    "role": "agent_done",
                    "provider": str(getattr(done, "provider", "") or ""),
                    "model": str(getattr(done, "model", "") or ""),
                    "requested_provider": str(getattr(done, "requested_provider", "") or ""),
                    "requested_model": str(getattr(done, "requested_model", "") or fallback_model),
                    "input_tokens": done.input_tokens,
                    "output_tokens": done.output_tokens,
                    "reasoning_tokens": done.reasoning_tokens,
                    "cached_tokens": done.cached_tokens,
                    "cache_write_tokens": done.cache_write_tokens,
                    "billed_cost": done.billed_cost,
                    "cost_source": done.cost_source,
                    "provider_usage": dict(
                        getattr(done, "provider_usage", {})
                        if isinstance(getattr(done, "provider_usage", {}), Mapping)
                        else {}
                    ),
                    **(
                        {"billing_receipt": getattr(done, "billing_receipt")}
                        if getattr(done, "billing_receipt", None) is not None
                        else {}
                    ),
                }
            ]
        for done_row in done_rows:
            if not call_records:
                breakdown.append(done_row)
                continue
            request_count = max(
                0,
                coerce_metric_int(done_row.get("request_count")),
            )
            response_ids = usage_row_response_ids(done_row)
            if request_count > 1 or len(response_ids) > 1:
                ignored_agent_done_summary_rows += 1
                policy_evidence = ignored_agent_done_summary_policy_evidence(
                    done_row,
                    physical_rows=breakdown,
                )
                if policy_evidence is not None:
                    ignored_agent_done_policy_evidence.append(policy_evidence)
                continue
            if response_ids:
                candidates = [
                    (0, index)
                    for index, row in enumerate(breakdown)
                    if response_ids & usage_row_response_ids(row)
                ]
            else:
                candidates = [
                    (priority, index)
                    for index, row in enumerate(breakdown)
                    if not usage_row_response_ids(row)
                    if (priority := usage_row_match_priority(row, done_row)) is not None
                ]
            if candidates:
                merge_usage_receipt_provenance(
                    breakdown[min(candidates)[1]],
                    done_row,
                )
            elif len(response_ids) == 1:
                breakdown.append(done_row)
            else:
                ignored_agent_done_summary_rows += 1
        done_trace = dict(done.ensemble_trace) if isinstance(done.ensemble_trace, Mapping) else {}
        if done_trace:
            if not trace:
                trace = done_trace
            else:
                for key, value in done_trace.items():
                    trace.setdefault(key, value)
                for key in (
                    "llm_request_count",
                    "physical_request_count",
                    "usage_missing_count",
                ):
                    trace[key] = max(
                        coerce_metric_int(trace.get(key)),
                        coerce_metric_int(done_trace.get(key)),
                    )
    observed_rows = [
        row
        for row in breakdown
        if str(row.get("role") or "").strip().casefold() not in MISSING_USAGE_PLACEHOLDER_ROLES
    ]
    observed_providers = {
        str(row.get("provider") or "").strip()
        for row in observed_rows
        if str(row.get("provider") or "").strip()
    }
    observed_models = {
        str(row.get("model") or "").strip()
        for row in observed_rows
        if str(row.get("model") or "").strip()
    }
    observed_requested_providers = {
        str(row.get("requested_provider") or "").strip()
        for row in observed_rows
        if str(row.get("requested_provider") or "").strip()
    }
    usage_missing_count = max(
        sum(
            1
            for row in breakdown
            if str(row.get("role") or "").strip().casefold() in MISSING_USAGE_PLACEHOLDER_ROLES
        ),
        coerce_metric_int(trace.get("usage_missing_count")) if trace else 0,
        coerce_metric_int(getattr(done, "usage_missing_count", 0) if done is not None else 0),
    )
    if done is not None and (breakdown or usage_missing_count):
        represented_missing = sum(1 for row in breakdown if usage_row_is_missing_placeholder(row))
        physical_request_count = len(breakdown) + max(
            0,
            usage_missing_count - represented_missing,
        )
        trace = dict(trace or {})
        trace["llm_request_count"] = max(
            coerce_metric_int(trace.get("llm_request_count")),
            physical_request_count,
        )
        trace["physical_request_count"] = max(
            coerce_metric_int(trace.get("physical_request_count")),
            physical_request_count,
        )
        trace["usage_missing_count"] = usage_missing_count
    if done is None:
        if not breakdown:
            return None
        sources = {str(row.get("cost_source") or "none").strip().casefold() for row in breakdown}
        if sources == {"provider_billed"}:
            envelope_source = "provider_billed"
        elif len(sources) == 1:
            envelope_source = next(iter(sources))
        else:
            envelope_source = "mixed"
        return DoneEvent(
            stop_reason="error",
            input_tokens=sum(coerce_metric_int(row.get("input_tokens")) for row in breakdown),
            output_tokens=sum(coerce_metric_int(row.get("output_tokens")) for row in breakdown),
            reasoning_tokens=sum(
                coerce_metric_int(row.get("reasoning_tokens")) for row in breakdown
            ),
            cached_tokens=sum(coerce_metric_int(row.get("cached_tokens")) for row in breakdown),
            cache_write_tokens=sum(
                coerce_metric_int(row.get("cache_write_tokens")) for row in breakdown
            ),
            billed_cost=sum(trusted_provider_billed_cost(row) for row in breakdown),
            model=(next(iter(observed_models)) if len(observed_models) == 1 else ""),
            provider=(next(iter(observed_providers)) if len(observed_providers) == 1 else ""),
            cost_source=envelope_source,
            requested_model=fallback_model,
            requested_provider=(
                next(iter(observed_requested_providers))
                if len(observed_requested_providers) == 1
                else ""
            ),
            model_usage_breakdown=breakdown,
            ensemble_trace=trace,
            usage_missing_count=usage_missing_count,
            provider_usage={
                "diagnostic_usage_only": True,
                "agent_llm_call_count": len(call_records),
                "requested_model": fallback_model,
                "requested_provider": (
                    next(iter(observed_requested_providers))
                    if len(observed_requested_providers) == 1
                    else ""
                ),
            },
        )
    if trace:
        trace["agent_iterations"] = done.iterations
    provider_usage: dict[str, Any] = dict(
        getattr(done, "provider_usage", {})
        if isinstance(getattr(done, "provider_usage", {}), Mapping)
        else {}
    )
    provider_usage.update(
        {
            "agent_iterations": done.iterations,
            "agent_llm_call_count": len(call_records),
            "agent_done_summary_rows_ignored": ignored_agent_done_summary_rows,
            "provider_identity_source": (
                "unique_model_usage_breakdown" if len(observed_providers) == 1 else "unresolved"
            ),
            "requested_model": str(getattr(done, "requested_model", "") or fallback_model),
            "requested_provider": str(getattr(done, "requested_provider", "") or ""),
        }
    )
    if ignored_agent_done_policy_evidence:
        provider_usage[IGNORED_AGENT_DONE_POLICY_EVIDENCE_KEY] = ignored_agent_done_policy_evidence
    done_provider = str(getattr(done, "provider", "") or "").strip()
    done_model = str(getattr(done, "model", "") or "").strip()
    provider = done_provider or (
        next(iter(observed_providers)) if len(observed_providers) == 1 else ""
    )
    model = done_model or (next(iter(observed_models)) if len(observed_models) == 1 else "")
    requested_provider = str(
        getattr(done, "requested_provider", "")
        or (
            next(iter(observed_requested_providers))
            if len(observed_requested_providers) == 1
            else ""
        )
        or ""
    )
    provider_usage["requested_provider"] = requested_provider
    provider_done = DoneEvent(
        stop_reason="stop",
        input_tokens=done.input_tokens,
        output_tokens=done.output_tokens,
        reasoning_content=done.reasoning_content,
        reasoning_tokens=done.reasoning_tokens,
        cached_tokens=done.cached_tokens,
        billed_cost=done.billed_cost,
        model=model,
        provider=provider,
        requested_model=str(getattr(done, "requested_model", "") or fallback_model),
        requested_provider=requested_provider,
        cache_write_tokens=done.cache_write_tokens,
        cost_source=done.cost_source,
        model_usage_breakdown=breakdown,
        ensemble_trace=trace,
        usage_missing_count=usage_missing_count,
        billing_receipt=getattr(done, "billing_receipt", None),
        provider_usage=provider_usage,
    )
    return provider_done


async def collect_agent_run(
    provider: Any,
    prompt: str,
    *,
    timeout: float,
    config: ChatConfig | None,
    tools: list[ToolDefinition] | None,
    tool_policy: dict[str, Any],
    task_id: str,
    group: str,
    output_dir: Path | None = None,
    max_iterations: int = DEFAULT_AGENT_MAX_ITERATIONS,
    finalization_policy: Mapping[str, Any] | None = None,
) -> RunResult:
    text_parts: list[str] = []
    done: AgentDoneEvent | None = None
    error = ""
    ttft_ms: int | None = None
    tool_call_count = 0
    trace_events: list[dict[str, Any]] = []
    started = time.monotonic()
    recorder = BenchmarkTurnCallRecorder()

    def _trace(kind: str, **payload: Any) -> None:
        trace_events.append(
            {
                "seq": len(trace_events) + 1,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "kind": kind,
                **payload,
            }
        )

    owner_cleanup_reason = _benchmark_owner_cleanup_reason(provider)
    if owner_cleanup_reason:
        error = (
            "benchmark_owner_cleanup_in_progress: a prior stream owned by this "
            "provider has not proved closure"
        )
        _trace(
            "cleanup_in_progress",
            code="benchmark_owner_cleanup_in_progress",
            reason=owner_cleanup_reason,
        )
        return RunResult(
            final_text="",
            done=None,
            error=error,
            latency_ms=int((time.monotonic() - started) * 1000),
            trace_events=trace_events,
        )

    tool_registry: ToolRegistry | None = None
    tool_context: ToolContext | None = None
    tool_handler = None
    if tool_policy.get("tool_mode") == TOOL_MODE_LOCAL_WEB_TOOLS:
        tool_registry = build_local_web_tool_registry(tool_policy)
        tool_context = build_benchmark_tool_context(
            task_id=task_id,
            group=group,
            tool_policy=tool_policy,
            output_dir=output_dir,
        )
        tool_handler = build_tool_handler(tool_registry, tool_context)

    model_id = getattr(provider, "model", "") or getattr(provider, "profile_name", "")
    agent = Agent(
        provider=provider,
        config=agent_config_from_chat_config(
            config,
            timeout=timeout,
            model_id=str(model_id or ""),
            max_iterations=max_iterations,
            finalization_policy=finalization_policy,
        ),
        tool_definitions=tools,
        tool_handler=tool_handler,
        tool_registry=tool_registry,
        tool_context=tool_context,
        session_key=f"draco:{group}:{task_id}",
        turn_call_logger=recorder,
    )
    try:

        async def _consume() -> None:
            nonlocal done, error, ttft_ms, tool_call_count
            async for event in _aclosing_events(agent.run_turn(prompt)):
                if isinstance(event, AgentTextDeltaEvent):
                    if event.presentation == "answer":
                        if ttft_ms is None and event.text:
                            ttft_ms = int((time.monotonic() - started) * 1000)
                            _trace(
                                "first_text_delta",
                                text_chars=len(event.text),
                                presentation=event.presentation,
                            )
                        else:
                            _trace(
                                "text_delta",
                                text_chars=len(event.text),
                                presentation=event.presentation,
                            )
                        text_parts.append(event.text)
                    else:
                        _trace(
                            "intermediate_text_delta",
                            text_chars=len(event.text),
                            presentation=event.presentation,
                        )
                elif isinstance(event, AgentThinkingEvent):
                    _trace("thinking_delta", text_chars=len(event.text))
                elif isinstance(event, AgentToolUseStartEvent):
                    tool_call_count += 1
                    _trace(
                        "tool_use_start",
                        tool_use_id=event.tool_use_id,
                        tool_name=event.tool_name,
                        synthetic_from_text=event.synthetic_from_text,
                    )
                elif isinstance(event, AgentToolUseDeltaEvent):
                    _trace(
                        "tool_use_delta",
                        tool_use_id=event.tool_use_id,
                        json_fragment_chars=len(event.json_fragment),
                    )
                elif isinstance(event, AgentToolResultEvent):
                    _trace(
                        "tool_result",
                        tool_use_id=event.tool_use_id,
                        tool_name=event.tool_name,
                        is_error=event.is_error,
                        result_chars=len(event.result or ""),
                        execution_status=compact_provider_status(event.execution_status),
                        diagnostic=compact_tool_result_diagnostic(event.result),
                    )
                elif isinstance(event, AgentRunHeartbeatEvent):
                    _trace(
                        "run_heartbeat",
                        phase=event.phase,
                        message=event.message,
                        idle_ms=event.idle_ms,
                    )
                elif isinstance(event, AgentStateChangeEvent):
                    _trace(
                        "state_change",
                        from_state=str(event.from_state),
                        to_state=str(event.to_state),
                    )
                elif isinstance(event, AgentWarningEvent):
                    _trace("warning", code=event.code, message=event.message)
                elif isinstance(event, AgentDoneEvent):
                    done = event
                    has_snapshot, authoritative_text = done_text_snapshot(event)
                    if has_snapshot:
                        text_parts[:] = [authoritative_text]
                    _trace(
                        "done",
                        usage={
                            "input_tokens": event.input_tokens,
                            "output_tokens": event.output_tokens,
                            "reasoning_tokens": event.reasoning_tokens,
                            "cached_tokens": event.cached_tokens,
                            "cache_write_tokens": event.cache_write_tokens,
                            "billed_cost": event.billed_cost,
                            "cost_usd": event.cost_usd,
                            "cost_source": event.cost_source,
                            "model": event.model,
                            "iterations": event.iterations,
                        },
                    )
                elif isinstance(event, AgentErrorEvent):
                    error = event.message
                    _trace(
                        "error",
                        message=event.message,
                        code=event.code,
                        request_started=event.request_started,
                        physical_request_count=event.physical_request_count,
                    )
                else:
                    _trace("agent_event", event_type=type(event).__name__)

        consume_outcome = await _await_benchmark_consumer(
            _consume(),
            timeout=timeout,
            owner=provider,
        )
        if consume_outcome == "cleanup_timeout":
            error = (
                "agent_stream_close_timeout: agent turn did not close "
                "within the bounded cleanup window"
            )
            _trace(
                "cleanup_timeout",
                code="agent_stream_close_timeout",
                timeout_s=RUNNER_STREAM_CLEANUP_TIMEOUT_SECONDS,
            )
        elif consume_outcome == "timeout":
            error = f"TimeoutError: agent run timed out after {timeout:g}s"
            _trace("timeout", timeout_s=timeout)
    except Exception as exc:  # noqa: BLE001 - benchmark rows should keep going
        error = type(exc).__name__
        _trace("exception", error=error)

    provider_done = provider_done_from_agent_done(
        done,
        recorder=recorder,
        fallback_model=str(model_id or ""),
    )
    trace_events.append(
        {
            "seq": len(trace_events) + 1,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "kind": "turn_call_log_summary",
            "llm_response_records": len(llm_response_records(recorder.records)),
            "records": copy.deepcopy(recorder.records),
        }
    )
    setup = consume_provider_setup(provider)
    setup_latency_ms = coerce_metric_int(setup.get("latency_ms"))
    setup_usage = setup.get("usage") if isinstance(setup.get("usage"), list) else []
    routing_trace = setup.get("routing") if isinstance(setup.get("routing"), dict) else {}
    if setup:
        trace_events.insert(
            0,
            {
                "seq": 0,
                "elapsed_ms": setup_latency_ms,
                "kind": "routing_setup",
                "routing": routing_trace,
                "usage": setup_usage,
            },
        )
    return RunResult(
        final_text="".join(text_parts),
        done=provider_done,
        error=error,
        latency_ms=int((time.monotonic() - started) * 1000) + setup_latency_ms,
        ttft_ms=(ttft_ms + setup_latency_ms if ttft_ms is not None else None),
        tool_call_count=tool_call_count,
        trace_events=copy.deepcopy(trace_events),
        setup_latency_ms=setup_latency_ms,
        setup_usage=setup_usage,
        routing_trace=routing_trace,
    )


def server_tool_counts_from_provider_usage(provider_usage: Any) -> dict[str, int]:
    if not isinstance(provider_usage, dict):
        return {}
    raw_counts = provider_usage.get("server_tool_use")
    if not isinstance(raw_counts, dict):
        return {}
    counts: dict[str, int] = {}
    for key, value in raw_counts.items():
        count = coerce_metric_int(value)
        if count:
            counts[str(key)] = counts.get(str(key), 0) + count
    return counts


def add_metric_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + int(value)


def server_tool_counts_from_usage_payload(usage: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    breakdown = usage.get("model_usage_breakdown")
    if isinstance(breakdown, list):
        for row in breakdown:
            if isinstance(row, dict):
                add_metric_counts(
                    counts,
                    server_tool_counts_from_provider_usage(row.get("provider_usage")),
                )
        if counts:
            return counts
    return server_tool_counts_from_provider_usage(usage.get("provider_usage"))


def llm_request_count_for_run(
    *,
    spec: dict[str, str],
    done: DoneEvent | None,
    provider_attempted: bool,
) -> int:
    if not provider_attempted:
        return 0
    if done is not None:
        usage = done_payload(done)
        trace = done.ensemble_trace if isinstance(done.ensemble_trace, Mapping) else {}
        evidence: dict[str, Any] = {
            "usage": usage,
            "request_started": True,
        }
        if trace:
            evidence["ensemble_trace"] = trace
        return derive_physical_request_count(evidence, default_request_count=1)
    return 1


def done_payload(done: DoneEvent | None) -> dict[str, Any]:
    if done is None:
        return {}
    payload = {
        "provider": str(getattr(done, "provider", "") or ""),
        "model": done.model,
        "requested_provider": str(getattr(done, "requested_provider", "") or ""),
        "requested_model": str(getattr(done, "requested_model", "") or ""),
        "stop_reason": done.stop_reason,
        "input_tokens": done.input_tokens,
        "output_tokens": done.output_tokens,
        "reasoning_tokens": done.reasoning_tokens,
        "cached_tokens": done.cached_tokens,
        "cache_write_tokens": done.cache_write_tokens,
        "billed_cost": done.billed_cost,
        "cost_source": done.cost_source,
        "usage_missing_count": max(
            0,
            coerce_metric_int(getattr(done, "usage_missing_count", 0)),
        ),
        "provider_usage": getattr(done, "provider_usage", {}),
        "model_usage_breakdown": done.model_usage_breakdown,
        "reasoning_content_chars": len(done.reasoning_content or ""),
        "thinking_signature_present": bool(done.thinking_signature),
    }
    billing_receipt = getattr(done, "billing_receipt", None)
    physical_attempt_id = str(
        getattr(done, "physical_attempt_id", "") or ""
    )
    if physical_attempt_id:
        payload["physical_attempt_id"] = physical_attempt_id
    if billing_receipt is not None:
        payload["billing_receipt"] = billing_receipt
    payload["billed_cost"] = trusted_provider_billed_cost(payload)
    exact_cost = exact_provider_usage_cost(payload)
    if exact_cost is not None:
        payload["cost_source"] = "provider_billed"
    elif billing_receipt is not None:
        payload["cost_source"] = "unavailable"
    trace = done.ensemble_trace if isinstance(done.ensemble_trace, Mapping) else {}
    evidence_run: dict[str, Any] = {
        "usage": payload,
        "request_started": True,
    }
    if trace:
        evidence_run["ensemble_trace"] = trace
    identity_material = {
        "provider": payload["provider"],
        "model": payload["model"],
        "requested_provider": payload["requested_provider"],
        "requested_model": payload["requested_model"],
        "stop_reason": payload["stop_reason"],
        "provider_usage": payload["provider_usage"],
        "usage_missing_count": payload["usage_missing_count"],
    }
    identity_seed = (
        "done:"
        + hashlib.sha256(
            json.dumps(
                identity_material,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    payload = canonicalize_run_usage(
        evidence_run,
        identity_seed=identity_seed,
        requested_provider=payload["requested_provider"],
        requested_model=payload["requested_model"],
        default_request_count=1,
    )
    server_tool_use = server_tool_counts_from_usage_payload(payload)
    payload["server_tool_use"] = server_tool_use
    payload["server_tool_call_count"] = sum(server_tool_use.values())
    return payload


IGNORED_AGENT_DONE_POLICY_EVIDENCE_KEY = "ignored_agent_done_summary_policy_evidence"


def ignored_agent_done_summary_policy_evidence(
    summary: Mapping[str, Any],
    *,
    physical_rows: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Retain policy contradictions from a non-physical AgentDone roll-up.

    A roll-up with ``request_count > 1`` is never a physical request and must
    not contribute tokens, cost, or request cardinality.  It can still carry
    security-relevant receipt evidence, however.  Preserve only a compact
    contradiction record so a later non-BYOK audit cannot lose an explicit
    BYOK assertion or a stable-receipt identity conflict.
    """

    stable = build_stable_receipt_evidence(summary)
    conflict_fields = {
        str(value).strip() for value in stable.get("conflict_fields") or [] if str(value).strip()
    }
    summary_provider = str(summary.get("provider") or "").strip().casefold()
    summary_model = str(summary.get("model") or "").strip()
    physical_providers = {
        str(row.get("provider") or "").strip().casefold()
        for row in physical_rows
        if str(row.get("provider") or "").strip()
    }
    physical_models = {
        str(row.get("model") or "").strip()
        for row in physical_rows
        if str(row.get("model") or "").strip()
    }
    if summary_provider and physical_providers and summary_provider not in physical_providers:
        conflict_fields.add("provider")
    if (
        summary_model
        and physical_models
        and not any(
            _formal_openrouter_models_equivalent(summary_model, physical_model)
            for physical_model in physical_models
        )
    ):
        conflict_fields.add("model")

    summary_ids = usage_row_response_ids(summary)
    matching_rows = [row for row in physical_rows if summary_ids & usage_row_response_ids(row)]
    if matching_rows:
        overlap_evidence = build_stable_receipt_evidence(summary, *matching_rows)
        overlap_conflicts = {
            str(value).strip()
            for value in overlap_evidence.get("conflict_fields") or []
            if str(value).strip()
        }
        # Cost/token totals on a multi-response summary are aggregates rather
        # than per-request receipt fields.  They must not poison accounting.
        if len(summary_ids) > 1:
            overlap_conflicts &= {"provider", "model", "is_byok"}
        conflict_fields.update(overlap_conflicts)

    byok_values = {
        value
        for key in ("usage_is_byok_values", "router_is_byok_values")
        for value in stable.get(key) or []
        if value is True or value is False
    }
    explicit_byok = True in byok_values
    if not explicit_byok and not conflict_fields:
        return None
    classification = "conflict" if conflict_fields else "explicit_byok"
    response_id_fingerprint = canonical_json_sha256(sorted(summary_ids)) if summary_ids else ""
    return {
        "source": "ignored_agent_done_summary",
        "classification": classification,
        "request_count": max(
            0,
            coerce_metric_int(summary.get("request_count")),
        ),
        "response_id_set_sha256": response_id_fingerprint,
        "explicit_byok": explicit_byok,
        "conflict_fields": sorted(conflict_fields),
    }


def diagnostic_done_from_error_event(event: ErrorEvent) -> DoneEvent | None:
    """Preserve receipts from a failed composite request for spend accounting."""

    nested_done = event.diagnostic_done if isinstance(event.diagnostic_done, DoneEvent) else None
    rows = [dict(row) for row in event.model_usage_breakdown if isinstance(row, Mapping)]
    trace = dict(event.ensemble_trace) if isinstance(event.ensemble_trace, dict) else None
    missing_count = max(
        coerce_metric_int(event.usage_missing_count),
        coerce_metric_int(trace.get("usage_missing_count")) if trace else 0,
    )
    explicit_count = (
        max(0, event.physical_request_count)
        if isinstance(event.physical_request_count, int)
        and not isinstance(event.physical_request_count, bool)
        else None
    )
    explicit_zero = explicit_count == 0 or event.request_started is False
    if (
        nested_done is not None
        and not rows
        and trace is None
        and explicit_count in {None, 1}
        and missing_count <= 0
    ):
        return nested_done
    if nested_done is not None:
        nested_rows = [
            dict(row) for row in nested_done.model_usage_breakdown if isinstance(row, Mapping)
        ]
        if not nested_rows:
            nested_rows = [
                {
                    "role": "diagnostic_request",
                    "provider": str(nested_done.provider or ""),
                    "model": str(nested_done.model or ""),
                    "requested_provider": str(nested_done.requested_provider or ""),
                    "requested_model": str(nested_done.requested_model or ""),
                    "input_tokens": nested_done.input_tokens,
                    "output_tokens": nested_done.output_tokens,
                    "reasoning_tokens": nested_done.reasoning_tokens,
                    "cached_tokens": nested_done.cached_tokens,
                    "cache_write_tokens": nested_done.cache_write_tokens,
                    "billed_cost": nested_done.billed_cost,
                    "cost_source": nested_done.cost_source,
                    "provider_usage": dict(nested_done.provider_usage),
                    **(
                        {"billing_receipt": nested_done.billing_receipt}
                        if nested_done.billing_receipt is not None
                        else {}
                    ),
                }
            ]
        nested_trace = (
            nested_done.ensemble_trace if isinstance(nested_done.ensemble_trace, dict) else {}
        )
        nested_placeholder_count = sum(
            1 for row in nested_rows if usage_row_is_missing_placeholder(row)
        )
        nested_real_receipt_count = len(nested_rows) - nested_placeholder_count
        nested_physical_count = max(
            coerce_metric_int(nested_trace.get("physical_request_count")),
            coerce_metric_int(nested_trace.get("llm_request_count")),
            nested_real_receipt_count
            + max(
                nested_placeholder_count,
                coerce_metric_int(nested_done.usage_missing_count),
                coerce_metric_int(nested_trace.get("usage_missing_count")),
            ),
        )
        missing_count = max(
            missing_count,
            nested_placeholder_count,
            coerce_metric_int(nested_done.usage_missing_count),
            coerce_metric_int(nested_trace.get("usage_missing_count")),
            nested_physical_count - nested_real_receipt_count,
        )

        outer_row_count = len(rows)
        consumed_rows: set[int] = set()
        matched_outer_by_nested: dict[int, int] = {}
        nested_match_order = sorted(
            range(len(nested_rows)),
            key=lambda index: (
                0 if usage_row_response_ids(nested_rows[index]) else 1,
                index,
            ),
        )
        for nested_index in nested_match_order:
            nested_row = nested_rows[nested_index]
            candidates = [
                (priority, index)
                for index, row in enumerate(rows[:outer_row_count])
                if index not in consumed_rows
                if (priority := usage_row_match_priority(row, nested_row)) is not None
            ]
            matched_index = min(candidates)[1] if candidates else None
            if matched_index is not None:
                consumed_rows.add(matched_index)
                matched_outer_by_nested[nested_index] = matched_index
        for nested_index, nested_row in enumerate(nested_rows):
            matched_index = matched_outer_by_nested.get(nested_index)
            if matched_index is None:
                rows.append(nested_row)
            else:
                merge_usage_receipt_provenance(rows[matched_index], nested_row)

    placeholder_count = sum(
        1
        for row in rows
        if str(row.get("role") or "").strip().casefold() in MISSING_USAGE_PLACEHOLDER_ROLES
    )
    real_receipt_count = max(0, len(rows) - placeholder_count)
    trace_count = max(
        coerce_metric_int(trace.get("physical_request_count")) if trace else 0,
        coerce_metric_int(trace.get("llm_request_count")) if trace else 0,
    )
    if explicit_zero and real_receipt_count == 0:
        physical_count = 0
        missing_count = 0
        rows = []
    else:
        physical_count = max(
            trace_count,
            explicit_count or 0,
            real_receipt_count + max(missing_count, placeholder_count),
        )
        missing_count = max(missing_count, physical_count - real_receipt_count)
    if not rows and physical_count <= 0 and missing_count <= 0:
        return None
    if physical_count > 0:
        trace = dict(trace or {})
        trace.setdefault("mode", "diagnostic_error")
        trace["llm_request_count"] = physical_count
        trace["physical_request_count"] = physical_count
        trace["usage_missing_count"] = missing_count
    models = {
        str(row.get("model") or "").strip()
        for row in rows
        if not usage_row_is_missing_placeholder(row)
        if str(row.get("model") or "").strip()
    }
    providers = {
        str(row.get("provider") or "").strip()
        for row in rows
        if not usage_row_is_missing_placeholder(row)
        if str(row.get("provider") or "").strip()
    }
    requested_models = {
        str(row.get("requested_model") or "").strip()
        for row in rows
        if str(row.get("requested_model") or "").strip()
    }
    requested_providers = {
        str(row.get("requested_provider") or "").strip()
        for row in rows
        if str(row.get("requested_provider") or "").strip()
    }
    sources = {str(row.get("cost_source") or "none").strip().casefold() for row in rows}
    cost_source = next(iter(sources)) if len(sources) == 1 else "mixed" if sources else "none"
    return DoneEvent(
        stop_reason="error",
        input_tokens=sum(coerce_metric_int(row.get("input_tokens")) for row in rows),
        output_tokens=sum(coerce_metric_int(row.get("output_tokens")) for row in rows),
        reasoning_tokens=sum(coerce_metric_int(row.get("reasoning_tokens")) for row in rows),
        cached_tokens=sum(coerce_metric_int(row.get("cached_tokens")) for row in rows),
        cache_write_tokens=sum(coerce_metric_int(row.get("cache_write_tokens")) for row in rows),
        billed_cost=sum(trusted_provider_billed_cost(row) for row in rows),
        model=next(iter(models)) if len(models) == 1 else "",
        provider=next(iter(providers)) if len(providers) == 1 else "",
        requested_model=(next(iter(requested_models)) if len(requested_models) == 1 else ""),
        requested_provider=(
            next(iter(requested_providers)) if len(requested_providers) == 1 else ""
        ),
        cost_source=cost_source,
        model_usage_breakdown=rows,
        ensemble_trace=trace,
        usage_missing_count=missing_count,
        billing_receipt=(rows[0].get("billing_receipt") if len(rows) == 1 else None),
        provider_usage={
            "diagnostic_usage_only": True,
            "terminal_error_code": str(event.code or ""),
        },
    )


def run_result_usage_payload(result: RunResult) -> dict[str, Any]:
    payload = done_payload(result.done)
    if not result.setup_usage:
        return payload
    merged = dict(payload)
    merged.setdefault("model", getattr(result.done, "model", "") if result.done else "")
    merged.setdefault(
        "requested_provider",
        getattr(result.done, "requested_provider", "") if result.done else "",
    )
    merged.setdefault(
        "requested_model",
        getattr(result.done, "requested_model", "") if result.done else "",
    )
    merged.setdefault("stop_reason", getattr(result.done, "stop_reason", "") if result.done else "")
    merged.setdefault("provider_usage", {})
    merged.setdefault("reasoning_content_chars", 0)
    merged.setdefault("thinking_signature_present", False)
    for key in (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_write_tokens",
    ):
        merged[key] = coerce_metric_int(merged.get(key)) + sum(
            coerce_metric_int(row.get(key)) for row in result.setup_usage
        )
    merged["billed_cost"] = float(merged.get("billed_cost") or 0.0) + sum(
        trusted_provider_billed_cost(row) for row in result.setup_usage
    )
    existing_breakdown = merged.get("model_usage_breakdown")
    merged["model_usage_breakdown"] = [
        *result.setup_usage,
        *(existing_breakdown if isinstance(existing_breakdown, list) else []),
    ]
    cost_sources = {str(row.get("cost_source") or "none") for row in result.setup_usage}
    if merged.get("cost_source"):
        cost_sources.add(str(merged["cost_source"]))
    cost_sources.discard("none")
    merged["cost_source"] = (
        next(iter(cost_sources)) if len(cost_sources) == 1 else "mixed" if cost_sources else "none"
    )
    server_tool_use = server_tool_counts_from_usage_payload(merged)
    merged["server_tool_use"] = server_tool_use
    merged["server_tool_call_count"] = sum(server_tool_use.values())
    return merged


def candidate_texts(
    done: DoneEvent | None,
    *,
    final_agent_call_only: bool = False,
) -> list[str]:
    if done is None:
        return []
    trace = done.ensemble_trace or {}
    candidates: list[Any] = []
    if isinstance(trace, dict):
        direct_candidates = trace.get("candidates")
        if isinstance(direct_candidates, list):
            candidates.extend(direct_candidates)
        calls = trace.get("calls")
        if isinstance(calls, list):
            selected_calls = calls[-1:] if final_agent_call_only else calls
            for call in selected_calls:
                if not isinstance(call, dict):
                    continue
                call_candidates = call.get("candidates")
                if isinstance(call_candidates, list):
                    candidates.extend(call_candidates)
    if not isinstance(candidates, list):
        return []
    return [
        str(candidate.get("text") or "")
        for candidate in candidates
        if isinstance(candidate, dict) and str(candidate.get("text") or "").strip()
    ]


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_NO_PHYSICAL_REQUEST_GATE_CODES = frozenset(
    {
        "agent_cleanup_in_progress",
        "agent_turn_in_progress",
        "benchmark_owner_cleanup_in_progress",
        "ensemble_cleanup_in_progress",
        "ensemble_call_in_progress",
    }
)


def run_result_was_blocked_before_request(result: RunResult) -> bool:
    """Return true only for ownership gates that start no LLM request."""

    return any(
        str(event.get("code") or "").strip() in _NO_PHYSICAL_REQUEST_GATE_CODES
        for event in result.trace_events
        if isinstance(event, dict)
    )


def run_result_error_physical_request_count(result: RunResult) -> int | None:
    """Return explicit adapter/Agent failure evidence, including zero."""

    for event in reversed(result.trace_events):
        if not isinstance(event, dict) or str(event.get("kind") or "") != "error":
            continue
        value = event.get("physical_request_count")
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
        if event.get("request_started") is False:
            return 0
    return None


def run_result_summary(
    result: RunResult,
    *,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    missing_usage_role: str | None = None,
    include_ensemble_trace: bool = False,
) -> dict[str, Any]:
    usage = run_result_usage_payload(result)
    server_tool_call_count = coerce_metric_int(usage.get("server_tool_call_count"))
    total_tool_call_count = result.tool_call_count + server_tool_call_count
    llm_request_count = 0
    usage_unknown_count = 0
    if result.done is not None:
        done_usage = done_payload(result.done)
        trace = (
            result.done.ensemble_trace if isinstance(result.done.ensemble_trace, Mapping) else {}
        )
        done_evidence: dict[str, Any] = {
            "usage": done_usage,
            "request_started": True,
        }
        if trace:
            done_evidence["ensemble_trace"] = trace
        llm_request_count = derive_physical_request_count(
            done_evidence,
            default_request_count=1,
        )
        usage_unknown_count = ensemble_usage_unknown_count(trace)
        usage_unknown_count = max(
            usage_unknown_count,
            coerce_metric_int(result.done.usage_missing_count),
            usage_unknown_count_from_usage_payload(done_usage),
        )
    elif result.error or result.final_text:
        explicit_request_count = run_result_error_physical_request_count(result)
        if explicit_request_count is not None:
            llm_request_count = explicit_request_count
            usage_unknown_count = max(
                usage_unknown_count,
                explicit_request_count,
            )
        elif not run_result_was_blocked_before_request(result):
            llm_request_count = 1
            usage_unknown_count = max(usage_unknown_count, 1)
    llm_request_count += usage_rows_request_count(result.setup_usage)
    if result.done is None and result.setup_usage:
        usage_unknown_count += usage_unknown_count_from_usage_payload(
            {"model_usage_breakdown": result.setup_usage}
        )
    if llm_request_count:
        identity_seed = (
            "run-result:"
            + hashlib.sha256(
                json.dumps(
                    {
                        "trace_events": result.trace_events,
                        "latency_ms": result.latency_ms,
                        "final_text_sha256": text_sha256(result.final_text),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        usage = canonicalize_run_usage(
            {
                "usage": usage,
                "physical_request_count": llm_request_count,
                "request_started": True,
            },
            identity_seed=identity_seed,
            requested_provider=(
                str(usage.get("requested_provider") or "")
                if requested_provider is None
                else requested_provider
            ),
            requested_model=(
                str(usage.get("requested_model") or "")
                if requested_model is None
                else requested_model
            ),
            role=missing_usage_role,
        )
        usage_unknown_count = max(
            usage_unknown_count,
            usage_unknown_count_from_usage_payload(usage),
        )
    summary = {
        "latency_ms": result.latency_ms,
        "ttft_ms": result.ttft_ms,
        "tool_call_count": result.tool_call_count,
        "stream_tool_call_count": result.tool_call_count,
        "server_tool_call_count": server_tool_call_count,
        "server_tool_use": usage.get("server_tool_use") or {},
        "total_tool_call_count": total_tool_call_count,
        "trajectory_steps": total_tool_call_count + llm_request_count,
        "llm_request_count": llm_request_count,
        "usage_unknown_count": usage_unknown_count,
        "error": result.error,
        "audit_warnings": list(result.audit_warnings),
        "final_text_chars": len(result.final_text),
        "final_text_sha256": text_sha256(result.final_text),
        "usage": usage,
        # Preserve the paid setup rows as an immutable mirror of their
        # aggregate usage entries for resume/finalizer reconciliation.
        "setup_usage": copy.deepcopy(result.setup_usage),
        "trace_events": result.trace_events,
        "setup_latency_ms": result.setup_latency_ms,
        "routing_trace": result.routing_trace,
    }
    selection_plan = (
        result.routing_trace.get("selection_plan")
        if isinstance(result.routing_trace, Mapping)
        else None
    )
    if (
        include_ensemble_trace
        or isinstance(selection_plan, Mapping)
        and selection_plan.get("ranking_thinking_assignment_enabled") is True
    ):
        summary["ensemble_trace"] = json_safe(
            result.done.ensemble_trace
            if result.done is not None
            and isinstance(result.done.ensemble_trace, Mapping)
            else {}
        )
    return summary


def judge_run_result_summary(
    result: RunResult,
    *,
    judge_provider: Any,
) -> dict[str, Any]:
    """Bind missing Judge usage to the frozen requested route at creation."""

    return run_result_summary(
        result,
        requested_provider=str(
            getattr(judge_provider, "provider_id", "") or ""
        ),
        requested_model=str(getattr(judge_provider, "model", "") or ""),
        missing_usage_role="unknown_request",
    )


def generation_postprocessing_failure_reason(
    stage: str,
    exc: Exception,
) -> str:
    """Return a non-sensitive terminal reason for paid-call postprocessing."""

    return (
        "generation_postprocessing_failed:"
        + str(stage or "unknown")
        + ":"
        + type(exc).__name__
    )


def primitive_unknown_usage_payload(
    *,
    physical_count: int,
    identity_seed: str,
    requested_provider: str,
    requested_model: str,
    role: str = "usage_missing",
) -> dict[str, Any]:
    """Build strict unknown units without depending on canonicalizers."""

    count = (
        physical_count
        if isinstance(physical_count, int)
        and not isinstance(physical_count, bool)
        and physical_count > 0
        else 0
    )
    units: list[dict[str, Any]] = []
    for ordinal in range(1, count + 1):
        canonical = json.dumps(
            {
                "identity_seed": str(identity_seed),
                "ordinal": ordinal,
                "role": role,
                "schema": USAGE_EVIDENCE_SCHEMA,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        evidence_id = (
            "sha256:"
            + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        )
        units.append(
            {
                "usage_evidence_schema": USAGE_EVIDENCE_SCHEMA,
                "usage_evidence_id": evidence_id,
                "usage_evidence_source": (
                    "emergency_physical_request_counter"
                ),
                "role": role,
                "physical_request_ordinal": ordinal,
                "provider": "",
                "model": "",
                "requested_provider": str(
                    requested_provider or ""
                ).strip(),
                "requested_model": str(
                    requested_model or ""
                ).strip(),
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "billed_cost": 0.0,
                "cost_source": "none",
                "usage_unknown": True,
                "provider_usage": {
                    "usage_unknown": True,
                    "usage_evidence_schema": USAGE_EVIDENCE_SCHEMA,
                    "usage_evidence_id": evidence_id,
                },
            }
        )
    return {
        "usage_evidence_schema": USAGE_EVIDENCE_SCHEMA,
        "model_usage_breakdown": units,
        "usage_missing_count": count,
    }


def emergency_generation_run_summary(
    result: RunResult,
    *,
    reason: str,
    stage: str,
    exception_type: str,
    identity_seed: str,
    expected_provider: str = "",
    expected_model: str = "",
) -> dict[str, Any]:
    """Persist conservative physical evidence when normal summarization fails."""

    source_error_present = bool(result.error)
    primitive_usage_fallback = False
    try:
        summary = run_result_summary(
            result,
            requested_provider=expected_provider or None,
            requested_model=expected_model or None,
        )
        summary_error_type = ""
        unvalidated_usage: Any = None
    except Exception as summary_exc:  # noqa: BLE001 - this is the last evidence boundary
        summary_error_type = type(summary_exc).__name__
        try:
            raw_usage = run_result_usage_payload(result)
        except Exception:  # noqa: BLE001 - retain a conservative unknown unit
            raw_usage = {}
        try:
            unvalidated_usage = json_safe(raw_usage)
        except Exception:  # noqa: BLE001 - never persist an unsafe object
            unvalidated_usage = {
                "capture_failed": True,
                "exception_type": summary_error_type,
            }
        if not isinstance(unvalidated_usage, Mapping):
            unvalidated_usage = {}
        breakdown = unvalidated_usage.get("model_usage_breakdown")
        represented_units = (
            len([unit for unit in breakdown if isinstance(unit, Mapping)])
            if isinstance(breakdown, list)
            else 0
        )
        explicit_count = run_result_error_physical_request_count(result)
        if explicit_count is not None:
            primary_count = max(0, explicit_count)
        elif result.done is not None:
            try:
                done_usage = done_payload(result.done)
                done_evidence: dict[str, Any] = {
                    "usage": done_usage,
                    "request_started": True,
                }
                if isinstance(result.done.ensemble_trace, Mapping):
                    done_evidence["ensemble_trace"] = (
                        result.done.ensemble_trace
                    )
                primary_count = derive_physical_request_count(
                    done_evidence,
                    default_request_count=1,
                )
            except Exception:  # noqa: BLE001 - one completed call is conservative
                primary_count = 1
        elif (
            bool(result.error or result.final_text)
            and not run_result_was_blocked_before_request(result)
        ):
            primary_count = 1
        else:
            primary_count = 0
        try:
            setup_count = usage_rows_request_count(result.setup_usage)
        except Exception:  # noqa: BLE001 - one row is at least one request
            setup_count = len(result.setup_usage)
        physical_count = max(
            represented_units,
            primary_count + setup_count,
        )
        try:
            canonical_usage = canonicalize_run_usage(
                {
                    "usage": dict(unvalidated_usage),
                    "physical_request_count": physical_count,
                    "request_started": physical_count > 0,
                },
                identity_seed=identity_seed,
                requested_provider=expected_provider,
                requested_model=expected_model,
            )
        except Exception:  # noqa: BLE001 - retry only with safe primitives
            try:
                canonical_usage = canonicalize_run_usage(
                    {
                        "usage": {},
                        "physical_request_count": physical_count,
                        "request_started": physical_count > 0,
                    },
                    identity_seed=identity_seed + ":unknown",
                    requested_provider=expected_provider,
                    requested_model=expected_model,
                )
            except Exception:  # noqa: BLE001 - canonicalizer itself is unavailable
                primitive_usage_fallback = True
                canonical_usage = primitive_unknown_usage_payload(
                    physical_count=physical_count,
                    identity_seed=identity_seed + ":primitive-unknown",
                    requested_provider=expected_provider,
                    requested_model=expected_model,
                )
        unknown_count = (
            physical_count
            if primitive_usage_fallback
            else usage_unknown_count_from_usage_payload(
                canonical_usage
            )
        )
        try:
            server_tool_use = server_tool_counts_from_usage_payload(
                canonical_usage
            )
        except Exception:  # noqa: BLE001 - tools are secondary evidence
            server_tool_use = {}
        server_tool_call_count = sum(server_tool_use.values())
        total_tool_call_count = (
            coerce_metric_int(result.tool_call_count)
            + server_tool_call_count
        )
        try:
            safe_trace_events = json_safe(result.trace_events)
        except Exception as trace_exc:  # noqa: BLE001 - primitive evidence only
            safe_trace_events = [
                {
                    "kind": "error",
                    "code": "trace_evidence_capture_failed",
                    "exception_type": type(trace_exc).__name__,
                }
            ]
        try:
            safe_routing_trace = safe_provider_build_routing_trace(
                result.routing_trace
            )
        except Exception as routing_exc:  # noqa: BLE001 - primitive evidence only
            safe_routing_trace = {
                "capture_failed": True,
                "exception_type": type(routing_exc).__name__,
            }
        summary = {
            "latency_ms": coerce_metric_int(result.latency_ms),
            "ttft_ms": result.ttft_ms,
            "tool_call_count": coerce_metric_int(result.tool_call_count),
            "stream_tool_call_count": coerce_metric_int(
                result.tool_call_count
            ),
            "server_tool_call_count": server_tool_call_count,
            "server_tool_use": server_tool_use,
            "total_tool_call_count": total_tool_call_count,
            "trajectory_steps": total_tool_call_count + physical_count,
            "llm_request_count": physical_count,
            "usage_unknown_count": unknown_count,
            "error": reason,
            "final_text_chars": len(result.final_text),
            "final_text_sha256": text_sha256(result.final_text),
            "usage": canonical_usage,
            "trace_events": safe_trace_events,
            "setup_latency_ms": coerce_metric_int(
                result.setup_latency_ms
            ),
            "routing_trace": safe_routing_trace,
        }
    summary["error"] = reason
    summary["generation_postprocessing_failure"] = {
        "stage": stage,
        "exception_type": exception_type,
        "summary_exception_type": summary_error_type,
        "source_error_present": source_error_present,
        "evidence_precision": (
            "unvalidated_raw_plus_primitive_unknown"
            if summary_error_type and primitive_usage_fallback
            else "unvalidated_raw_plus_conservative_unknown"
            if summary_error_type
            else "canonical"
        ),
    }
    if unvalidated_usage is not None:
        summary["unvalidated_usage_evidence"] = unvalidated_usage
    return summary


def recover_paid_generation_postprocessing_failure(
    pending: Mapping[str, Any],
    exc: Exception,
) -> tuple[RunResult, list[dict[str, Any]], int, str]:
    """Commit one terminal attempt after a paid-call postprocessing exception."""

    result = pending.get("result")
    if not isinstance(result, RunResult):
        raise exc
    stage = str(pending.get("stage") or "unknown")
    reason = generation_postprocessing_failure_reason(stage, exc)
    attempt_id = str(pending.get("attempt_id") or "")
    attempt_index = coerce_metric_int(pending.get("attempt_index"))
    attempts_value = pending.get("attempts")
    attempts = (
        attempts_value
        if isinstance(attempts_value, list)
        else []
    )
    result.trace_events = [
        *(
            list(result.trace_events)
            if isinstance(result.trace_events, list)
            else []
        ),
        {
            "kind": "error",
            "code": "generation_postprocessing_failed",
            "stage": stage,
            "exception_type": type(exc).__name__,
        },
    ]
    run_summary = emergency_generation_run_summary(
        result,
        reason=reason,
        stage=stage,
        exception_type=type(exc).__name__,
        identity_seed=f"generation-attempt:{attempt_id or 'unknown'}",
        expected_provider=str(pending.get("expected_provider") or ""),
        expected_model=str(pending.get("expected_model") or ""),
    )
    result.error = reason
    existing = next(
        (
            attempt
            for attempt in reversed(attempts)
            if isinstance(attempt, dict)
            and str(attempt.get("attempt_id") or "") == attempt_id
        ),
        None,
    )
    if existing is None:
        existing = {
            "attempt_id": attempt_id,
            "attempt_kind": "generation",
            "attempt": attempt_index,
            "started_at": pending.get("attempt_started_at"),
            "completed_at": time.time(),
        }
        provider_native_g1_recovery = (
            pending.get("provider_native_g1_recovery") is True
        )
        if pending.get("adaptive_g1") is True or provider_native_g1_recovery:
            try:
                safe_plan = json_safe(
                    copy.deepcopy(
                        dict(pending.get("selection_plan") or {})
                    )
                )
            except Exception as plan_exc:  # noqa: BLE001 - evidence stays terminal
                safe_plan = {
                    "capture_failed": True,
                    "exception_type": type(plan_exc).__name__,
                }
            existing.update(
                {
                    "selection_plan": safe_plan,
                    "deterministic_proposer_failures": [],
                    "excluded_proposer_identities": sorted(
                        str(value)
                        for value in (
                            pending.get(
                                "excluded_proposer_identities"
                            )
                            or []
                        )
                    ),
                }
            )
            if provider_native_g1_recovery:
                existing["proposer_recovery_owner"] = "provider"
        attempts.append(existing)
    existing.update(
        {
            "completed_at": time.time(),
            "retryable": False,
            "retry_reason": reason,
            "retry_suppressed_reason": reason,
            "will_retry": False,
            "retry_backoff_s": 0.0,
            "run": run_summary,
            "generation_postprocessing_failure": {
                "stage": stage,
                "exception_type": type(exc).__name__,
            },
        }
    )
    return result, attempts, 0, reason


def usage_rows_request_count(rows: list[dict[str, Any]]) -> int:
    """Count physical requests represented by aggregate setup-usage rows."""

    total = 0
    for row in rows:
        provider_usage = row.get("provider_usage")
        response_ids = (
            provider_usage.get("response_ids") if isinstance(provider_usage, Mapping) else None
        )
        total += max(
            1,
            coerce_metric_int(row.get("request_count")),
            len(response_ids) if isinstance(response_ids, list) else 0,
        )
    return total


def bounded_generation_attempts(value: int | None) -> int:
    try:
        attempts = GENERATION_MAX_ATTEMPTS if value is None else int(value)
    except (TypeError, ValueError):
        attempts = GENERATION_MAX_ATTEMPTS
    return max(1, min(GENERATION_MAX_ATTEMPTS, attempts))


def bounded_generation_retry_backoff(value: Any) -> float:
    try:
        backoff = float(value)
    except (TypeError, ValueError):
        backoff = DEFAULT_GENERATION_RETRY_BACKOFF_SECONDS
    return max(0.0, backoff)


_ENSEMBLE_METADATA_ONLY_REASONS = frozenset(
    {
        "missing_proposer_usage_metadata",
        "missing_proposer_stop_reason",
        "missing_aggregator_usage_metadata",
        "missing_aggregator_stop_reason",
        "missing_actual_proposer_identity",
        "missing_requested_proposer_identity",
        "missing_actual_aggregator_model",
        "missing_actual_aggregator_provider",
        "missing_requested_aggregator_identity",
    }
)
_ENSEMBLE_METADATA_REPAIR_STATUSES = frozenset({"backfilled", "unavailable"})

# Once a provider returned a Done event and visible answer text, post-call
# validation is primarily evidence/audit work.  Missing quorum evidence,
# answer-content binding failures, and an invalid terminal-call shape may still
# invalidate execution.  Identity/usage metadata defects remain recorded but
# must not erase an otherwise usable answer or prevent Judge from running.
_ENSEMBLE_EXECUTION_BLOCKING_REASONS = frozenset(
    {
        "aggregator_fallback_used_or_unknown",
        "incomplete_agent_call_output_binding",
        "intermediate_fallback_visible_output",
        "invalid_agent_call_output_binding",
        "invalid_intermediate_fallback_flag",
        "invalid_intermediate_fallback_outcome",
        "invalid_intermediate_fallback_quorum",
        "invalid_intermediate_fallback_request",
        "invalid_intermediate_fallback_role",
        "missing_ensemble_trace",
        "missing_ensemble_call_trace",
        "missing_actual_proposer_candidates",
        "missing_agent_call_output_binding",
        "missing_aggregator_output_binding",
        "insufficient_actual_proposer_quorum",
        "insufficient_selected_proposer_quorum",
        "wrong_agent_call_output_binding",
        "wrong_aggregator_output_binding",
        "wrong_aggregator_output_length",
        "wrong_intermediate_fallback_execution_role",
        "wrong_intermediate_fallback_model",
        "wrong_intermediate_fallback_provider",
    }
)


def ensemble_execution_blocking_reason(reason: str) -> bool:
    """Return whether one post-call reason proves execution unusable."""

    return reason in _ENSEMBLE_EXECUTION_BLOCKING_REASONS


def record_generation_audit_warnings(
    result: RunResult,
    reasons: Sequence[str],
) -> None:
    """Retain post-call evidence defects without turning them into errors."""

    result.audit_warnings = list(
        dict.fromkeys(
            [
                *result.audit_warnings,
                *(str(reason).strip() for reason in reasons if str(reason).strip()),
            ]
        )
    )


def ensemble_metadata_only_reason(reason: str) -> bool:
    """Return true only for explicitly enumerated, non-generation metadata gaps."""

    return reason in _ENSEMBLE_METADATA_ONLY_REASONS


def ensemble_metadata_field_resolved(
    record: Mapping[str, Any],
    field: str,
) -> bool:
    """Accept an absent trace field only after an explicit provenance audit."""

    repair = record.get("metadata_repair")
    item = repair.get(field) if isinstance(repair, Mapping) else None
    return bool(
        isinstance(item, Mapping)
        and str(item.get("status") or "") in _ENSEMBLE_METADATA_REPAIR_STATUSES
        and str(item.get("source") or "").strip()
    )


def g1_registry_contract_reasons(
    trace: Mapping[str, Any],
    contract: Mapping[str, Any] | None,
    task_analyzer_execution_contract: Mapping[str, Any] | None = None,
) -> list[str]:
    """Fail closed when a G1 call drifts from its frozen registry allowlist."""

    if not isinstance(contract, Mapping):
        return []
    reasons: list[str] = []
    profile_id = str(contract.get("profile_id") or "").strip()
    selection_mode = str(contract.get("selection_mode") or "").strip()
    candidate_scope = str(contract.get("candidate_scope") or "").strip()
    if not candidate_scope:
        candidate_scope = "exact_routes"
    expected_policy = (
        "all_registry_models" if candidate_scope == "registry_all" else "exact_openrouter_routes"
    )
    declared_policy = str(contract.get("policy") or "").strip()
    source_version = str(contract.get("source_registry_snapshot_version") or "").strip()
    expected_hash = str(contract.get("expected_routes_sha256") or "").strip()
    expected_source_registry_hash = str(
        contract.get("expected_source_registry_snapshot_sha256") or ""
    ).strip()
    expected_ranking_schema = str(
        contract.get("expected_ranking_config_schema_version") or ""
    ).strip()
    expected_ranking_version = str(contract.get("expected_ranking_config_version") or "").strip()
    expected_ranking_hash = str(contract.get("expected_ranking_config_sha256") or "").strip()
    expected_proposer_max = coerce_metric_int(contract.get("expected_proposer_count_max"))
    expected_count = coerce_metric_int(contract.get("expected_candidate_count"))
    expected_routes = contract.get("expected_routes")
    if (
        not profile_id
        or selection_mode != "router_dynamic"
        or candidate_scope not in {"registry_all", "exact_routes"}
        or (
            declared_policy != expected_policy
            if "candidate_scope" in contract
            else declared_policy not in {"", expected_policy}
        )
        or contract.get("user_profile_enabled") is not False
        or not source_version
        or len(expected_hash) != 64
        or len(expected_source_registry_hash) != 64
        or not expected_ranking_schema
        or not expected_ranking_version
        or len(expected_ranking_hash) != 64
        or expected_proposer_max <= 0
        or expected_proposer_max > expected_count
        or expected_count <= 0
        or not isinstance(expected_routes, Mapping)
        or len(expected_routes) != expected_count
    ):
        return ["invalid_g1_registry_contract"]
    expected_identities = {f"openrouter:{str(model).strip().lower()}" for model in expected_routes}
    expected_filtered_version = f"{source_version}+{profile_id}+{expected_hash[:12]}"
    executed_plan = trace.get("selection_plan")
    if not isinstance(executed_plan, Mapping):
        return ["missing_g1_selection_plan"]
    analyzer_trace = executed_plan.get("task_analyzer")
    if (
        isinstance(analyzer_trace, Mapping)
        and analyzer_trace.get("source") != "frozen_replay"
        and (
            isinstance(
                executed_plan.get("task_analyzer_execution_contract"),
                Mapping,
            )
            or isinstance(analyzer_trace.get("chain"), Mapping)
        )
    ):
        from opensquilla.eval.draco_task_analyzer_execution import (
            task_analyzer_execution_contract_from_g1_registry,
            validate_task_analyzer_execution_trace,
            validated_task_analyzer_execution_contract,
        )

        expected_execution = validated_task_analyzer_execution_contract(
            task_analyzer_execution_contract
        )
        if expected_execution is None:
            try:
                expected_execution = validated_task_analyzer_execution_contract(
                    task_analyzer_execution_contract_from_g1_registry(contract)
                )
            except (TypeError, ValueError):
                expected_execution = None
        declared_execution = executed_plan.get(
            "task_analyzer_execution_contract"
        )
        chain_trace = analyzer_trace.get("chain")
        new_trace = bool(
            isinstance(chain_trace, Mapping)
            and (
                "schema_repair_max_retries" in chain_trace
                or "deadline" in chain_trace
            )
        )
        if expected_execution is None or (
            new_trace and declared_execution != expected_execution
        ):
            reasons.append("invalid_g1_task_analyzer_execution_contract")
        else:
            _, execution_reasons = validate_task_analyzer_execution_trace(
                execution_contract=expected_execution,
                analyzer_trace=analyzer_trace,
                models_equivalent=_formal_openrouter_models_equivalent,
            )
            if execution_reasons:
                reasons.append("invalid_g1_task_analyzer_execution_trace")
    if executed_plan.get("analyzer_failure_fallback") is True:
        fallback = contract.get("analyzer_failure_fallback_ensemble")
        proposer_routes = (
            fallback.get("proposers")
            if isinstance(fallback, Mapping)
            else None
        )
        aggregator_route = (
            fallback.get("aggregator")
            if isinstance(fallback, Mapping)
            else None
        )
        if (
            not isinstance(proposer_routes, list)
            or len(proposer_routes) != 4
            or not isinstance(aggregator_route, Mapping)
            or fallback.get("min_successful_proposers") != 1
            or fallback.get("complete_proposers_only") is not True
            or fallback.get("aggregator_max_recovery_actions") != 1
        ):
            return ["invalid_g1_analyzer_failure_fallback_contract"]
        expected_p = [
            f"{str(route.get('provider') or '')}:"
            f"{str(route.get('model') or '')}"
            for route in proposer_routes
            if isinstance(route, Mapping)
        ]
        expected_a = (
            f"{str(aggregator_route.get('provider') or '')}:"
            f"{str(aggregator_route.get('model') or '')}"
        )
        if len(expected_p) != 4 or len(set(expected_p)) != 4:
            return ["invalid_g1_analyzer_failure_fallback_contract"]
        if executed_plan.get("user_profile_enabled") is not False:
            reasons.append("wrong_g1_user_profile_enabled")
        if executed_plan.get("analyzer_failure_fallback_schema") != (
            "opensquilla.router-dynamic-analyzer-failure-fallback/v1"
        ):
            reasons.append("wrong_g1_analyzer_failure_fallback_schema")
        if executed_plan.get("selected_P") != expected_p:
            reasons.append("wrong_g1_selected_proposers")
        if executed_plan.get("selected_A") != expected_a:
            reasons.append("wrong_g1_selected_aggregator")
        if (
            executed_plan.get("complete_proposers_only") is not True
            or coerce_metric_int(
                executed_plan.get("effective_min_successful_proposers")
            )
            != 1
            or coerce_metric_int(executed_plan.get("N_min")) != 1
            or coerce_metric_int(executed_plan.get("N_max")) != 4
            or executed_plan.get("aggregator_max_recovery_actions") != 1
        ):
            reasons.append("wrong_g1_analyzer_failure_fallback_quorum")
        analyzer_trace = executed_plan.get("task_analyzer")
        analyzer_chain_trace = (
            analyzer_trace.get("chain")
            if isinstance(analyzer_trace, Mapping)
            else None
        )
        if (
            not isinstance(analyzer_trace, Mapping)
            or analyzer_trace.get("schema_valid") is not False
            or not isinstance(analyzer_chain_trace, Mapping)
            or analyzer_chain_trace.get("exhausted") is not True
        ):
            reasons.append("wrong_g1_analyzer_failure_fallback_activation")
        ranking_parameters = executed_plan.get("ranking_parameters")
        if (
            not isinstance(ranking_parameters, Mapping)
            or canonical_json_sha256(ranking_parameters).removeprefix("sha256:")
            != expected_ranking_hash
            or executed_plan.get("ranking_config_schema_version")
            != expected_ranking_schema
            or executed_plan.get("ranking_config_version")
            != expected_ranking_version
            or executed_plan.get("ranking_config_hash")
            != expected_ranking_hash
        ):
            reasons.append("wrong_g1_ranking_config_trace")
        return list(dict.fromkeys(reasons))
    if executed_plan.get("user_profile_enabled") is not False:
        reasons.append("wrong_g1_user_profile_enabled")
    ranking_parameters = executed_plan.get("ranking_parameters")
    ranking_parameters_valid = isinstance(ranking_parameters, Mapping)
    if not ranking_parameters_valid:
        reasons.append("missing_g1_ranking_parameters")
    else:
        try:
            actual_ranking_hash = canonical_json_sha256(ranking_parameters).removeprefix("sha256:")
        except (TypeError, ValueError):
            actual_ranking_hash = ""
        if actual_ranking_hash != expected_ranking_hash:
            reasons.append("wrong_g1_ranking_config_hash")
        if (
            str(ranking_parameters.get("schema_version") or "") != expected_ranking_schema
            or str(ranking_parameters.get("config_version") or "") != expected_ranking_version
        ):
            reasons.append("wrong_g1_ranking_config_identity")
    if (
        str(executed_plan.get("ranking_config_schema_version") or "") != expected_ranking_schema
        or str(executed_plan.get("ranking_config_version") or "") != expected_ranking_version
        or str(executed_plan.get("ranking_config_hash") or "") != expected_ranking_hash
    ):
        reasons.append("wrong_g1_ranking_config_trace")
    allowlist = executed_plan.get("candidate_allowlist")
    if not isinstance(allowlist, Mapping):
        reasons.append("missing_g1_candidate_allowlist")
    else:
        expected_fields = {
            "policy": expected_policy,
            "profile_id": profile_id,
            "source_registry_snapshot_version": source_version,
            "filtered_registry_snapshot_version": expected_filtered_version,
            "expected_routes_sha256": expected_hash,
            "expected_source_registry_snapshot_sha256": (expected_source_registry_hash),
            "expected_candidate_count": expected_count,
            "candidate_count": expected_count,
        }
        if "candidate_scope" in contract:
            expected_fields["candidate_scope"] = candidate_scope
        for field, expected_value in expected_fields.items():
            if allowlist.get(field) != expected_value:
                reasons.append(f"wrong_g1_candidate_allowlist_{field}")
        traced_identities = allowlist.get("expected_identities")
        if (
            not isinstance(traced_identities, list)
            or set(traced_identities) != expected_identities
            or len(traced_identities) != expected_count
        ):
            reasons.append("wrong_g1_candidate_allowlist_identities")
    if coerce_metric_int(executed_plan.get("candidate_pool_size")) != expected_count:
        reasons.append("wrong_g1_candidate_pool_size")
    if executed_plan.get("registry_snapshot_version") != expected_filtered_version:
        reasons.append("wrong_g1_registry_snapshot_version")
    registry_hash = str(executed_plan.get("registry_snapshot_hash") or "")
    if len(registry_hash) != 64 or any(char not in "0123456789abcdef" for char in registry_hash):
        reasons.append("invalid_g1_registry_snapshot_hash")
    candidate_pool = executed_plan.get("candidate_pool")
    candidate_pool_identities = (
        [str(item.get("identity") or "") for item in candidate_pool if isinstance(item, Mapping)]
        if isinstance(candidate_pool, list)
        else []
    )
    if (
        not isinstance(candidate_pool, list)
        or len(candidate_pool) != expected_count
        or len(candidate_pool_identities) != expected_count
        or len(set(candidate_pool_identities)) != expected_count
        or set(candidate_pool_identities) != expected_identities
    ):
        reasons.append("wrong_g1_candidate_pool")
    selected_p = executed_plan.get("selected_P")
    if (
        not isinstance(selected_p, list)
        or not selected_p
        or any(not isinstance(identity, str) for identity in selected_p)
        or len(set(str(identity) for identity in selected_p)) != len(selected_p)
        or any(str(identity) not in expected_identities for identity in selected_p)
    ):
        reasons.append("wrong_g1_selected_proposers")
    selected_a = executed_plan.get("selected_A")
    if not isinstance(selected_a, str) or selected_a not in expected_identities:
        reasons.append("wrong_g1_selected_aggregator")
    task_profile = executed_plan.get("task_profile")
    derived_min = derived_max = 0
    derived_bound_reasons: list[str] = []
    if ranking_parameters_valid and isinstance(task_profile, Mapping):
        try:
            from opensquilla.provider.ranking_router import _proposer_bounds

            derived_min, derived_max, derived_bound_reasons = _proposer_bounds(
                task_profile,
                {},
                ranking_parameters,
            )
        except Exception:  # noqa: BLE001 - malformed trace must fail closed
            reasons.append("invalid_g1_proposer_bound_evidence")
    else:
        reasons.append("missing_g1_task_profile")
    proposer_policy = executed_plan.get("proposer_recovery_policy")
    explicit_quorum = (
        proposer_policy.get("quorum_required")
        if isinstance(proposer_policy, Mapping)
        else None
    )
    if (
        isinstance(explicit_quorum, int)
        and not isinstance(explicit_quorum, bool)
        and explicit_quorum > 0
        and (explicit_quorum > derived_min or explicit_quorum > derived_max)
    ):
        derived_min = max(derived_min, explicit_quorum)
        derived_max = max(derived_max, explicit_quorum)
        derived_bound_reasons.append("proposer_recovery_quorum")
    declared_min = coerce_metric_int(executed_plan.get("N_min"))
    declared_max = coerce_metric_int(executed_plan.get("N_max"))
    traced_bound_reasons = executed_plan.get("bound_reasons")
    if (
        derived_min <= 0
        or derived_max < derived_min
        or derived_max > expected_proposer_max
        or declared_min != derived_min
        or declared_max != derived_max
        or not isinstance(traced_bound_reasons, list)
        or traced_bound_reasons != derived_bound_reasons
    ):
        reasons.append("wrong_g1_proposer_bounds")
    selected_count = len(selected_p) if isinstance(selected_p, list) else 0
    expanded_selected_p = expanded_proposer_slot_identities(executed_plan)
    expanded_models = [
        identity.partition(":")[2] for identity in expanded_selected_p
    ]
    expanded_count = len(expanded_selected_p)
    if not expanded_selected_p:
        reasons.append("invalid_g1_expanded_proposer_roster")
    selected_aggregator_model = selected_a.partition(":")[2] if isinstance(selected_a, str) else ""
    if (
        selected_count < derived_min
        or selected_count > derived_max
        or selected_count > expected_proposer_max
        or coerce_metric_int(executed_plan.get("proposer_count")) != selected_count
        or coerce_metric_int(executed_plan.get("proposer_sample_count"))
        != expanded_count
        or executed_plan.get("proposer_models") != expanded_models
        or str(executed_plan.get("aggregator_model") or "") != selected_aggregator_model
    ):
        reasons.append("wrong_g1_selected_proposer_count")
    try:
        from opensquilla.provider.ranking_router import (
            ranking_trace_replay_reasons,
        )

        reasons.extend(ranking_trace_replay_reasons(executed_plan))
    except Exception:  # noqa: BLE001 - completion evidence must fail closed
        reasons.append("g1_frozen_ranker_replay_failed")
    replay_contract = contract.get("task_analysis_execution")
    if replay_contract is not None:
        from opensquilla.provider.ranking_router import (
            frozen_task_analysis_plan_reasons,
        )

        reasons.extend(
            frozen_task_analysis_plan_reasons(executed_plan, replay_contract)
        )
    return list(dict.fromkeys(reasons))


def authorized_dynamic_aggregator_fallback(
    trace: Mapping[str, Any],
    *,
    expected_plan: Mapping[str, Any],
    final_request: Mapping[str, Any],
    usage: Mapping[str, Any] | None,
) -> tuple[str, str, list[str]]:
    """Bind a dynamic aggregator fallback to its frozen roster and physical receipt."""

    reasons: list[str] = []
    raw_candidates = expected_plan.get("aggregator_candidates")
    candidate_identities = (
        [str(identity or "").strip() for identity in raw_candidates]
        if isinstance(raw_candidates, list)
        else []
    )
    selected_a = str(expected_plan.get("selected_A") or "").strip()
    if (
        len(candidate_identities) < 2
        or not selected_a
        or candidate_identities[0] != selected_a
        or len(candidate_identities) != len(set(candidate_identities))
        or any(
            identity.count(":") != 1
            or not identity.partition(":")[0]
            or not identity.partition(":")[2]
            for identity in candidate_identities
        )
    ):
        return "", "", ["invalid_aggregator_fallback_roster"]

    recovery = trace.get("aggregator_recovery")
    execution = final_request.get("execution")
    if (
        not isinstance(usage, Mapping)
        or not isinstance(execution, Mapping)
        or not isinstance(recovery, Mapping)
    ):
        return "", "", ["incomplete_aggregator_fallback_identity"]

    # The frozen trace and recovery receipt select the backup identity.  Request
    # fields must bind to it exactly.  Actual identity fields are different:
    # an interrupted stream can legitimately leave all of them absent together
    # with an explicit unknown-usage receipt.  Missing actual identity is then
    # metadata/audit uncertainty, while every non-empty actual value is still
    # checked fail-closed against the frozen backup.
    trace_identity = str(trace.get("executed_A") or "").strip()
    recovery_identity = str(recovery.get("executed_A") or "").strip()
    chosen_identity = (
        trace_identity
        if trace_identity == recovery_identity
        and trace_identity in candidate_identities[1:]
        else ""
    )
    if not chosen_identity:
        reasons.append("unauthorized_aggregator_fallback_identity")
    chosen_provider, _, chosen_model = chosen_identity.partition(":")

    def provider_matches(value: Any) -> bool:
        return (
            isinstance(value, str)
            and bool(value.strip())
            and value.strip().casefold() == chosen_provider.casefold()
        )

    def model_matches(value: Any) -> bool:
        if not isinstance(value, str) or not value.strip() or not chosen_model:
            return False
        return (
            _formal_openrouter_models_equivalent(value.strip(), chosen_model)
            if chosen_provider.casefold() == "openrouter"
            else value.strip() == chosen_model
        )

    requested_provider_values = (
        usage.get("requested_provider"),
        execution.get("requested_provider"),
        execution.get("provider"),
    )
    requested_model_values = (
        usage.get("requested_model"),
        execution.get("requested_model"),
        execution.get("model"),
    )
    if not chosen_identity or not all(
        provider_matches(value) for value in requested_provider_values
    ) or not all(model_matches(value) for value in requested_model_values):
        reasons.append("unauthorized_aggregator_fallback_identity")

    attempts = recovery.get("attempts") if isinstance(recovery, Mapping) else None
    selected_attempt = recovery.get("selected_attempt") if isinstance(recovery, Mapping) else None
    selected_rows = (
        [
            item
            for item in attempts
            if isinstance(item, Mapping)
            and type(item.get("attempt")) is int
            and item.get("attempt") == selected_attempt
        ]
        if isinstance(attempts, list)
        and isinstance(selected_attempt, int)
        and not isinstance(selected_attempt, bool)
        and selected_attempt > 0
        else []
    )
    selected_row = selected_rows[0] if len(selected_rows) == 1 else None
    raw_physical_id = (
        selected_row.get("physical_attempt_id")
        if isinstance(selected_row, Mapping)
        else None
    )
    physical_id = raw_physical_id if isinstance(raw_physical_id, str) else ""
    raw_usage_physical_id = usage.get("physical_attempt_id")
    usage_physical_id = (
        raw_usage_physical_id if isinstance(raw_usage_physical_id, str) else ""
    )
    expected_fallback_index = (
        candidate_identities.index(chosen_identity)
        if chosen_identity in candidate_identities
        else -1
    )
    actual_provider_values = (
        usage.get("provider"),
        execution.get("actual_provider"),
        selected_row.get("actual_provider") if isinstance(selected_row, Mapping) else None,
    )
    actual_model_values = (
        usage.get("model"),
        execution.get("actual_model"),
        selected_row.get("actual_model") if isinstance(selected_row, Mapping) else None,
    )
    present_actual_providers = [
        value for value in actual_provider_values if isinstance(value, str) and value.strip()
    ]
    present_actual_models = [
        value for value in actual_model_values if isinstance(value, str) and value.strip()
    ]
    if any(
        value not in (None, "") and not isinstance(value, str)
        for value in (*actual_provider_values, *actual_model_values)
    ) or any(not provider_matches(value) for value in present_actual_providers) or any(
        not model_matches(value) for value in present_actual_models
    ):
        reasons.append("unauthorized_aggregator_fallback_identity")
    if not present_actual_providers:
        reasons.append("missing_actual_aggregator_provider")
    if not present_actual_models:
        reasons.append("missing_actual_aggregator_model")
    degraded_delivery = bool(
        trace.get("execution_outcome") == "degraded_success"
        and trace.get("delivery_outcome") in {"degraded_success", "partial_usable"}
        and isinstance(recovery, Mapping)
        and recovery.get("degraded") is True
        and recovery.get("success") is False
    )
    selected_outcome_valid = bool(
        isinstance(selected_row, Mapping)
        and (
            selected_row.get("outcome") == "succeeded"
            and isinstance(recovery, Mapping)
            and recovery.get("success") is True
            or selected_row.get("outcome") == "failed"
            and degraded_delivery
        )
    )
    if (
        recovery.get("candidate_ids") != candidate_identities
        or recovery.get("executed_A") != chosen_identity
        or trace.get("executed_A") != chosen_identity
        or not isinstance(selected_row, Mapping)
        or selected_row.get("request_started") is not True
        or type(selected_row.get("physical_request_count")) is not int
        or selected_row.get("physical_request_count") != 1
        or len(physical_id) != 32
        or any(character not in "0123456789abcdef" for character in physical_id)
        or len(usage_physical_id) != 32
        or any(character not in "0123456789abcdef" for character in usage_physical_id)
        or usage_physical_id != physical_id
        or not selected_outcome_valid
        or selected_row.get("attempt") != selected_attempt
        or type(selected_row.get("fallback_index")) is not int
        or selected_row.get("fallback_index") != expected_fallback_index
        or type(recovery.get("fallback_index")) is not int
        or recovery.get("fallback_index") != expected_fallback_index
        or str(selected_row.get("requested_provider") or "").strip().casefold()
        != chosen_provider.casefold()
        or not (
            _formal_openrouter_models_equivalent(
                str(selected_row.get("requested_model") or "").strip(),
                chosen_model,
            )
            if chosen_provider.casefold() == "openrouter"
            else str(selected_row.get("requested_model") or "").strip()
            == chosen_model
        )
    ):
        reasons.append("invalid_aggregator_fallback_physical_evidence")
    return chosen_provider, chosen_model, list(dict.fromkeys(reasons))


def ensemble_call_core_reasons(
    trace: Mapping[str, Any],
    *,
    expected_selection_mode: str = "",
    expected_selection_plan: Mapping[str, Any] | None = None,
    expected_g1_registry_contract: Mapping[str, Any] | None = None,
    expected_task_analyzer_execution_contract: Mapping[str, Any] | None = None,
    final_text: str = "",
    require_output_binding: bool = False,
) -> list[str]:
    """Validate one physical ensemble call without trusting declared counts."""

    reasons: list[str] = []
    if str(trace.get("request_outcome") or "llm_response") != "llm_response":
        reasons.append("aggregator_call_error")
    expected_plan = (
        dict(expected_selection_plan) if isinstance(expected_selection_plan, Mapping) else {}
    )
    executed_plan = trace.get("selection_plan")
    executed_mode = str(
        trace.get("selection_strategy")
        or (
            executed_plan.get("strategy")
            if isinstance(executed_plan, Mapping)
            else ""
        )
        or ""
    )
    dynamic_selection = bool(
        expected_selection_mode == "router_dynamic"
        or executed_mode == "router_dynamic"
    )
    dynamic_aggregator_fallback = bool(
        dynamic_selection and trace.get("fallback_used") is True
    )
    if trace.get("fallback_used") is not False and not dynamic_aggregator_fallback:
        reasons.append("aggregator_fallback_used_or_unknown")
    if str(trace.get("final_request_role") or "") != "aggregator":
        reasons.append("final_request_not_aggregator")

    total = trace.get("total_candidates")
    successful = trace.get("successful_proposers")
    candidate_rows = trace.get("candidates")
    quorum_plan = expected_plan or (
        dict(executed_plan) if isinstance(executed_plan, Mapping) else {}
    )
    complete_only_analyzer_fallback = bool(
        quorum_plan.get("analyzer_failure_fallback") is True
        and quorum_plan.get("complete_proposers_only") is True
        and quorum_plan.get("effective_min_successful_proposers") == 1
    )
    dynamic_usable_contract = bool(
        dynamic_selection
        and not complete_only_analyzer_fallback
        and (
            any(
                field in trace
                for field in (
                    "usable_proposers",
                    "partial_proposers",
                    "execution_quorum_required",
                    "execution_quorum_met",
                )
            )
            or (
                isinstance(candidate_rows, list)
                and any(
                    isinstance(candidate, Mapping)
                    and (
                        "usable_for_aggregation" in candidate
                        or "completion_outcome" in candidate
                    )
                    for candidate in candidate_rows
                )
            )
        )
    )
    declared_quorum = (
        frozen_proposer_quorum(
            expected_plan
            or (executed_plan if isinstance(executed_plan, Mapping) else None),
            total,
        )
        if isinstance(total, int)
        and not isinstance(total, bool)
        and total > 0
        else 0
    )
    declared_counts_valid = bool(
        isinstance(total, int)
        and not isinstance(total, bool)
        and total > 0
        and isinstance(successful, int)
        and not isinstance(successful, bool)
        and 0 <= successful <= total
        and (
            dynamic_usable_contract
            or successful >= declared_quorum
        )
    )
    if not declared_counts_valid:
        reasons.append("insufficient_proposer_quorum")

    expected_total = coerce_metric_int(expected_plan.get("proposer_sample_count"))
    if expected_total <= 0:
        expected_models = expected_plan.get("proposer_models")
        if isinstance(expected_models, list):
            expected_total = len(expected_models)
    if expected_total and total != expected_total:
        reasons.append("wrong_executed_proposer_count")
    if expected_total and not dynamic_usable_contract and (
        not isinstance(successful, int)
        or isinstance(successful, bool)
        or successful
        < frozen_proposer_quorum(expected_plan, expected_total)
    ):
        reasons.append("insufficient_configured_proposer_quorum")

    if expected_selection_mode:
        if executed_mode != expected_selection_mode:
            reasons.append("wrong_executed_selection_mode")
    reasons.extend(
        g1_registry_contract_reasons(
            trace,
            expected_g1_registry_contract,
            expected_task_analyzer_execution_contract,
        )
    )
    if expected_plan:
        if not isinstance(executed_plan, Mapping):
            reasons.append("missing_executed_selection_plan")
        else:
            for field in (
                "strategy",
                "selection_mode",
                "profile",
                "proposer_models",
                "selected_P",
                "proposer_sample_count",
                "aggregator_model",
                "selected_A",
                "aggregator_candidates",
                "effective_min_successful_proposers",
                "proposer_recovery_policy",
            ):
                expected_value = expected_plan.get(field)
                if (
                    expected_value not in (None, [], "")
                    and executed_plan.get(field) != expected_value
                ):
                    reasons.append(f"wrong_executed_{field}")

    if not isinstance(candidate_rows, list) or not candidate_rows:
        reasons.append("missing_actual_proposer_candidates")
    else:
        if isinstance(total, int) and not isinstance(total, bool) and len(candidate_rows) != total:
            reasons.append("wrong_actual_proposer_count")
        proven_successes: list[bool] = []
        proven_usable: list[bool] = []
        proven_partials: list[bool] = []
        for candidate in candidate_rows:
            content = candidate.get("content") if isinstance(candidate, Mapping) else None
            content_proven = bool(
                isinstance(candidate, Mapping)
                and candidate.get("request_started") is True
                and isinstance(candidate.get("physical_request_count"), int)
                and not isinstance(candidate.get("physical_request_count"), bool)
                and candidate.get("physical_request_count") > 0
                and isinstance(content, Mapping)
                and coerce_metric_int(content.get("chars")) > 0
                and bool(str(content.get("text") or "").strip())
            )
            strict_proven = bool(
                content_proven
                and isinstance(candidate, Mapping)
                and candidate.get("ok") is True
                and not candidate.get("error")
                and (
                    not complete_only_analyzer_fallback
                    or candidate.get("completion_outcome") == "complete"
                )
            )
            partial_proven = bool(
                dynamic_usable_contract
                and content_proven
                and isinstance(candidate, Mapping)
                and candidate.get("ok") is False
                and candidate.get("usable_for_aggregation") is True
                and candidate.get("completion_outcome") == "partial_usable"
                and bool(str(candidate.get("error") or "").strip())
                and bool(str(candidate.get("error_code") or "").strip())
            )
            usable_proven = strict_proven or partial_proven
            proven_successes.append(strict_proven)
            proven_usable.append(usable_proven)
            proven_partials.append(partial_proven)
            if (
                isinstance(candidate, Mapping)
                and candidate.get("ok") is True
                and not strict_proven
                and not (
                    complete_only_analyzer_fallback
                    and content_proven
                    and candidate.get("completion_outcome") == "partial_usable"
                )
            ):
                reasons.append("invalid_successful_proposer_evidence")
            if dynamic_usable_contract and isinstance(candidate, Mapping):
                expected_outcome = (
                    "complete"
                    if strict_proven
                    else "partial_usable"
                    if partial_proven
                    else "failed"
                )
                if (
                    type(candidate.get("usable_for_aggregation")) is not bool
                    or candidate.get("usable_for_aggregation") is not usable_proven
                    or candidate.get("completion_outcome") != expected_outcome
                ):
                    reasons.append("invalid_usable_proposer_evidence")
                if candidate.get("selected_for_aggregation") is True and not usable_proven:
                    reasons.append("invalid_selected_proposer_evidence")
            if isinstance(candidate, Mapping) and strict_proven:
                if candidate.get(
                    "usage_reported"
                ) is not True and not ensemble_metadata_field_resolved(candidate, "usage"):
                    reasons.append("missing_proposer_usage_metadata")
                if not str(
                    candidate.get("stop_reason") or ""
                ).strip() and not ensemble_metadata_field_resolved(candidate, "stop_reason"):
                    reasons.append("missing_proposer_stop_reason")
        actual_successful = sum(proven_successes)
        if (
            not isinstance(successful, int)
            or isinstance(successful, bool)
            or actual_successful != successful
        ):
            reasons.append("successful_proposer_count_mismatch")
        actual_usable = sum(proven_usable)
        actual_partials = sum(proven_partials)
        quorum_total = expected_total or (
            total if isinstance(total, int) and not isinstance(total, bool) else 0
        )
        configured_quorum = (
            frozen_proposer_quorum(quorum_plan, quorum_total)
            if quorum_total > 0
            else 0
        )
        execution_quorum_required = (
            max(2, configured_quorum)
            if dynamic_usable_contract and actual_partials
            else configured_quorum
        )
        if dynamic_usable_contract:
            selected_count = sum(
                1
                for candidate in candidate_rows
                if isinstance(candidate, Mapping)
                and candidate.get("selected_for_aggregation") is True
            )
            selected_usable_count = sum(
                1
                for index, candidate in enumerate(candidate_rows)
                if isinstance(candidate, Mapping)
                and candidate.get("selected_for_aggregation") is True
                and proven_usable[index]
            )
            if (
                trace.get("usable_proposers") != actual_usable
                or trace.get("partial_proposers") != actual_partials
                or trace.get("selected_candidate_count") != selected_count
                or trace.get("execution_quorum_required")
                != execution_quorum_required
                or trace.get("execution_quorum_met")
                is not (actual_usable >= execution_quorum_required)
            ):
                reasons.append("invalid_proposer_execution_quorum_evidence")
            if actual_usable < execution_quorum_required:
                reasons.append("insufficient_actual_proposer_quorum")
            if selected_usable_count < execution_quorum_required:
                reasons.append("insufficient_selected_proposer_quorum")
        elif expected_total and actual_successful < configured_quorum:
            reasons.append("insufficient_actual_proposer_quorum")
        raw_expected_selected_p = expected_plan.get("selected_P")
        expected_slot_identities = expanded_proposer_slot_identities(
            expected_plan
        )
        if isinstance(raw_expected_selected_p, list):
            if not expected_slot_identities:
                reasons.append("invalid_expected_proposer_slot_roster")
            elif len(candidate_rows) != len(expected_slot_identities):
                reasons.append("wrong_actual_proposer_count")
            else:
                # Recovery may legally replace any initial slot with a member
                # from the frozen backup roster.  Candidate identity is
                # therefore a roster-membership audit, not a positional
                # equality check against the original selected_P array.
                allowed_proposer_identities = set(expected_slot_identities)
                raw_backup_p = expected_plan.get("backup_P")
                if isinstance(raw_backup_p, list):
                    allowed_proposer_identities.update(
                        identity.strip()
                        for identity in raw_backup_p
                        if isinstance(identity, str)
                        and identity.strip()
                        and ":" in identity
                    )
                requested_identity_missing = False
                requested_identity_wrong = False
                actual_identity_missing = False
                actual_identity_wrong = False
                for candidate_index, candidate in enumerate(candidate_rows):
                    execution = (
                        candidate.get("execution") if isinstance(candidate, Mapping) else None
                    )
                    requested_provider = (
                        candidate.get("requested_provider")
                        if isinstance(candidate, Mapping)
                        else None
                    ) or (
                        execution.get("requested_provider") or execution.get("provider")
                        if isinstance(execution, Mapping)
                        else None
                    )
                    requested_model = (
                        candidate.get("requested_model") if isinstance(candidate, Mapping) else None
                    ) or (execution.get("model") if isinstance(execution, Mapping) else None)
                    candidate_provider = (
                        candidate.get("provider") if isinstance(candidate, Mapping) else None
                    )
                    candidate_model = (
                        candidate.get("model") if isinstance(candidate, Mapping) else None
                    )
                    if (
                        not isinstance(requested_provider, str)
                        or not requested_provider.strip()
                        or not isinstance(requested_model, str)
                        or not requested_model.strip()
                    ):
                        requested_identity_missing = True
                        requested_identity = ""
                    else:
                        requested_identity = (
                            requested_provider.strip()
                            + ":"
                            + requested_model.strip()
                        )
                    if (
                        requested_identity
                        and requested_identity not in allowed_proposer_identities
                    ):
                        requested_identity_wrong = True
                    provider_missing = (
                        not isinstance(candidate_provider, str) or not candidate_provider.strip()
                    )
                    model_missing = (
                        not isinstance(candidate_model, str) or not candidate_model.strip()
                    )
                    if proven_usable[candidate_index] and (
                        (
                            provider_missing
                            and not ensemble_metadata_field_resolved(
                                candidate,
                                "actual_provider",
                            )
                        )
                        or (
                            model_missing
                            and not ensemble_metadata_field_resolved(
                                candidate,
                                "actual_model",
                            )
                        )
                    ):
                        actual_identity_missing = True
                    elif (
                        isinstance(candidate_provider, str)
                        and candidate_provider.strip()
                        and isinstance(candidate_model, str)
                        and candidate_model.strip()
                        and (
                            candidate_provider.strip()
                            + ":"
                            + candidate_model.strip()
                        )
                        not in allowed_proposer_identities
                    ) or (
                        requested_identity
                        and isinstance(candidate_provider, str)
                        and candidate_provider.strip()
                        and isinstance(candidate_model, str)
                        and candidate_model.strip()
                        and (
                            candidate_provider.strip()
                            != requested_provider.strip()
                            or candidate_model.strip()
                            != requested_model.strip()
                        )
                    ):
                        actual_identity_wrong = True
                if requested_identity_missing:
                    reasons.append("missing_requested_proposer_identity")
                if requested_identity_wrong:
                    reasons.append("wrong_requested_proposer_identity")
                if actual_identity_missing:
                    reasons.append("missing_actual_proposer_identity")
                if actual_identity_wrong:
                    reasons.append("wrong_actual_proposer_identity")

    final_request = trace.get("final_request")
    if (
        not isinstance(final_request, Mapping)
        or final_request.get("request_started") is not True
        or str(final_request.get("role") or "") != "aggregator"
        or final_request.get("error")
        or trace.get("aggregator_error")
    ):
        reasons.append("aggregator_request_incomplete")
        return list(dict.fromkeys(reasons))

    usage = final_request.get("usage")
    if not isinstance(usage, Mapping) and not ensemble_metadata_field_resolved(
        final_request, "usage"
    ):
        reasons.append("missing_aggregator_usage_metadata")
    if (
        not isinstance(usage, Mapping) or not str(usage.get("stop_reason") or "").strip()
    ) and not ensemble_metadata_field_resolved(final_request, "stop_reason"):
        reasons.append("missing_aggregator_stop_reason")

    fallback_expected_provider = ""
    fallback_expected_model = ""
    if dynamic_aggregator_fallback:
        (
            fallback_expected_provider,
            fallback_expected_model,
            fallback_reasons,
        ) = authorized_dynamic_aggregator_fallback(
            trace,
            expected_plan=expected_plan,
            final_request=final_request,
            usage=usage if isinstance(usage, Mapping) else None,
        )
        reasons.extend(fallback_reasons)

    if expected_plan:
        expected_aggregator_model = (
            fallback_expected_model
            if dynamic_aggregator_fallback
            else expected_plan.get("aggregator_model")
        )
        expected_selected_a = (
            f"{fallback_expected_provider}:{fallback_expected_model}"
            if dynamic_aggregator_fallback and fallback_expected_provider
            else expected_plan.get("selected_A")
        )
        expected_provider, separator, selected_model = (
            expected_selected_a.partition(":")
            if isinstance(expected_selected_a, str)
            else ("", "", "")
        )
        actual_model = usage.get("model") if isinstance(usage, Mapping) else None
        actual_provider = usage.get("provider") if isinstance(usage, Mapping) else None
        requested_model = usage.get("requested_model") if isinstance(usage, Mapping) else None
        requested_provider = usage.get("requested_provider") if isinstance(usage, Mapping) else None
        if not isinstance(expected_aggregator_model, str) or not (
            expected_aggregator_model.strip()
        ):
            reasons.append("wrong_actual_aggregator_model")
        elif (
            not isinstance(actual_model, str) or not actual_model.strip()
        ) and not ensemble_metadata_field_resolved(final_request, "actual_model"):
            reasons.append("missing_actual_aggregator_model")
        elif actual_model.strip() != expected_aggregator_model.strip():
            reasons.append("wrong_actual_aggregator_model")
        if (
            separator != ":"
            or not expected_provider.strip()
            or selected_model.strip()
            != (
                expected_aggregator_model.strip()
                if isinstance(expected_aggregator_model, str)
                else ""
            )
        ):
            reasons.append("wrong_actual_aggregator_provider")
        elif (
            not isinstance(actual_provider, str) or not actual_provider.strip()
        ) and not ensemble_metadata_field_resolved(final_request, "actual_provider"):
            reasons.append("missing_actual_aggregator_provider")
        elif actual_provider.strip() != expected_provider.strip():
            reasons.append("wrong_actual_aggregator_provider")
        if (
            not isinstance(requested_provider, str)
            or not requested_provider.strip()
            or not isinstance(requested_model, str)
            or not requested_model.strip()
        ):
            reasons.append("missing_requested_aggregator_identity")
        elif requested_provider.strip() != expected_provider.strip() or requested_model.strip() != (
            expected_aggregator_model.strip() if isinstance(expected_aggregator_model, str) else ""
        ):
            reasons.append("wrong_requested_aggregator_identity")

    if require_output_binding:
        output = (
            trace.get("assembled_output")
            if trace.get("output_binding_schema") == "opensquilla.ensemble-output-binding/v1"
            else final_request.get("output")
        )
        if not isinstance(output, Mapping):
            reasons.append("missing_aggregator_output_binding")
        else:
            output_text = output.get("text")
            output_chars = coerce_metric_int(output.get("chars"))
            output_truncated = output.get("truncated") is True
            if not isinstance(output_text, str) or not output_text.strip() or output_chars <= 0:
                reasons.append("missing_aggregator_output_binding")
            elif output_chars > len(final_text):
                reasons.append("wrong_aggregator_output_length")
            else:
                final_output_tail = final_text[-output_chars:]
                if (output_truncated and not final_output_tail.startswith(output_text)) or (
                    not output_truncated and output_text != final_output_tail
                ):
                    reasons.append("wrong_aggregator_output_binding")
    return list(dict.fromkeys(reasons))


def ensemble_call_trace_sequence(
    trace: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[str]]:
    """Return a complete, ordered Agent-loop call sequence or strict reasons."""

    calls = trace.get("calls")
    if calls is None and str(trace.get("mode") or "") != "agent_loop":
        return [trace], []
    reasons: list[str] = []
    if not isinstance(calls, list) or not calls:
        return [], ["missing_ensemble_call_trace"]
    if any(not isinstance(item, Mapping) for item in calls):
        return [], ["invalid_ensemble_call_trace"]
    call_traces = [item for item in calls if isinstance(item, Mapping)]
    raw_count = trace.get("agent_llm_call_count")
    if (
        not isinstance(raw_count, int)
        or isinstance(raw_count, bool)
        or raw_count <= 0
        or raw_count != len(call_traces)
    ):
        reasons.append("wrong_agent_llm_call_count")
    if coerce_metric_int(trace.get("untraced_agent_llm_call_count")) != 0:
        reasons.append("untraced_agent_llm_calls")
    indices = [item.get("agent_call_index") for item in call_traces]
    if indices != list(range(1, len(call_traces) + 1)):
        reasons.append("invalid_agent_call_index_sequence")
    return call_traces, reasons


def g1_retry_physical_usage_binding_reasons(
    run: Mapping[str, Any],
    *,
    identity_seed: str,
) -> list[str]:
    """Strictly bind every paid managed-G1 ledger before more paid work."""

    from opensquilla.provider.thinking_execution import (
        THINKING_PHYSICAL_EVIDENCE_SCHEMA,
    )

    trace = run.get("ensemble_trace")
    calls, reasons = ensemble_call_trace_sequence(
        trace if isinstance(trace, Mapping) else {}
    )
    strict_calls = [
        call
        for call in calls
        if isinstance(call.get("selection_plan"), Mapping)
        and (
            call["selection_plan"].get(
                "thinking_physical_evidence_schema"
            )
            == THINKING_PHYSICAL_EVIDENCE_SCHEMA
            or call["selection_plan"].get(
                "proposer_recovery_policy"
            )
            is not None
        )
    ]
    if not strict_calls:
        return [
            *reasons,
            "missing_g1_thinking_physical_evidence_schema",
        ]
    if len(strict_calls) != len(calls):
        reasons.append("mixed_g1_thinking_physical_evidence_schema")

    ledger_ids: list[str] = []
    for call in strict_calls:
        candidates = call.get("candidates")
        for candidate in candidates if isinstance(candidates, list) else []:
            execution = (
                candidate.get("execution")
                if isinstance(candidate, Mapping)
                else None
            )
            physical_attempts = (
                execution.get("physical_attempts")
                if isinstance(execution, Mapping)
                else None
            )
            ledger_ids.extend(
                str(attempt.get("physical_attempt_id") or "")
                for attempt in (
                    physical_attempts
                    if isinstance(physical_attempts, list)
                    else []
                )
                if isinstance(attempt, Mapping)
                and attempt.get("request_started") is True
            )
        recovery = call.get("aggregator_recovery")
        recovery_attempts = (
            recovery.get("attempts")
            if isinstance(recovery, Mapping)
            else None
        )
        ledger_ids.extend(
            str(attempt.get("physical_attempt_id") or "")
            for attempt in (
                recovery_attempts
                if isinstance(recovery_attempts, list)
                else []
            )
            if isinstance(attempt, Mapping)
            and attempt.get("request_started") is True
        )

    def valid_id(value: str) -> bool:
        return len(value) == 32 and all(
            character in "0123456789abcdef"
            for character in value
        )

    if (
        any(not valid_id(attempt_id) for attempt_id in ledger_ids)
        or len(ledger_ids) != len(set(ledger_ids))
    ):
        reasons.append("invalid_g1_thinking_physical_attempt_set")
    try:
        units = canonical_run_usage_units(
            run,
            identity_seed=identity_seed,
        )
        total_request_count = derive_physical_request_count(run)
    except Exception as exc:  # noqa: BLE001 - suppress retry without raw evidence
        reasons.append(
            "g1_physical_usage_canonicalization_failed:"
            + type(exc).__name__
        )
        return list(dict.fromkeys(reasons))

    def is_task_analyzer_unit(unit: Mapping[str, Any]) -> bool:
        role = str(unit.get("role") or "").strip().casefold()
        return role in {"task_analyzer", "task_analyzer_attempt"} or (
            role == "unknown_request"
            and str(unit.get("label") or "").strip().casefold()
            == "task_analyzer"
        )

    analyzer_unit_count = sum(
        1 for unit in units if is_task_analyzer_unit(unit)
    )
    generation_units = [
        unit for unit in units if not is_task_analyzer_unit(unit)
    ]
    usage_ids: list[str] = []
    for unit in generation_units:
        attempt_id = str(unit.get("physical_attempt_id") or "")
        provider_usage = unit.get("provider_usage")
        nested_id = (
            str(provider_usage.get("physical_attempt_id") or "")
            if isinstance(provider_usage, Mapping)
            else ""
        )
        if not valid_id(attempt_id) or nested_id != attempt_id:
            reasons.append(
                "invalid_g1_thinking_usage_physical_attempt_id"
            )
            continue
        usage_ids.append(attempt_id)
    if len(usage_ids) != len(set(usage_ids)):
        reasons.append("invalid_g1_thinking_usage_physical_attempt_set")
    if Counter(usage_ids) != Counter(ledger_ids):
        reasons.append("g1_thinking_physical_usage_set_mismatch")
    expected_generation_requests = max(
        0,
        total_request_count - analyzer_unit_count,
    )
    if (
        len(ledger_ids) != expected_generation_requests
        or len(generation_units) != expected_generation_requests
    ):
        reasons.append(
            "g1_thinking_physical_usage_multiplicity_mismatch"
        )
    return list(dict.fromkeys(reasons))


_ADMISSIBLE_NONTERMINAL_FALLBACK_CORE_REASONS = frozenset(
    {
        "aggregator_fallback_used_or_unknown",
        "final_request_not_aggregator",
        "insufficient_proposer_quorum",
        "insufficient_configured_proposer_quorum",
        "insufficient_actual_proposer_quorum",
        "aggregator_request_incomplete",
    }
)


def admissible_empty_nonterminal_fallback_reasons(
    trace: Mapping[str, Any],
    *,
    expected_selection_plan: Mapping[str, Any] | None = None,
) -> list[str]:
    """Validate the one nonterminal fallback shape that cannot add answer text."""

    reasons: list[str] = []
    if str(trace.get("request_outcome") or "llm_response") != "llm_response":
        reasons.append("invalid_intermediate_fallback_outcome")
    if trace.get("fallback_used") is not True:
        reasons.append("invalid_intermediate_fallback_flag")
    if str(trace.get("final_request_role") or "") != "fallback_single":
        reasons.append("invalid_intermediate_fallback_role")
    plan = (
        dict(expected_selection_plan)
        if isinstance(expected_selection_plan, Mapping)
        else {}
    )
    total = trace.get("total_candidates")
    successful = trace.get("successful_proposers")
    required_quorum = (
        2
        if plan.get("proposer_recovery_policy") is not None
        else legal_proposer_quorum(total)
        if isinstance(total, int) and not isinstance(total, bool)
        else 0
    )
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or total <= 0
        or not isinstance(successful, int)
        or isinstance(successful, bool)
        or not 0 <= successful < required_quorum
    ):
        reasons.append("invalid_intermediate_fallback_quorum")
    final_request = trace.get("final_request")
    if (
        not isinstance(final_request, Mapping)
        or final_request.get("request_started") is not True
        or str(final_request.get("role") or "") != "fallback_single"
        or final_request.get("error")
        or trace.get("aggregator_error")
    ):
        reasons.append("invalid_intermediate_fallback_request")
        return list(dict.fromkeys(reasons))
    output = final_request.get("output")
    output_chars = output.get("chars") if isinstance(output, Mapping) else None
    if (
        not isinstance(output, Mapping)
        or output.get("text") != ""
        or not isinstance(output_chars, int)
        or isinstance(output_chars, bool)
        or output_chars != 0
        or output.get("truncated") is not False
    ):
        reasons.append("intermediate_fallback_visible_output")
    usage = final_request.get("usage")
    if not isinstance(usage, Mapping):
        reasons.append("missing_intermediate_fallback_usage")
        return list(dict.fromkeys(reasons))
    execution = final_request.get("execution")
    if not isinstance(execution, Mapping):
        reasons.append("missing_intermediate_fallback_execution")
        return list(dict.fromkeys(reasons))
    actual_provider = str(usage.get("provider") or "").strip().casefold()
    requested_provider = str(usage.get("requested_provider") or "").strip().casefold()
    actual_model = str(usage.get("model") or "").strip()
    requested_model = str(usage.get("requested_model") or "").strip()
    execution_providers = [
        str(execution.get(field) or "").strip().casefold()
        for field in ("requested_provider", "provider", "actual_provider")
    ]
    execution_models = [
        str(execution.get(field) or "").strip()
        for field in ("requested_model", "model", "actual_model")
    ]
    if (
        actual_provider != "openrouter"
        or requested_provider != "openrouter"
        or any(provider != "openrouter" for provider in execution_providers)
    ):
        reasons.append("wrong_intermediate_fallback_provider")
    selected_p = plan.get("selected_P")
    backup_p = plan.get("backup_P")
    allowed_identities = [
        *(
            selected_p
            if isinstance(selected_p, list)
            else []
        ),
        *(
            backup_p
            if isinstance(backup_p, list)
            else []
        ),
    ]
    allowed_models = (
        [
            str(identity).partition(":")[2].strip()
            for identity in allowed_identities
            if isinstance(identity, str)
            and identity.partition(":")[1] == ":"
            and identity.partition(":")[0].strip().casefold() == "openrouter"
            and identity.partition(":")[2].strip()
        ]
        if isinstance(selected_p, list)
        else []
    )
    if (
        not actual_model
        or not requested_model
        or not allowed_models
        or not any(
            _formal_openrouter_models_equivalent(actual_model, model)
            and _formal_openrouter_models_equivalent(requested_model, model)
            and all(
                _formal_openrouter_models_equivalent(execution_model, model)
                for execution_model in execution_models
            )
            for model in allowed_models
        )
    ):
        reasons.append("wrong_intermediate_fallback_model")
    if str(execution.get("role") or "") != "fallback_single":
        reasons.append("wrong_intermediate_fallback_execution_role")
    if not str(usage.get("stop_reason") or "").strip() and not ensemble_metadata_field_resolved(
        final_request,
        "stop_reason",
    ):
        reasons.append("missing_intermediate_fallback_stop_reason")
    return list(dict.fromkeys(reasons))


def agent_call_output_sequence_reasons(
    calls: Sequence[Mapping[str, Any]],
    *,
    final_text: str,
) -> list[str]:
    """Bind every visible Agent-loop response segment to the stored answer."""

    if len(calls) <= 1:
        return []
    reasons: list[str] = []
    offset = 0
    for call in calls:
        final_request = call.get("final_request")
        output = (
            call.get("assembled_output")
            if call.get("output_binding_schema") == "opensquilla.ensemble-output-binding/v1"
            else final_request.get("output")
            if isinstance(final_request, Mapping)
            else None
        )
        if not isinstance(output, Mapping):
            reasons.append("missing_agent_call_output_binding")
            continue
        output_text = output.get("text")
        output_chars = output.get("chars")
        if (
            not isinstance(output_text, str)
            or not isinstance(output_chars, int)
            or isinstance(output_chars, bool)
            or output_chars < 0
            or offset + output_chars > len(final_text)
        ):
            reasons.append("invalid_agent_call_output_binding")
            continue
        segment = final_text[offset : offset + output_chars]
        if output.get("truncated") is True:
            if not segment.startswith(output_text):
                reasons.append("wrong_agent_call_output_binding")
        elif len(output_text) != output_chars or output_text != segment:
            reasons.append("wrong_agent_call_output_binding")
        offset += output_chars
    if offset != len(final_text):
        reasons.append("incomplete_agent_call_output_binding")
    return list(dict.fromkeys(reasons))


def ensemble_generation_retry_reason(
    result: RunResult,
    *,
    expected_selection_mode: str = "",
    expected_selection_plan: Mapping[str, Any] | None = None,
    expected_g1_registry_contract: Mapping[str, Any] | None = None,
    expected_task_analyzer_execution_contract: Mapping[str, Any] | None = None,
) -> str:
    """Return only a reason that proves the ensemble answer is unusable."""

    done = result.done
    if done is None or not isinstance(done.ensemble_trace, dict):
        return "missing_ensemble_trace"
    trace = done.ensemble_trace
    call_traces, sequence_reasons = ensemble_call_trace_sequence(trace)
    if not call_traces:
        return "missing_ensemble_call_trace"
    record_generation_audit_warnings(
        result,
        [
            reason
            for reason in sequence_reasons
            if not ensemble_execution_blocking_reason(reason)
        ],
    )
    blocking_sequence_reasons = [
        reason
        for reason in sequence_reasons
        if ensemble_execution_blocking_reason(reason)
    ]
    if blocking_sequence_reasons:
        return blocking_sequence_reasons[0]
    output_sequence_reasons = agent_call_output_sequence_reasons(
        call_traces,
        final_text=result.final_text,
    )
    blocking_output_sequence_reasons = [
        reason
        for reason in output_sequence_reasons
        if ensemble_execution_blocking_reason(reason)
    ]
    record_generation_audit_warnings(
        result,
        [
            reason
            for reason in output_sequence_reasons
            if reason not in blocking_output_sequence_reasons
        ],
    )
    if blocking_output_sequence_reasons:
        return blocking_output_sequence_reasons[0]
    for index, call_trace in enumerate(call_traces):
        reasons = ensemble_call_core_reasons(
            call_trace,
            expected_selection_mode=expected_selection_mode,
            expected_selection_plan=expected_selection_plan,
            expected_g1_registry_contract=expected_g1_registry_contract,
            expected_task_analyzer_execution_contract=(
                expected_task_analyzer_execution_contract
            ),
            final_text=result.final_text,
            require_output_binding=index == len(call_traces) - 1,
        )
        if index < len(call_traces) - 1 and call_trace.get("fallback_used") is True:
            fallback_reasons = admissible_empty_nonterminal_fallback_reasons(
                call_trace,
                expected_selection_plan=expected_selection_plan,
            )
            blocking_fallback_reasons = [
                reason
                for reason in fallback_reasons
                if ensemble_execution_blocking_reason(reason)
            ]
            record_generation_audit_warnings(
                result,
                [
                    reason
                    for reason in fallback_reasons
                    if reason not in blocking_fallback_reasons
                ],
            )
            if blocking_fallback_reasons:
                return blocking_fallback_reasons[0]
            reasons = [
                reason
                for reason in reasons
                if reason not in _ADMISSIBLE_NONTERMINAL_FALLBACK_CORE_REASONS
            ]
        blocking_reasons = [
            reason
            for reason in reasons
            if ensemble_execution_blocking_reason(reason)
        ]
        record_generation_audit_warnings(
            result,
            [reason for reason in reasons if reason not in blocking_reasons],
        )
        if blocking_reasons:
            return blocking_reasons[0]
    return ""


def backfill_result_requested_identity(
    result: RunResult,
    *,
    expected_model: str = "",
    expected_provider: str = "",
    expected_selection_plan: Mapping[str, Any] | None = None,
) -> bool:
    """Backfill only absent requested identity from the frozen request plan."""

    done = result.done
    if done is None:
        return False
    changed_fields: list[str] = []
    plan = dict(expected_selection_plan) if isinstance(expected_selection_plan, Mapping) else {}
    selected_a = plan.get("selected_A")
    plan_provider, separator, plan_model = (
        selected_a.partition(":") if isinstance(selected_a, str) else ("", "", "")
    )
    if separator == ":" and plan_provider.strip() and plan_model.strip():
        expected_provider = plan_provider.strip()
        expected_model = plan_model.strip()
    if expected_model and not str(done.requested_model or "").strip():
        done.requested_model = expected_model
        changed_fields.append("requested_model")
    if expected_provider and not str(done.requested_provider or "").strip():
        done.requested_provider = expected_provider
        changed_fields.append("requested_provider")

    root_trace = done.ensemble_trace
    if isinstance(root_trace, dict):
        calls = root_trace.get("calls")
        call_traces = (
            [item for item in calls if isinstance(item, dict)]
            if isinstance(calls, list)
            else [root_trace]
        )
        for call_trace in call_traces:
            call_plan = (
                call_trace.get("selection_plan")
                if isinstance(call_trace.get("selection_plan"), Mapping)
                else plan
            )
            selected_p = (
                expanded_proposer_slot_identities(call_plan)
                if isinstance(call_plan, Mapping)
                else ()
            )
            candidates = call_trace.get("candidates")
            if (
                selected_p
                and isinstance(candidates, list)
                and len(selected_p) == len(candidates)
            ):
                for candidate, identity in zip(candidates, selected_p, strict=True):
                    if not isinstance(candidate, dict) or not isinstance(identity, str):
                        continue
                    provider, identity_separator, model = identity.partition(":")
                    if identity_separator != ":" or not provider.strip() or not model.strip():
                        continue
                    if not str(candidate.get("requested_provider") or "").strip():
                        candidate["requested_provider"] = provider.strip()
                        changed_fields.append("candidate.requested_provider")
                    if not str(candidate.get("requested_model") or "").strip():
                        candidate["requested_model"] = model.strip()
                        changed_fields.append("candidate.requested_model")
            call_selected_a = (
                call_plan.get("selected_A") if isinstance(call_plan, Mapping) else None
            )
            final_provider, final_separator, final_model = (
                call_selected_a.partition(":") if isinstance(call_selected_a, str) else ("", "", "")
            )
            if final_separator != ":" or not final_provider.strip() or not final_model.strip():
                continue
            final_request = call_trace.get("final_request")
            if not isinstance(final_request, dict):
                continue
            usage = final_request.get("usage")
            if isinstance(usage, dict):
                if not str(usage.get("requested_provider") or "").strip():
                    usage["requested_provider"] = final_provider.strip()
                    changed_fields.append("aggregator.requested_provider")
                if not str(usage.get("requested_model") or "").strip():
                    usage["requested_model"] = final_model.strip()
                    changed_fields.append("aggregator.requested_model")
            execution = final_request.get("execution")
            if isinstance(execution, dict):
                if not str(execution.get("requested_provider") or "").strip():
                    execution["requested_provider"] = final_provider.strip()
                if not str(execution.get("requested_model") or "").strip():
                    execution["requested_model"] = final_model.strip()

    if changed_fields:
        provider_usage = (
            dict(done.provider_usage) if isinstance(done.provider_usage, Mapping) else {}
        )
        provider_usage["requested_identity_backfill"] = {
            "source": "frozen_request_configuration",
            "fields": sorted(set(changed_fields)),
        }
        done.provider_usage = provider_usage
    return bool(changed_fields)


def single_generation_identity_reason(
    result: RunResult,
    *,
    expected_model: str = "",
    expected_provider: str = "",
) -> str:
    """Validate one fixed/router-single generation from physical receipts."""

    done = result.done
    if done is None:
        return GENERATION_MISSING_DONE_ERROR
    breakdown = [
        row
        for row in done.model_usage_breakdown
        if isinstance(row, Mapping)
        and str(row.get("role") or "").strip().casefold() not in MISSING_USAGE_PLACEHOLDER_ROLES
    ]
    models = {
        str(row.get("model") or "").strip()
        for row in breakdown
        if str(row.get("model") or "").strip()
    }
    providers = {
        str(row.get("provider") or "").strip()
        for row in breakdown
        if str(row.get("provider") or "").strip()
    }
    direct_model = str(done.model or "").strip()
    direct_provider = str(done.provider or "").strip()
    requested_model = str(done.requested_model or "").strip()
    requested_provider = str(done.requested_provider or "").strip()
    actual_model = direct_model or (next(iter(models)) if len(models) == 1 else "")
    actual_provider = direct_provider or (next(iter(providers)) if len(providers) == 1 else "")
    if direct_model and models and any(model != direct_model for model in models):
        return "actual_model_receipt_mismatch"
    if direct_provider and providers and any(provider != direct_provider for provider in providers):
        return "actual_provider_receipt_mismatch"
    if not actual_model:
        return "actual_model_evidence_missing"
    if expected_model and actual_model != expected_model:
        return "wrong_actual_model"
    if not actual_provider:
        return "actual_provider_evidence_missing"
    if expected_provider and actual_provider != expected_provider:
        return "wrong_actual_provider"
    if not requested_model:
        return "missing_requested_model_identity"
    if expected_model and requested_model != expected_model:
        return "wrong_requested_model"
    if not requested_provider:
        return "missing_requested_provider_identity"
    if expected_provider and requested_provider != expected_provider:
        return "wrong_requested_provider"
    return ""


def generation_retry_reason(
    result: RunResult,
    *,
    expected_selection_mode: str = "",
    expected_selection_plan: Mapping[str, Any] | None = None,
    expected_g1_registry_contract: Mapping[str, Any] | None = None,
    expected_task_analyzer_execution_contract: Mapping[str, Any] | None = None,
    expected_model: str = "",
    expected_provider: str = "",
) -> str:
    if result.error:
        if (
            expected_selection_mode
            and result.done is not None
            and result.final_text.strip()
        ):
            record_generation_audit_warnings(
                result,
                ["provider_error_after_visible_ensemble_output"],
            )
        else:
            return result.error
    if result.done is None:
        return GENERATION_MISSING_DONE_ERROR
    if not result.final_text.strip():
        return GENERATION_EMPTY_OUTPUT_ERROR
    if expected_selection_mode:
        return ensemble_generation_retry_reason(
            result,
            expected_selection_mode=expected_selection_mode,
            expected_selection_plan=expected_selection_plan,
            expected_g1_registry_contract=expected_g1_registry_contract,
            expected_task_analyzer_execution_contract=(
                expected_task_analyzer_execution_contract
            ),
        )
    if expected_model or expected_provider:
        return single_generation_identity_reason(
            result,
            expected_model=expected_model,
            expected_provider=expected_provider,
        )
    return ""


_PROVIDER_NATIVE_PROPOSER_RETRY_REASONS = frozenset(
    {
        "ensemble_insufficient_proposers",
        "ensemble_no_proposers",
        "insufficient_actual_proposer_quorum",
        "insufficient_selected_proposer_quorum",
        "missing_actual_proposer_candidates",
    }
)


def provider_native_proposer_retry_reason(reason: str) -> bool:
    """Return whether provider-owned proposer recovery exhausts this reason."""

    normalized = str(reason or "").strip().casefold()
    return bool(
        normalized in _PROVIDER_NATIVE_PROPOSER_RETRY_REASONS
        or normalized.startswith("ensemble_proposer_")
        or normalized.startswith("ensemble proposer ")
        or ("successful proposer" in normalized and "requires" in normalized)
    )


def is_agent_hard_timeout(result: RunResult) -> bool:
    if any(str(event.get("kind") or "") == "timeout" for event in result.trace_events):
        return True
    normalized_error = str(result.error or "").strip().casefold()
    return (
        ("agent run timed out" in normalized_error)
        or ("agent turn timed out" in normalized_error)
        or ("agent_runtime_timeout" in normalized_error)
    )


def generation_cleanup_failure_reason(result: RunResult) -> str:
    """Return a non-retryable cleanup/ownership failure code, if present."""

    for event in result.trace_events:
        code = str(event.get("code") or "").strip()
        if (
            code.endswith("_close_timeout")
            or code.endswith("_cleanup_timeout")
            or code
            in {
                "agent_cleanup_in_progress",
                "agent_turn_in_progress",
                "benchmark_owner_cleanup_in_progress",
                "ensemble_cleanup_in_progress",
                "ensemble_call_in_progress",
                "provider_stream_close_failed",
                "provider_stream_close_unavailable",
            }
        ):
            return code
    normalized_error = str(result.error or "").strip().casefold()
    for marker in (
        "close_timeout",
        "close_failed",
        "close_unavailable",
        "cleanup_timeout",
        "cleanup in progress",
        "cleanup_in_progress",
        "still closing",
        "turn_in_progress",
        "call_in_progress",
    ):
        if marker in normalized_error:
            return marker
    return ""


def mark_empty_generation_output(result: RunResult) -> None:
    if result.error or result.final_text.strip():
        return
    result.error = GENERATION_EMPTY_OUTPUT_ERROR
    result.trace_events.append(
        {
            "seq": len(result.trace_events) + 1,
            "elapsed_ms": result.latency_ms,
            "kind": GENERATION_EMPTY_OUTPUT_ERROR,
        }
    )


def mark_retryable_generation_error(result: RunResult, reason: str) -> None:
    if result.error or not reason:
        return
    result.error = reason
    result.trace_events.append(
        {
            "seq": len(result.trace_events) + 1,
            "elapsed_ms": result.latency_ms,
            "kind": reason,
        }
    )


def selected_generation_attempt(
    attempts: list[dict[str, Any]],
    selected_result: RunResult,
) -> int:
    selected_sha = text_sha256(selected_result.final_text)
    selected_error = selected_result.error
    for attempt in attempts:
        run = attempt.get("run")
        if not isinstance(run, dict):
            continue
        if run.get("final_text_sha256") == selected_sha and run.get("error") == selected_error:
            return coerce_metric_int(attempt.get("attempt"))
    return coerce_metric_int(attempts[-1].get("attempt")) if attempts else 0


def _reasoning_only_length_failures_from_trace(
    root_trace: Mapping[str, Any],
    *,
    require_coherent_envelope: bool = True,
) -> list[dict[str, Any]]:
    """Extract only exact, fully receipted reasoning-only length failures."""

    if not isinstance(root_trace, Mapping):
        return []
    call_traces, sequence_reasons = ensemble_call_trace_sequence(root_trace)
    if sequence_reasons:
        return []
    failures: list[dict[str, Any]] = []
    envelope_physical_count = 0
    for call_index, call_trace in enumerate(call_traces, start=1):
        candidates = call_trace.get("candidates")
        if not isinstance(candidates, list):
            return []
        call_physical_count = 0
        if require_coherent_envelope:
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    return []
                started = candidate.get("request_started")
                physical_count = candidate.get("physical_request_count")
                missing_count = candidate.get("usage_missing_count")
                if not isinstance(started, bool):
                    return []
                if started:
                    if (
                        isinstance(physical_count, bool)
                        or not isinstance(physical_count, int)
                        or physical_count <= 0
                        or candidate.get("usage_reported") is not True
                        or isinstance(missing_count, bool)
                        or not isinstance(missing_count, int)
                        or missing_count != 0
                    ):
                        return []
                    call_physical_count += physical_count
                elif physical_count not in (None, 0):
                    return []
            for field, expected in (
                ("physical_request_count", call_physical_count),
                ("llm_request_count", call_physical_count),
                ("usage_missing_count", 0),
            ):
                value = call_trace.get(field)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value != expected
                ):
                    return []
            envelope_physical_count += call_physical_count
        for candidate_index, candidate in enumerate(candidates):
            if not isinstance(candidate, Mapping) or candidate.get("ok") is True:
                continue
            stop_reason = str(candidate.get("stop_reason") or "").strip().casefold()
            if stop_reason not in REASONING_ONLY_LENGTH_STOP_REASONS:
                continue
            content = candidate.get("content")
            content_chars = content.get("chars") if isinstance(content, Mapping) else None
            visible_chars = (
                coerce_metric_int(content_chars)
                if isinstance(content_chars, int | float)
                and not isinstance(content_chars, bool)
                else len(
                    str(
                        (
                            content.get("text")
                            if isinstance(content, Mapping)
                            else candidate.get("text")
                        )
                        or ""
                    )
                )
            )
            reasoning_tokens = coerce_metric_int(candidate.get("reasoning_tokens"))
            output_tokens = coerce_metric_int(candidate.get("output_tokens"))
            if reasoning_tokens <= 0:
                for field in (
                    "model_usage_breakdown",
                    "diagnostic_model_usage_breakdown",
                ):
                    breakdown = candidate.get(field)
                    if isinstance(breakdown, list):
                        reasoning_tokens += sum(
                            coerce_metric_int(row.get("reasoning_tokens"))
                            for row in breakdown
                            if isinstance(row, Mapping)
                        )
            if output_tokens <= 0:
                breakdown = candidate.get("model_usage_breakdown")
                if isinstance(breakdown, list):
                    output_tokens = sum(
                        coerce_metric_int(row.get("output_tokens"))
                        for row in breakdown
                        if isinstance(row, Mapping)
                    )
            if (
                visible_chars != 0
                or reasoning_tokens <= 0
                or candidate.get("request_started") is not True
                or isinstance(candidate.get("physical_request_count"), bool)
                or not isinstance(candidate.get("physical_request_count"), int)
                or int(candidate["physical_request_count"]) <= 0
                or (
                    require_coherent_envelope
                    and (
                        candidate.get("usage_reported") is not True
                        or isinstance(candidate.get("usage_missing_count"), bool)
                        or not isinstance(candidate.get("usage_missing_count"), int)
                        or int(candidate["usage_missing_count"]) != 0
                    )
                )
                or str(candidate.get("error") or "").strip()
                or str(candidate.get("error_code") or "").strip()
                or str(candidate.get("text") or "").strip()
                or (
                    isinstance(content, Mapping)
                    and str(content.get("text") or "").strip()
                )
            ):
                continue
            provider = (
                str(
                    candidate.get("requested_provider")
                    or candidate.get("provider")
                    or ""
                )
                .strip()
                .lower()
            )
            model = (
                str(
                    candidate.get("requested_model")
                    or candidate.get("model")
                    or ""
                )
                .strip()
                .lower()
            )
            if not provider or not model:
                continue
            actual_provider = str(
                candidate.get("provider") or provider
            ).strip().lower()
            actual_model = str(
                candidate.get("model") or model
            ).strip().lower()
            raw_candidate_index = candidate.get("index")
            normalized_candidate_index = (
                raw_candidate_index
                if isinstance(raw_candidate_index, int)
                and not isinstance(raw_candidate_index, bool)
                and raw_candidate_index >= 0
                else candidate_index
            )
            failures.append(
                {
                    "identity": f"{provider}:{model}",
                    "reason": "reasoning_only_length",
                    "call_index": call_index,
                    "candidate_index": normalized_candidate_index,
                    "provider": actual_provider,
                    "model": actual_model,
                    "requested_provider": provider,
                    "requested_model": model,
                    "ok": False,
                    "request_started": True,
                    "physical_request_count": coerce_metric_int(
                        candidate.get("physical_request_count")
                    ),
                    "usage_reported": candidate.get("usage_reported") is True,
                    "usage_missing_count": coerce_metric_int(
                        candidate.get("usage_missing_count")
                    ),
                    "stop_reason": stop_reason,
                    "visible_output_chars": visible_chars,
                    "input_tokens": coerce_metric_int(candidate.get("input_tokens")),
                    "output_tokens": output_tokens,
                    "reasoning_tokens": reasoning_tokens,
                    "error": "",
                    "error_code": "",
                }
            )
    if require_coherent_envelope:
        for field, expected in (
            ("physical_request_count", envelope_physical_count),
            ("llm_request_count", envelope_physical_count),
            ("usage_missing_count", 0),
        ):
            value = root_trace.get(field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value != expected
            ):
                return []
    return failures


def deterministic_reasoning_only_length_failures(
    result: RunResult,
    *,
    require_coherent_envelope: bool = True,
) -> list[dict[str, Any]]:
    done = result.done
    root_trace = done.ensemble_trace if done is not None else None
    if not isinstance(root_trace, Mapping) or done is None:
        return []
    failures = _reasoning_only_length_failures_from_trace(
        root_trace,
        require_coherent_envelope=require_coherent_envelope,
    )
    if not failures:
        return []
    if not require_coherent_envelope:
        return failures
    if done.usage_missing_count != 0:
        return []
    call_traces, sequence_reasons = ensemble_call_trace_sequence(root_trace)
    if sequence_reasons:
        return []
    expected_physical_count = sum(
        int(call.get("physical_request_count") or 0)
        for call in call_traces
        if isinstance(call.get("physical_request_count"), int)
        and not isinstance(call.get("physical_request_count"), bool)
    )
    represented_usage_count = sum(
        max(1, coerce_metric_int(row.get("request_count")))
        for row in done.model_usage_breakdown
        if isinstance(row, Mapping)
    )
    if (
        expected_physical_count <= 0
        or represented_usage_count != expected_physical_count
    ):
        return []
    return failures


def g1_retry_plan_provenance_reason(
    initial_plan: Mapping[str, Any],
    retry_plan: Mapping[str, Any],
    *,
    excluded_proposer_identities: set[str],
) -> str:
    from opensquilla.provider.ranking_router import (
        ROUTER_DYNAMIC_RETRY_ROUTING_SCHEMA,
        router_dynamic_task_analysis_reuse_reasons,
    )

    parent_decision_id = str(initial_plan.get("decision_id") or "")
    expected_exclusions = sorted(excluded_proposer_identities)
    retry_routing = retry_plan.get("retry_routing")
    reuse_binding = retry_plan.get("task_analysis_reuse")
    reuse_hash = (
        str(reuse_binding.get("projection_sha256") or "")
        if isinstance(reuse_binding, Mapping)
        else ""
    )
    if (
        not parent_decision_id
        or retry_plan.get("retry_parent_decision_id") != parent_decision_id
        or retry_plan.get("retry_excluded_proposer_identities")
        != expected_exclusions
        or retry_plan.get("task_analysis_reused") is not True
        or not isinstance(retry_routing, Mapping)
        or retry_routing.get("schema")
        != ROUTER_DYNAMIC_RETRY_ROUTING_SCHEMA
        or retry_routing.get("reason")
        != "prior_attempt_reasoning_only_length"
        or retry_routing.get("parent_decision_id") != parent_decision_id
        or retry_routing.get("excluded_proposer_identities")
        != expected_exclusions
        or retry_routing.get("task_analysis_reused") is not True
        or retry_routing.get("task_analysis_source_decision_id")
        != parent_decision_id
        or retry_routing.get("task_analysis_reuse_sha256") != reuse_hash
        or router_dynamic_task_analysis_reuse_reasons(
            initial_plan,
            retry_plan,
        )
    ):
        return "reasoning_only_retry_provenance_invalid"
    return ""


def g1_immutable_selection_plan_payload(
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    from opensquilla.provider.thinking_execution import (
        immutable_selection_plan_payload,
    )

    return immutable_selection_plan_payload(plan)


def g1_execution_plan_mutation_reason(
    expected_plan: Mapping[str, Any],
    observed_plan: Mapping[str, Any],
) -> str:
    from opensquilla.provider.thinking_execution import (
        validate_thinking_execution_plan_mutation,
    )

    return (
        "g1_attempt_thinking_execution_provenance_invalid"
        if validate_thinking_execution_plan_mutation(
            expected_plan,
            observed_plan,
        )
        else ""
    )


def backfill_g1_result_routing_trace_from_ensemble(
    expected_plan: Mapping[str, Any],
    result: RunResult,
) -> bool:
    """Recover a missing top-level plan from matching physical call traces."""

    if not expected_plan:
        return False
    routing = result.routing_trace
    if not isinstance(routing, Mapping) or "selection_plan" in routing:
        return False
    done = result.done
    trace = done.ensemble_trace if done is not None else None
    if not isinstance(trace, Mapping) or not trace:
        return False
    calls, sequence_reasons = ensemble_call_trace_sequence(trace)
    if sequence_reasons or not calls:
        return False
    try:
        expected_hash = canonical_json_sha256(
            g1_immutable_selection_plan_payload(expected_plan)
        )
        observed_plan: dict[str, Any] | None = None
        for call in calls:
            physical_plan = call.get("selection_plan")
            if (
                not isinstance(physical_plan, Mapping)
                or canonical_json_sha256(
                    g1_immutable_selection_plan_payload(physical_plan)
                )
                != expected_hash
            ):
                return False
            if observed_plan is None:
                observed_plan = copy.deepcopy(dict(physical_plan))
        if observed_plan is None:
            return False
        recovered_routing = copy.deepcopy(dict(routing))
    except Exception:  # noqa: BLE001 - contradictory evidence remains unmodified
        return False
    recovered_routing["selection_plan"] = observed_plan
    recovered_routing["selection_plan_backfill"] = {
        "source": "ensemble_call_trace",
        "call_count": len(calls),
    }
    result.routing_trace = recovered_routing
    return True


def g1_attempt_plan_consistency_reason(
    expected_plan: Mapping[str, Any],
    result: RunResult,
) -> str:
    if not expected_plan:
        return "g1_attempt_plan_provenance_invalid"
    expected_hash = canonical_json_sha256(
        g1_immutable_selection_plan_payload(expected_plan)
    )
    routing = result.routing_trace
    if not isinstance(routing, Mapping) or not routing:
        return "g1_attempt_plan_provenance_invalid"
    routed_plan = routing.get("selection_plan")
    if (
        not isinstance(routed_plan, Mapping)
        or canonical_json_sha256(
            g1_immutable_selection_plan_payload(routed_plan)
        )
        != expected_hash
        or g1_execution_plan_mutation_reason(expected_plan, routed_plan)
    ):
        return "g1_attempt_plan_provenance_invalid"
    done = result.done
    trace = done.ensemble_trace if done is not None else None
    explicit_request_count = run_result_error_physical_request_count(result)
    has_physical_request_evidence = (
        done is not None
        or explicit_request_count is not None
        and explicit_request_count > 0
        or explicit_request_count is None
        and bool(result.error or result.final_text)
        and not run_result_was_blocked_before_request(result)
    )
    if has_physical_request_evidence and (
        not isinstance(trace, Mapping) or not trace
    ):
        return "g1_attempt_plan_provenance_invalid"
    if isinstance(trace, Mapping) and trace:
        calls, sequence_reasons = ensemble_call_trace_sequence(trace)
        if sequence_reasons or not calls:
            return "g1_attempt_plan_provenance_invalid"
        previous_physical_plan: Mapping[str, Any] = expected_plan
        for call in calls:
            physical_plan = call.get("selection_plan")
            if (
                not isinstance(physical_plan, Mapping)
                or canonical_json_sha256(
                    g1_immutable_selection_plan_payload(physical_plan)
                )
                != expected_hash
            ):
                return "g1_attempt_plan_provenance_invalid"
            from opensquilla.provider.thinking_execution import (
                validate_thinking_execution_call,
            )

            validated_plan, execution_reason = validate_thinking_execution_call(
                previous_physical_plan,
                call,
            )
            if execution_reason:
                return "g1_attempt_plan_provenance_invalid"
            previous_physical_plan = validated_plan
        if g1_execution_plan_mutation_reason(
            routed_plan,
            previous_physical_plan,
        ):
            return "g1_attempt_plan_provenance_invalid"
    return ""


def _normalized_g1_retry_identities(raw: Any) -> tuple[set[str], bool]:
    if raw is None:
        return set(), True
    if not isinstance(raw, list):
        return set(), False
    normalized = [str(identity or "").strip().lower() for identity in raw]
    valid = bool(
        all(
            identity
            and identity.partition(":")[1] == ":"
            and identity.partition(":")[0]
            and identity.partition(":")[2]
            for identity in normalized
        )
        and len(normalized) == len(set(normalized))
    )
    return set(normalized), valid


def g1_provider_native_recovery_policy_reason(
    plan: Mapping[str, Any] | None,
) -> str:
    """Validate an explicit provider-owned proposer recovery contract."""

    if not isinstance(plan, Mapping):
        return ""
    policy = plan.get("proposer_recovery_policy")
    if policy is None:
        return ""
    if not isinstance(policy, Mapping):
        return "invalid_g1_proposer_recovery_policy"
    expected_policy = formal_proposer_recovery_policy_for_plan(plan)
    if expected_policy is None or dict(policy) != expected_policy:
        return "invalid_g1_proposer_recovery_policy"
    configured_backup_count = expected_policy["configured_backup_count"]
    effective_backup_count = expected_policy["effective_backup_count"]
    backups = plan.get("backup_P")
    selected = plan.get("selected_P")
    aggregator_candidates = plan.get("aggregator_candidates")
    if (
        not isinstance(backups, list)
        or len(backups) != effective_backup_count
        or not isinstance(selected, list)
        or not selected
        or not isinstance(aggregator_candidates, list)
        or not aggregator_candidates
        or any(
            not isinstance(identity, str)
            or not identity
            or identity != identity.strip().casefold()
            or identity.count(":") != 1
            for identity in [
                *selected,
                *backups,
                *aggregator_candidates,
            ]
        )
        or len(set(selected)) != len(selected)
        or len(set(backups)) != len(backups)
        or bool(set(backups).intersection(selected))
        or bool(set(backups).intersection(aggregator_candidates))
        or plan.get("configured_proposer_backup_count") != configured_backup_count
        or plan.get("effective_proposer_backup_count") != effective_backup_count
        or plan.get("effective_min_successful_proposers")
        != expected_policy["quorum_required"]
    ):
        return "invalid_g1_proposer_recovery_roster"
    from opensquilla.provider.protocol import (
        provider_retry_roster_fingerprint,
    )

    fingerprint = provider_retry_roster_fingerprint(plan)
    if (
        len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        return "invalid_g1_proposer_recovery_fingerprint"
    return ""


def g1_provider_native_recovery_enabled(
    plan: Mapping[str, Any] | None,
) -> bool:
    return bool(
        isinstance(plan, Mapping)
        and plan.get("proposer_recovery_policy") is not None
        and not g1_provider_native_recovery_policy_reason(plan)
    )


async def collect_generation_with_retries(
    provider: Any,
    prompt: str,
    *,
    timeout: float,
    config: ChatConfig | None = None,
    tools: list[ToolDefinition] | None = None,
    runner_mode: str = RUNNER_MODE_PROVIDER,
    tool_policy: dict[str, Any] | None = None,
    task_id: str = "",
    group: str = "",
    output_dir: Path | None = None,
    agent_max_iterations: int = DEFAULT_AGENT_MAX_ITERATIONS,
    finalization_policy: Mapping[str, Any] | None = None,
    max_attempts: int = GENERATION_MAX_ATTEMPTS,
    attempt_offset: int = 0,
    attempt_budget_limit: int = GENERATION_MAX_ATTEMPTS,
    retry_backoff_seconds: float = 0.0,
    expected_model: str = "",
    expected_provider: str = "",
    expected_g1_registry_contract: Mapping[str, Any] | None = None,
    expected_task_analyzer_execution_contract: Mapping[str, Any] | None = None,
    paid_attempt_sink: dict[str, Any] | None = None,
) -> tuple[RunResult, list[dict[str, Any]], int]:
    attempts: list[dict[str, Any]] = []
    best_non_empty: RunResult | None = None
    best_non_empty_provider: Any | None = None
    last_result: RunResult | None = None
    last_result_provider: Any | None = None
    initial_provider = provider
    if paid_attempt_sink is not None:
        paid_attempt_sink.clear()
    current_provider = provider
    initial_selection_plan = getattr(initial_provider, "selection_plan", None)
    native_g1_recovery_reason = (
        g1_provider_native_recovery_policy_reason(initial_selection_plan)
        if group == "G1"
        else ""
    )
    provider_native_g1_recovery = bool(
        group == "G1"
        and g1_provider_native_recovery_enabled(initial_selection_plan)
    )
    managed_g1_lifecycle = bool(
        group == "G1"
        and isinstance(initial_selection_plan, Mapping)
        and initial_selection_plan.get(
            "ranking_thinking_assignment_enabled"
        )
        is True
    )
    router_dynamic_g1 = bool(
        group == "G1"
        and isinstance(initial_selection_plan, Mapping)
        and initial_selection_plan.get("strategy") == "router_dynamic"
    )
    raw_retry_provider_factory = (
        getattr(
            provider,
            "_draco_reasoning_only_retry_factory",
            None,
        )
        if (managed_g1_lifecycle or router_dynamic_g1)
        and not provider_native_g1_recovery
        else None
    )
    g1_plan_lifecycle = bool(
        managed_g1_lifecycle
        or (
            router_dynamic_g1
            and callable(raw_retry_provider_factory)
        )
        or provider_native_g1_recovery
    )
    g1_retry_lifecycle = bool(
        g1_plan_lifecycle
        and not provider_native_g1_recovery
    )
    frozen_unmanaged_selection_plan = (
        copy.deepcopy(dict(initial_selection_plan or {}))
        if str((GROUP_SPECS.get(group) or {}).get("kind") or "")
        == "selection_mode"
        and not g1_plan_lifecycle
        else {}
    )

    def remember_selected_provider(selected_provider: Any) -> None:
        if (
            selected_provider is not initial_provider
            or hasattr(initial_provider, "_draco_selected_retry_provider")
        ):
            setattr(
                initial_provider,
                "_draco_selected_retry_provider",
                selected_provider,
            )

    def fail_before_generation_call(
        guard_error: str,
        *,
        attempt_id: str,
        attempt_index: int,
        attempt_started_at: float,
        selection_plan: Mapping[str, Any],
    ) -> tuple[RunResult, list[dict[str, Any]], int]:
        """Fail closed without dropping already-paid provider setup evidence."""

        observed_plan: Mapping[str, Any] | None = None
        observed_plan_error = ""
        try:
            observed_plan = _provider_selection_plan_execution_snapshot(
                current_provider
            )
        except Exception as exc:  # noqa: BLE001 - preserve paid setup on guard failure
            observed_plan_error = type(exc).__name__
        frozen_routing = getattr(
            current_provider,
            "_draco_frozen_routing_trace",
            None,
        )
        raw_setup = getattr(
            current_provider,
            "_draco_setup_metrics",
            None,
        )
        setup_snapshot_error = ""
        raw_setup_usage = (
            raw_setup.get("usage")
            if isinstance(raw_setup, Mapping)
            and isinstance(raw_setup.get("usage"), list)
            else []
        )
        try:
            setup_usage = copy.deepcopy(raw_setup_usage)
        except Exception as exc:  # noqa: BLE001 - use primitive analyzer recovery
            setup_snapshot_error = type(exc).__name__
            setup_usage = conservative_task_analyzer_usage_rows(
                {
                    "attempt_count": len(raw_setup_usage),
                    "physical_attempts": raw_setup_usage,
                },
                provider_id="openrouter",
                model_id=TASK_ANALYZER_MODEL,
                source="g1_pre_call_guard_setup_recovery",
                fallback_reason=type(exc).__name__,
            )
        try:
            safe_selection_plan = json_safe(
                copy.deepcopy(dict(selection_plan))
            )
            if not isinstance(safe_selection_plan, Mapping):
                raise TypeError(
                    "selection plan did not serialize to an object"
                )
            safe_selection_plan = dict(safe_selection_plan)
        except Exception as exc:  # noqa: BLE001 - preserve setup, not raw data
            safe_selection_plan = {
                "capture_failed": True,
                "exception_type": type(exc).__name__,
            }
        safe_observed_plan: dict[str, Any] | None = None
        if isinstance(observed_plan, Mapping):
            try:
                safe_observed_plan = safe_provider_build_routing_trace(
                    observed_plan
                )
            except Exception as exc:  # noqa: BLE001 - diagnostics are secondary
                observed_plan_error = (
                    observed_plan_error or type(exc).__name__
                )
        raw_setup_routing = (
            raw_setup.get("routing")
            if isinstance(raw_setup, Mapping)
            else None
        )
        try:
            routing_trace = (
                safe_provider_build_routing_trace(frozen_routing)
                if isinstance(frozen_routing, Mapping)
                else safe_provider_build_routing_trace(
                    raw_setup_routing
                )
                if isinstance(raw_setup_routing, Mapping)
                else {"selection_plan": safe_selection_plan}
            )
            if not isinstance(routing_trace, dict):
                routing_trace = dict(routing_trace)
        except Exception as exc:  # noqa: BLE001 - preserve paid setup evidence
            routing_trace = {
                "selection_plan": safe_selection_plan,
                "capture_failed": True,
                "exception_type": type(exc).__name__,
            }
        routing_trace["pre_call_guard"] = {
            "error": guard_error,
            "expected_selection_plan": safe_selection_plan,
            "observed_selection_plan": safe_observed_plan,
            "observed_selection_plan_error": observed_plan_error,
            "setup_snapshot_error": setup_snapshot_error,
            "request_started": False,
            "physical_request_count": 0,
        }
        guarded_result = RunResult(
            final_text="",
            done=None,
            error=guard_error,
            setup_latency_ms=coerce_metric_int(
                raw_setup.get("latency_ms")
                if isinstance(raw_setup, Mapping)
                else 0
            ),
            setup_usage=setup_usage,
            routing_trace=routing_trace,
            trace_events=[
                {
                    "seq": 1,
                    "elapsed_ms": coerce_metric_int(
                        raw_setup.get("latency_ms")
                        if isinstance(raw_setup, Mapping)
                        else 0
                    ),
                    "kind": "error",
                    "code": "g1_pre_call_guard_failed",
                    "request_started": False,
                    "physical_request_count": 0,
                }
            ],
        )
        try:
            guard_run_summary = run_result_summary(guarded_result)
        except Exception as exc:  # noqa: BLE001 - setup evidence must survive
            guard_run_summary = emergency_generation_run_summary(
                guarded_result,
                reason=guard_error,
                stage="g1_pre_call_guard_evidence",
                exception_type=type(exc).__name__,
                identity_seed=f"generation-attempt:{attempt_id}",
            )
        attempts.append(
            {
                "attempt_id": attempt_id,
                "attempt_kind": (
                    "provider_build_after_paid_setup"
                    if setup_usage
                    else "generation_pre_call_guard"
                ),
                "attempt": attempt_index,
                "started_at": attempt_started_at,
                "completed_at": time.time(),
                "retryable": False,
                "retry_reason": guard_error,
                "retry_suppressed_reason": guard_error,
                "will_retry": False,
                "retry_backoff_s": 0.0,
                "selection_plan": safe_selection_plan,
                "deterministic_proposer_failures": [],
                "excluded_proposer_identities": sorted(
                    excluded_proposer_identities
                ),
                "run": guard_run_summary,
            }
        )
        if isinstance(raw_setup, Mapping):
            setattr(current_provider, "_draco_setup_metrics", None)
        remember_selected_provider(current_provider)
        return guarded_result, attempts, 0

    retry_provider_factory = (
        raw_retry_provider_factory if g1_retry_lifecycle else None
    )
    raw_prior_exclusions = (
        getattr(
            provider,
            "_draco_prior_excluded_proposer_identities",
            [],
        )
        if g1_retry_lifecycle
        else []
    )
    prior_exclusions, prior_exclusions_valid = _normalized_g1_retry_identities(
        raw_prior_exclusions
    )
    if (
        g1_retry_lifecycle
        and (
            not prior_exclusions_valid
            or raw_prior_exclusions != sorted(prior_exclusions)
        )
    ):
        raise ValueError("frozen G1 prior proposer exclusions are invalid")
    excluded_proposer_identities = (
        set(prior_exclusions) if g1_retry_lifecycle else set()
    )
    frozen_initial_plan = (
        getattr(
            provider,
            "_draco_g1_initial_selection_plan",
            None,
        )
        if g1_retry_lifecycle
        else None
    )
    g1_retry_provenance_parent = (
        copy.deepcopy(dict(frozen_initial_plan))
        if isinstance(frozen_initial_plan, Mapping)
        else copy.deepcopy(
            dict(getattr(initial_provider, "selection_plan", {}) or {})
        )
    )
    g1_thinking_plan_prefixes: dict[str, dict[str, Any]] = {}
    raw_thinking_history = (
        getattr(
            provider,
            "_draco_g1_thinking_execution_history",
            [],
        )
        if managed_g1_lifecycle
        else []
    )
    if (
        managed_g1_lifecycle
        and (
            not isinstance(raw_thinking_history, list)
            or any(not isinstance(plan, Mapping) for plan in raw_thinking_history)
        )
    ):
        raise ValueError("frozen G1 thinking execution history is invalid")
    g1_thinking_execution_history = [
        copy.deepcopy(dict(plan))
        for plan in raw_thinking_history
        if isinstance(plan, Mapping)
    ]
    total_attempt_budget_limit = bounded_generation_attempts(
        attempt_budget_limit
    )
    if (
        not isinstance(attempt_offset, int)
        or isinstance(attempt_offset, bool)
        or not 0 <= attempt_offset < total_attempt_budget_limit
    ):
        raise ValueError(
            "generation attempt offset must be an integer within the configured budget"
        )
    attempt_limit = min(
        bounded_generation_attempts(max_attempts),
        total_attempt_budget_limit - attempt_offset,
    )
    if native_g1_recovery_reason:
        return fail_before_generation_call(
            native_g1_recovery_reason,
            attempt_id=uuid.uuid4().hex,
            attempt_index=attempt_offset + 1,
            attempt_started_at=time.time(),
            selection_plan=(
                initial_selection_plan
                if isinstance(initial_selection_plan, Mapping)
                else {}
            ),
        )
    for local_attempt_index in range(1, attempt_limit + 1):
        attempt_index = attempt_offset + local_attempt_index
        attempt_id = uuid.uuid4().hex
        attempt_started_at = time.time()
        if g1_plan_lifecycle:
            selection_plan_snapshot = getattr(
                current_provider,
                "selection_plan_execution_snapshot",
                None,
            )
            try:
                expected_selection_plan = copy.deepcopy(
                    dict(
                        selection_plan_snapshot()
                        if callable(selection_plan_snapshot)
                        else getattr(current_provider, "selection_plan", {})
                        or {}
                    )
                )
            except Exception as exc:  # noqa: BLE001 - preserve paid setup evidence
                guard_error = (
                    "g1_thinking_execution_pre_call_guard_exception:"
                    "selection_plan_snapshot:"
                    + type(exc).__name__
                )
                return fail_before_generation_call(
                    guard_error,
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    attempt_started_at=attempt_started_at,
                    selection_plan=initial_selection_plan,
                )
            if (
                managed_g1_lifecycle
                and expected_selection_plan.get(
                    "ranking_thinking_assignment_enabled"
                )
                is not True
            ):
                guard_error = "g1_thinking_execution_managed_mode_changed_before_call"
                return fail_before_generation_call(
                    guard_error,
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    attempt_started_at=attempt_started_at,
                    selection_plan=initial_selection_plan,
                )
            adaptive_g1 = g1_retry_lifecycle
        else:
            expected_selection_plan = copy.deepcopy(
                frozen_unmanaged_selection_plan
            )
            adaptive_g1 = False
        if managed_g1_lifecycle:
            from opensquilla.provider.thinking_execution import (
                THINKING_PHYSICAL_EVIDENCE_SCHEMA,
                validate_thinking_execution_history_closure,
            )

            try:
                (
                    closed_execution_plan,
                    _,
                    closure_reason,
                ) = validate_thinking_execution_history_closure(
                    g1_thinking_execution_history,
                    expected_selection_plan,
                )
                expected_selection_plan = copy.deepcopy(
                    closed_execution_plan
                )
            except Exception as exc:  # noqa: BLE001 - preserve paid setup evidence
                guard_error = (
                    "g1_thinking_execution_pre_call_guard_exception:"
                    "history_closure:"
                    + type(exc).__name__
                )
                return fail_before_generation_call(
                    guard_error,
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    attempt_started_at=attempt_started_at,
                    selection_plan=expected_selection_plan,
                )
            if closure_reason:
                guard_error = (
                    "g1_thinking_execution_pre_call_guard_failed:"
                    + closure_reason
                )
                return fail_before_generation_call(
                    guard_error,
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    attempt_started_at=attempt_started_at,
                    selection_plan=expected_selection_plan,
                )
        strict_g1_physical_binding_required = bool(
            provider_native_g1_recovery
            or (
                managed_g1_lifecycle
                and expected_selection_plan.get(
                    "thinking_physical_evidence_schema"
                )
                == THINKING_PHYSICAL_EVIDENCE_SCHEMA
            )
        )
        decision_id = str(expected_selection_plan.get("decision_id") or "")
        cached_execution_plan = g1_thinking_plan_prefixes.get(decision_id)
        if managed_g1_lifecycle and cached_execution_plan is not None:
            try:
                cached_immutable_hash = canonical_json_sha256(
                    g1_immutable_selection_plan_payload(cached_execution_plan)
                )
                observed_immutable_hash = canonical_json_sha256(
                    g1_immutable_selection_plan_payload(
                        expected_selection_plan
                    )
                )
            except Exception as exc:  # noqa: BLE001 - preserve paid setup evidence
                guard_error = (
                    "g1_thinking_execution_pre_call_guard_exception:"
                    "immutable_hash:"
                    + type(exc).__name__
                )
                return fail_before_generation_call(
                    guard_error,
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    attempt_started_at=attempt_started_at,
                    selection_plan=expected_selection_plan,
                )
            if cached_immutable_hash != observed_immutable_hash:
                guard_error = "g1_thinking_execution_immutable_plan_changed_before_retry"
                return fail_before_generation_call(
                    guard_error,
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    attempt_started_at=attempt_started_at,
                    selection_plan=expected_selection_plan,
                )
            if (
                expected_selection_plan.get("executed_thinking_assignment")
                != cached_execution_plan.get("executed_thinking_assignment")
                or expected_selection_plan.get(
                    "thinking_execution_fallbacks",
                    [],
                )
                != cached_execution_plan.get("thinking_execution_fallbacks", [])
            ):
                guard_error = "g1_thinking_execution_state_reset_before_retry"
                return fail_before_generation_call(
                    guard_error,
                    attempt_id=attempt_id,
                    attempt_index=attempt_index,
                    attempt_started_at=attempt_started_at,
                    selection_plan=expected_selection_plan,
                )
            expected_selection_plan = copy.deepcopy(cached_execution_plan)
        if runner_mode == RUNNER_MODE_AGENT_LOOP:
            result = await collect_agent_run(
                current_provider,
                prompt,
                timeout=timeout,
                config=config,
                tools=tools,
                tool_policy=tool_policy or {},
                task_id=task_id,
                group=group,
                output_dir=output_dir,
                max_iterations=agent_max_iterations,
                finalization_policy=finalization_policy,
            )
        else:
            result = await collect_run(
                current_provider,
                prompt,
                timeout=timeout,
                config=config,
                tools=tools,
            )
        if paid_attempt_sink is not None:
            paid_attempt_sink.update(
                {
                    "result": result,
                    "attempts": attempts,
                    "attempt_id": attempt_id,
                    "attempt_index": attempt_index,
                    "attempt_started_at": attempt_started_at,
                    "selection_plan": expected_selection_plan,
                    "adaptive_g1": adaptive_g1,
                    "provider_native_g1_recovery": (
                        provider_native_g1_recovery
                    ),
                    "excluded_proposer_identities": tuple(
                        sorted(excluded_proposer_identities)
                    ),
                    "expected_provider": expected_provider,
                    "expected_model": expected_model,
                    "current_provider": current_provider,
                    "stage": "paid_call_returned",
                }
            )
        last_result = result
        last_result_provider = current_provider
        group_spec = GROUP_SPECS.get(group) or {}
        expected_selection_mode = (
            str(group_spec.get("selection_mode") or "")
            if group_spec.get("kind") == "selection_mode"
            else ""
        )
        if paid_attempt_sink is not None:
            paid_attempt_sink["stage"] = "requested_identity_backfill"
        backfill_result_requested_identity(
            result,
            expected_model=expected_model,
            expected_provider=expected_provider,
            expected_selection_plan=expected_selection_plan,
        )
        if adaptive_g1 or provider_native_g1_recovery:
            backfill_g1_result_routing_trace_from_ensemble(
                expected_selection_plan,
                result,
            )
        if paid_attempt_sink is not None:
            paid_attempt_sink["stage"] = "generation_retry_reason"
        reason = generation_retry_reason(
            result,
            expected_selection_mode=expected_selection_mode,
            expected_selection_plan=expected_selection_plan,
            expected_g1_registry_contract=expected_g1_registry_contract,
            expected_task_analyzer_execution_contract=(
                expected_task_analyzer_execution_contract
            ),
            expected_model=expected_model,
            expected_provider=expected_provider,
        )
        mark_retryable_generation_error(result, reason)
        retry_suppressed_reason = generation_cleanup_failure_reason(result)
        if (
            provider_native_g1_recovery
            and reason
            and provider_native_proposer_retry_reason(reason)
            and not retry_suppressed_reason
        ):
            retry_suppressed_reason = (
                "provider_native_proposer_recovery_terminal"
            )
        if (
            not retry_suppressed_reason
            and runner_mode == RUNNER_MODE_AGENT_LOOP
            and is_agent_hard_timeout(result)
        ):
            retry_suppressed_reason = "agent_hard_timeout"
        if paid_attempt_sink is not None:
            paid_attempt_sink["stage"] = "g1_attempt_plan_consistency"
        consistency_reason = (
            g1_attempt_plan_consistency_reason(
                expected_selection_plan,
                result,
            )
            if adaptive_g1 or provider_native_g1_recovery
            else ""
        )
        if consistency_reason:
            reason = consistency_reason
            retry_suppressed_reason = consistency_reason
            mark_retryable_generation_error(result, consistency_reason)
        if (
            not consistency_reason
            and result.final_text.strip()
            and best_non_empty is None
        ):
            best_non_empty = result
            best_non_empty_provider = current_provider
        if paid_attempt_sink is not None:
            paid_attempt_sink["stage"] = "deterministic_failure_extraction"
        deterministic_failures = (
            deterministic_reasoning_only_length_failures(
                result,
                require_coherent_envelope=False,
            )
            if adaptive_g1
            else []
        )
        will_retry = (
            bool(reason) and not retry_suppressed_reason and local_attempt_index < attempt_limit
        )
        prepare_deferred_g1_retry = bool(
            adaptive_g1
            and reason
            and not retry_suppressed_reason
            and deterministic_failures
            and attempt_index < total_attempt_budget_limit
            and not will_retry
        )
        if paid_attempt_sink is not None:
            paid_attempt_sink["stage"] = "run_result_summary"
        attempt_run_summary = run_result_summary(
            result,
            include_ensemble_trace=(
                adaptive_g1 or provider_native_g1_recovery
            ),
        )
        if strict_g1_physical_binding_required:
            if paid_attempt_sink is not None:
                paid_attempt_sink["stage"] = "g1_physical_usage_binding"
            physical_binding_reasons = (
                g1_retry_physical_usage_binding_reasons(
                    attempt_run_summary,
                    identity_seed=f"generation-attempt:{attempt_id}",
                )
            )
            if physical_binding_reasons:
                physical_binding_failure = (
                    "g1_physical_usage_binding_failed:"
                    + physical_binding_reasons[0]
                )
                if not reason:
                    reason = physical_binding_failure
                    mark_retryable_generation_error(
                        result,
                        physical_binding_failure,
                    )
                retry_suppressed_reason = physical_binding_failure
                will_retry = False
                prepare_deferred_g1_retry = False
        if (
            managed_g1_lifecycle
            and (will_retry or prepare_deferred_g1_retry)
        ):
            execute_g1_retry_this_wave = will_retry
            if retry_suppressed_reason:
                will_retry = False
                prepare_deferred_g1_retry = False
            else:
                will_retry = execute_g1_retry_this_wave
            if (
                (will_retry or prepare_deferred_g1_retry)
                and result.done is not None
            ):
                trace = result.done.ensemble_trace
                calls, call_reasons = ensemble_call_trace_sequence(
                    trace if isinstance(trace, Mapping) else {}
                )
                if not call_reasons and calls:
                    for call in calls:
                        physical_plan = call.get("selection_plan")
                        if isinstance(physical_plan, Mapping):
                            g1_thinking_execution_history.append(
                                copy.deepcopy(dict(physical_plan))
                            )
                    last_plan = calls[-1].get("selection_plan")
                    if isinstance(last_plan, Mapping) and decision_id:
                        g1_thinking_plan_prefixes[decision_id] = copy.deepcopy(
                            dict(last_plan)
                        )
        retry_backoff_s = (
            bounded_generation_retry_backoff(retry_backoff_seconds) * (2 ** (attempt_index - 1))
            if will_retry
            else 0.0
        )
        if paid_attempt_sink is not None:
            paid_attempt_sink["stage"] = "attempt_serialization"
        attempt_record = {
            "attempt_id": attempt_id,
            "attempt_kind": "generation",
            "attempt": attempt_index,
            "started_at": attempt_started_at,
            "completed_at": time.time(),
            "retryable": bool(reason),
            "retry_reason": reason,
            "retry_suppressed_reason": retry_suppressed_reason,
            "will_retry": will_retry,
            "retry_backoff_s": retry_backoff_s,
            "run": attempt_run_summary,
        }
        if provider_native_g1_recovery:
            attempt_record["proposer_recovery_owner"] = "provider"
        if adaptive_g1 or provider_native_g1_recovery:
            attempt_record.update(
                {
                    "selection_plan": json_safe(expected_selection_plan),
                    "deterministic_proposer_failures": (
                        deterministic_failures
                        if adaptive_g1
                        else []
                    ),
                    "excluded_proposer_identities": sorted(
                        excluded_proposer_identities
                    ),
                }
            )
        attempts.append(attempt_record)
        if paid_attempt_sink is not None:
            paid_attempt_sink["stage"] = "attempt_committed"
            paid_attempt_sink["attempt_appended"] = True
        if not reason:
            remember_selected_provider(current_provider)
            return result, attempts, attempt_index
        if retry_suppressed_reason:
            remember_selected_provider(current_provider)
            return result, attempts, 0
        if (
            adaptive_g1
            and deterministic_failures
            and (will_retry or prepare_deferred_g1_retry)
        ):
            excluded_proposer_identities.update(
                str(failure["identity"]) for failure in deterministic_failures
            )
            if not callable(retry_provider_factory):
                attempts[-1]["will_retry"] = False
                attempts[-1]["retry_suppressed_reason"] = (
                    "reasoning_only_retry_factory_unavailable"
                )
                remember_selected_provider(current_provider)
                return result, attempts, 0
            if paid_attempt_sink is not None:
                paid_attempt_sink["stage"] = "retry_provider_build"
            try:
                retry_provider = retry_provider_factory(
                    sorted(excluded_proposer_identities)
                )
            except Exception as exc:  # noqa: BLE001 - fail closed before another paid call
                attempts[-1]["will_retry"] = False
                attempts[-1]["retry_suppressed_reason"] = (
                    f"reasoning_only_retry_provider_build_failed:{type(exc).__name__}"
                )
                remember_selected_provider(current_provider)
                return result, attempts, 0
            if paid_attempt_sink is not None:
                paid_attempt_sink["stage"] = "retry_plan_validation"
            retry_plan = dict(getattr(retry_provider, "selection_plan", {}) or {})
            provenance_reason = g1_retry_plan_provenance_reason(
                g1_retry_provenance_parent,
                retry_plan,
                excluded_proposer_identities=excluded_proposer_identities,
            )
            if provenance_reason:
                attempts[-1]["will_retry"] = False
                attempts[-1]["retry_suppressed_reason"] = provenance_reason
                remember_selected_provider(current_provider)
                return result, attempts, 0
            prior_selected = list(expected_selection_plan.get("selected_P") or [])
            retry_selected = list(retry_plan.get("selected_P") or [])
            if (
                not retry_selected
                or retry_selected == prior_selected
                or any(
                    str(identity).strip().lower() in excluded_proposer_identities
                    for identity in retry_selected
                )
            ):
                attempts[-1]["will_retry"] = False
                attempts[-1]["retry_suppressed_reason"] = (
                    "reasoning_only_retry_roster_unchanged"
                )
                remember_selected_provider(current_provider)
                return result, attempts, 0
            expected_quorum = legal_proposer_quorum(len(retry_selected))
            if (
                coerce_metric_int(
                    getattr(retry_provider, "min_successful_proposers", 0)
                )
                != expected_quorum
            ):
                attempts[-1]["will_retry"] = False
                attempts[-1]["retry_suppressed_reason"] = (
                    "reasoning_only_retry_quorum_changed"
                )
                remember_selected_provider(current_provider)
                return result, attempts, 0
            if paid_attempt_sink is not None:
                paid_attempt_sink["stage"] = "retry_execution_projection"
            retry_snapshot = getattr(
                retry_provider,
                "selection_plan_execution_snapshot",
                None,
            )
            retry_target_plan = copy.deepcopy(
                dict(
                    retry_snapshot()
                    if callable(retry_snapshot)
                    else retry_plan
                )
            )
            if managed_g1_lifecycle:
                from opensquilla.provider.thinking_execution import (
                    project_thinking_execution_history,
                    restore_projected_thinking_execution,
                )

                projected_plan, projection_audit, projection_reason = (
                    project_thinking_execution_history(
                        g1_thinking_execution_history,
                        retry_target_plan,
                    )
                )
                if projection_reason:
                    attempts[-1]["will_retry"] = False
                    attempts[-1]["retry_suppressed_reason"] = (
                        "g1_thinking_execution_projection_failed:"
                        + projection_reason
                    )
                    remember_selected_provider(current_provider)
                    return result, attempts, 0
                if (
                    retry_target_plan.get("executed_thinking_assignment")
                    != projected_plan.get("executed_thinking_assignment")
                    or retry_target_plan.get(
                        "thinking_execution_fallbacks",
                        [],
                    )
                    != projected_plan.get(
                        "thinking_execution_fallbacks",
                        [],
                    )
                ):
                    try:
                        restore_projected_thinking_execution(
                            retry_provider,
                            target_plan=retry_target_plan,
                            projected_plan=projected_plan,
                        )
                    except Exception as exc:  # noqa: BLE001 - fail closed before another paid call
                        attempts[-1]["will_retry"] = False
                        attempts[-1]["retry_suppressed_reason"] = (
                            "g1_thinking_execution_projection_restore_failed:"
                            + type(exc).__name__
                        )
                        remember_selected_provider(current_provider)
                        return result, attempts, 0
                retry_plan = copy.deepcopy(
                    dict(
                        retry_snapshot()
                        if callable(retry_snapshot)
                        else projected_plan
                    )
                )
                attempts[-1]["thinking_execution_projection"] = json_safe(
                    projection_audit
                )
            else:
                retry_plan = retry_target_plan
            attempts[-1]["retry_selection_plan"] = json_safe(retry_plan)
            attempts[-1]["retry_excluded_proposer_identities"] = sorted(
                excluded_proposer_identities
            )
            if prepare_deferred_g1_retry:
                attempts[-1]["retry_deferred_to_next_wave"] = True
            else:
                current_provider = retry_provider
                setattr(
                    initial_provider,
                    "_draco_selected_retry_provider",
                    retry_provider,
                )
        if retry_backoff_s > 0:
            await asyncio.sleep(retry_backoff_s)
    selected = (
        best_non_empty
        or last_result
        or RunResult(
            final_text="",
            done=None,
            error=GENERATION_MISSING_DONE_ERROR,
        )
    )
    selected_provider = (
        best_non_empty_provider
        if best_non_empty is not None
        else last_result_provider
        if last_result is not None
        else initial_provider
    )
    remember_selected_provider(selected_provider)
    mark_empty_generation_output(selected)
    return selected, attempts, 0


def sum_generation_attempt_metric(attempts: list[dict[str, Any]], key: str) -> int:
    total = 0
    for attempt in attempts:
        run = attempt.get("run")
        if isinstance(run, dict):
            total += coerce_metric_int(run.get(key))
    return total


def sum_generation_attempt_server_tools(attempts: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for attempt in attempts:
        run = attempt.get("run")
        if not isinstance(run, dict):
            continue
        server_tool_use = run.get("server_tool_use")
        if isinstance(server_tool_use, dict):
            add_metric_counts(counts, server_tool_use)
    return counts


def sum_generation_attempt_billed_cost(attempts: list[dict[str, Any]]) -> float:
    total = 0.0
    for attempt in attempts:
        run = attempt.get("run")
        if not isinstance(run, dict):
            continue
        usage = run.get("usage")
        if not isinstance(usage, dict):
            continue
        billed_cost = usage.get("billed_cost")
        if isinstance(billed_cost, int | float):
            total += float(billed_cost)
    return total


def bounded_judge_attempts(value: int | None) -> int:
    try:
        attempts = JUDGE_MAX_ATTEMPTS if value is None else int(value)
    except (TypeError, ValueError):
        attempts = JUDGE_MAX_ATTEMPTS
    return max(1, min(JUDGE_MAX_ATTEMPTS, attempts))


def validated_prior_judge_attempts(
    judgment: Mapping[str, Any],
    *,
    max_attempts: int,
    unit_label: str,
) -> list[dict[str, Any]]:
    """Validate cumulative physical Judge evidence before spending more."""

    attempts = judgment.get("judge_attempts")
    if not isinstance(attempts, list):
        raise ValueError(f"{unit_label} lacks cumulative Judge attempt evidence")
    if len(attempts) > max_attempts:
        raise ValueError(f"{unit_label} exceeds the cumulative Judge attempt budget")
    declared_count = judgment.get("judge_attempt_count")
    if (
        not isinstance(declared_count, int)
        or isinstance(declared_count, bool)
        or declared_count != len(attempts)
    ):
        raise ValueError(f"{unit_label} has contradictory Judge attempt evidence")
    copied: list[dict[str, Any]] = []
    for expected_ordinal, attempt in enumerate(attempts, start=1):
        if not isinstance(attempt, Mapping):
            raise ValueError(f"{unit_label} has invalid Judge attempt evidence")
        ordinal = attempt.get("attempt")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal != expected_ordinal:
            raise ValueError(f"{unit_label} has non-cumulative Judge attempt ordinals")
        attempt_id = attempt.get("attempt_id")
        if (
            not isinstance(attempt_id, str)
            or len(attempt_id) != 32
            or any(char not in "0123456789abcdef" for char in attempt_id)
        ):
            raise ValueError(f"{unit_label} has an invalid Judge attempt identity")
        if not isinstance(attempt.get("run"), Mapping):
            raise ValueError(f"{unit_label} lacks a physical Judge run")
        copied.append(copy.deepcopy(dict(attempt)))
    return copied


def judge_attempt_budget_fields(
    *,
    attempts: list[dict[str, Any]],
    prior_attempts_used: int,
    max_attempts: int,
    new_attempt_count: int,
    exhausted: bool,
) -> dict[str, Any]:
    used = len(attempts)
    return {
        "judge_attempt_evidence_schema": JUDGE_ATTEMPT_EVIDENCE_SCHEMA,
        "judge_attempt_budget_scope": JUDGE_ATTEMPT_BUDGET_SCOPE,
        "judge_attempt_budget_limit": max_attempts,
        "prior_judge_attempts_used": prior_attempts_used,
        "judge_attempt_count": used,
        "judge_attempt_budget_used": used,
        "judge_attempt_budget_remaining": max(0, max_attempts - used),
        "judge_new_attempt_count": new_attempt_count,
        "judge_attempt_budget_exhausted": exhausted,
    }


def indexed_prior_criterion_judgments(
    prior_judge: Mapping[str, Any] | None,
    *,
    rubric: str,
    criteria: list[dict[str, Any]],
    repeats: int,
    max_attempts: int,
) -> dict[tuple[str, int], dict[str, Any]]:
    """Bind prior Judge attempts to their immutable criterion/repeat unit."""

    if prior_judge is None:
        return {}
    if (
        prior_judge.get("judge_attempt_evidence_schema") != JUDGE_ATTEMPT_EVIDENCE_SCHEMA
        or prior_judge.get("judge_attempt_budget_scope") != JUDGE_ATTEMPT_BUDGET_SCOPE
        or prior_judge.get("judge_attempt_budget_limit_per_unit") != max_attempts
    ):
        raise ValueError("prior Judge result lacks the formal cumulative budget contract")
    if prior_judge.get("prior_judge_attempts"):
        raise ValueError(
            "legacy flat prior Judge attempts cannot prove a cumulative per-unit budget"
        )
    if (
        prior_judge.get("mode") != "draco_criterion_judgments"
        or str(prior_judge.get("rubric_id") or "") != rubric
        or coerce_metric_int(prior_judge.get("judge_repeats")) != repeats
    ):
        raise ValueError("prior Judge result is not bound to the current rubric contract")
    raw_judgments = prior_judge.get("criterion_judgments")
    expected_keys = {
        (str(criterion.get("id") or ""), repeat_index)
        for repeat_index in range(repeats)
        for criterion in criteria
    }
    if not isinstance(raw_judgments, list) or len(raw_judgments) != len(expected_keys):
        raise ValueError("prior Judge result does not cover every criterion/repeat unit")
    indexed: dict[tuple[str, int], dict[str, Any]] = {}
    for raw in raw_judgments:
        if not isinstance(raw, Mapping):
            raise ValueError("prior Judge criterion evidence is invalid")
        key = (
            str(raw.get("id") or ""),
            coerce_metric_int(raw.get("repeat_index")),
        )
        if key not in expected_keys or key in indexed:
            raise ValueError("prior Judge criterion/repeat binding is invalid or duplicated")
        copied = copy.deepcopy(dict(raw))
        copied["judge_attempts"] = validated_prior_judge_attempts(
            raw,
            max_attempts=max_attempts,
            unit_label=f"Judge unit {key[0]}/{key[1]}",
        )
        indexed[key] = copied
    return indexed


def closed_judge_criterion(
    *,
    criterion: dict[str, Any],
    repeat_index: int,
    prior_judgment: Mapping[str, Any],
    attempts: list[dict[str, Any]],
    max_attempts: int,
) -> dict[str, Any]:
    """Return an explicit no-call terminal row for an exhausted Judge unit."""

    prior_used = len(attempts)
    return {
        **criterion,
        "repeat_index": repeat_index,
        "verdict": str(prior_judgment.get("verdict") or ""),
        "met": None,
        "rationale": str(prior_judgment.get("rationale") or "")[:1000],
        "raw": str(prior_judgment.get("raw") or "")[:1000],
        "error": JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR,
        "judge_attempts": attempts,
        **judge_attempt_budget_fields(
            attempts=attempts,
            prior_attempts_used=prior_used,
            max_attempts=max_attempts,
            new_attempt_count=0,
            exhausted=True,
        ),
    }


async def judge_text(
    *,
    judge_provider: Any | None,
    task: dict[str, Any],
    answer: str,
    dry_run: bool,
    judge_repeats: int = 1,
    judge_concurrency: int = 1,
    judge_max_attempts: int = JUDGE_MAX_ATTEMPTS,
    judge_semaphore: asyncio.Semaphore | None = None,
    prior_judge: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not answer.strip():
        return None
    criteria = rubric_criteria(task)
    if dry_run:
        if criteria:
            repeats = max(1, int(judge_repeats or 1))
            judgments = [
                {
                    **criterion,
                    "repeat_index": repeat_index,
                    "verdict": "UNMET" if criterion["weight"] < 0 else "MET",
                    "met": criterion["weight"] >= 0,
                    "rationale": "dry-run heuristic",
                }
                for repeat_index in range(repeats)
                for criterion in criteria
            ]
            return score_criterion_judgments(
                rubric_id=rubric_id(task),
                judgments=judgments,
                judge_model="dry-run",
                judge_repeats=repeats,
            )
        score = min(20, max(4, len(answer) // 40))
        dry_judge = normalize_legacy_judge_result(
            {
                "mode": "legacy_dimension_score",
                "scores": {
                    "accuracy": score // 4,
                    "completeness": score // 4,
                    "objectivity": score // 4,
                    "citation": score // 4,
                },
                "total": score,
                "rationale": "dry-run heuristic",
            }
        )
        dry_judge["judge_model"] = "dry-run"
        dry_judge["judge_cost_exempt"] = True
        return dry_judge
    if judge_provider is None:
        return None
    max_attempts = bounded_judge_attempts(judge_max_attempts)
    # One experiment-wide semaphore covers both structured rubric judging and
    # the legacy single-call Judge path.  Otherwise task concurrency can
    # silently multiply the configured Judge concurrency.
    semaphore = judge_semaphore or asyncio.Semaphore(max(1, int(judge_concurrency or 1)))
    if criteria:
        repeats = max(1, int(judge_repeats or 1))
        prior_index = indexed_prior_criterion_judgments(
            prior_judge,
            rubric=rubric_id(task),
            criteria=criteria,
            repeats=repeats,
            max_attempts=max_attempts,
        )

        async def _guarded_judge(
            criterion: dict[str, Any],
            repeat_index: int,
        ) -> dict[str, Any]:
            key = (str(criterion.get("id") or ""), repeat_index)
            prior_judgment = prior_index.get(key)
            prior_attempts = (
                validated_prior_judge_attempts(
                    prior_judgment,
                    max_attempts=max_attempts,
                    unit_label=f"Judge unit {key[0]}/{key[1]}",
                )
                if prior_judgment is not None
                else []
            )
            if (
                prior_judgment is not None
                and isinstance(prior_judgment.get("met"), bool)
                and not prior_judgment.get("error")
            ):
                reused = copy.deepcopy(prior_judgment)
                reused.update(
                    judge_attempt_budget_fields(
                        attempts=prior_attempts,
                        prior_attempts_used=len(prior_attempts),
                        max_attempts=max_attempts,
                        new_attempt_count=0,
                        exhausted=False,
                    )
                )
                return reused
            if len(prior_attempts) >= max_attempts:
                return closed_judge_criterion(
                    criterion=criterion,
                    repeat_index=repeat_index,
                    prior_judgment=prior_judgment or {},
                    attempts=prior_attempts,
                    max_attempts=max_attempts,
                )
            async with semaphore:
                return await judge_criterion(
                    judge_provider=judge_provider,
                    task=task,
                    answer=answer,
                    criterion=criterion,
                    repeat_index=repeat_index,
                    max_attempts=max_attempts,
                    prior_attempts=prior_attempts,
                )

        judgments = await asyncio.gather(
            *[
                _guarded_judge(criterion, repeat_index)
                for repeat_index in range(repeats)
                for criterion in criteria
            ]
        )
        result = score_criterion_judgments(
            rubric_id=rubric_id(task),
            judgments=judgments,
            judge_model=str(getattr(judge_provider, "model", "") or ""),
            judge_repeats=repeats,
        )
        result.update(
            {
                "judge_attempt_evidence_schema": JUDGE_ATTEMPT_EVIDENCE_SCHEMA,
                "judge_attempt_budget_scope": JUDGE_ATTEMPT_BUDGET_SCOPE,
                "judge_attempt_budget_limit_per_unit": max_attempts,
                "judge_attempt_count": sum(
                    coerce_metric_int(item.get("judge_attempt_count")) for item in judgments
                ),
                "judge_new_attempt_count": sum(
                    coerce_metric_int(item.get("judge_new_attempt_count")) for item in judgments
                ),
                "judge_attempt_budget_exhausted_count": sum(
                    item.get("judge_attempt_budget_exhausted") is True for item in judgments
                ),
            }
        )
        result["judge_attempt_budget_exhausted"] = bool(
            result["judge_attempt_budget_exhausted_count"]
        )
        return result
    rubric = task.get("rubric") or "accuracy, completeness, objectivity, citation quality"
    reference = task.get("reference") or ""
    prompt = (
        "Judge this DRACO answer. Return only JSON with keys scores, total, rationale. "
        "scores must contain accuracy, completeness, objectivity, citation, each 1-5.\n\n"
        f"Task:\n{task['prompt']}\n\n"
        f"Rubric:\n{rubric}\n\n"
        f"Reference:\n{reference}\n\n"
        f"Answer:\n{answer}"
    )
    if prior_judge is not None:
        if (
            prior_judge.get("judge_attempt_evidence_schema") != JUDGE_ATTEMPT_EVIDENCE_SCHEMA
            or prior_judge.get("judge_attempt_budget_scope") != JUDGE_ATTEMPT_BUDGET_SCOPE
            or prior_judge.get("judge_attempt_budget_limit") != max_attempts
        ):
            raise ValueError(
                "prior legacy Judge result lacks the formal cumulative budget contract"
            )
        if prior_judge.get("prior_judge_attempts"):
            raise ValueError(
                "legacy flat prior Judge attempts cannot prove a cumulative Judge budget"
            )
    attempts = (
        validated_prior_judge_attempts(
            prior_judge,
            max_attempts=max_attempts,
            unit_label="legacy Judge unit",
        )
        if prior_judge is not None
        else []
    )
    prior_attempts_used = len(attempts)
    if prior_attempts_used >= max_attempts:
        return {
            **copy.deepcopy(dict(prior_judge or {})),
            "mode": "legacy_dimension_score",
            "score_status": "incomplete",
            "judge_error_count": 1,
            "normalized_score": None,
            "total": None,
            "error": JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR,
            "judge_attempts": attempts,
            **judge_attempt_budget_fields(
                attempts=attempts,
                prior_attempts_used=prior_attempts_used,
                max_attempts=max_attempts,
                new_attempt_count=0,
                exhausted=True,
            ),
        }
    last_result: RunResult | None = None
    last_cleanup_failure = ""
    for attempt_index in range(prior_attempts_used + 1, max_attempts + 1):
        async with semaphore:
            result = await collect_run(
                judge_provider,
                prompt,
                timeout=120.0,
                config=ChatConfig(temperature=0.0, thinking=False),
            )
        last_result = result
        cleanup_failure = generation_cleanup_failure_reason(result)
        if cleanup_failure:
            last_cleanup_failure = cleanup_failure
        parsed = extract_json_object(result.final_text)
        attempts.append(
            {
                "attempt_id": uuid.uuid4().hex,
                "attempt": attempt_index,
                "parsed": parsed is not None,
                "schema_valid": False,
                "retry_suppressed_reason": cleanup_failure,
                "run": judge_run_result_summary(
                    result,
                    judge_provider=judge_provider,
                ),
            }
        )
        if cleanup_failure:
            break
        if parsed is not None:
            normalized = normalize_legacy_judge_result(parsed)
            attempts[-1]["schema_valid"] = normalized.get("score_status") == "complete"
            if (
                normalized.get("score_status") == "complete"
                and not result.error
            ):
                normalized["judge_run"] = judge_run_result_summary(
                    result,
                    judge_provider=judge_provider,
                )
                normalized["judge_attempts"] = attempts
                normalized.update(
                    judge_attempt_budget_fields(
                        attempts=attempts,
                        prior_attempts_used=prior_attempts_used,
                        max_attempts=max_attempts,
                        new_attempt_count=len(attempts) - prior_attempts_used,
                        exhausted=False,
                    )
                )
                return normalized
    last_text = last_result.final_text if last_result is not None else ""
    parsed_any = any(bool(attempt.get("parsed")) for attempt in attempts)
    return {
        "mode": "legacy_dimension_score",
        "score_status": "incomplete",
        "judge_error_count": 1,
        "normalized_score": None,
        "total": None,
        "error": (
            last_cleanup_failure
            or (last_result.error if last_result is not None else "")
            or ("judge_json_schema_invalid" if parsed_any else "judge_json_parse_failed")
        ),
        "raw": last_text[:2000],
        "judge_run": (
            judge_run_result_summary(
                last_result,
                judge_provider=judge_provider,
            )
            if last_result is not None
            else {}
        ),
        "judge_attempts": attempts,
        **judge_attempt_budget_fields(
            attempts=attempts,
            prior_attempts_used=prior_attempts_used,
            max_attempts=max_attempts,
            new_attempt_count=len(attempts) - prior_attempts_used,
            exhausted=len(attempts) >= max_attempts,
        ),
    }


def rubric_id(task: dict[str, Any]) -> str:
    rubric = parse_maybe_json(task.get("rubric"))
    if isinstance(rubric, dict):
        return str(rubric.get("id") or task.get("id") or "")
    return str(task.get("id") or "")


async def judge_criterion(
    *,
    judge_provider: Any,
    task: dict[str, Any],
    answer: str,
    criterion: dict[str, Any],
    repeat_index: int = 0,
    max_attempts: int = JUDGE_MAX_ATTEMPTS,
    prior_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    weight = coerce_weight(criterion.get("weight"))
    criterion_type = "negative" if weight < 0 else "positive"
    prompt = (
        "You are grading a DRACO deep research answer against one rubric criterion.\n"
        "Return only JSON with keys verdict and rationale. verdict must be MET or UNMET.\n"
        "Positive criteria describe desired content. Negative criteria describe an error; "
        "for negative criteria, MET means the answer contains that error.\n\n"
        f"Original query:\n{task['prompt']}\n\n"
        f"Answer:\n{answer}\n\n"
        "Criterion:\n"
        f"- id: {criterion.get('id')}\n"
        f"- section: {criterion.get('section_title') or criterion.get('section_id')}\n"
        f"- type: {criterion_type}\n"
        f"- weight: {weight}\n"
        f"- requirement: {criterion.get('requirement')}\n"
    )
    attempt_limit = bounded_judge_attempts(max_attempts)
    attempts = copy.deepcopy(list(prior_attempts or []))
    prior_attempts_used = len(attempts)
    if prior_attempts_used >= attempt_limit:
        return closed_judge_criterion(
            criterion=criterion,
            repeat_index=repeat_index,
            prior_judgment={},
            attempts=attempts,
            max_attempts=attempt_limit,
        )
    last_row: dict[str, Any] | None = None
    for attempt_index in range(prior_attempts_used + 1, attempt_limit + 1):
        result = await collect_run(
            judge_provider,
            prompt,
            timeout=120.0,
            config=ChatConfig(temperature=0.0, thinking=False),
        )
        parsed = extract_json_object(result.final_text) or {}
        met = parse_verdict(parsed.get("verdict"))
        cleanup_failure = generation_cleanup_failure_reason(result)
        run_summary = judge_run_result_summary(
            result,
            judge_provider=judge_provider,
        )
        attempts.append(
            {
                "attempt_id": uuid.uuid4().hex,
                "attempt": attempt_index,
                "verdict": parsed.get("verdict") if parsed else "",
                "met": met,
                "retry_suppressed_reason": cleanup_failure,
                "run": run_summary,
            }
        )
        row = {
            **criterion,
            "weight": weight,
            "repeat_index": repeat_index,
            "verdict": parsed.get("verdict") if parsed else "",
            "met": met,
            "rationale": str(parsed.get("rationale") or parsed.get("reason") or "")[:1000],
            "judge_run": run_summary,
            "judge_attempts": list(attempts),
            **judge_attempt_budget_fields(
                attempts=attempts,
                prior_attempts_used=prior_attempts_used,
                max_attempts=attempt_limit,
                new_attempt_count=len(attempts) - prior_attempts_used,
                exhausted=False,
            ),
        }
        if met is not None and not cleanup_failure and not result.error:
            return row
        row["error"] = result.error or "judge_verdict_parse_failed"
        row["raw"] = result.final_text[:1000]
        last_row = row
        if cleanup_failure:
            row["retry_suppressed_reason"] = cleanup_failure
            row["error"] = cleanup_failure
            break
    if last_row is not None and len(attempts) >= attempt_limit:
        last_row["last_judge_error"] = last_row.get("error")
        last_row["error"] = JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR
        last_row["judge_attempt_budget_exhausted"] = True
    return (
        last_row
        if last_row is not None
        else {
            **criterion,
            "weight": weight,
            "repeat_index": repeat_index,
            "verdict": "",
            "met": None,
            "rationale": "",
            "error": "judge_verdict_parse_failed",
            "judge_attempts": [],
            **judge_attempt_budget_fields(
                attempts=[],
                prior_attempts_used=prior_attempts_used,
                max_attempts=attempt_limit,
                new_attempt_count=0,
                exhausted=False,
            ),
        }
    )


def quality_total(judge: dict[str, Any] | None) -> float | None:
    if not isinstance(judge, dict):
        return None
    if judge.get("mode") == "legacy_dimension_score":
        if judge.get("score_status") != "complete":
            return None
        scores = valid_legacy_judge_scores(judge)
        if scores is None:
            return None
        return clamp_percent(sum(scores.values()) / 20.0 * 100.0)
    if judge.get("mode") == "draco_criterion_judgments" and judge.get("score_status") != "complete":
        return None
    normalized = judge.get("normalized_score")
    if isinstance(normalized, int | float):
        return clamp_percent(float(normalized))
    total = judge.get("total")
    if isinstance(total, int | float):
        if judge.get("mode") == "legacy_dimension_score":
            total_float = float(total)
            normalized_total = total_float if total_float > 20.0 else total_float / 20.0 * 100.0
            return clamp_percent(normalized_total)
        return float(total)
    return None


async def run_one(
    *,
    task: dict[str, Any],
    group: str,
    config: GatewayConfig,
    inherited: ProviderConfig,
    dry_run: bool,
    judge_provider: Any | None,
    judge_candidates: bool,
    judge_repeats: int,
    judge_concurrency: int,
    judge_max_attempts: int,
    judge_semaphore: asyncio.Semaphore | None,
    timeout: float,
    ensemble_proposer_timeout: float | None,
    ensemble_aggregator_timeout: float | None,
    ensemble_proposer_early_stop_success_count: int | None,
    ensemble_proposer_early_stop_after: float | None,
    expand_ensemble_timeouts_to_task_timeout: bool,
    tool_policy: dict[str, Any],
    generation_policy: dict[str, Any],
    experiment_config: DracoExperimentConfig | None = None,
    runner_mode: str = RUNNER_MODE_PROVIDER,
    output_dir: Path | None = None,
    agent_max_iterations: int = DEFAULT_AGENT_MAX_ITERATIONS,
    agent_finalization_policy: Mapping[str, Any] | None = None,
    generation_max_attempts: int = GENERATION_MAX_ATTEMPTS,
    generation_attempt_offset: int = 0,
    generation_retry_backoff: float = DEFAULT_GENERATION_RETRY_BACKOFF_SECONDS,
    tools: list[ToolDefinition] | None = None,
    run_compatibility_fingerprint: str = "",
    g1_registry_contract: Mapping[str, Any] | None = None,
    task_analyzer_execution_contract: Mapping[str, Any] | None = None,
    frozen_g1_lifecycle: Mapping[str, Any] | None = None,
    require_openrouter_non_byok: bool = False,
) -> dict[str, Any]:
    spec = GROUP_SPECS[group]
    audit_provider_routing = _openrouter_audit_provider_routing(
        inherited.provider_routing,
        g1_registry_contract if group == "G1" else None,
    )
    started = time.time()
    generation_attempt_budget_limit = bounded_generation_attempts(
        generation_max_attempts
    )
    if (
        not isinstance(generation_attempt_offset, int)
        or isinstance(generation_attempt_offset, bool)
        or not 0 <= generation_attempt_offset < generation_attempt_budget_limit
    ):
        raise ValueError(
            "generation attempt offset must be an integer within the configured budget"
        )
    provider = None
    build: ProviderBuildResult | None = None
    effective_prompt = str(task["prompt"])
    provider_error = ""
    failed_build_setup_latency_ms = 0
    failed_build_setup_usage: list[dict[str, Any]] = []
    failed_build_routing_trace: dict[str, Any] = {}
    frozen_build_routing_trace: dict[str, Any] = {}
    generation_retry_backoff_s = bounded_generation_retry_backoff(generation_retry_backoff)
    finalization_policy = normalized_agent_finalization_policy(agent_finalization_policy)
    effective_timeout = group_timeout_seconds(
        requested_timeout=timeout,
        config=config,
        group=group,
        ensemble_proposer_timeout=ensemble_proposer_timeout,
        ensemble_aggregator_timeout=ensemble_aggregator_timeout,
    )
    turn_absolute_deadline = time.monotonic() + effective_timeout
    generation_config = generation_chat_config(
        generation_policy,
        model=spec["model"] if spec["kind"] == "single" else None,
        tool_choice=tool_policy.get("openrouter_fusion_tool_choice"),
    )
    try:
        build = await build_experiment_provider(
            config=config,
            inherited=inherited,
            group=group,
            prompt=effective_prompt,
            task_id=str(task["id"]),
            task_input_sha256=canonical_json_sha256(task),
            prompt_sha256=text_sha256(str(task["prompt"])),
            dry_run=dry_run,
            enable_proposer_tools=bool(
                tool_policy.get("tools_enabled")
                and tool_policy.get("tool_mode") != TOOL_MODE_LOCAL_WEB_TOOLS
            ),
            ensemble_proposer_timeout=ensemble_proposer_timeout,
            ensemble_aggregator_timeout=ensemble_aggregator_timeout,
            experiment_config=experiment_config,
            g1_registry_contract=g1_registry_contract,
            task_analyzer_execution_contract=(
                task_analyzer_execution_contract
                if group == "G1"
                else None
            ),
            generation_policy=generation_policy,
            tools=tools,
            frozen_g1_lifecycle=frozen_g1_lifecycle,
            turn_absolute_deadline=turn_absolute_deadline,
        )
        candidate_prompt = build.prompt
        safe_routing_trace = json_safe(build.routing_trace)
        if not isinstance(safe_routing_trace, Mapping):
            raise TypeError("provider routing trace must serialize to an object")
        candidate_routing_trace = json.loads(
            json.dumps(dict(safe_routing_trace), ensure_ascii=False)
        )
        candidate_generation_config = with_openrouter_model_capabilities(
            generation_config,
            str(
                build.routing_trace.get("fallback_model")
                or build.routing_trace.get("applied_model")
                or build.routing_trace.get("routed_model")
                or build.routing_trace.get("model")
                or ""
            ),
        )
        effective_prompt = candidate_prompt
        frozen_build_routing_trace = candidate_routing_trace
        generation_config = candidate_generation_config
        provider = build.provider
    except Exception as exc:  # noqa: BLE001 - report config errors per row
        provider = None
        if isinstance(exc, ProviderBuildError):
            provider_error = str(exc)
            failed_build_setup_latency_ms = exc.setup_latency_ms
            failed_build_setup_usage = list(exc.setup_usage)
            failed_build_routing_trace = dict(exc.routing_trace)
        elif build is not None and build.setup_usage:
            provider_error = (
                "provider_build_failed_after_setup:"
                + type(exc).__name__
            )
            failed_build_setup_latency_ms = build.setup_latency_ms
            failed_build_setup_usage = list(build.setup_usage)
            failed_build_routing_trace = (
                safe_provider_build_routing_trace(build.routing_trace)
            )
        else:
            provider_error = (
                "provider_build_failed:" + type(exc).__name__
            )
    expected_generation_model = ""
    if spec.get("kind") == "single":
        expected_generation_model = str(spec.get("model") or "").strip()
    elif spec.get("kind") == "router_single":
        expected_generation_model = str(
            frozen_build_routing_trace.get("applied_model")
            or frozen_build_routing_trace.get("fallback_model")
            or ""
        ).strip()
    expected_generation_provider = (
        "dry"
        if dry_run and spec.get("kind") in {"single", "router_single"}
        else str(inherited.provider or "").strip()
    )
    terminal_generation_reason_override = ""
    if provider is not None:
        paid_attempt_sink: dict[str, Any] = {}
        try:
            (
                run,
                generation_attempts,
                selected_generation_attempt_index,
            ) = await collect_generation_with_retries(
                provider,
                effective_prompt,
                timeout=effective_timeout,
                config=generation_config,
                tools=tools,
                runner_mode=runner_mode,
                tool_policy=tool_policy,
                task_id=str(task["id"]),
                group=group,
                output_dir=output_dir,
                agent_max_iterations=agent_max_iterations,
                finalization_policy=finalization_policy,
                max_attempts=(
                    generation_attempt_budget_limit
                    - generation_attempt_offset
                ),
                attempt_offset=generation_attempt_offset,
                attempt_budget_limit=generation_attempt_budget_limit,
                retry_backoff_seconds=generation_retry_backoff_s,
                expected_model=expected_generation_model,
                expected_provider=expected_generation_provider,
                expected_g1_registry_contract=g1_registry_contract,
                expected_task_analyzer_execution_contract=(
                    task_analyzer_execution_contract
                ),
                paid_attempt_sink=paid_attempt_sink,
            )
        except Exception as exc:  # noqa: BLE001 - commit a returned paid call
            if not paid_attempt_sink:
                raise
            (
                run,
                generation_attempts,
                selected_generation_attempt_index,
                terminal_generation_reason_override,
            ) = recover_paid_generation_postprocessing_failure(
                paid_attempt_sink,
                exc,
            )
            recovered_provider = paid_attempt_sink.get(
                "current_provider"
            )
            if recovered_provider is not None:
                provider = recovered_provider
        provider = getattr(
            provider,
            "_draco_selected_retry_provider",
            provider,
        )
    else:
        run = RunResult(
            final_text="",
            done=None,
            error=provider_error,
            setup_latency_ms=failed_build_setup_latency_ms,
            setup_usage=failed_build_setup_usage,
            routing_trace=failed_build_routing_trace,
            trace_events=[
                {
                    "seq": 1,
                    "elapsed_ms": failed_build_setup_latency_ms,
                    "kind": "error",
                    "code": "provider_build_failed_after_setup",
                    "request_started": False,
                    "physical_request_count": 0,
                }
            ]
            if failed_build_setup_usage
            else [],
        )
        generation_attempts = (
            [
                {
                    "attempt_id": uuid.uuid4().hex,
                    "attempt_kind": "provider_build_after_paid_setup",
                    "attempt": generation_attempt_offset + 1,
                    "started_at": started,
                    "completed_at": time.time(),
                    "retryable": True,
                    "retry_reason": provider_error,
                    "retry_suppressed_reason": "",
                    "will_retry": False,
                    "retry_backoff_s": 0.0,
                    "run": run_result_summary(run),
                }
            ]
            if failed_build_setup_usage
            else []
        )
        selected_generation_attempt_index = 0
    if terminal_generation_reason_override:
        terminal_generation_reason = (
            terminal_generation_reason_override
        )
    else:
        try:
            terminal_generation_reason = generation_retry_reason(
                run,
                expected_selection_mode=(
                    str(spec.get("selection_mode") or "")
                    if spec.get("kind") == "selection_mode"
                    else ""
                ),
                expected_selection_plan=(
                    dict(getattr(provider, "selection_plan", {}) or {})
                    if provider is not None
                    and spec.get("kind") == "selection_mode"
                    else {}
                ),
                expected_g1_registry_contract=g1_registry_contract,
                expected_task_analyzer_execution_contract=(
                    task_analyzer_execution_contract
                ),
                expected_model=expected_generation_model,
                expected_provider=expected_generation_provider,
            )
        except Exception as exc:  # noqa: BLE001 - the paid attempt is committed
            pending = {
                "result": run,
                "attempts": generation_attempts,
                "attempt_id": (
                    generation_attempts[-1].get("attempt_id")
                    if generation_attempts
                    and isinstance(generation_attempts[-1], Mapping)
                    else uuid.uuid4().hex
                ),
                "attempt_index": (
                    generation_attempts[-1].get("attempt")
                    if generation_attempts
                    and isinstance(generation_attempts[-1], Mapping)
                    else generation_attempt_offset + 1
                ),
                "attempt_started_at": started,
                "expected_provider": expected_generation_provider,
                "expected_model": expected_generation_model,
                "current_provider": provider,
                "stage": "terminal_generation_validation",
            }
            (
                run,
                generation_attempts,
                selected_generation_attempt_index,
                terminal_generation_reason,
            ) = recover_paid_generation_postprocessing_failure(
                pending,
                exc,
            )
    mark_retryable_generation_error(run, terminal_generation_reason)
    generation_accepted = not bool(terminal_generation_reason)
    generation_completed_at = time.time()
    usage_payload = run_result_usage_payload(run)
    generation_non_byok_audit: dict[str, Any] | None = None
    if require_openrouter_non_byok and generation_accepted:
        generation_non_byok_audit = openrouter_non_byok_audit(
            {
                "llm_request_count": (
                    llm_request_count_for_run(
                        spec=spec,
                        done=run.done,
                        provider_attempted=provider is not None,
                    )
                    + usage_rows_request_count(run.setup_usage)
                ),
                "usage": usage_payload,
                "execution": {"generation_attempts": generation_attempts},
                "judge": None,
                "candidate_judges": [],
                "tool_policy": tool_policy,
            },
            provider_routing=audit_provider_routing,
        )
    profile_proposer_timeout_s = getattr(provider, "proposer_timeout_seconds", None)
    profile_aggregator_timeout_s = getattr(provider, "aggregator_timeout_seconds", None)
    profile_min_successful_proposers = getattr(
        provider,
        "min_successful_proposers",
        None,
    )
    profile_quorum_grace_s = getattr(provider, "quorum_grace_seconds", None)
    profile_candidate_max_chars = getattr(provider, "candidate_max_chars", None)
    profile_proposer_tools = getattr(provider, "proposer_tools", None)
    profile_aggregator_tools = getattr(provider, "aggregator_tools", None)
    profile_aggregator_recovery_mode = getattr(
        provider,
        "aggregator_recovery_mode",
        None,
    )
    profile_aggregator_recovery_top_k = getattr(
        provider,
        "aggregator_recovery_top_k",
        None,
    )
    profile_aggregator_max_tokens_cap = getattr(
        provider,
        "aggregator_max_tokens_cap",
        None,
    )
    profile_aggregator_visible_answer_reserve_tokens = getattr(
        provider,
        "aggregator_visible_answer_reserve_tokens",
        None,
    )
    profile_wait_for_all_proposers = (
        profile_quorum_grace_s is not None and float(profile_quorum_grace_s) <= 0
    )
    profile_proposer_early_stop_success_count = getattr(
        provider,
        "proposer_early_stop_success_count",
        None,
    )
    profile_proposer_early_stop_after_s = getattr(
        provider,
        "proposer_early_stop_after_seconds",
        None,
    )
    should_judge = generation_accepted and run.done is not None
    judge = (
        await judge_text(
            judge_provider=judge_provider,
            task=task,
            answer=run.final_text,
            dry_run=dry_run and judge_provider is not None,
            judge_repeats=judge_repeats,
            judge_concurrency=judge_concurrency,
            judge_max_attempts=judge_max_attempts,
            judge_semaphore=judge_semaphore,
        )
        if should_judge
        else None
    )
    candidate_judges: list[dict[str, Any] | None] = []
    if judge_candidates and should_judge:
        for candidate in candidate_texts(
            run.done,
            final_agent_call_only=runner_mode == RUNNER_MODE_AGENT_LOOP,
        ):
            candidate_judges.append(
                await judge_text(
                    judge_provider=judge_provider,
                    task=task,
                    answer=candidate,
                    dry_run=dry_run and judge_provider is not None,
                    judge_repeats=judge_repeats,
                    judge_concurrency=judge_concurrency,
                    judge_max_attempts=judge_max_attempts,
                    judge_semaphore=judge_semaphore,
                )
            )
    fused_total = quality_total(judge)
    candidate_totals = [
        total for total in (quality_total(item) for item in candidate_judges) if total is not None
    ]
    completed_at = time.time()
    final_text_sha = text_sha256(run.final_text)
    prompt_sha = text_sha256(str(task["prompt"]))
    selected_server_tool_call_count = coerce_metric_int(usage_payload.get("server_tool_call_count"))
    server_tool_use = usage_payload.get("server_tool_use") or {}
    selected_total_tool_call_count = run.tool_call_count + selected_server_tool_call_count
    selected_llm_request_count = llm_request_count_for_run(
        spec=spec,
        done=run.done,
        provider_attempted=provider is not None,
    ) + usage_rows_request_count(run.setup_usage)
    ensemble_trace = run.done.ensemble_trace if run.done is not None else {}
    selected_usage_unknown_count = max(
        ensemble_usage_unknown_count(ensemble_trace),
        usage_unknown_count_from_usage_payload(usage_payload),
    )
    selected_trajectory_steps = selected_total_tool_call_count + selected_llm_request_count
    selected_billed_cost = float(usage_payload.get("billed_cost") or 0.0)
    attempt_stream_tool_call_count = sum_generation_attempt_metric(
        generation_attempts, "stream_tool_call_count"
    )
    attempt_server_tool_call_count = sum_generation_attempt_metric(
        generation_attempts, "server_tool_call_count"
    )
    attempt_total_tool_call_count = sum_generation_attempt_metric(
        generation_attempts, "total_tool_call_count"
    )
    attempt_llm_request_count = sum_generation_attempt_metric(
        generation_attempts, "llm_request_count"
    )
    attempt_usage_unknown_count = sum_generation_attempt_metric(
        generation_attempts, "usage_unknown_count"
    )
    attempt_trajectory_steps = sum_generation_attempt_metric(
        generation_attempts, "trajectory_steps"
    )
    attempt_latency_ms = sum_generation_attempt_metric(generation_attempts, "latency_ms")
    attempt_server_tool_use = sum_generation_attempt_server_tools(generation_attempts)
    generation_attempt_total_billed_cost = sum_generation_attempt_billed_cost(generation_attempts)
    if not generation_attempts:
        generation_attempt_total_billed_cost = float(usage_payload.get("billed_cost") or 0.0)
    generation_retry_reasons = [
        str(attempt.get("retry_reason") or "")
        for attempt in generation_attempts
        if attempt.get("retry_reason")
    ]
    actual_latency_ms = attempt_latency_ms if generation_attempts else run.latency_ms
    actual_stream_tool_call_count = (
        attempt_stream_tool_call_count if generation_attempts else run.tool_call_count
    )
    actual_server_tool_call_count = (
        attempt_server_tool_call_count if generation_attempts else selected_server_tool_call_count
    )
    actual_server_tool_use = attempt_server_tool_use if generation_attempts else server_tool_use
    actual_total_tool_call_count = (
        attempt_total_tool_call_count if generation_attempts else selected_total_tool_call_count
    )
    actual_llm_request_count = (
        attempt_llm_request_count if generation_attempts else selected_llm_request_count
    )
    actual_trajectory_steps = (
        attempt_trajectory_steps
        if generation_attempts
        else actual_total_tool_call_count + actual_llm_request_count
    )
    actual_usage_unknown_count = (
        attempt_usage_unknown_count if generation_attempts else selected_usage_unknown_count
    )
    selected_generation_succeeded = generation_accepted
    if not selected_generation_succeeded:
        selected_generation_attempt_index = 0
        selected_server_tool_call_count = 0
        server_tool_use = {}
        selected_total_tool_call_count = 0
        selected_llm_request_count = 0
        selected_usage_unknown_count = 0
        selected_trajectory_steps = 0
        selected_billed_cost = 0.0
    selected_attempt_metrics = {
        "latency_ms": run.latency_ms if selected_generation_succeeded else 0,
        "stream_tool_call_count": (run.tool_call_count if selected_generation_succeeded else 0),
        "server_tool_call_count": selected_server_tool_call_count,
        "server_tool_use": server_tool_use,
        "total_tool_call_count": selected_total_tool_call_count,
        "trajectory_steps": selected_trajectory_steps,
        "llm_request_count": selected_llm_request_count,
        "usage_unknown_count": selected_usage_unknown_count,
        "billed_cost_usd": selected_billed_cost,
        "generation_attempt": selected_generation_attempt_index,
    }
    actual_spend_metrics = {
        "latency_ms": actual_latency_ms,
        "stream_tool_call_count": actual_stream_tool_call_count,
        "server_tool_call_count": actual_server_tool_call_count,
        "server_tool_use": actual_server_tool_use,
        "total_tool_call_count": actual_total_tool_call_count,
        "trajectory_steps": actual_trajectory_steps,
        "llm_request_count": actual_llm_request_count,
        "usage_unknown_count": actual_usage_unknown_count,
        "billed_cost_usd": generation_attempt_total_billed_cost,
        "generation_attempt_count": len(generation_attempts),
    }
    row = {
        "task_id": task["id"],
        "group": group,
        "domain": task.get("domain", ""),
        "prompt": task["prompt"],
        "prompt_sha256": prompt_sha,
        "task_input_sha256": canonical_json_sha256(task),
        "run_compatibility_fingerprint": run_compatibility_fingerprint,
        "metadata": task.get("metadata", {}),
        "provider_spec": dict(spec),
        "routing_trace": run.routing_trace or frozen_build_routing_trace,
        "runner_mode": runner_mode,
        "agent_finalization_policy": dict(finalization_policy),
        "tools_enabled": bool(tool_policy.get("tools_enabled")),
        "tool_policy": tool_policy,
        "generation_policy": generation_policy,
        "generation_config": compact_chat_config(generation_config, generation_policy),
        "contamination_blocked_domains": (tool_policy.get("contamination_blocked_domains") or []),
        "started_at": started,
        "generation_completed_at": generation_completed_at,
        "completed_at": completed_at,
        "total_elapsed_ms": int((completed_at - started) * 1000),
        "latency_ms": run.latency_ms,
        "ttft_ms": run.ttft_ms,
        "tool_call_count": run.tool_call_count,
        "stream_tool_call_count": run.tool_call_count,
        "server_tool_call_count": selected_server_tool_call_count,
        "server_tool_use": server_tool_use,
        "total_tool_call_count": selected_total_tool_call_count,
        "trajectory_steps": selected_trajectory_steps,
        "llm_request_count": selected_llm_request_count,
        "usage_unknown_count": selected_usage_unknown_count,
        "selected_attempt_metrics": selected_attempt_metrics,
        "selected_generation_succeeded": selected_generation_succeeded,
        "actual_spend_metrics": actual_spend_metrics,
        "selected_attempt_billed_cost_usd": selected_billed_cost,
        "actual_spend_billed_cost_usd": generation_attempt_total_billed_cost,
        "generation_attempt_count": len(generation_attempts),
        "generation_attempt_evidence_schema": GENERATION_ATTEMPT_EVIDENCE_SCHEMA,
        "generation_attempt_budget_limit": generation_attempt_budget_limit,
        "generation_attempt_budget_used": (generation_attempt_offset + len(generation_attempts)),
        "generation_max_attempts": generation_attempt_budget_limit,
        "generation_retry_backoff_s": generation_retry_backoff_s,
        "generation_attempt_total_billed_cost": generation_attempt_total_billed_cost,
        "generation_retry_reasons": generation_retry_reasons,
        "error": (
            None
            if generation_accepted
            and isinstance(ensemble_trace, Mapping)
            and ensemble_trace
            else run.error or None
        ),
        "final_text": run.final_text,
        "final_text_chars": len(run.final_text),
        "final_text_sha256": final_text_sha,
        "execution": {
            "provider_error": provider_error,
            "run_error": run.error,
            "judge_skipped_reason": "run_not_done" if not should_judge else "",
            "requested_timeout_s": timeout,
            "effective_timeout_s": effective_timeout,
            "profile_proposer_timeout_s": profile_proposer_timeout_s,
            "profile_aggregator_timeout_s": profile_aggregator_timeout_s,
            "profile_min_successful_proposers": profile_min_successful_proposers,
            "profile_quorum_grace_s": profile_quorum_grace_s,
            "profile_wait_for_all_proposers": profile_wait_for_all_proposers,
            "profile_candidate_max_chars": profile_candidate_max_chars,
            "profile_proposer_tools": profile_proposer_tools,
            "profile_aggregator_tools": profile_aggregator_tools,
            "profile_aggregator_recovery_mode": (profile_aggregator_recovery_mode),
            "profile_aggregator_recovery_top_k": (profile_aggregator_recovery_top_k),
            "profile_aggregator_max_tokens_cap": (profile_aggregator_max_tokens_cap),
            "profile_aggregator_visible_answer_reserve_tokens": (
                profile_aggregator_visible_answer_reserve_tokens
            ),
            "profile_proposer_early_stop_success_count": (
                profile_proposer_early_stop_success_count
            ),
            "profile_proposer_early_stop_after_s": profile_proposer_early_stop_after_s,
            "runner_mode": runner_mode,
            "agent_max_iterations": agent_max_iterations,
            "agent_finalization_policy": dict(finalization_policy),
            "latency_ms": run.latency_ms,
            "selected_generation_latency_ms": run.latency_ms,
            "selected_attempt_metrics": selected_attempt_metrics,
            "actual_spend_metrics": actual_spend_metrics,
            "routing_setup_latency_ms": run.setup_latency_ms,
            "routing_trace": run.routing_trace or frozen_build_routing_trace,
            "ttft_ms": run.ttft_ms,
            "total_elapsed_ms": int((completed_at - started) * 1000),
            "tool_call_count": run.tool_call_count,
            "stream_tool_call_count": run.tool_call_count,
            "server_tool_call_count": selected_server_tool_call_count,
            "server_tool_use": server_tool_use,
            "total_tool_call_count": selected_total_tool_call_count,
            "trajectory_steps": selected_trajectory_steps,
            "llm_request_count": selected_llm_request_count,
            "usage_unknown_count": selected_usage_unknown_count,
            "generation_attempt_count": len(generation_attempts),
            "generation_max_attempts": generation_attempt_budget_limit,
            "generation_retry_backoff_s": generation_retry_backoff_s,
            "selected_generation_attempt": selected_generation_attempt_index,
            "generation_retry_reasons": generation_retry_reasons,
            "generation_attempt_total_billed_cost": generation_attempt_total_billed_cost,
            "generation_attempts": generation_attempts,
            "prior_generation_attempts_used": generation_attempt_offset,
            "metadata_repair_attempted": False,
            "generation_attempt_budget_remaining": max(
                0,
                generation_attempt_budget_limit
                - generation_attempt_offset
                - len(generation_attempts),
            ),
        },
        "run_trace": {
            "event_count": len(run.trace_events),
            "events": run.trace_events,
        },
        "usage": usage_payload,
        "ensemble_trace": ensemble_trace,
        "generation_audit_warnings": list(run.audit_warnings),
        "judge": judge,
        "candidate_judges": candidate_judges,
        "quality_total": fused_total,
        "fusion_delta": (
            fused_total - max(candidate_totals)
            if fused_total is not None and candidate_totals
            else None
        ),
    }
    if generation_non_byok_audit is not None:
        row["openrouter_non_byok_audit"] = generation_non_byok_audit
    repair_row_cost_metadata_with_estimates(row)
    row["cost_accounting"] = public_cost_accounting(row_cost_accounting(row))
    final_non_byok_audit: dict[str, Any] | None = None
    if require_openrouter_non_byok:
        final_non_byok_audit = openrouter_non_byok_audit(
            row,
            provider_routing=audit_provider_routing,
        )
        row["openrouter_non_byok_audit"] = final_non_byok_audit
    judge_reasons = judge_completion_reasons(
        row,
        judge_required=judge_provider is not None,
    )
    cost_metadata_complete = bool(row["cost_accounting"].get("actual_llm_cost_complete"))
    if generation_accepted and not row.get("error") and judge_reasons:
        row["error"] = (
            JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR
            if isinstance(judge, Mapping)
            and judge.get("judge_attempt_budget_exhausted") is True
            else "judge_incomplete"
        )
    row["execution_status"] = execution_status_payload(
        generation_accepted=generation_accepted,
        final_text=run.final_text,
        run_error=run.error,
        ensemble_trace=ensemble_trace,
    )
    row["audit_status"] = row_audit_status(
        row,
        non_byok_audit=final_non_byok_audit,
    )
    row["completion_status"] = {
        "generation_accepted": generation_accepted,
        "cost_metadata_complete": cost_metadata_complete,
        "cost_metadata_scope": "actual_llm_spend",
        "all_provider_cost_complete": bool(
            row["cost_accounting"].get("actual_spend_cost_complete")
        ),
        "actual_external_tool_cost_complete": bool(
            row["cost_accounting"].get("actual_external_cost_complete")
        ),
        "judge_complete": not judge_reasons,
        "status": (
            "complete"
            if generation_accepted and not judge_reasons
            else "incomplete"
        ),
        "incomplete_reasons": [
            *([] if generation_accepted else [terminal_generation_reason]),
            *judge_reasons,
        ],
        "audit_warnings": list(row["audit_status"]["warnings"]),
    }
    return row


def group_timeout_seconds(
    *,
    requested_timeout: float,
    config: GatewayConfig,
    group: str,
    ensemble_proposer_timeout: float | None = None,
    ensemble_aggregator_timeout: float | None = None,
) -> float:
    if requested_timeout <= 0:
        return requested_timeout
    spec = GROUP_SPECS[group]
    if spec["kind"] != "profile":
        return requested_timeout
    profile = config.llm_ensemble.profiles.get(spec["profile"])
    if profile is None:
        return requested_timeout
    proposer_timeout_s, aggregator_timeout_s = profile_timeout_seconds(
        profile,
        proposer_timeout_override=ensemble_proposer_timeout,
        aggregator_timeout_override=ensemble_aggregator_timeout,
    )
    moa_layers = max(1, int(getattr(profile, "moa_layers", 1) or 1))
    profile_budget = (
        proposer_timeout_s + aggregator_timeout_s * moa_layers + PROFILE_TIMEOUT_MARGIN_SECONDS
    )
    return max(float(requested_timeout), profile_budget)


def trace_row(row: dict[str, Any]) -> dict[str, Any]:
    return trace_row_from_result(row)


async def _settled_to_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Wait for a blocking artifact mutation to settle before propagating cancel."""

    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as exc:
            if task.done() and task.cancelled():
                if cancellation is not None:
                    cancellation.add_note(
                        "selection-plan pack worker was cancelled during "
                        "cancellation settlement"
                    )
                    raise cancellation from None
                raise RuntimeError(
                    "selection-plan pack worker was cancelled before settlement"
                ) from exc
            if cancellation is None:
                cancellation = exc
            continue
        except BaseException as worker_exc:
            if cancellation is None:
                raise
            cancellation.add_note(
                "selection-plan pack worker settled with "
                f"{type(worker_exc).__name__}; original cancellation preserved"
            )
            raise cancellation from None
    if cancellation is not None:
        raise cancellation
    return result


class AsyncSelectionPlanEvidenceWriter:
    """Single-flight event-loop boundary around the synchronous pack appender."""

    def __init__(self, appender: SelectionPlanPackAppender) -> None:
        self._appender = appender
        self._lock = asyncio.Lock()
        self._closed = False

    async def compact_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if self._closed:
                raise RuntimeError("selection-plan pack writer is closed")
            compacted = await _settled_to_thread(
                compact_selection_plan_evidence_row,
                row,
                appender=self._appender,
            )
            if not isinstance(compacted, dict):
                raise RuntimeError("selection-plan compact worker returned a non-object")
            return compacted

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await _settled_to_thread(self._appender.close)


async def _cleanup_async_resource(
    resource: Any,
    *,
    label: str,
    preserve_primary: bool,
) -> bool:
    """Close an async resource without replacing an active business failure."""

    if resource is None:
        return True
    try:
        await resource.aclose()
    except BaseException as cleanup_exc:
        if not preserve_primary:
            raise
        print(
            f"Failed to close {label}: {type(cleanup_exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return False
    return True


def terminal_selection_plan_evidence_binding(
    *,
    pack_path: Path,
    results_path: Path,
    trace_path: Path,
    checkpoint_path: Path,
) -> dict[str, Any]:
    """Authenticate durable rows and their pack into one terminal contract."""

    compact_row_count = 0
    with SelectionPlanPackReader(pack_path, owner_only=True) as reader:
        path_snapshots: dict[str, tuple[int, ...]] = {}
        def observe_result_row(row: Mapping[str, Any]) -> None:
            nonlocal compact_row_count
            compact_row_count += int(
                validate_compact_selection_plan_evidence_row(
                    row,
                    reader=reader,
                )
            )

        verification = verify_durable_draco_artifacts(
            results_path=results_path,
            trace_path=trace_path,
            checkpoint_path=checkpoint_path,
            result_row_observer=observe_result_row,
            path_snapshot_out=path_snapshots,
        )
        reader.verify_snapshot()
        verify_durable_artifact_path_snapshots(
            path_snapshots,
            results_path=results_path,
            trace_path=trace_path,
            checkpoint_path=checkpoint_path,
        )
        return selection_plan_evidence_manifest_binding(
            pack_index=reader.index,
            durable_artifact_verification=verification,
            compact_row_count=compact_row_count,
        )


def percentile(values: list[int], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return float(ordered[index])


def numeric_pct_delta(value: Any, baseline: Any) -> float | None:
    if isinstance(value, int | float) and isinstance(baseline, int | float):
        baseline_float = float(baseline)
        if baseline_float == 0.0:
            return None
        return (float(value) - baseline_float) / baseline_float * 100.0
    return None


def row_metric_int(row: dict[str, Any], key: str, fallback_key: str | None = None) -> int:
    value = row.get(key)
    if value is None and fallback_key is not None:
        value = row.get(fallback_key)
    return coerce_metric_int(value)


def row_generation_attempt_usage_total(row: dict[str, Any], key: str) -> float | None:
    execution = row.get("execution") or {}
    attempts = execution.get("generation_attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    total = 0.0
    observed_usage = False
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        run = attempt.get("run")
        if not isinstance(run, dict):
            continue
        usage = run.get("usage")
        if not isinstance(usage, dict):
            continue
        value = usage.get(key)
        if isinstance(value, int | float):
            total += float(value)
            observed_usage = True
    return total if observed_usage else 0.0


def row_usage_number(row: dict[str, Any], key: str) -> float:
    """Return usage for the selected successful generation attempt only."""

    if row.get("selected_generation_succeeded") is False:
        return 0.0
    usage = row.get("usage") or {}
    if isinstance(usage, dict):
        value = usage.get(key)
        if isinstance(value, int | float):
            return float(value)
    return 0.0


def row_actual_spend_usage_number(row: dict[str, Any], key: str) -> float:
    """Return usage charged across every generation attempt."""

    attempt_total = row_generation_attempt_usage_total(row, key)
    if attempt_total is not None:
        return attempt_total
    usage = row.get("usage") or {}
    if isinstance(usage, dict):
        value = usage.get(key)
        if isinstance(value, int | float):
            return float(value)
    return 0.0


def row_billed_cost(row: dict[str, Any]) -> float:
    """Compatibility cost: selected successful generation attempt."""

    return row_usage_number(row, "billed_cost")


def row_actual_spend_billed_cost(row: dict[str, Any]) -> float:
    attempt_cost = row_generation_attempt_usage_total(row, "billed_cost")
    if attempt_cost is not None:
        return attempt_cost
    value = row.get("generation_attempt_total_billed_cost")
    if isinstance(value, int | float):
        return float(value)
    execution = row.get("execution") or {}
    value = execution.get("generation_attempt_total_billed_cost")
    if isinstance(value, int | float):
        return float(value)
    return row_billed_cost(row)


def _usage_token_count(usage: dict[str, Any]) -> int:
    # OpenRouter reports reasoning_tokens as a completion/output-token detail,
    # not an additional billed token bucket.  Keep it separately observable
    # without adding it to input + output a second time.
    return sum(coerce_metric_int(usage.get(key)) for key in ("input_tokens", "output_tokens"))


def _finite_nonnegative_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )


def _openrouter_router_provider_metadata_is_complete(
    router_metadata: Mapping[str, Any],
) -> bool:
    """Require attested metadata for the upstream that served the response."""

    attempts = router_metadata.get("attempts")
    if isinstance(attempts, list):
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                continue
            status = attempt.get("status")
            if (
                isinstance(attempt.get("provider"), str)
                and bool(str(attempt.get("provider")).strip())
                and isinstance(attempt.get("model"), str)
                and bool(str(attempt.get("model")).strip())
                and isinstance(status, int)
                and not isinstance(status, bool)
                and 200 <= status < 300
            ):
                return True
    endpoints = router_metadata.get("endpoints")
    available = endpoints.get("available") if isinstance(endpoints, Mapping) else None
    if isinstance(available, list):
        return any(
            isinstance(endpoint, Mapping)
            and endpoint.get("selected") is True
            and isinstance(endpoint.get("provider"), str)
            and bool(str(endpoint.get("provider")).strip())
            and isinstance(endpoint.get("model"), str)
            and bool(str(endpoint.get("model")).strip())
            for endpoint in available
        )
    return False


def _normalize_openrouter_provider_identity(value: Any) -> str:
    """Normalize OpenRouter provider slugs and display names for comparison."""

    return "".join(character for character in str(value or "").casefold() if character.isalnum())


@cache
def _formal_openrouter_model_aliases() -> dict[str, frozenset[str]]:
    """Bind requested model ids to the frozen registry's serving-model aliases."""

    from opensquilla.provider.ranking_router import load_model_registry_snapshot

    aliases: dict[str, set[str]] = {
        str(model).strip().casefold(): {str(model).strip().casefold()}
        for model in OPENROUTER_DEFAULT_PROVIDER_ROUTING
        if str(model).strip()
    }
    snapshot = load_model_registry_snapshot()
    rows = snapshot.get("models")
    if not isinstance(rows, list):
        return {model: frozenset(values) for model, values in aliases.items()}
    for row in rows:
        facts = row.get("registry_facts") if isinstance(row, Mapping) else None
        if not isinstance(facts, Mapping):
            continue
        model = str(facts.get("model_id") or "").strip().casefold()
        version = str(facts.get("version") or "").strip().casefold()
        if not model:
            continue
        aliases.setdefault(model, {model})
        if version:
            aliases[model].add(version)
    return {model: frozenset(values) for model, values in aliases.items()}


def _formal_openrouter_models_equivalent(left: Any, right: Any) -> bool:
    """Treat a frozen requested model and its serving version as one identity."""

    left_model = str(left or "").strip().casefold()
    right_model = str(right or "").strip().casefold()
    if not left_model or not right_model:
        return False
    if left_model == right_model:
        return True
    for requested_model, aliases in _formal_openrouter_model_aliases().items():
        equivalence_class = {requested_model, *aliases}
        if left_model in equivalence_class and right_model in equivalence_class:
            return True
    return False


def _openrouter_router_provider_metadata_pin_state(
    unit: Mapping[str, Any],
    *,
    provider_routing: Mapping[str, str] | None = None,
) -> str:
    """Return ``exact``, ``missing``, or ``conflict`` for serving-route proof."""

    provider_usage = unit.get("provider_usage")
    if not isinstance(provider_usage, Mapping):
        return "missing"
    router_metadata = provider_usage.get("router_metadata")
    if not isinstance(router_metadata, Mapping):
        return "missing"
    routes = {
        str(model).strip().casefold(): str(provider).strip()
        for model, provider in (
            provider_routing
            if provider_routing is not None
            else OPENROUTER_DEFAULT_PROVIDER_ROUTING
        ).items()
        if str(model).strip() and str(provider).strip()
    }
    router_requested = str(router_metadata.get("requested") or "").strip().casefold()
    if not router_requested or router_requested not in routes:
        return "missing"
    request_identities = {
        str(value).strip().casefold()
        for value in (
            unit.get("requested_model"),
            provider_usage.get("requested_model"),
        )
        if str(value or "").strip().casefold() in routes
    }
    if request_identities and request_identities != {router_requested}:
        return "conflict"
    configured_provider = routes[router_requested].strip().casefold()
    auto_route = configured_provider == "auto"
    expected_provider = _normalize_openrouter_provider_identity(configured_provider)
    allowed_models = _formal_openrouter_model_aliases().get(
        router_requested,
        frozenset({router_requested}),
    )

    def endpoint_matches(endpoint: Mapping[str, Any]) -> bool:
        model_matches = str(endpoint.get("model") or "").strip().casefold() in allowed_models
        provider_identity = _normalize_openrouter_provider_identity(endpoint.get("provider"))
        return model_matches and (
            bool(provider_identity) if auto_route else provider_identity == expected_provider
        )

    successful_attempts = [
        attempt
        for attempt in (router_metadata.get("attempts") or [])
        if isinstance(attempt, Mapping)
        and isinstance(attempt.get("status"), int)
        and not isinstance(attempt.get("status"), bool)
        and 200 <= int(attempt["status"]) < 300
    ]
    if successful_attempts and any(
        not endpoint_matches(attempt) for attempt in successful_attempts
    ):
        return "conflict"
    endpoints = router_metadata.get("endpoints")
    available = endpoints.get("available") if isinstance(endpoints, Mapping) else None
    selected = (
        [
            endpoint
            for endpoint in available
            if isinstance(endpoint, Mapping) and endpoint.get("selected") is True
        ]
        if isinstance(available, list)
        else []
    )
    if selected and any(not endpoint_matches(endpoint) for endpoint in selected):
        return "conflict"
    if not successful_attempts and not selected:
        return "missing"
    return "exact"


def _openrouter_audit_provider_routing(
    provider_routing: Mapping[str, str],
    g1_registry_contract: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Add auditable auto-route sentinels for unpinned registry-all candidates."""

    routes = {
        str(model).strip().casefold(): str(provider).strip()
        for model, provider in provider_routing.items()
        if str(model).strip() and str(provider).strip()
    }
    if (
        isinstance(g1_registry_contract, Mapping)
        and g1_registry_contract.get("candidate_scope") == "registry_all"
    ):
        expected_routes = g1_registry_contract.get("expected_routes")
        if isinstance(expected_routes, Mapping):
            for model, provider in expected_routes.items():
                normalized_model = str(model).strip().casefold()
                if normalized_model and str(provider).strip().casefold() == "auto":
                    routes.setdefault(normalized_model, "auto")
    return routes


def _openrouter_provider_billed_cost_is_exact(unit: Mapping[str, Any]) -> bool:
    """Preserve the non-audit OpenRouter provider-billed cost contract."""

    if str(unit.get("provider") or "").strip().casefold() != "openrouter":
        return False
    provider_usage = unit.get("provider_usage")
    if not isinstance(provider_usage, Mapping):
        return False
    router_metadata = provider_usage.get("router_metadata")
    response_ids = provider_usage.get("response_ids")
    billed_cost = unit.get("billed_cost")
    reported_cost = provider_usage.get("provider_reported_cost")
    return (
        provider_usage.get("is_byok") is False
        and isinstance(router_metadata, Mapping)
        and router_metadata.get("is_byok") is False
        and _finite_nonnegative_number(billed_cost)
        and _finite_nonnegative_number(reported_cost)
        and round(float(billed_cost) * 1_000_000_000) == round(float(reported_cost) * 1_000_000_000)
        and isinstance(response_ids, list)
        and bool(response_ids)
        and all(
            isinstance(response_id, str) and bool(response_id.strip())
            for response_id in response_ids
        )
    )


def _openrouter_non_byok_receipt_is_exact(
    unit: Mapping[str, Any],
    *,
    provider_routing: Mapping[str, str] | None = None,
) -> bool:
    """Require physical OpenRouter receipt evidence before calling cost exact."""

    if str(unit.get("provider") or "").strip().casefold() != "openrouter":
        return False
    provider_usage = unit.get("provider_usage")
    if not isinstance(provider_usage, Mapping):
        return False
    router_metadata = provider_usage.get("router_metadata")
    response_ids = provider_usage.get("response_ids")
    billed_cost = unit.get("billed_cost")
    reported_cost = provider_usage.get("provider_reported_cost")
    if (
        isinstance(billed_cost, bool)
        or not isinstance(billed_cost, int | float)
        or isinstance(reported_cost, bool)
        or not isinstance(reported_cost, int | float)
        or not _finite_nonnegative_number(billed_cost)
        or not _finite_nonnegative_number(reported_cost)
    ):
        return False
    receipt_present, receipt_status, receipt_cost = _billing_receipt_state(unit)
    if receipt_present and (receipt_status != "confirmed" or receipt_cost is None):
        return False
    exact_costs = [float(billed_cost), float(reported_cost)]
    if receipt_cost is not None:
        exact_costs.append(receipt_cost)
    return (
        provider_usage.get("is_byok") is False
        and isinstance(router_metadata, Mapping)
        and router_metadata.get("is_byok") is False
        and _openrouter_router_provider_metadata_is_complete(router_metadata)
        and _openrouter_router_provider_metadata_pin_state(
            unit,
            provider_routing=provider_routing,
        )
        == "exact"
        and len({round(cost * 1_000_000_000) for cost in exact_costs}) == 1
        and isinstance(response_ids, list)
        and bool(response_ids)
        and all(
            isinstance(response_id, str) and bool(response_id.strip())
            for response_id in response_ids
        )
    )


def _first_usage_cost(unit: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in unit and _finite_nonnegative_number(unit.get(key)):
            return float(unit[key])
    return None


def _coerce_provider_billing_receipt(value: Any) -> ProviderBillingReceipt | None:
    """Validate the complete provider-native billing receipt schema."""

    if value is None:
        return None

    def receipt_int(raw: Any, *, nullable: bool = False) -> int | None:
        if raw is None and nullable:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw > (1 << 63) - 1:
            raise ValueError("billing receipt nanos must be ledger-safe integers")
        return int(raw)

    if isinstance(value, ProviderBillingReceipt):
        candidate = value
    elif isinstance(value, Mapping):
        try:
            raw_fx = value.get("fx_native_per_usd_nanos")
            raw_schema = value.get("schema_version", 1)
            if (
                isinstance(raw_fx, bool)
                or not isinstance(raw_fx, int)
                or isinstance(raw_schema, bool)
                or not isinstance(raw_schema, int)
            ):
                return None
            candidate = ProviderBillingReceipt(
                currency=str(value.get("currency") or ""),
                status=str(value.get("status") or ""),  # type: ignore[arg-type]
                amount_nanos=receipt_int(value.get("amount_nanos"), nullable=True),
                usd_equivalent_nanos=receipt_int(
                    value.get("usd_equivalent_nanos"),
                    nullable=True,
                ),
                fx_native_per_usd_nanos=raw_fx,
                schema_version=raw_schema,
            )
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        return None
    try:
        amount_nanos = receipt_int(candidate.amount_nanos, nullable=True)
        usd_nanos = receipt_int(candidate.usd_equivalent_nanos, nullable=True)
    except ValueError:
        return None
    if (
        not isinstance(candidate.currency, str)
        or len(candidate.currency) != 3
        or any(character < "A" or character > "Z" for character in candidate.currency)
        or candidate.status not in {"confirmed", "pending"}
        or isinstance(candidate.fx_native_per_usd_nanos, bool)
        or not isinstance(candidate.fx_native_per_usd_nanos, int)
        or candidate.fx_native_per_usd_nanos <= 0
        or candidate.fx_native_per_usd_nanos > (1 << 63) - 1
        or isinstance(candidate.schema_version, bool)
        or not isinstance(candidate.schema_version, int)
        or candidate.schema_version != 1
    ):
        return None
    if candidate.status == "confirmed" and (amount_nanos is None or usd_nanos is None):
        return None
    if candidate.status == "pending" and usd_nanos is not None:
        return None
    if candidate.status == "confirmed":
        expected_usd_nanos = (
            amount_nanos * 1_000_000_000 + candidate.fx_native_per_usd_nanos // 2
        ) // candidate.fx_native_per_usd_nanos
        if expected_usd_nanos != usd_nanos:
            return None
    return candidate


def _billing_receipt_state(
    unit: Mapping[str, Any],
) -> tuple[bool, str, float | None]:
    receipt = unit.get("billing_receipt", unit.get("billingReceipt"))
    if receipt is None:
        return False, "", None
    normalized = _coerce_provider_billing_receipt(receipt)
    if normalized is None:
        return True, "invalid", None
    if normalized.status == "confirmed":
        return (
            True,
            normalized.status,
            int(normalized.usd_equivalent_nanos or 0) / 1_000_000_000,
        )
    return True, normalized.status, None


def exact_provider_usage_cost(unit: Mapping[str, Any]) -> float | None:
    receipt_present, receipt_status, receipt_cost = _billing_receipt_state(unit)
    if receipt_present:
        return receipt_cost if receipt_status == "confirmed" else None
    source = str(unit.get("cost_source") or "none").strip().casefold()
    billed_cost = _first_usage_cost(unit, "billed_cost")
    if source == "openrouter_usage":
        return billed_cost
    if source == "provider_billed" and _openrouter_provider_billed_cost_is_exact(unit):
        return billed_cost
    return None


def trusted_provider_billed_cost(unit: Mapping[str, Any]) -> float:
    exact_cost = exact_provider_usage_cost(unit)
    if exact_cost is not None:
        return exact_cost
    receipt_present, _, _ = _billing_receipt_state(unit)
    if receipt_present:
        return 0.0
    source = str(unit.get("cost_source") or "none").strip().casefold()
    reported = _first_usage_cost(unit, "billed_cost")
    if source in {"provider_billed", "openrouter_usage"}:
        return float(reported or 0.0)
    if source in {"", "none", "unavailable"} and reported and reported > 0.0:
        return reported
    return 0.0


def _mixed_usage_cost(unit: Mapping[str, Any]) -> float | None:
    total = _first_usage_cost(unit, "cost_usd", "costUsd")
    if total is not None:
        return total
    billed = _first_usage_cost(
        unit,
        "billed_cost_usd",
        "billedCostUsd",
        "billed_cost",
    )
    estimated = _first_usage_cost(
        unit,
        "estimated_cost_usd",
        "estimatedCostUsd",
    )
    if billed is None and estimated is None:
        return None
    return (billed or 0.0) + (estimated or 0.0)


def _load_frozen_model_registry_snapshot() -> Mapping[str, Any]:
    """Load the repository model registry used by the frozen routing contract."""

    from opensquilla.provider.ranking_router import load_model_registry_snapshot

    return load_model_registry_snapshot()


def _registry_price_value(
    price: Mapping[str, Any],
    *keys: str,
    required: bool,
) -> tuple[float | None, bool]:
    """Return one non-conflicting finite registry rate and whether it is valid."""

    raw_values = [price[key] for key in keys if key in price]
    if not raw_values:
        return None, not required
    values: list[float] = []
    for value in raw_values:
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(float(value))
            or float(value) < 0.0
        ):
            return None, False
        values.append(float(value))
    if len(set(values)) != 1:
        return None, False
    return values[0], True


@cache
def _frozen_openrouter_registry_price_index() -> tuple[
    dict[str, ResolvedModelPrice],
    dict[str, str],
]:
    """Build exact OpenRouter model prices from the immutable registry snapshot."""

    try:
        snapshot = _load_frozen_model_registry_snapshot()
        snapshot_hash = canonical_json_sha256(snapshot)
    except Exception:  # noqa: BLE001 - missing audit metadata must not fail execution
        return {}, {
            "source": "frozen_openrouter_model_registry",
            "snapshot_version": "",
            "snapshot_canonical_sha256": "",
        }
    snapshot_version = str(snapshot.get("snapshot_version") or "").strip()
    provenance = {
        "source": "frozen_openrouter_model_registry",
        "snapshot_version": snapshot_version,
        "snapshot_canonical_sha256": snapshot_hash,
    }
    rows = snapshot.get("models")
    if not snapshot_version or not isinstance(rows, list):
        return {}, provenance
    prices: dict[str, ResolvedModelPrice] = {}
    invalid_models: set[str] = set()
    for row in rows:
        facts = row.get("registry_facts") if isinstance(row, Mapping) else None
        if not isinstance(facts, Mapping):
            continue
        provider = str(facts.get("provider") or "").strip().casefold()
        model = str(facts.get("model_id") or "").strip().casefold()
        price = facts.get("price")
        if provider != "openrouter" or not model or not isinstance(price, Mapping):
            continue
        input_per_m, input_valid = _registry_price_value(
            price,
            "input_per_million",
            required=True,
        )
        output_per_m, output_valid = _registry_price_value(
            price,
            "output_per_million",
            required=True,
        )
        cache_read_per_m, cache_read_valid = _registry_price_value(
            price,
            "cache_read_per_million",
            "input_cache_read_per_million",
            required=False,
        )
        cache_write_per_m, cache_write_valid = _registry_price_value(
            price,
            "cache_write_per_million",
            "input_cache_write_per_million",
            required=False,
        )
        if (
            model in prices
            or model in invalid_models
            or not all(
                (
                    input_valid,
                    output_valid,
                    cache_read_valid,
                    cache_write_valid,
                )
            )
            or input_per_m is None
            or output_per_m is None
        ):
            prices.pop(model, None)
            invalid_models.add(model)
            continue
        prices[model] = ResolvedModelPrice(
            PriceEntry(
                input_per_m=input_per_m,
                output_per_m=output_per_m,
                cache_read_per_m=cache_read_per_m,
                cache_write_per_m=cache_write_per_m,
            ),
            provenance["source"],
        )
    return prices, provenance


def _frozen_estimate_price(
    model: str,
    provider: str,
) -> tuple[ResolvedModelPrice | None, dict[str, str]]:
    """Resolve only reproducible registry/static prices for audit estimates."""

    normalized_provider = str(provider or "").strip().casefold()
    normalized_model = str(model or "").strip().casefold()
    registry_prices, registry_provenance = _frozen_openrouter_registry_price_index()
    if normalized_provider in {"", "openrouter"}:
        if normalized_model in registry_prices:
            return registry_prices[normalized_model], {
                **registry_provenance,
                "registry_provider": "openrouter",
                "registry_model_id": normalized_model,
            }
        if normalized_model == "qwen/qwen3.8-max":
            return ResolvedModelPrice(
                PriceEntry(
                    input_per_m=2.0,
                    output_per_m=6.0,
                    cache_read_per_m=0.25,
                    cache_write_per_m=2.5,
                ),
                "static_table",
            ), {
                "source": "static_table",
                "snapshot_version": "",
                "snapshot_canonical_sha256": "",
                "registry_provider": "",
                "registry_model_id": "",
            }
        # An empty provider is the legacy OpenRouter form used by older
        # receipts.  A registry miss must remain unknown: the layered pricing
        # resolver can consult the live OpenRouter catalog, which would make a
        # supposedly frozen repair depend on the network and current prices.
        return None, {
            **registry_provenance,
            "registry_provider": "openrouter",
            "registry_model_id": normalized_model,
        }
    resolved = resolve_model_price(model, provider)
    price_source = str(resolved.source or "").strip().casefold()
    if price_source in {"local_free", "static_table"}:
        return resolved, {
            "source": price_source,
            "snapshot_version": "",
            "snapshot_canonical_sha256": "",
            "registry_provider": "",
            "registry_model_id": "",
        }
    return None, {
        "source": price_source or "unknown",
        "snapshot_version": "",
        "snapshot_canonical_sha256": "",
        "registry_provider": "",
        "registry_model_id": "",
    }


def _stored_estimate_price_source(unit: Mapping[str, Any]) -> str:
    provider_usage = unit.get("provider_usage")
    if not isinstance(provider_usage, Mapping):
        return ""
    pricing = provider_usage.get("estimate_pricing")
    nested_source = pricing.get("source") if isinstance(pricing, Mapping) else None
    return str(nested_source or provider_usage.get("price_source") or "").strip().casefold()


def _discard_non_frozen_stored_estimate(unit: dict[str, Any]) -> bool:
    """Remove a legacy mutable-price estimate while retaining its provenance."""

    source = str(unit.get("cost_source") or "none").strip().casefold()
    estimated_value = _first_usage_cost(
        unit,
        "estimated_cost_usd",
        "estimatedCostUsd",
    )
    if not source.startswith("opensquilla_") or estimated_value is None:
        return False
    price_source = _stored_estimate_price_source(unit)
    if price_source in {
        "frozen_openrouter_model_registry",
        "local_free",
        "static_table",
    }:
        return False
    provider_usage = (
        dict(unit.get("provider_usage"))
        if isinstance(unit.get("provider_usage"), Mapping)
        else {}
    )
    provider_usage["discarded_cost_estimate_provenance"] = {
        "reason": "non_frozen_price_source",
        "cost_source": source,
        "price_source": price_source or "unknown",
        "estimated_cost_usd": float(estimated_value),
    }
    unit["provider_usage"] = provider_usage
    for key in ("estimated_cost_usd", "estimatedCostUsd", "cost_usd", "costUsd"):
        unit.pop(key, None)
    unit["cost_source"] = "none"
    return True


def estimate_missing_usage_costs(
    usage: Any,
    *,
    default_provider: str = "",
    default_model: str = "",
) -> bool:
    """Fill missing dollars only from repository-frozen prices, never as exact.

    ``resolve_model_price`` also supports mutable user overrides, live
    OpenRouter endpoint prices, and a generic default.  Those sources are
    useful for interactive estimates but are not reproducible campaign
    evidence, so they must leave the request cost unknown here.
    """

    if not isinstance(usage, dict):
        return False
    breakdown = usage.get("model_usage_breakdown")
    units = (
        [item for item in breakdown if isinstance(item, dict)]
        if isinstance(breakdown, list) and breakdown
        else [usage]
    )
    changed = False
    estimated_total = 0.0
    all_units_priced = True
    for unit in units:
        changed |= _discard_non_frozen_stored_estimate(unit)
        source = str(unit.get("cost_source") or "none").strip().casefold()
        exact_cost = exact_provider_usage_cost(unit)
        already_known = (
            exact_cost is not None
            or (
                source.startswith("opensquilla_")
                and _first_usage_cost(
                    unit,
                    "estimated_cost_usd",
                    "estimatedCostUsd",
                    "cost_usd",
                    "costUsd",
                )
                is not None
            )
            or (source == "mixed" and _mixed_usage_cost(unit) is not None)
        )
        if already_known:
            known_cost = (
                exact_cost
                if exact_cost is not None
                else _mixed_usage_cost(unit)
                if source == "mixed"
                else _first_usage_cost(
                    unit,
                    "estimated_cost_usd",
                    "estimatedCostUsd",
                    "cost_usd",
                    "costUsd",
                )
            )
            estimated_total += float(known_cost or 0.0)
            continue
        model = str(unit.get("model") or default_model or "")
        provider = str(unit.get("provider") or default_provider or "")
        input_tokens = coerce_metric_int(unit.get("input_tokens"))
        output_tokens = coerce_metric_int(unit.get("output_tokens"))
        cached_tokens = max(
            coerce_metric_int(unit.get("cache_read_tokens")),
            coerce_metric_int(unit.get("cached_tokens")),
        )
        cache_write_tokens = coerce_metric_int(unit.get("cache_write_tokens"))
        if not model or not any((input_tokens, output_tokens, cached_tokens, cache_write_tokens)):
            all_units_priced = False
            continue
        resolved, price_provenance = _frozen_estimate_price(model, provider)
        if resolved is None:
            all_units_priced = False
            unavailable_reason = (
                "frozen_registry_price_unavailable"
                if price_provenance.get("source")
                == "frozen_openrouter_model_registry"
                else "non_frozen_price_source"
            )
            provider_usage = (
                dict(unit.get("provider_usage"))
                if isinstance(unit.get("provider_usage"), Mapping)
                else {}
            )
            rejected_provenance = {
                "status": "unavailable",
                "reason": unavailable_reason,
                "provider": provider,
                "model": model,
                **price_provenance,
            }
            if provider_usage.get("cost_estimate_provenance") != rejected_provenance:
                provider_usage["cost_estimate_provenance"] = rejected_provenance
                unit["provider_usage"] = provider_usage
                changed = True
            continue
        estimate = estimate_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            price=resolved.entry,
        )
        unit["estimated_cost_usd"] = float(estimate.cost_usd)
        unit["cost_usd"] = float(estimate.cost_usd)
        unit["cost_source"] = "opensquilla_static_estimate"
        provider_usage = (
            dict(unit.get("provider_usage"))
            if isinstance(unit.get("provider_usage"), Mapping)
            else {}
        )
        provider_usage.update(
            {
                "cost_repair": "token_price_estimate",
                "estimate_basis": estimate.basis,
                "price_source": resolved.source,
                "estimate_pricing": {
                    **price_provenance,
                    "provider": provider,
                    "model": model,
                    "input_per_m": float(resolved.entry.input_per_m),
                    "output_per_m": float(resolved.entry.output_per_m),
                    "cache_read_per_m": (
                        float(resolved.entry.cache_read_per_m)
                        if resolved.entry.cache_read_per_m is not None
                        else None
                    ),
                    "cache_write_per_m": (
                        float(resolved.entry.cache_write_per_m)
                        if resolved.entry.cache_write_per_m is not None
                        else None
                    ),
                },
            }
        )
        unit["provider_usage"] = provider_usage
        estimated_total += float(estimate.cost_usd)
        changed = True
    if isinstance(breakdown, list) and breakdown and all_units_priced:
        usage["estimated_cost_usd"] = estimated_total
        usage["cost_usd"] = estimated_total
        if str(usage.get("cost_source") or "none").casefold() not in {
            "provider_billed",
            "mixed",
        }:
            usage["cost_source"] = "opensquilla_static_estimate"
    return changed


def repair_row_cost_metadata_with_estimates(row: dict[str, Any]) -> bool:
    """Estimate every retained generation/Judge receipt without rerunning models."""

    provider_spec = row.get("provider_spec")
    default_provider = (
        str(provider_spec.get("provider") or "") if isinstance(provider_spec, Mapping) else ""
    )
    default_model = (
        str(provider_spec.get("model") or "") if isinstance(provider_spec, Mapping) else ""
    )
    changed = estimate_missing_usage_costs(
        row.get("usage"),
        default_provider=default_provider,
        default_model=default_model,
    )
    execution = row.get("execution")
    attempts = execution.get("generation_attempts") if isinstance(execution, Mapping) else None
    if isinstance(attempts, list):
        for attempt in attempts:
            run = attempt.get("run") if isinstance(attempt, Mapping) else None
            if isinstance(run, dict):
                changed |= estimate_missing_usage_costs(
                    run.get("usage"),
                    default_provider=default_provider,
                    default_model=default_model,
                )
    for judge in [row.get("judge"), *(row.get("candidate_judges") or [])]:
        for run in iter_judge_attempt_runs(judge):
            changed |= estimate_missing_usage_costs(run.get("usage"))
    return changed


def _unclosed_stream_count(value: Any) -> int:
    """Count explicit unclosed physical streams without treating them as execution errors."""

    if isinstance(value, Mapping):
        count = int(value.get("stream_closed") is False)
        return count + sum(_unclosed_stream_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_unclosed_stream_count(item) for item in value)
    return 0


def _ensemble_execution_degraded_reasons(value: Any) -> list[str]:
    """Extract delivery degradation without rewriting failed physical evidence."""

    reasons: list[str] = []
    if isinstance(value, Mapping):
        if value.get("execution_outcome") == "degraded_success" or value.get(
            "delivery_outcome"
        ) in {"degraded_success", "partial_usable"}:
            reasons.append("aggregator_partial_usable")
        if (
            value.get("schema") == "opensquilla.ensemble-aggregator-recovery/v1"
            and value.get("degraded") is True
        ):
            reasons.append("aggregator_recovery_degraded")
        if (
            value.get("fallback_used") is True
            and value.get("final_request_role") == "aggregator"
        ):
            reasons.append("aggregator_fallback_used")
        candidates = value.get("candidates")
        if isinstance(candidates, list) and any(
            isinstance(candidate, Mapping)
            and candidate.get("selected_for_aggregation") is True
            and candidate.get("completion_outcome") == "partial_usable"
            and candidate.get("usable_for_aggregation") is True
            for candidate in candidates
        ):
            reasons.append("partial_proposer_used")
        for item in value.values():
            reasons.extend(_ensemble_execution_degraded_reasons(item))
    elif isinstance(value, list):
        for item in value:
            reasons.extend(_ensemble_execution_degraded_reasons(item))
    return list(dict.fromkeys(reasons))


def execution_status_payload(
    *,
    generation_accepted: bool,
    final_text: str,
    run_error: str | None,
    ensemble_trace: Any,
) -> dict[str, Any]:
    """Project model execution independently from policy and billing audits."""

    if (
        not generation_accepted
        or not str(final_text or "").strip()
        or (
            run_error
            and not (isinstance(ensemble_trace, Mapping) and ensemble_trace)
        )
    ):
        return {
            "status": "execution_failed",
            "success": False,
            "degraded_reasons": [],
        }
    unclosed = _unclosed_stream_count(ensemble_trace)
    degraded_reasons = _ensemble_execution_degraded_reasons(ensemble_trace)
    if run_error and isinstance(ensemble_trace, Mapping) and ensemble_trace:
        degraded_reasons.append("provider_error_after_visible_ensemble_output")
    if unclosed:
        degraded_reasons.append("unclosed_physical_stream")
    degraded_reasons = list(dict.fromkeys(degraded_reasons))
    return {
        "status": "degraded_success" if degraded_reasons else "success",
        "success": True,
        "degraded_reasons": degraded_reasons,
        "unclosed_stream_count": unclosed,
    }


def row_audit_status(
    row: Mapping[str, Any],
    *,
    non_byok_audit: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Describe evidence quality without changing whether the task executed."""

    accounting = row.get("cost_accounting")
    llm = (
        accounting.get("actual_llm_total")
        if isinstance(accounting, Mapping)
        and isinstance(accounting.get("actual_llm_total"), Mapping)
        else {}
    )
    exact = bool(llm.get("cost_exact"))
    complete = bool(llm.get("cost_complete"))
    estimated = coerce_metric_int(llm.get("estimated_request_count"))
    mixed = coerce_metric_int(llm.get("mixed_request_count"))
    unknown = coerce_metric_int(llm.get("unknown_request_count"))
    if exact:
        cost_status = "exact"
    elif complete and estimated and mixed:
        cost_status = "mixed_estimated"
    elif complete and estimated:
        cost_status = "estimated"
    elif complete and mixed:
        cost_status = "mixed"
    elif complete:
        cost_status = "recorded"
    else:
        cost_status = "unknown"

    usage_unknown = max(
        coerce_metric_int(row.get("usage_unknown_count")),
        usage_unknown_count_from_usage_payload(row.get("usage")),
        ensemble_usage_unknown_count(row.get("ensemble_trace")),
    )
    unclosed = _unclosed_stream_count(row.get("ensemble_trace"))
    policy_status = (
        str(non_byok_audit.get("status") or "unverified")
        if isinstance(non_byok_audit, Mapping)
        else "not_required"
    )
    policy_compliant = (
        non_byok_audit.get("pass") is True
        if isinstance(non_byok_audit, Mapping)
        else None
    )
    warnings: list[str] = []
    stored_generation_warnings = row.get("generation_audit_warnings")
    if isinstance(stored_generation_warnings, list):
        warnings.extend(
            str(reason).strip()
            for reason in stored_generation_warnings
            if str(reason).strip()
        )
    if policy_compliant is False:
        warnings.append(f"openrouter_non_byok_{policy_status}")
    if not complete:
        warnings.append("llm_cost_unknown")
    elif not exact:
        warnings.append(f"llm_cost_{cost_status}")
    if usage_unknown:
        warnings.append("usage_unknown")
    if unclosed:
        warnings.append("unclosed_physical_stream")
    return {
        "status": "warning" if warnings else "pass",
        "warnings": list(dict.fromkeys(warnings)),
        "separate_from_execution": True,
        "policy": {
            "status": policy_status,
            "compliant": policy_compliant,
            "evidence": dict(non_byok_audit) if isinstance(non_byok_audit, Mapping) else None,
        },
        "cost": {
            "status": cost_status,
            "complete": complete,
            "exact": exact,
            "estimated_request_count": estimated,
            "mixed_request_count": mixed,
            "unknown_request_count": unknown,
        },
        "usage_unknown_count": usage_unknown,
        "unclosed_stream_count": unclosed,
    }


def usage_cost_accounting(
    usage: Any,
    *,
    expected_requests: int = 0,
    scope: str,
) -> dict[str, Any]:
    usage_dict = usage if isinstance(usage, dict) else {}
    breakdown = usage_dict.get("model_usage_breakdown")
    units = (
        [item for item in breakdown if isinstance(item, dict)]
        if isinstance(breakdown, list) and breakdown
        else ([usage_dict] if usage_dict else [])
    )
    raw_observed = len(units)
    units = deduplicate_stable_usage_receipts(units)
    observed = len(units)
    request_count = max(max(0, int(expected_requests)), observed)
    counts = {"exact": 0, "estimated": 0, "mixed": 0, "unknown": 0}
    tokens = {"exact": 0, "estimated": 0, "mixed": 0, "unknown": 0}
    recorded_cost = 0.0
    for unit in units:
        source = str(unit.get("cost_source") or "none").strip().casefold()
        cost: float | None = None
        exact_cost = exact_provider_usage_cost(unit)
        if exact_cost is not None:
            cost = exact_cost
            category = "exact" if cost is not None else "unknown"
        elif source.startswith("opensquilla_"):
            cost = _first_usage_cost(
                unit,
                "estimated_cost_usd",
                "estimatedCostUsd",
                "cost_usd",
                "costUsd",
            )
            category = "estimated" if cost is not None else "unknown"
        elif source == "mixed":
            cost = _mixed_usage_cost(unit)
            category = "mixed" if cost is not None else "unknown"
        else:
            category = "unknown"
        counts[category] += 1
        tokens[category] += _usage_token_count(unit)
        if cost is not None and category != "unknown":
            recorded_cost += cost
    missing = max(0, request_count - observed)
    counts["unknown"] += missing
    known_requests = counts["exact"] + counts["estimated"] + counts["mixed"]
    total_tokens = sum(tokens.values())
    return {
        "scope": scope,
        "request_count": request_count,
        "usage_observed_request_count": observed,
        "duplicate_stable_receipt_count": raw_observed - observed,
        "exact_request_count": counts["exact"],
        "estimated_request_count": counts["estimated"],
        "mixed_request_count": counts["mixed"],
        "unknown_request_count": counts["unknown"],
        "total_tokens": total_tokens,
        "exact_tokens": tokens["exact"],
        "estimated_tokens": tokens["estimated"],
        "mixed_tokens": tokens["mixed"],
        "unknown_tokens": tokens["unknown"],
        "recorded_cost_usd": recorded_cost,
        "known_request_coverage_pct": (
            known_requests / request_count * 100.0 if request_count else 100.0
        ),
        "exact_request_coverage_pct": (
            counts["exact"] / request_count * 100.0 if request_count else 100.0
        ),
        "known_token_coverage_pct": (
            (total_tokens - tokens["unknown"]) / total_tokens * 100.0
            if total_tokens
            else (100.0 if not counts["unknown"] else 0.0)
        ),
        "cost_complete": counts["unknown"] == 0,
        "cost_exact": (
            counts["unknown"] == 0 and counts["estimated"] == 0 and counts["mixed"] == 0
        ),
        # Internal-only provenance lets nested account merges deduplicate a
        # physical response across attempts and scopes. It is stripped before
        # result rows are serialized.
        "_stable_usage_receipts": units,
        "_receipt_provenance_complete": True,
    }


def merge_cost_accounting(scope: str, accounts: list[dict[str, Any]]) -> dict[str, Any]:
    if all(
        account.get("_receipt_provenance_complete") is True
        and isinstance(account.get("_stable_usage_receipts"), list)
        for account in accounts
    ):
        combined_units = [
            unit
            for account in accounts
            for unit in account["_stable_usage_receipts"]
            if isinstance(unit, Mapping)
        ]
        merged = usage_cost_accounting(
            {"model_usage_breakdown": combined_units} if combined_units else None,
            expected_requests=sum(
                max(0, coerce_metric_int(account.get("request_count"))) for account in accounts
            ),
            scope=scope,
        )
        merged["duplicate_stable_receipt_count"] += sum(
            max(0, coerce_metric_int(account.get("duplicate_stable_receipt_count")))
            for account in accounts
        )
        return merged

    merged_numeric: dict[str, int | float] = {
        "request_count": 0,
        "usage_observed_request_count": 0,
        "duplicate_stable_receipt_count": 0,
        "exact_request_count": 0,
        "estimated_request_count": 0,
        "mixed_request_count": 0,
        "unknown_request_count": 0,
        "total_tokens": 0,
        "exact_tokens": 0,
        "estimated_tokens": 0,
        "mixed_tokens": 0,
        "unknown_tokens": 0,
        "recorded_cost_usd": 0.0,
    }
    for account in accounts:
        for key in tuple(merged_numeric):
            value = account.get(key)
            if isinstance(value, int | float):
                merged_numeric[key] += value
    requests = int(merged_numeric["request_count"])
    known_requests = requests - int(merged_numeric["unknown_request_count"])
    total_tokens = int(merged_numeric["total_tokens"])
    merged: dict[str, Any] = {"scope": scope, **merged_numeric}
    merged.update(
        {
            "known_request_coverage_pct": (
                known_requests / requests * 100.0 if requests else 100.0
            ),
            "exact_request_coverage_pct": (
                int(merged_numeric["exact_request_count"]) / requests * 100.0 if requests else 100.0
            ),
            "known_token_coverage_pct": (
                (total_tokens - int(merged_numeric["unknown_tokens"])) / total_tokens * 100.0
                if total_tokens
                else (100.0 if not merged_numeric["unknown_request_count"] else 0.0)
            ),
            "cost_complete": int(merged_numeric["unknown_request_count"]) == 0,
            "cost_exact": (
                int(merged_numeric["unknown_request_count"]) == 0
                and int(merged_numeric["estimated_request_count"]) == 0
                and int(merged_numeric["mixed_request_count"]) == 0
            ),
        }
    )
    return merged


def public_cost_accounting(value: Any) -> Any:
    """Remove internal receipt provenance before serializing result rows."""

    if isinstance(value, Mapping):
        return {
            key: public_cost_accounting(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [public_cost_accounting(item) for item in value]
    return value


def summarized_run_expected_request_count(run: Mapping[str, Any]) -> int:
    """Preserve explicit zero-request evidence while supporting legacy rows."""

    declared = coerce_metric_int(run.get("llm_request_count"))
    trace_events = run.get("trace_events")
    if isinstance(trace_events, list):
        for event in reversed(trace_events):
            if not isinstance(event, Mapping):
                continue
            physical = event.get("physical_request_count")
            if isinstance(physical, int) and not isinstance(physical, bool):
                return max(declared, max(0, physical)) if physical else 0
            if event.get("request_started") is False:
                return 0
            if str(event.get("code") or "").strip() in _NO_PHYSICAL_REQUEST_GATE_CODES:
                return 0
    # Legacy summaries do not prove whether the request started.
    return max(1, declared)


def actual_generation_spend_accounting(row: dict[str, Any]) -> dict[str, Any]:
    accounts: list[dict[str, Any]] = []
    execution = row.get("execution") or {}
    attempts = execution.get("generation_attempts") if isinstance(execution, dict) else None
    observed_attempts = 0
    if isinstance(attempts, list):
        for attempt in attempts:
            run = attempt.get("run") if isinstance(attempt, dict) else None
            if not isinstance(run, dict):
                continue
            observed_attempts += 1
            accounts.append(
                usage_cost_accounting(
                    run.get("usage"),
                    expected_requests=summarized_run_expected_request_count(run),
                    scope="generation_attempt",
                )
            )
    actual_metrics = row.get("actual_spend_metrics")
    declared_attempts = max(
        coerce_metric_int(row.get("generation_attempt_count")),
        coerce_metric_int(
            actual_metrics.get("generation_attempt_count")
            if isinstance(actual_metrics, dict)
            else 0
        ),
    )
    if declared_attempts > observed_attempts:
        accounts.append(
            usage_cost_accounting(
                None,
                expected_requests=declared_attempts - observed_attempts,
                scope="missing_generation_attempt",
            )
        )
    if not accounts:
        expected_requests = row_llm_request_count(row)
        if str(row.get("final_text") or "").strip():
            expected_requests = max(1, expected_requests)
        accounts.append(
            usage_cost_accounting(
                row.get("usage"),
                expected_requests=expected_requests,
                scope="generation_attempt",
            )
        )
    return merge_cost_accounting("actual_generation_spend", accounts)


def generation_cost_accounting(row: dict[str, Any]) -> dict[str, Any]:
    """Account for only the selected generation attempt."""

    if row.get("selected_generation_succeeded") is False:
        return usage_cost_accounting(
            None,
            expected_requests=0,
            scope="selected_generation_attempt",
        )
    return usage_cost_accounting(
        row.get("usage"),
        expected_requests=row_llm_request_count(row),
        scope="selected_generation_attempt",
    )


def iter_judge_attempt_runs(judge: Any):
    if not isinstance(judge, dict):
        return
    prior_attempts = judge.get("prior_judge_attempts")
    if isinstance(prior_attempts, list):
        for attempt in prior_attempts:
            run = attempt.get("run") if isinstance(attempt, dict) else None
            if isinstance(run, dict):
                yield run
    criteria = judge.get("criterion_judgments")
    if isinstance(criteria, list):
        for criterion in criteria:
            attempts = criterion.get("judge_attempts") if isinstance(criterion, dict) else None
            if not isinstance(attempts, list):
                continue
            for attempt in attempts:
                run = attempt.get("run") if isinstance(attempt, dict) else None
                if isinstance(run, dict):
                    yield run
        return
    attempts = judge.get("judge_attempts")
    if isinstance(attempts, list):
        for attempt in attempts:
            run = attempt.get("run") if isinstance(attempt, dict) else None
            if isinstance(run, dict):
                yield run


def judge_cost_accounting(judge: Any, *, scope: str) -> dict[str, Any]:
    if not isinstance(judge, dict):
        return merge_cost_accounting(scope, [])
    if (
        judge.get("judge_cost_exempt") is True
        or str(judge.get("judge_model") or "").casefold() == "dry-run"
    ):
        return merge_cost_accounting(scope, [])
    runs = list(iter_judge_attempt_runs(judge))
    accounts = [
        usage_cost_accounting(
            run.get("usage"),
            expected_requests=summarized_run_expected_request_count(run),
            scope=f"{scope}_attempt",
        )
        for run in runs
    ]
    declared_attempts = coerce_metric_int(judge.get("judge_attempt_count"))
    criteria = judge.get("criterion_judgments")
    if isinstance(criteria, list):
        declared_attempts = max(
            declared_attempts,
            sum(
                max(
                    1,
                    coerce_metric_int(item.get("judge_attempt_count")),
                )
                for item in criteria
                if isinstance(item, dict)
            ),
            coerce_metric_int(judge.get("criteria_count")),
        )
    elif judge.get("score_status") or judge.get("error") or judge.get("judge_run"):
        declared_attempts = max(1, declared_attempts)
    if declared_attempts > len(runs):
        accounts.append(
            usage_cost_accounting(
                None,
                expected_requests=declared_attempts - len(runs),
                scope=f"{scope}_missing_attempt",
            )
        )
    return merge_cost_accounting(scope, accounts)


def external_tool_cost_accounting(
    row: dict[str, Any],
    *,
    actual_spend: bool = False,
) -> dict[str, Any]:
    tool_policy = row.get("tool_policy") or {}
    mode = str(tool_policy.get("tool_mode") or TOOL_MODE_PROVIDER_ONLY)
    actual_metrics = row.get("actual_spend_metrics") or {}
    tool_calls = (
        coerce_metric_int(actual_metrics.get("total_tool_call_count"))
        if actual_spend and isinstance(actual_metrics, dict)
        else row_total_tool_call_count(row)
    )
    local = tool_policy.get("local_web_tools") or {}
    search = local.get("web_search") or {}
    fetch = local.get("web_fetch") or {}
    provider = str(search.get("provider") or "")
    firecrawl_allowed = bool(fetch.get("allow_firecrawl"))
    untracked_paid_path_configured = mode == TOOL_MODE_LOCAL_WEB_TOOLS and (
        provider == "brave" or firecrawl_allowed
    )
    untracked_cost_possible = untracked_paid_path_configured and tool_calls > 0
    return {
        "scope": "actual_external_tools" if actual_spend else "external_tools",
        "tool_call_count": tool_calls,
        "recorded_cost_usd": 0.0,
        "estimated_cost_usd": None,
        "potentially_unpriced_tool_call_count_upper_bound": (
            tool_calls if untracked_cost_possible else 0
        ),
        "cost_complete": not untracked_cost_possible,
        "cost_exact": not untracked_cost_possible,
        "cost_status": "unknown" if untracked_cost_possible else "exact",
        "cost_precision": "unknown" if untracked_cost_possible else "exact",
        "recorded_cost_is_lower_bound": untracked_cost_possible,
        "recorded_cost_usd_is_lower_bound": untracked_cost_possible,
        "separate_from_task_completion": True,
        "note": (
            "Brave/Firecrawl spend is not returned by the local tool API"
            if untracked_cost_possible
            else "no untracked paid local tool path configured"
        ),
    }


def row_cost_accounting(row: dict[str, Any]) -> dict[str, Any]:
    generation = generation_cost_accounting(row)
    actual_generation = actual_generation_spend_accounting(row)
    judge = judge_cost_accounting(row.get("judge"), scope="judge")
    candidate_accounts = [
        judge_cost_accounting(item, scope="candidate_judge")
        for item in (row.get("candidate_judges") or [])
    ]
    candidate_judge = merge_cost_accounting("candidate_judge", candidate_accounts)
    llm_total = merge_cost_accounting("llm_total", [generation, judge, candidate_judge])
    actual_llm_total = merge_cost_accounting(
        "actual_llm_total",
        [actual_generation, judge, candidate_judge],
    )
    external = external_tool_cost_accounting(row)
    actual_external = external_tool_cost_accounting(row, actual_spend=True)
    return {
        "generation": generation,
        "selected_generation_attempt": generation,
        "actual_generation_spend": actual_generation,
        "judge": judge,
        "candidate_judge": candidate_judge,
        "llm_total": llm_total,
        "actual_llm_total": actual_llm_total,
        "external_tools": external,
        "actual_external_tools": actual_external,
        "recorded_total_cost_usd": float(llm_total["recorded_cost_usd"])
        + float(external["recorded_cost_usd"]),
        "actual_spend_recorded_total_cost_usd": (
            float(actual_llm_total["recorded_cost_usd"])
            + float(actual_external["recorded_cost_usd"])
        ),
        "result_llm_cost_complete": bool(llm_total["cost_complete"]),
        "result_llm_cost_exact": bool(llm_total["cost_exact"]),
        "actual_llm_cost_complete": bool(actual_llm_total["cost_complete"]),
        "actual_llm_cost_exact": bool(actual_llm_total["cost_exact"]),
        "actual_spend_llm_cost_complete": bool(actual_llm_total["cost_complete"]),
        "actual_spend_llm_cost_exact": bool(actual_llm_total["cost_exact"]),
        "external_cost_complete": bool(external["cost_complete"]),
        "external_cost_exact": bool(external["cost_exact"]),
        "actual_external_cost_complete": bool(actual_external["cost_complete"]),
        "actual_external_cost_exact": bool(actual_external["cost_exact"]),
        "result_cost_complete": bool(llm_total["cost_complete"])
        and bool(external["cost_complete"]),
        "result_cost_exact": bool(llm_total["cost_exact"]) and bool(external["cost_exact"]),
        "actual_spend_cost_complete": bool(actual_llm_total["cost_complete"])
        and bool(actual_external["cost_complete"]),
        "actual_spend_cost_exact": bool(actual_llm_total["cost_exact"])
        and bool(actual_external["cost_exact"]),
        "recorded_total_cost_is_lower_bound": not (
            bool(llm_total["cost_complete"]) and bool(external["cost_complete"])
        ),
        "actual_spend_recorded_total_cost_is_lower_bound": not (
            bool(actual_llm_total["cost_complete"]) and bool(actual_external["cost_complete"])
        ),
        "scope_note": (
            "Compatibility cost fields use the selected generation attempt plus recorded "
            "Judge attempts. actual_spend_* includes every generation attempt. "
            "Lifecycle completeness is gated on actual LLM spend; unpriced local Web "
            "tools remain a separate explicit lower-bound gap. Whole-experiment spend "
            "must also retain failed/replaced shards and preflight calls."
        ),
    }


def _usage_units_for_openrouter_non_byok_audit(
    usage: Any,
) -> list[Mapping[str, Any]]:
    if not isinstance(usage, Mapping):
        return []
    if not usage:
        return []
    breakdown = usage.get("model_usage_breakdown")
    if isinstance(breakdown, list) and breakdown:
        return [item for item in breakdown if isinstance(item, Mapping)]
    return [usage]


def _actual_llm_usage_units_for_openrouter_non_byok_audit(
    row: dict[str, Any],
) -> list[Mapping[str, Any]]:
    units: list[Mapping[str, Any]] = []
    execution = row.get("execution")
    attempts = execution.get("generation_attempts") if isinstance(execution, Mapping) else None
    observed_generation_run = False
    if isinstance(attempts, list):
        for attempt in attempts:
            run = attempt.get("run") if isinstance(attempt, Mapping) else None
            if not isinstance(run, Mapping):
                continue
            observed_generation_run = True
            units.extend(_usage_units_for_openrouter_non_byok_audit(run.get("usage")))
    actual_metrics = row.get("actual_spend_metrics")
    declared_generation_attempts = max(
        coerce_metric_int(row.get("generation_attempt_count")),
        coerce_metric_int(
            actual_metrics.get("generation_attempt_count")
            if isinstance(actual_metrics, Mapping)
            else 0
        ),
    )
    if not observed_generation_run and declared_generation_attempts <= 0:
        units.extend(_usage_units_for_openrouter_non_byok_audit(row.get("usage")))
    for judge in [row.get("judge"), *(row.get("candidate_judges") or [])]:
        if not isinstance(judge, dict):
            continue
        if (
            judge.get("judge_cost_exempt") is True
            or str(judge.get("judge_model") or "").casefold() == "dry-run"
        ):
            continue
        for run in iter_judge_attempt_runs(judge):
            units.extend(_usage_units_for_openrouter_non_byok_audit(run.get("usage")))
    return deduplicate_stable_usage_receipts(units)


def _independent_openrouter_policy_evidence(
    row: dict[str, Any],
) -> list[Mapping[str, Any]]:
    """Return non-physical policy evidence without changing request counts."""

    usage_payloads: list[Mapping[str, Any]] = []
    execution = row.get("execution")
    attempts = execution.get("generation_attempts") if isinstance(execution, Mapping) else None
    observed_generation_run = False
    if isinstance(attempts, list):
        for attempt in attempts:
            run = attempt.get("run") if isinstance(attempt, Mapping) else None
            usage = run.get("usage") if isinstance(run, Mapping) else None
            if isinstance(usage, Mapping):
                observed_generation_run = True
                usage_payloads.append(usage)
    actual_metrics = row.get("actual_spend_metrics")
    declared_generation_attempts = max(
        coerce_metric_int(row.get("generation_attempt_count")),
        coerce_metric_int(
            actual_metrics.get("generation_attempt_count")
            if isinstance(actual_metrics, Mapping)
            else 0
        ),
    )
    if not observed_generation_run and declared_generation_attempts <= 0:
        usage = row.get("usage")
        if isinstance(usage, Mapping):
            usage_payloads.append(usage)
    for judge in [row.get("judge"), *(row.get("candidate_judges") or [])]:
        if not isinstance(judge, dict):
            continue
        if (
            judge.get("judge_cost_exempt") is True
            or str(judge.get("judge_model") or "").casefold() == "dry-run"
        ):
            continue
        for run in iter_judge_attempt_runs(judge):
            usage = run.get("usage")
            if isinstance(usage, Mapping):
                usage_payloads.append(usage)

    evidence: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for usage in usage_payloads:
        units = [usage]
        breakdown = usage.get("model_usage_breakdown")
        if isinstance(breakdown, list):
            units.extend(item for item in breakdown if isinstance(item, Mapping))
        for unit in units:
            provider_usage = unit.get("provider_usage")
            raw_evidence = (
                provider_usage.get(IGNORED_AGENT_DONE_POLICY_EVIDENCE_KEY)
                if isinstance(provider_usage, Mapping)
                else None
            )
            if not isinstance(raw_evidence, list):
                continue
            for item in raw_evidence:
                if not isinstance(item, Mapping):
                    continue
                fingerprint = canonical_json_sha256(item)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                evidence.append(item)
    return evidence


def classify_openrouter_non_byok_unit(
    unit: Mapping[str, Any],
    *,
    provider_routing: Mapping[str, str] | None = None,
) -> str:
    """Classify policy evidence without mistaking missing metadata for BYOK."""

    provider = str(unit.get("provider") or "").strip().casefold()
    if provider and provider != "openrouter":
        return "conflict"
    provider_usage = unit.get("provider_usage")
    if not isinstance(provider_usage, Mapping):
        return "unverified"
    stable_receipt_evidence = provider_usage.get(STABLE_RECEIPT_EVIDENCE_KEY)
    if isinstance(stable_receipt_evidence, Mapping):
        if stable_receipt_evidence.get("receipt_conflict") is True or stable_receipt_evidence.get(
            "conflict_fields"
        ):
            return "conflict"
        evidence_providers = {
            str(value).strip().casefold()
            for value in stable_receipt_evidence.get("providers") or []
            if str(value).strip()
        }
        if any(value != "openrouter" for value in evidence_providers):
            return "conflict"
        evidence_byok_values = {
            value
            for key in (
                "usage_is_byok_values",
                "router_is_byok_values",
            )
            for value in stable_receipt_evidence.get(key) or []
            if value is True or value is False
        }
        if len(evidence_byok_values) > 1:
            return "conflict"
        if True in evidence_byok_values:
            return "explicit_byok"
    router_metadata = provider_usage.get("router_metadata")
    router_is_byok = (
        router_metadata.get("is_byok") if isinstance(router_metadata, Mapping) else None
    )
    usage_is_byok = provider_usage.get("is_byok")
    if (usage_is_byok is True and router_is_byok is False) or (
        usage_is_byok is False and router_is_byok is True
    ):
        return "conflict"
    if usage_is_byok is True or router_is_byok is True:
        return "explicit_byok"
    pin_state = _openrouter_router_provider_metadata_pin_state(
        unit,
        provider_routing=provider_routing,
    )
    if pin_state == "conflict":
        return "conflict"
    if _openrouter_non_byok_receipt_is_exact(
        unit,
        provider_routing=provider_routing,
    ):
        return "exact"
    return "unverified"


def openrouter_non_byok_audit(
    row: dict[str, Any],
    *,
    provider_routing: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    # Keep this stricter audit separate from ordinary cost accounting. Native
    # receipts and legacy openrouter_usage remain valid cost evidence, but do
    # not prove the OpenRouter non-BYOK execution policy by themselves.
    accounting = row_cost_accounting(row)
    llm_total = accounting.get("actual_llm_total") or {}
    request_count = coerce_metric_int(llm_total.get("request_count"))
    units = _actual_llm_usage_units_for_openrouter_non_byok_audit(row)
    categories = {
        "exact": 0,
        "unverified": 0,
        "explicit_byok": 0,
        "conflict": 0,
    }
    for unit in units:
        category = classify_openrouter_non_byok_unit(
            unit,
            provider_routing=provider_routing,
        )
        categories[category] += 1
    independent_evidence = _independent_openrouter_policy_evidence(row)
    independent_explicit_byok_count = sum(
        1 for item in independent_evidence if item.get("classification") == "explicit_byok"
    )
    independent_conflict_count = sum(
        1 for item in independent_evidence if item.get("classification") == "conflict"
    )
    evidence_unit_count = sum(categories.values())
    missing_evidence_request_count = max(0, request_count - evidence_unit_count)
    evidence_overflow_count = max(0, evidence_unit_count - request_count)
    verified_count = min(request_count, categories["exact"])
    explicit_byok_count = categories["explicit_byok"]
    conflict_count = categories["conflict"]
    unverified_count = categories["unverified"] + missing_evidence_request_count
    policy_safe_to_continue = (
        explicit_byok_count == 0
        and conflict_count == 0
        and independent_explicit_byok_count == 0
        and independent_conflict_count == 0
        and evidence_overflow_count == 0
    )
    passed = (
        request_count > 0
        and evidence_unit_count == request_count
        and categories["exact"] == request_count
        and categories["unverified"] == 0
        and policy_safe_to_continue
    )
    return {
        "pass": passed,
        "policy": "every recorded or expected LLM request must prove is_byok=false",
        "status": (
            "exact"
            if passed
            else "metadata_incomplete"
            if policy_safe_to_continue
            else "policy_violation"
        ),
        "policy_safe_to_continue": policy_safe_to_continue,
        "request_count": request_count,
        "exact_request_count": verified_count,
        "unverified_request_count": unverified_count,
        "explicit_byok_request_count": explicit_byok_count,
        "conflict_request_count": conflict_count,
        "independent_policy_evidence_count": len(independent_evidence),
        "independent_explicit_byok_evidence_count": (independent_explicit_byok_count),
        "independent_conflict_evidence_count": independent_conflict_count,
        "unverified_or_byok_request_count": (
            max(0, request_count - verified_count) + evidence_overflow_count
        ),
        "evidence_unit_count": evidence_unit_count,
        "missing_evidence_request_count": missing_evidence_request_count,
        "evidence_overflow_count": evidence_overflow_count,
        "recorded_request_count": evidence_unit_count,
        "missing_expected_request_count": missing_evidence_request_count,
        "unexpected_recorded_request_count": evidence_overflow_count,
        "request_count_match": evidence_unit_count == request_count,
        "note": (
            "pass requires every request to carry OpenRouter provider identity, "
            "is_byok=false at usage and router levels, serving-provider metadata, "
            "a response id, and matching provider-reported cost; generic billing "
            "receipts and legacy openrouter_usage do not satisfy this policy"
        ),
    }


def row_server_tool_call_count(row: dict[str, Any]) -> int:
    if row.get("selected_generation_succeeded") is False:
        return 0
    if row.get("server_tool_call_count") is not None:
        return row_metric_int(row, "server_tool_call_count")
    usage = row.get("usage") or {}
    if isinstance(usage, dict):
        return sum(server_tool_counts_from_usage_payload(usage).values())
    return 0


def row_total_tool_call_count(row: dict[str, Any]) -> int:
    if row.get("selected_generation_succeeded") is False:
        return 0
    if row.get("total_tool_call_count") is not None:
        return row_metric_int(row, "total_tool_call_count")
    stream_count = row_metric_int(row, "stream_tool_call_count", "tool_call_count")
    return stream_count + row_server_tool_call_count(row)


def row_llm_request_count(row: dict[str, Any]) -> int:
    if row.get("selected_generation_succeeded") is False:
        return 0
    provider_spec = row.get("provider_spec") or {}
    execution = row.get("execution") or {}
    default_request_count = int(
        provider_spec.get("kind") == "single" and not execution.get("provider_error")
    )
    usage = row.get("usage")
    if isinstance(usage, Mapping) and isinstance(
        usage.get("model_usage_breakdown"),
        list,
    ):
        usage = dict(usage)
        usage["model_usage_breakdown"] = deduplicate_stable_usage_receipts(
            [
                dict(unit) if isinstance(unit, Mapping) else unit
                for unit in usage["model_usage_breakdown"]
            ]
        )
    # Row-level accounting is an audit boundary: never let a stale/undersized
    # scalar hide additional distinct receipts.  Newly persisted runs are
    # canonicalized strictly, while legacy or deliberately adversarial rows
    # are charged/audited at the larger of their declaration and evidence.
    evidence_count = derive_physical_request_count(
        {"usage": usage},
        default_request_count=default_request_count,
    )
    declared_count = (
        row_metric_int(row, "llm_request_count") if row.get("llm_request_count") is not None else 0
    )
    return max(evidence_count, declared_count)


def ensemble_usage_unknown_count(trace: Any) -> int:
    if not isinstance(trace, dict):
        return 0
    direct_missing = max(
        0,
        coerce_metric_int(trace.get("usage_missing_count")),
    )
    detected_missing = 0
    calls = trace.get("calls")
    if isinstance(calls, list):
        detected_missing = sum(ensemble_usage_unknown_count(call) for call in calls)
    early_stop = trace.get("proposer_early_stop")
    if isinstance(early_stop, dict):
        detected_missing = max(
            detected_missing,
            coerce_metric_int(early_stop.get("usage_unknown_count")),
        )
    candidates = trace.get("candidates")
    if isinstance(candidates, list):
        detected_missing = max(
            detected_missing,
            sum(
                1
                for candidate in candidates
                if isinstance(candidate, dict) and candidate.get("error_code") == "early_stopped"
            ),
        )
    return max(direct_missing, detected_missing)


def usage_unknown_count_from_usage_payload(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    breakdown = usage.get("model_usage_breakdown")
    units = (
        [item for item in breakdown if isinstance(item, dict)]
        if isinstance(breakdown, list) and breakdown
        else [usage]
    )
    represented_missing = sum(
        1
        for item in units
        if (
            str(item.get("role") or "").strip().casefold() in MISSING_USAGE_PLACEHOLDER_ROLES
            or item.get("error_code") == "early_stopped"
            or str(item.get("cost_source") or "none") == "unknown_canceled"
        )
    )
    unknown_receipt_cost = sum(
        1
        for item in units
        if (
            str(item.get("role") or "").strip().casefold() not in MISSING_USAGE_PLACEHOLDER_ROLES
            and item.get("error_code") != "early_stopped"
            and str(item.get("cost_source") or "none") != "unknown_canceled"
            and (
                _usage_token_count(item) > 0
                and exact_provider_usage_cost(item) is None
                and str(item.get("cost_source") or "none") != "mixed"
                and not str(item.get("cost_source") or "none").startswith("opensquilla_")
            )
        )
    )
    explicit_missing = max(
        0,
        coerce_metric_int(usage.get("usage_missing_count")),
    )
    return unknown_receipt_cost + max(explicit_missing, represented_missing)


def row_generation_attempt_usage_unknown_count(row: dict[str, Any]) -> int:
    execution = row.get("execution") or {}
    attempts = execution.get("generation_attempts") if isinstance(execution, dict) else None
    if not isinstance(attempts, list):
        return 0
    total = 0
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        run = attempt.get("run")
        if not isinstance(run, dict):
            continue
        total += max(
            coerce_metric_int(run.get("usage_unknown_count")),
            usage_unknown_count_from_usage_payload(run.get("usage")),
            ensemble_usage_unknown_count(run.get("ensemble_trace")),
        )
    return total


def row_usage_unknown_count(row: dict[str, Any]) -> int:
    direct = row_metric_int(row, "usage_unknown_count")
    execution = row.get("execution") or {}
    execution_value = 0
    if isinstance(execution, dict):
        execution_value = coerce_metric_int(execution.get("usage_unknown_count"))
    return max(
        direct,
        execution_value,
        row_generation_attempt_usage_unknown_count(row),
        usage_unknown_count_from_usage_payload(row.get("usage")),
        ensemble_usage_unknown_count(row.get("ensemble_trace")),
    )


def row_trajectory_steps(row: dict[str, Any]) -> int:
    llm_requests = row_llm_request_count(row)
    tool_calls = row_total_tool_call_count(row)
    if llm_requests or tool_calls:
        return llm_requests + tool_calls
    return row_metric_int(row, "trajectory_steps")


def completed_quality_value(row: dict[str, Any]) -> float:
    if row.get("error"):
        return 0.0
    value = row.get("quality_total")
    return float(value) if isinstance(value, int | float) else 0.0


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"groups": {}}
    judging_enabled = any(isinstance(row.get("judge"), dict) for row in rows)
    for group in sorted({row["group"] for row in rows}):
        group_rows = [row for row in rows if row["group"] == group]
        completed_rows = [row for row in group_rows if not row.get("error")]
        latencies = [int(row.get("latency_ms") or 0) for row in group_rows]
        scored_totals = [
            completed_quality_value(row)
            for row in completed_rows
            if row["quality_total"] is not None
        ]
        quality_values = (
            [completed_quality_value(row) for row in group_rows] if judging_enabled else []
        )
        pass_rates = [
            float((row.get("judge") or {}).get("pass_rate"))
            for row in completed_rows
            if isinstance((row.get("judge") or {}).get("pass_rate"), int | float)
        ]
        # Recompute with the current accounting contract. Persisted rows may
        # carry an older/stale cost_accounting object and must not bypass new
        # completeness gates.
        cost_accounts = [row_cost_accounting(row) for row in group_rows]
        completed_cost_accounts = [row_cost_accounting(row) for row in completed_rows]
        generation_costs = [
            float(account["generation"]["recorded_cost_usd"]) for account in cost_accounts
        ]
        actual_generation_costs = [
            float(account["actual_generation_spend"]["recorded_cost_usd"])
            for account in cost_accounts
        ]
        judge_costs = [float(account["judge"]["recorded_cost_usd"]) for account in cost_accounts]
        candidate_judge_costs = [
            float(account["candidate_judge"]["recorded_cost_usd"]) for account in cost_accounts
        ]
        costs = [float(account["recorded_total_cost_usd"]) for account in cost_accounts]
        actual_spend_costs = [
            float(account["actual_spend_recorded_total_cost_usd"]) for account in cost_accounts
        ]
        completed_costs = [
            float(account["recorded_total_cost_usd"]) for account in completed_cost_accounts
        ]
        group_llm_cost = merge_cost_accounting(
            "group_llm_total",
            [account["llm_total"] for account in cost_accounts],
        )
        actual_group_llm_cost = merge_cost_accounting(
            "actual_group_llm_total",
            [account["actual_llm_total"] for account in cost_accounts],
        )
        visible_tokens = [
            int(row_usage_number(row, "input_tokens")) + int(row_usage_number(row, "output_tokens"))
            for row in group_rows
        ]
        reasoning_tokens = [int(row_usage_number(row, "reasoning_tokens")) for row in group_rows]
        all_tokens = list(visible_tokens)
        stream_tool_calls = [
            row_metric_int(row, "stream_tool_call_count", "tool_call_count") for row in group_rows
        ]
        server_tool_calls = [row_server_tool_call_count(row) for row in group_rows]
        total_tool_calls = [row_total_tool_call_count(row) for row in group_rows]
        trajectory_steps = [row_trajectory_steps(row) for row in group_rows]
        llm_requests = [row_llm_request_count(row) for row in group_rows]
        usage_unknown = [
            int(account["llm_total"]["unknown_request_count"]) for account in cost_accounts
        ]
        summary["groups"][group] = {
            "rows": len(group_rows),
            "task_ids": sorted(str(row.get("task_id") or "") for row in group_rows),
            "completed": len(completed_rows),
            "scored_rows": len(scored_totals),
            "score_coverage_pct": (
                len(scored_totals) / len(group_rows) * 100.0 if group_rows else 0.0
            ),
            "avg_quality": statistics.mean(quality_values) if quality_values else None,
            "avg_quality_scored": (statistics.mean(scored_totals) if scored_totals else None),
            "avg_pass_rate": statistics.mean(pass_rates) if pass_rates else None,
            "judge_errors": sum(
                int((row.get("judge") or {}).get("judge_error_count") or 0)
                for row in completed_rows
            ),
            "avg_cost_usd": statistics.mean(costs) if costs else 0.0,
            "avg_cost_completed_usd": (
                statistics.mean(completed_costs) if completed_costs else None
            ),
            "recorded_total_cost_usd": sum(costs),
            "avg_actual_spend_cost_usd": (
                statistics.mean(actual_spend_costs) if actual_spend_costs else 0.0
            ),
            "actual_spend_recorded_total_cost_usd": sum(actual_spend_costs),
            "recorded_generation_cost_usd": sum(generation_costs),
            "actual_spend_generation_cost_usd": sum(actual_generation_costs),
            "recorded_judge_cost_usd": sum(judge_costs),
            "recorded_candidate_judge_cost_usd": sum(candidate_judge_costs),
            "avg_recorded_generation_cost_usd": (
                statistics.mean(generation_costs) if generation_costs else 0.0
            ),
            "avg_actual_spend_generation_cost_usd": (
                statistics.mean(actual_generation_costs) if actual_generation_costs else 0.0
            ),
            "avg_recorded_judge_cost_usd": (statistics.mean(judge_costs) if judge_costs else 0.0),
            "avg_recorded_candidate_judge_cost_usd": (
                statistics.mean(candidate_judge_costs) if candidate_judge_costs else 0.0
            ),
            "known_cost_request_coverage_pct": group_llm_cost["known_request_coverage_pct"],
            "exact_cost_request_coverage_pct": group_llm_cost["exact_request_coverage_pct"],
            "unknown_cost_request_count": group_llm_cost["unknown_request_count"],
            "unknown_cost_tokens": group_llm_cost["unknown_tokens"],
            "llm_cost_complete_rows": sum(
                1 for account in cost_accounts if account["llm_total"]["cost_complete"]
            ),
            "result_cost_complete_rows": sum(
                1 for account in cost_accounts if account["result_cost_complete"]
            ),
            "actual_spend_known_cost_request_coverage_pct": actual_group_llm_cost[
                "known_request_coverage_pct"
            ],
            "actual_spend_unknown_cost_request_count": actual_group_llm_cost[
                "unknown_request_count"
            ],
            "actual_spend_cost_complete_rows": sum(
                1 for account in cost_accounts if account["actual_spend_cost_complete"]
            ),
            "avg_visible_tokens": (statistics.mean(visible_tokens) if visible_tokens else 0.0),
            "avg_reasoning_tokens": (
                statistics.mean(reasoning_tokens) if reasoning_tokens else 0.0
            ),
            "avg_total_tokens": statistics.mean(all_tokens) if all_tokens else 0.0,
            "avg_stream_tool_calls": (
                statistics.mean(stream_tool_calls) if stream_tool_calls else 0.0
            ),
            "avg_server_tool_calls": (
                statistics.mean(server_tool_calls) if server_tool_calls else 0.0
            ),
            "avg_tool_calls": (statistics.mean(total_tool_calls) if total_tool_calls else 0.0),
            "total_tool_calls": sum(total_tool_calls),
            "tool_call_rate_pct": (
                sum(1 for count in total_tool_calls if count > 0) / len(total_tool_calls) * 100.0
                if total_tool_calls
                else 0.0
            ),
            "avg_trajectory_steps": (
                statistics.mean(trajectory_steps) if trajectory_steps else 0.0
            ),
            "avg_llm_requests": (statistics.mean(llm_requests) if llm_requests else 0.0),
            "total_llm_requests": sum(llm_requests),
            "avg_usage_unknown": (statistics.mean(usage_unknown) if usage_unknown else 0.0),
            "total_usage_unknown": sum(usage_unknown),
            "latency_p50_ms": percentile(latencies, 50),
            "latency_p95_ms": percentile(latencies, 95),
        }
    for item in summary["groups"].values():
        for baseline in ("B0", "B1"):
            baseline_item = summary["groups"].get(baseline) or {}
            suffix = baseline.lower()
            item[f"avg_quality_pct_delta_vs_{suffix}"] = numeric_pct_delta(
                item.get("avg_quality"),
                baseline_item.get("avg_quality"),
            )
            comparable_costs = (
                item.get("result_cost_complete_rows") == item.get("rows")
                and baseline_item.get("result_cost_complete_rows") == baseline_item.get("rows")
                and item.get("completed") == item.get("rows")
                and baseline_item.get("completed") == baseline_item.get("rows")
                and item.get("task_ids") == baseline_item.get("task_ids")
            )
            item[f"avg_cost_pct_delta_vs_{suffix}"] = (
                numeric_pct_delta(
                    item.get("avg_cost_usd"),
                    baseline_item.get("avg_cost_usd"),
                )
                if comparable_costs
                else None
            )
    return summary


def result_summary_fact(row: dict[str, Any]) -> DracoResultSummaryFact:
    return build_result_summary_fact(
        row,
        row_cost_accounting=row_cost_accounting,
        row_usage_number=row_usage_number,
        row_metric_int=row_metric_int,
        row_server_tool_call_count=row_server_tool_call_count,
        row_total_tool_call_count=row_total_tool_call_count,
        row_trajectory_steps=row_trajectory_steps,
        row_llm_request_count=row_llm_request_count,
    )


def summarize_facts(facts: list[DracoResultSummaryFact]) -> dict[str, Any]:
    return summarize_result_facts(
        facts,
        merge_cost_accounting=merge_cost_accounting,
    )


def render_markdown(
    summary: dict[str, Any],
    jsonl_path: Path,
    tool_policy: dict[str, Any] | None = None,
    generation_policy: dict[str, Any] | None = None,
    runner_mode: str = DEFAULT_DRACO_RUNNER_MODE,
    agent_max_iterations: int = DEFAULT_AGENT_MAX_ITERATIONS,
    agent_finalization_policy: Mapping[str, Any] | None = None,
) -> str:
    stamp = jsonl_path.stem.removeprefix("draco_ensemble_")
    trace_path = jsonl_path.parent / f"draco_run_{stamp}.trace.jsonl"
    policy = tool_policy or benchmark_tool_policy()
    generation = generation_policy or generation_thinking_policy()
    finalization = normalized_agent_finalization_policy(agent_finalization_policy)
    blocked_domains = policy.get("contamination_blocked_domains") or []
    tool_line = (
        f"Runner mode: `{runner_mode}`; tool mode: `{policy.get('tool_mode') or RUNNER_MODE}`; "
        "tools enabled: "
        f"`{str(bool(policy.get('tools_enabled'))).lower()}`"
    )
    if policy.get("tools_enabled"):
        tool_names = ", ".join(str(name) for name in policy.get("tool_names") or [])
        if tool_names:
            tool_line = f"{tool_line}; tools: `{tool_names}`."
        else:
            tool_line = f"{tool_line}."
    if not policy.get("tools_enabled"):
        tool_line = (
            f"Runner mode: `{runner_mode}`; tool mode: "
            f"`{policy.get('tool_mode') or RUNNER_MODE}`; "
            "external research tools are not attached."
        )
    group_tool_policies = policy.get("group_tool_policies") or {}
    fusion_groups = [
        str(group)
        for group, group_policy in group_tool_policies.items()
        if isinstance(group_policy, dict) and group_policy.get("openrouter_fusion_enabled")
    ]
    fusion_line = ""
    if fusion_groups:
        if not policy.get("tools_enabled"):
            tool_line = (
                f"Runner mode: `{runner_mode}`; tool mode: "
                f"`{policy.get('tool_mode') or RUNNER_MODE}`; "
                "no global external research tools are attached."
            )
        fusion_line = (
            "OpenRouter Fusion groups: "
            f"`{', '.join(sorted(fusion_groups))}` use only `openrouter:fusion` "
            "with `tool_choice=required`; Fusion's internal web_search/web_fetch "
            "domain controls are not exposed in the documented tool parameters."
        )
    generation_budget_note = f"budget: `{generation.get('thinking_budget_tokens')}`"
    if generation.get("max_thinking_budget_tokens") is not None:
        generation_budget_note = (
            f"{generation_budget_note}, "
            f"max budget: `{generation.get('max_thinking_budget_tokens')}`"
        )

    def _signed_pct(value: Any) -> str:
        return f"{float(value):+.2f}%" if isinstance(value, int | float) else ""

    lines = [
        "# DRACO Ensemble Summary",
        "",
        f"Raw JSONL: `{jsonl_path}`",
        f"Trace JSONL: `{trace_path}`",
        "",
        "Generation thinking: "
        f"`{generation.get('generation_thinking')}` "
        f"(enabled: `{generation.get('thinking_enabled')}`, "
        f"level: `{generation.get('thinking_level')}`, "
        f"{generation_budget_note}, "
        f"temperature: `{generation.get('temperature')}`).",
        f"Agent max iterations: `{agent_max_iterations}`.",
        "Agent finalization policy: "
        f"`{json.dumps(finalization, ensure_ascii=False, sort_keys=True)}`.",
        tool_line,
        *([fusion_line] if fusion_line else []),
        "Contamination blocked domains: "
        f"`{', '.join(blocked_domains) if blocked_domains else '(none)'}`.",
        "Cost accounting: selected columns contain only the accepted generation attempt "
        "plus rubric/candidate Judge attempts; actual-spend columns contain every "
        "generation attempt plus those Judge attempts. Unpriced requests remain unknown "
        "rather than being treated as $0; selected cost deltas are blank unless both "
        "groups are complete. Preflight and replaced/failed shard spend must still be "
        "audited at the whole-experiment level.",
        "",
        "| Group | Rows | Done | Avg Quality | AvgQ Scored | Avg Pass | "
        "Judge Err | Avg Selected LLM $ | Avg Selected Gen $ | Avg Actual LLM $ | "
        "Avg Actual Gen $ | Avg Judge $ | Selected Known Cost % | Actual Known Cost % | "
        "Selected Complete | Actual Complete | Avg Visible | Avg Reason | Avg Tokens | "
        "Avg Tools | Tool % | "
        "Avg Steps | Avg LLM Req | Unknown Cost Req | p50 ms | p95 ms | "
        "AvgQ % vs B0 | Avg$ % vs B0 | "
        "AvgQ % vs B1 | Avg$ % vs B1 |",
        "| --- |" + " ---: |" * 29,
    ]
    for group, item in sorted(summary["groups"].items()):
        lines.append(
            "| {group} | {rows} | {done} | {quality} | {quality_scored} | "
            "{pass_rate} | {judge_errors} | {cost:.6f} | {generation_cost:.6f} | "
            "{actual_cost:.6f} | {actual_generation_cost:.6f} | {judge_cost:.6f} | "
            "{known_cost:.1f}% | {actual_known_cost:.1f}% | "
            "{cost_complete}/{rows} | {actual_cost_complete}/{rows} | "
            "{visible_tokens:.1f} | {reasoning_tokens:.1f} | "
            "{tokens:.1f} | {tool_calls:.1f} | {tool_rate:.1f}% | "
            "{steps:.1f} | {llm_requests:.1f} | {usage_unknown:.1f} | "
            "{p50:.0f} | {p95:.0f} | "
            "{q_b0} | {cost_b0} | {q_b1} | {cost_b1} |".format(
                group=group,
                rows=item["rows"],
                done=item["completed"],
                quality=(f"{item['avg_quality']:.2f}" if item["avg_quality"] is not None else ""),
                quality_scored=(
                    f"{item['avg_quality_scored']:.2f}"
                    if item["avg_quality_scored"] is not None
                    else ""
                ),
                pass_rate=(
                    f"{item['avg_pass_rate']:.2f}" if item["avg_pass_rate"] is not None else ""
                ),
                judge_errors=item["judge_errors"],
                cost=item["avg_cost_usd"],
                generation_cost=item["avg_recorded_generation_cost_usd"],
                actual_cost=item["avg_actual_spend_cost_usd"],
                actual_generation_cost=item["avg_actual_spend_generation_cost_usd"],
                judge_cost=(
                    item["avg_recorded_judge_cost_usd"]
                    + item["avg_recorded_candidate_judge_cost_usd"]
                ),
                known_cost=item["known_cost_request_coverage_pct"],
                actual_known_cost=item["actual_spend_known_cost_request_coverage_pct"],
                cost_complete=item["result_cost_complete_rows"],
                actual_cost_complete=item["actual_spend_cost_complete_rows"],
                visible_tokens=item["avg_visible_tokens"],
                reasoning_tokens=item["avg_reasoning_tokens"],
                tokens=item["avg_total_tokens"],
                tool_calls=item["avg_tool_calls"],
                tool_rate=item["tool_call_rate_pct"],
                steps=item["avg_trajectory_steps"],
                llm_requests=item["avg_llm_requests"],
                usage_unknown=item["avg_usage_unknown"],
                p50=item["latency_p50_ms"],
                p95=item["latency_p95_ms"],
                q_b0=_signed_pct(item.get("avg_quality_pct_delta_vs_b0")),
                cost_b0=_signed_pct(item.get("avg_cost_pct_delta_vs_b0")),
                q_b1=_signed_pct(item.get("avg_quality_pct_delta_vs_b1")),
                cost_b1=_signed_pct(item.get("avg_cost_pct_delta_vs_b1")),
            )
        )
    return "\n".join(lines) + "\n"


def manifest_args(args: argparse.Namespace) -> dict[str, Any]:
    keys = [
        "input",
        "config",
        "experiment_config",
        "experiment_config_override",
        "experiment_config_set",
        "output_dir",
        "groups",
        "task_ids",
        "max_tasks",
        "concurrency",
        "timeout",
        "ensemble_proposer_timeout",
        "ensemble_aggregator_timeout",
        "ensemble_proposer_early_stop_success_count",
        "ensemble_proposer_early_stop_after",
        "expand_ensemble_timeouts_to_task_timeout",
        "runner_mode",
        "agent_max_iterations",
        *AGENT_FINALIZATION_POLICY_FIELDS,
        "require_clean_source",
        "dry_run",
        "judge_model",
        "judge_repeats",
        "judge_concurrency",
        "judge_max_attempts",
        "judge_candidates",
        "generation_max_attempts",
        "generation_max_tokens",
        "generation_retry_backoff",
        "tool_mode",
        "contamination_blocked_domains",
        "local_web_search_provider",
        "local_web_search_api_key_env",
        "allow_firecrawl_web_fetch",
        "require_openrouter_non_byok",
        "continue_after_cost_audit_failure",
        "openrouter_web_search_engine",
        "openrouter_web_search_max_results",
        "openrouter_web_search_max_total_results",
        "openrouter_web_search_context_size",
        "openrouter_web_fetch_engine",
        "openrouter_web_fetch_max_uses",
        "openrouter_web_fetch_max_content_tokens",
        "openrouter_fusion_analysis_models",
        "openrouter_fusion_model",
        "openrouter_fusion_max_tool_calls",
        "openrouter_fusion_max_completion_tokens",
        "openrouter_fusion_reasoning_effort",
        "openrouter_fusion_temperature",
    ]

    def _json_value(value: Any) -> Any:
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, list):
            return [_json_value(item) for item in value]
        return value

    payload: dict[str, Any] = {}
    for key in keys:
        payload[key] = _json_value(getattr(args, key, None))
    selection_plan_mode = getattr(
        args,
        "selection_plan_evidence_mode",
        DEFAULT_SELECTION_PLAN_EVIDENCE_MODE,
    )
    if selection_plan_mode != DEFAULT_SELECTION_PLAN_EVIDENCE_MODE:
        payload["selection_plan_evidence_mode"] = selection_plan_mode
    bundle = getattr(args, "_draco_experiment_config_bundle", None)
    if isinstance(bundle, DracoExperimentConfigBundle):
        effective_path = getattr(args, "_effective_experiment_config_path", None)
        payload["experiment_config"] = str(effective_path or bundle.base_path)
        payload["experiment_config_override"] = []
        payload["experiment_config_set"] = []
    return payload


def reconstructed_cli_args(args: argparse.Namespace) -> list[str]:
    cli_args: list[str] = []
    for key, value in manifest_args(args).items():
        if value is None or value == "" or value is False:
            continue
        flag = f"--{key.replace('_', '-')}"
        if value is True:
            cli_args.append(flag)
        elif isinstance(value, list):
            for item in value:
                cli_args.extend([flag, str(item)])
        else:
            cli_args.extend([flag, str(value)])
    return cli_args


def command_argv(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        *reconstructed_cli_args(args),
    ]


def command_payload(args: argparse.Namespace) -> dict[str, Any]:
    argv = command_argv(args)
    pythonpath = os.environ.get("PYTHONPATH", "")
    shell = shlex.join(argv)
    if pythonpath:
        shell = f"PYTHONPATH={shlex.quote(pythonpath)} {shell}"
    return {
        "cwd": str(Path.cwd()),
        "python": sys.executable,
        "argv": argv,
        "shell": shell,
        "pythonpath": pythonpath,
        "parsed_args": manifest_args(args),
        "replay_validation": gateway_replay_validation_contract(),
    }


def validate_formal_openrouter_runtime_transport(
    config: GatewayConfig,
) -> dict[str, Any]:
    return _shared_openrouter_transport(
        config,
        resolve_runtime_config=resolve_llm_runtime_config,
        provider_spec_resolver=get_provider_spec,
    )


def validate_strict_openrouter_non_byok_environment(
    config: GatewayConfig,
) -> dict[str, Any]:
    return _shared_strict_non_byok_environment(
        config,
        resolve_runtime_config=resolve_llm_runtime_config,
        provider_spec_resolver=get_provider_spec,
    )


def resolved_llm_runtime_contract(config: GatewayConfig) -> dict[str, Any]:
    return _shared_runtime_contract(
        config,
        resolve_runtime_config=resolve_llm_runtime_config,
    )


def source_provenance() -> dict[str, Any]:
    runner_path = Path(__file__).resolve()
    payload: dict[str, Any] = {
        "runner_path": str(runner_path),
        "runner_sha256": hashlib.sha256(runner_path.read_bytes()).hexdigest(),
    }
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        tracked_diff = subprocess.run(
            [
                "git",
                "diff",
                "--binary",
                "HEAD",
                "--",
                "scripts",
                "src",
                "configs",
                "pyproject.toml",
                "uv.lock",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        untracked = subprocess.run(
            [
                "git",
                "ls-files",
                "--others",
                "--exclude-standard",
                "--",
                "scripts",
                "src",
                "configs",
                "pyproject.toml",
                "uv.lock",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.splitlines()
        source_digest = hashlib.sha256()
        source_digest.update(head.encode("utf-8"))
        source_digest.update(b"\0")
        source_digest.update(tracked_diff.encode("utf-8"))
        for relative in sorted(untracked):
            untracked_path = ROOT / relative
            if not untracked_path.is_file():
                continue
            source_digest.update(b"\0")
            source_digest.update(relative.encode("utf-8"))
            source_digest.update(b"\0")
            source_digest.update(hashlib.sha256(untracked_path.read_bytes()).digest())
        tracked_dirty = bool(tracked_diff)
        payload.update(
            {
                "git_head": head,
                "git_tracked_dirty": tracked_dirty,
                "git_untracked_file_count": len(untracked),
                "git_dirty": tracked_dirty or bool(untracked),
                "source_tree_sha256": source_digest.hexdigest(),
            }
        )
    except (OSError, subprocess.SubprocessError):
        payload.update(
            {
                "git_head": None,
                "git_tracked_dirty": None,
                "git_untracked_file_count": None,
                "git_dirty": None,
                "source_tree_sha256": None,
            }
        )
    return payload


def build_run_compatibility(
    *,
    args: argparse.Namespace,
    config: GatewayConfig,
    groups: list[str],
    group_tool_policies: dict[str, dict[str, Any]],
    generation_policy: dict[str, Any],
) -> dict[str, Any]:
    bundle = getattr(args, "_draco_experiment_config_bundle", None)
    effective_experiment_config = (
        bundle.config.model_dump(mode="json")
        if isinstance(bundle, DracoExperimentConfigBundle)
        else None
    )
    if isinstance(effective_experiment_config, dict):
        if not effective_experiment_config.get("router_dynamic_ranking_override"):
            effective_experiment_config.pop("router_dynamic_ranking_override", None)
        ensemble_config = effective_experiment_config.get("ensemble")
        if isinstance(ensemble_config, dict):
            # Legacy compatibility input only; the frozen ranking JSON owns
            # the router_dynamic backup roster size.
            ensemble_config.pop("proposer_backup_count", None)
        # Scheduling-only concurrency may change between a canary, a full run,
        # and retry waves without changing any model, prompt, or scoring semantics.
        runner_config = effective_experiment_config.get("runner")
        if isinstance(runner_config, dict):
            runner_config.pop("concurrency", None)
        judge_config = effective_experiment_config.get("judge")
        if isinstance(judge_config, dict):
            judge_config.pop("concurrency", None)
    global_experiment_profile = None
    if isinstance(effective_experiment_config, dict):
        global_experiment_profile = {
            key: effective_experiment_config[key]
            for key in (
                "schema_version",
                "profile_id",
                "benchmark_input",
                "g1_routing",
                "timeouts",
                "runner",
                "generation",
                "tools",
                "judge",
            )
            if key in effective_experiment_config
        }
    source = dict(getattr(args, "_source_provenance", {}) or {})
    source_identity = {
        "git_head": source.get("git_head"),
        "source_tree_sha256": source.get("source_tree_sha256"),
    }
    gateway_contract = gateway_execution_contract(config)
    runtime_contract = resolved_llm_runtime_contract(config)
    contracts: dict[str, dict[str, Any]] = {}
    fingerprints: dict[str, str] = {}
    for group in groups:
        tool_contract = json.loads(json.dumps(group_tool_policies[group], ensure_ascii=False))
        local_tool_contract = tool_contract.get("local_web_tools")
        if isinstance(local_tool_contract, dict):
            local_tool_contract.pop("preflight", None)
        contract = {
            "schema": RUN_COMPATIBILITY_SCHEMA,
            "benchmark": "DRACO",
            "durable_artifact_capability": durable_artifact_capability_contract(),
            "group": group,
            "group_spec": GROUP_SPECS[group],
            "source_identity": source_identity,
            "runner": {
                "mode": args.runner_mode,
                "agent_max_iterations": args.agent_max_iterations,
                "finalization_policy": agent_finalization_policy_from_args(args),
            },
            "tools": tool_contract,
            "generation": {
                "policy": generation_policy,
                "max_attempts": args.generation_max_attempts,
                "retry_backoff_seconds": args.generation_retry_backoff,
            },
            "judge": {
                "model": args.judge_model,
                "repeats": args.judge_repeats,
                "max_attempts": args.judge_max_attempts,
                "judge_candidates": args.judge_candidates,
            },
            "timeouts": {
                "task_seconds": args.timeout,
                "proposer_seconds": args.ensemble_proposer_timeout,
                "aggregator_seconds": args.ensemble_aggregator_timeout,
                "proposer_early_stop_success_count": (
                    args.ensemble_proposer_early_stop_success_count
                ),
                "proposer_early_stop_after_seconds": args.ensemble_proposer_early_stop_after,
                "expand_to_task_timeout": args.expand_ensemble_timeouts_to_task_timeout,
            },
            "gateway_execution": gateway_contract,
            "resolved_llm_runtime": runtime_contract,
            "cost_policy": {
                "require_openrouter_non_byok": bool(
                    getattr(args, "require_openrouter_non_byok", False)
                )
            },
            "global_experiment_profile": global_experiment_profile,
            "experiment_config": (
                {"sha256": canonical_json_sha256(effective_experiment_config)}
                if GROUP_SPECS[group].get("experiment_config")
                and isinstance(effective_experiment_config, dict)
                else None
            ),
            "g1_registry_contract": (
                dict(getattr(args, "_g1_registry_contract", {}) or {}) if group == "G1" else None
            ),
            "task_analyzer_execution_contract": (
                dict(
                    getattr(
                        args,
                        "_task_analyzer_execution_contract",
                        {},
                    )
                    or {}
                )
                if group == "G1"
                else None
            ),
            "formal_runtime_freeze": dict(getattr(args, "_formal_runtime_freeze", {}) or {}),
            "dry_run": bool(args.dry_run),
        }
        contracts[group] = contract
        fingerprints[group] = canonical_json_sha256(contract)
    return {
        "schema": RUN_COMPATIBILITY_SCHEMA,
        "fingerprints": fingerprints,
        "contracts": contracts,
    }


def write_command_file(
    path: Path,
    *,
    args: argparse.Namespace,
    stamp: str,
) -> dict[str, Any]:
    payload = command_payload(args)
    lines = [
        "# DRACO benchmark command",
        f"stamp: {stamp}",
        f"cwd: {payload['cwd']}",
        f"python: {payload['python']}",
    ]
    if payload["pythonpath"]:
        lines.append(f"PYTHONPATH: {payload['pythonpath']}")
    lines.extend(
        [
            "",
            payload["shell"],
            "",
            "# Parsed args",
            json.dumps(payload["parsed_args"], ensure_ascii=False, indent=2, sort_keys=True),
            "",
            "# Replay validation",
            json.dumps(payload["replay_validation"], ensure_ascii=False, sort_keys=True),
            "",
        ]
    )
    atomic_write_text(path, "\n".join(lines))
    return payload


def write_experiment_config_artifacts(
    output_dir: Path,
    *,
    args: argparse.Namespace,
    stamp: str,
) -> dict[str, str]:
    return publish_experiment_config_artifacts(
        output_dir,
        args=args,
        stamp=stamp,
        canonical_json_sha256=canonical_json_sha256,
        replay_validation_contract=gateway_replay_validation_contract,
    )


def write_manifest(
    path: Path,
    *,
    args: argparse.Namespace,
    stamp: str,
    status: str,
    started_at: float,
    tasks: list[dict[str, Any]],
    groups: list[str],
    artifacts: dict[str, str],
    rows_written: int = 0,
    finished_at: float | None = None,
    summary: dict[str, Any] | None = None,
    tool_policy: dict[str, Any] | None = None,
    command: dict[str, Any] | None = None,
    failure: dict[str, Any] | None = None,
    selection_plan_enabled: bool = False,
    selection_plan_binding: Mapping[str, Any] | None = None,
) -> None:
    policy = tool_policy or benchmark_tool_policy(args)
    generation_policy = generation_thinking_policy(args)
    payload: dict[str, Any] = {
        "schema": DRACO_RUN_MANIFEST_SCHEMA_V2,
        "benchmark": "DRACO",
        "durable_artifact_capability": durable_artifact_capability_contract(),
        "runner": f"scripts/{Path(__file__).name}",
        "runner_mode": getattr(args, "runner_mode", DEFAULT_DRACO_RUNNER_MODE),
        "agent_max_iterations": getattr(
            args,
            "agent_max_iterations",
            DEFAULT_AGENT_MAX_ITERATIONS,
        ),
        "agent_finalization_policy": agent_finalization_policy_from_args(args),
        "tool_policy": policy,
        "generation_policy": generation_policy,
        "stamp": stamp,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_ms": (int((finished_at - started_at) * 1000) if finished_at is not None else None),
        "args": manifest_args(args),
        "groups": groups,
        "group_specs": {group: GROUP_SPECS[group] for group in groups},
        "task_count": len(tasks),
        "task_ids": [str(task["id"]) for task in tasks],
        "rows_written": rows_written,
        "artifacts": artifacts,
        "effective_argument_sources": dict(getattr(args, "_effective_argument_sources", {}) or {}),
        "source_provenance": dict(getattr(args, "_source_provenance", None) or source_provenance()),
    }
    run_compatibility = getattr(args, "_run_compatibility", None)
    if isinstance(run_compatibility, dict):
        payload["run_compatibility"] = run_compatibility
    benchmark_alignments = dict(getattr(args, "_benchmark_alignments", {}) or {})
    if benchmark_alignments:
        payload["benchmark_alignments"] = benchmark_alignments
    g1_registry_contract = getattr(args, "_g1_registry_contract", None)
    if isinstance(g1_registry_contract, Mapping):
        payload["g1_registry_contract"] = dict(g1_registry_contract)
    task_analyzer_execution_contract = getattr(
        args,
        "_task_analyzer_execution_contract",
        None,
    )
    if isinstance(task_analyzer_execution_contract, Mapping):
        payload["task_analyzer_execution_contract"] = dict(
            task_analyzer_execution_contract
        )
    formal_runtime_freeze = getattr(args, "_formal_runtime_freeze", None)
    if isinstance(formal_runtime_freeze, Mapping):
        payload["formal_runtime_freeze"] = dict(formal_runtime_freeze)
    input_validation = getattr(args, "_draco_input_validation", None)
    if input_validation is not None:
        payload["benchmark_input_validation"] = input_validation
    if command is not None:
        payload["command"] = command
    if summary is not None:
        payload["summary"] = summary
    if failure is not None:
        payload["failure"] = failure
    if selection_plan_enabled:
        raw_pack_path = artifacts.get(SELECTION_PLAN_PACK_ARTIFACT_FIELD)
        if not isinstance(raw_pack_path, str) or not Path(raw_pack_path).is_absolute():
            raise ValueError(
                "content-addressed selection-plan manifest lacks its absolute pack artifact"
            )
        payload[SELECTION_PLAN_EVIDENCE_ROW_FIELD] = (
            selection_plan_evidence_capability_contract()
        )
        if status == "running":
            if selection_plan_binding is not None:
                raise ValueError(
                    "running selection-plan manifest cannot publish a terminal binding"
                )
        else:
            if selection_plan_binding is None:
                raise ValueError(
                    "terminal selection-plan manifest requires its durable binding"
                )
            payload[SELECTION_PLAN_EVIDENCE_MANIFEST_FIELD] = dict(
                selection_plan_binding
            )
    elif selection_plan_binding is not None:
        raise ValueError(
            "selection-plan binding cannot be published without its capability"
        )
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


async def _amain_with_run_lock(
    args: argparse.Namespace,
    *,
    run_lock_holder: list[DracoArtifactRunLock | None],
) -> int:
    # Freeze source identity once. A long-running benchmark must not attribute
    # its final manifest to files modified after this process started.
    args._source_provenance = source_provenance()
    if getattr(args, "require_clean_source", False) and (
        args._source_provenance.get("git_dirty") is not False
    ):
        raise ValueError(
            "--require-clean-source requires a clean, readable Git worktree before launch"
        )
    groups = parse_groups(args.groups)
    alignment = apply_b2_g12_argument_alignment(args, groups)
    bundle = getattr(args, "_draco_experiment_config_bundle", None)
    experiment_config = bundle.config if isinstance(bundle, DracoExperimentConfigBundle) else None
    if experiment_config is not None:
        all_tasks = load_tasks(args.input, max_tasks=0)
        input_validation = validate_reference_input(
            args.input,
            task_ids=[str(task["id"]) for task in all_tasks],
            config=experiment_config.benchmark_input,
        )
        args._draco_input_validation = input_validation
        if alignment is not None:
            alignment["input_validation"] = input_validation
        tasks = select_tasks_by_ids(all_tasks, getattr(args, "task_ids", None))
        if args.max_tasks > 0:
            tasks = tasks[: args.max_tasks]
    elif getattr(args, "task_ids", None):
        all_tasks = load_tasks(args.input, max_tasks=0)
        tasks = select_tasks_by_ids(all_tasks, args.task_ids)
        if args.max_tasks > 0:
            tasks = tasks[: args.max_tasks]
    else:
        tasks = load_tasks(args.input, max_tasks=args.max_tasks)
    if alignment is not None:
        effective = alignment["effective_args"]
        print(
            "DRACO shared experiment profile applied: "
            f"concurrency={effective['concurrency']}, timeout={effective['timeout']}, "
            f"web={effective['local_web_search_provider']}, "
            f"judge={effective['judge_model']}",
            file=sys.stderr,
            flush=True,
        )
    args.runner_mode = str(
        getattr(args, "runner_mode", DEFAULT_DRACO_RUNNER_MODE) or DEFAULT_DRACO_RUNNER_MODE
    ).strip()
    if args.runner_mode not in SUPPORTED_RUNNER_MODES:
        raise ValueError(f"unknown runner mode: {args.runner_mode}")
    validate_runner_mode_for_groups(args.runner_mode, groups)
    agent_finalization_policy = normalize_agent_runner_args(args)
    args.generation_max_attempts = bounded_generation_attempts(
        getattr(args, "generation_max_attempts", GENERATION_MAX_ATTEMPTS)
    )
    args.generation_retry_backoff = bounded_generation_retry_backoff(
        getattr(
            args,
            "generation_retry_backoff",
            DEFAULT_GENERATION_RETRY_BACKOFF_SECONDS,
        )
    )
    tool_policy = benchmark_tool_policy(args)
    smoke_only = bool(getattr(args, "local_web_tools_smoke_only", False))
    if smoke_only and bool(getattr(args, "dry_run", False)):
        raise ValueError("--local-web-tools-smoke-only cannot be combined with --dry-run")
    if smoke_only and tool_policy.get("tool_mode") != TOOL_MODE_LOCAL_WEB_TOOLS:
        raise ValueError("--local-web-tools-smoke-only requires --tool-mode=local_web_tools")
    validate_tool_mode_for_runner(
        args.runner_mode,
        str(tool_policy.get("tool_mode") or ""),
        smoke_only=smoke_only,
    )
    generation_policy = generation_thinking_policy(args)
    config = GatewayConfig.load(args.config)
    args._formal_runtime_freeze = enforce_formal_draco_runtime_config(
        config,
        experiment_config,
        groups,
    )
    if experiment_config is not None:
        args._formal_web_search_transport = validate_formal_web_search_transport(
            config,
            experiment_config,
        )
        args._formal_openrouter_runtime_transport = (
            validate_formal_openrouter_runtime_transport(config)
        )
    # Process-local credentials take precedence over any inline value in the
    # reference config. Only an irreversible key fingerprint enters the run
    # compatibility contract.
    api_key_env = str(getattr(config.llm, "api_key_env", "") or "").strip()
    process_api_key = os.environ.get(api_key_env or "OPENROUTER_API_KEY", "").strip()
    if process_api_key:
        config.llm.api_key = process_api_key
    if getattr(args, "require_openrouter_non_byok", False) and not getattr(args, "dry_run", False):
        args._strict_openrouter_non_byok_environment = (
            validate_strict_openrouter_non_byok_environment(config)
        )
    if "G1" in groups:
        if experiment_config is None:
            raise ValueError("G1 requires an experiment config")
        args._g1_registry_contract = validate_g1_registry_contract(
            experiment_config,
            config,
        )
        from opensquilla.eval.draco_task_analyzer_execution import (
            task_analyzer_execution_contract_from_g1_registry,
            validated_task_analyzer_execution_contract,
        )

        replay = experiment_config.g1_routing.task_analysis_execution
        replay_execution_contract = getattr(
            replay,
            "source_task_analyzer_execution_contract",
            None,
        )
        if replay_execution_contract is not None:
            execution_contract = validated_task_analyzer_execution_contract(
                replay_execution_contract
            )
        else:
            execution_contract = validated_task_analyzer_execution_contract(
                task_analyzer_execution_contract_from_g1_registry(
                    args._g1_registry_contract
                )
            )
        if execution_contract is None:
            raise ValueError("G1 task Analyzer execution contract is invalid")
        args._task_analyzer_execution_contract = execution_contract
        if alignment is not None:
            alignment["g1_registry_contract"] = dict(args._g1_registry_contract)
            alignment["task_analyzer_execution_contract"] = dict(
                execution_contract
            )
    sandbox_runtime = configure_benchmark_sandbox_runtime(config, tool_policy)
    fetch_runtime = configure_local_web_fetch_runtime(tool_policy)
    search_runtime = configure_local_web_search_runtime(
        config,
        tool_policy,
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    if search_runtime or sandbox_runtime or fetch_runtime:
        local_web_tools = dict(tool_policy.get("local_web_tools") or {})
        if search_runtime:
            local_web_tools["search_runtime"] = search_runtime
        if sandbox_runtime:
            local_web_tools["sandbox_runtime"] = sandbox_runtime
        if fetch_runtime:
            local_web_tools["fetch_runtime"] = fetch_runtime
        tool_policy = {**tool_policy, "local_web_tools": local_web_tools}
    stable_group_tool_policies = benchmark_tool_policies_for_groups(
        tool_policy,
        groups,
        args=args,
    )
    args._run_compatibility = build_run_compatibility(
        args=args,
        config=config,
        groups=groups,
        group_tool_policies=stable_group_tool_policies,
        generation_policy=generation_policy,
    )
    preflight_started_at = time.time()
    try:
        web_preflight = await run_local_web_tools_preflight(
            tool_policy,
            dry_run=bool(getattr(args, "dry_run", False)),
        )
    except Exception as exc:
        if not smoke_only:
            output_dir = args.output_dir
            output_dir.mkdir(parents=True, exist_ok=True)
            failure_stamp = time.strftime("%Y%m%d-%H%M%S")
            failure_manifest = output_dir / (
                f"draco_run_{failure_stamp}.preflight-failed.manifest.json"
            )
            failure_artifacts = {"manifest_json": str(failure_manifest)}
            failure_artifacts.update(
                write_experiment_config_artifacts(
                    output_dir,
                    args=args,
                    stamp=failure_stamp,
                )
            )
            write_manifest(
                failure_manifest,
                args=args,
                stamp=failure_stamp,
                status="preflight_failed",
                started_at=preflight_started_at,
                finished_at=time.time(),
                tasks=tasks,
                groups=groups,
                artifacts=failure_artifacts,
                tool_policy=tool_policy,
                command=command_payload(args),
                failure={
                    "stage": "local_web_tools_preflight",
                    "error_class": type(exc).__name__,
                    "error": str(exc)[:1000],
                    "model_or_judge_started": False,
                },
            )
        raise
    if web_preflight:
        local_web_tools = dict(tool_policy.get("local_web_tools") or {})
        local_web_tools["preflight"] = web_preflight
        tool_policy = {**tool_policy, "local_web_tools": local_web_tools}
    if smoke_only:
        print(
            json.dumps({"local_web_tools_preflight": web_preflight}, ensure_ascii=False),
            flush=True,
        )
        return 0
    inherited = inherited_provider_config(config)
    group_tool_policies = benchmark_tool_policies_for_groups(
        tool_policy,
        groups,
        args=args,
    )
    manifest_tool_policy = {
        **tool_policy,
        "group_tool_policies": group_tool_policies,
    }
    if (
        tool_policy.get("tools_enabled")
        and tool_policy.get("tool_mode") == TOOL_MODE_OPENROUTER_SERVER_TOOLS
        and inherited.provider != "openrouter"
        and not getattr(args, "dry_run", False)
    ):
        raise ValueError(
            "--tool-mode=openrouter_server_tools requires an OpenRouter runtime provider"
        )
    if (
        any(policy.get("openrouter_fusion_enabled") for policy in group_tool_policies.values())
        and inherited.provider != "openrouter"
        and not getattr(args, "dry_run", False)
    ):
        raise ValueError(
            "OpenRouter Fusion experiment groups require an OpenRouter runtime provider"
        )
    judge_provider = None
    if args.judge_model:
        judge_provider = build_single_provider(
            inherited=inherited,
            group="judge",
            model=args.judge_model,
            dry_run=args.dry_run,
        )
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = output_dir.resolve(strict=True)
    content_addressed_selection_plans = (
        getattr(
            args,
            "selection_plan_evidence_mode",
            DEFAULT_SELECTION_PLAN_EVIDENCE_MODE,
        )
        == SELECTION_PLAN_EVIDENCE_MODE_CONTENT_ADDRESSED
    )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_started_at = time.time()
    jsonl_path = output_dir / f"draco_ensemble_{stamp}.jsonl"
    trace_path = output_dir / f"draco_run_{stamp}.trace.jsonl"
    checkpoint_path = output_dir / f"draco_run_{stamp}.checkpoint.json"
    selection_plan_pack_path = (
        output_dir / f"draco_run_{stamp}.selection-plan.pack.jsonl"
    )
    manifest_path = output_dir / f"draco_run_{stamp}.manifest.json"
    command_path = output_dir / f"draco_run_{stamp}.command.txt"
    summary_json_path = jsonl_path.with_suffix(".summary.json")
    run_lock = DracoArtifactRunLock(
        checkpoint_path.with_suffix(checkpoint_path.suffix + ".lock")
    )
    run_lock_holder[0] = run_lock
    run_lock.acquire()
    protected_paths = (
        jsonl_path,
        trace_path,
        checkpoint_path,
        manifest_path,
        command_path,
        summary_json_path,
        jsonl_path.with_suffix(".md"),
        *((selection_plan_pack_path,) if content_addressed_selection_plans else ()),
    )
    if any(path.exists() for path in protected_paths):
        run_lock.close()
        raise FileExistsError("DRACO artifact stamp already exists")
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    judge_semaphore = asyncio.Semaphore(max(1, int(getattr(args, "judge_concurrency", 1) or 1)))
    summary_facts: list[DracoResultSummaryFact] = []
    artifacts = {
        "results_jsonl": str(jsonl_path),
        "trace_jsonl": str(trace_path),
        "checkpoint_json": str(checkpoint_path),
        "manifest_json": str(manifest_path),
        "command_txt": str(command_path),
        "summary_json": str(summary_json_path),
        "summary_markdown": str(jsonl_path.with_suffix(".md")),
    }
    if content_addressed_selection_plans:
        artifacts[SELECTION_PLAN_PACK_ARTIFACT_FIELD] = str(
            selection_plan_pack_path
        )
    try:
        artifacts.update(write_experiment_config_artifacts(output_dir, args=args, stamp=stamp))
        command = write_command_file(command_path, args=args, stamp=stamp)
    except BaseException:
        run_lock.close()
        raise
    selection_plan_writer: AsyncSelectionPlanEvidenceWriter | None = None
    if content_addressed_selection_plans:
        try:
            selection_plan_writer = AsyncSelectionPlanEvidenceWriter(
                SelectionPlanPackAppender(selection_plan_pack_path)
            )
        except BaseException:
            run_lock.close()
            raise
    try:
        artifact_writer = AsyncDurableDracoArtifactWriter(
            DurableDracoArtifactWriter(
                results_path=jsonl_path,
                trace_path=trace_path,
                checkpoint_path=checkpoint_path,
            )
        )
    except BaseException:
        await _cleanup_async_resource(
            selection_plan_writer,
            label="selection-plan pack",
            preserve_primary=True,
        )
        run_lock.close()
        raise
    try:
        write_manifest(
            manifest_path,
            args=args,
            stamp=stamp,
            status="running",
            started_at=run_started_at,
            tasks=tasks,
            groups=groups,
            artifacts=artifacts,
            tool_policy=manifest_tool_policy,
            command=command,
            selection_plan_enabled=content_addressed_selection_plans,
        )
    except BaseException:
        await _cleanup_async_resource(
            artifact_writer,
            label="durable artifact writer",
            preserve_primary=True,
        )
        await _cleanup_async_resource(
            selection_plan_writer,
            label="selection-plan pack",
            preserve_primary=True,
        )
        run_lock.close()
        raise

    async def _guarded(task: dict[str, Any], group: str) -> dict[str, Any]:
        group_tool_policy = group_tool_policies[group]
        group_tools = benchmark_tools_for_policy(group_tool_policy)
        async with semaphore:
            return await run_one(
                task=task,
                group=group,
                config=config,
                inherited=inherited,
                dry_run=args.dry_run,
                judge_provider=judge_provider,
                judge_candidates=args.judge_candidates,
                judge_repeats=args.judge_repeats,
                judge_concurrency=getattr(args, "judge_concurrency", 1),
                judge_max_attempts=getattr(args, "judge_max_attempts", JUDGE_MAX_ATTEMPTS),
                judge_semaphore=judge_semaphore,
                timeout=args.timeout,
                ensemble_proposer_timeout=getattr(args, "ensemble_proposer_timeout", None),
                ensemble_aggregator_timeout=getattr(args, "ensemble_aggregator_timeout", None),
                ensemble_proposer_early_stop_success_count=getattr(
                    args,
                    "ensemble_proposer_early_stop_success_count",
                    None,
                ),
                ensemble_proposer_early_stop_after=getattr(
                    args,
                    "ensemble_proposer_early_stop_after",
                    None,
                ),
                expand_ensemble_timeouts_to_task_timeout=getattr(
                    args,
                    "expand_ensemble_timeouts_to_task_timeout",
                    False,
                ),
                tool_policy=group_tool_policy,
                generation_policy=generation_policy,
                experiment_config=(experiment_config if group in {"B2", "G1"} else None),
                runner_mode=args.runner_mode,
                output_dir=output_dir,
                agent_max_iterations=args.agent_max_iterations,
                agent_finalization_policy=agent_finalization_policy,
                generation_max_attempts=getattr(
                    args, "generation_max_attempts", GENERATION_MAX_ATTEMPTS
                ),
                generation_retry_backoff=getattr(
                    args,
                    "generation_retry_backoff",
                    DEFAULT_GENERATION_RETRY_BACKOFF_SECONDS,
                ),
                tools=group_tools,
                run_compatibility_fingerprint=args._run_compatibility["fingerprints"][group],
                g1_registry_contract=(
                    getattr(args, "_g1_registry_contract", None) if group == "G1" else None
                ),
                task_analyzer_execution_contract=(
                    getattr(
                        args,
                        "_task_analyzer_execution_contract",
                        None,
                    )
                    if group == "G1"
                    else None
                ),
                require_openrouter_non_byok=bool(
                    getattr(args, "require_openrouter_non_byok", False)
                    and not getattr(args, "dry_run", False)
                ),
            )

    expected_result_keys = {(group, str(task["id"])) for task in tasks for group in groups}
    task_window = DracoRollingTaskWindow(
        [
            lambda task=task, group=group: _guarded(task, group)
            for task in tasks
            for group in groups
        ],
        max_live_tasks=max(1, args.concurrency),
    )
    rows_persisted = 0
    try:
        async with artifact_writer:
            row_index = 0
            while (completed_task := await task_window.next_completed_task()) is not None:
                row = completed_task.result()
                row_index += 1
                row["row_index"] = row_index
                if getattr(
                    args,
                    "require_openrouter_non_byok",
                    False,
                ) and not getattr(args, "dry_run", False):
                    audit = openrouter_non_byok_audit(
                        row,
                        provider_routing=_openrouter_audit_provider_routing(
                            inherited.provider_routing,
                            (
                                getattr(args, "_g1_registry_contract", None)
                                if row.get("group") == "G1"
                                else None
                            ),
                        ),
                    )
                    row["openrouter_non_byok_audit"] = audit
                    row["audit_status"] = row_audit_status(
                        row,
                        non_byok_audit=audit,
                    )
                if selection_plan_writer is not None:
                    row = await selection_plan_writer.compact_row(row)
                row[DRACO_DURABLE_RESULT_ROW_FIELD] = durable_artifact_capability_contract()
                row = seal_result_row(row)
                trace_value = trace_row(row)
                append_outcome = await artifact_writer.append(row, trace_value)
                rows_persisted = append_outcome.paired_row_count
                if not append_outcome.committed:
                    raise RuntimeError("new DRACO run produced duplicate sealed evidence")
                summary_facts.append(result_summary_fact(row))
                task_window.release_completed(completed_task)
                print(
                    f"{row['group']} {row['task_id']} error={bool(row['error'])}",
                    flush=True,
                )
                del append_outcome, completed_task, row, trace_value
    except BaseException as exc:
        await task_window.cancel_and_wait()
        rows_persisted = artifact_writer.paired_row_count
        failure = task_window.failure_payload(
            exc,
            rows_written=rows_persisted,
        )
        pack_closed = await _cleanup_async_resource(
            selection_plan_writer,
            label="selection-plan pack",
            preserve_primary=True,
        )
        if pack_closed:
            try:
                selection_plan_binding = (
                    await _settled_to_thread(
                        terminal_selection_plan_evidence_binding,
                        pack_path=selection_plan_pack_path,
                        results_path=jsonl_path,
                        trace_path=trace_path,
                        checkpoint_path=checkpoint_path,
                    )
                    if content_addressed_selection_plans
                    else None
                )
                write_manifest(
                    manifest_path,
                    args=args,
                    stamp=stamp,
                    status="aborted",
                    started_at=run_started_at,
                    finished_at=time.time(),
                    tasks=tasks,
                    groups=groups,
                    artifacts=artifacts,
                    rows_written=rows_persisted,
                    tool_policy=manifest_tool_policy,
                    command=command,
                    failure=failure,
                    selection_plan_enabled=content_addressed_selection_plans,
                    selection_plan_binding=selection_plan_binding,
                )
            except BaseException as manifest_exc:
                print(
                    f"Failed to publish aborted manifest: {type(manifest_exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
        run_lock.close()
        raise
    try:
        result_failures, _ = result_failures_and_coverage(
            summary_facts,
            expected_keys=expected_result_keys,
            completion_rows=(
                iter_verified_result_rows(jsonl_path)
                if any(fact.completion_failed for fact in summary_facts)
                else None
            ),
        )
    except BaseException:
        await _cleanup_async_resource(
            selection_plan_writer,
            label="selection-plan pack",
            preserve_primary=True,
        )
        run_lock.close()
        raise
    manifest_failure: dict[str, Any] | None = None
    if result_failures:
        result_failure: dict[str, Any] = {
            "stage": "result_completion",
            "failure_count": len(result_failures),
            "failures": result_failures,
            "model_or_judge_started": any(
                bool(item.get("model_or_judge_started")) for item in result_failures
            ),
        }
        if manifest_failure is None:
            manifest_failure = result_failure
        else:
            manifest_failure = {
                "stage": "multiple_failures",
                "failures": [manifest_failure, result_failure],
                "model_or_judge_started": True,
            }
    run_status = "complete"
    if result_failures:
        flattened_reasons = {
            str(reason) for failure in result_failures for reason in failure.get("reasons", [])
        }
        run_status = (
            "metadata_incomplete"
            if flattened_reasons
            and flattened_reasons
            <= {
                "cost_metadata_incomplete",
                "openrouter_non_byok_verification_failed",
                "openrouter_non_byok_metadata_incomplete",
            }
            else "judge_incomplete"
            if flattened_reasons and flattened_reasons <= {"judge_incomplete"}
            else "result_incomplete"
        )
    try:
        summary = summarize_facts(summary_facts)
        summary_path = jsonl_path.with_suffix(".md")
        atomic_write_text(
            summary_path,
            render_markdown(
                summary,
                jsonl_path,
                tool_policy=manifest_tool_policy,
                generation_policy=generation_policy,
                runner_mode=args.runner_mode,
                agent_max_iterations=args.agent_max_iterations,
                agent_finalization_policy=agent_finalization_policy,
            ),
        )
        atomic_write_text(
            summary_json_path,
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        )
        if selection_plan_writer is not None:
            await selection_plan_writer.aclose()
            selection_plan_binding = await _settled_to_thread(
                terminal_selection_plan_evidence_binding,
                pack_path=selection_plan_pack_path,
                results_path=jsonl_path,
                trace_path=trace_path,
                checkpoint_path=checkpoint_path,
            )
        else:
            selection_plan_binding = None
        write_manifest(
            manifest_path,
            args=args,
            stamp=stamp,
            status=run_status,
            started_at=run_started_at,
            finished_at=time.time(),
            tasks=tasks,
            groups=groups,
            rows_written=len(summary_facts),
            artifacts=artifacts,
            summary=summary,
            tool_policy=manifest_tool_policy,
            command=command,
            failure=manifest_failure,
            selection_plan_enabled=content_addressed_selection_plans,
            selection_plan_binding=selection_plan_binding,
        )
        print(f"wrote {jsonl_path}")
        print(f"wrote {trace_path}")
        print(f"wrote {checkpoint_path}")
        print(f"wrote {manifest_path}")
        print(f"wrote {command_path}")
        print(f"wrote {summary_json_path}")
        print(f"wrote {summary_path}")
        for key, path in artifacts.items():
            if key.startswith("experiment_config_"):
                print(f"wrote {path}")
        return 2 if manifest_failure is not None else 0
    finally:
        await _cleanup_async_resource(
            selection_plan_writer,
            label="selection-plan pack",
            preserve_primary=sys.exc_info()[0] is not None,
        )
        run_lock.close()


async def amain(args: argparse.Namespace) -> int:
    run_lock_holder: list[DracoArtifactRunLock | None] = [None]
    try:
        return await _amain_with_run_lock(args, run_lock_holder=run_lock_holder)
    finally:
        run_lock = run_lock_holder[0]
        if run_lock is not None:
            run_lock.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="DRACO JSONL input.")
    parser.add_argument("--config", type=Path, default=None, help="OpenSquilla TOML config.")
    parser.add_argument(
        "--experiment-config",
        type=Path,
        default=(
            Path(os.environ["OPENSQUILLA_DRACO_EXPERIMENT_CONFIG"])
            if os.environ.get("OPENSQUILLA_DRACO_EXPERIMENT_CONFIG")
            else None
        ),
        help=(
            "Base B2 experiment JSON. Defaults to the bundled G12-derived "
            "quality-first profile; "
            "OPENSQUILLA_DRACO_EXPERIMENT_CONFIG can inject another base file."
        ),
    )
    parser.add_argument(
        "--experiment-config-override",
        type=Path,
        action="append",
        default=[],
        help="JSON overlay applied after the base experiment config; repeatable.",
    )
    parser.add_argument(
        "--experiment-config-override-json",
        default=None,
        metavar="JSON_OBJECT",
        help=(
            "One sparse JSON object applied after override files and before "
            "--experiment-config-set. The effective object is validated and frozen "
            "for the run."
        ),
    )
    parser.add_argument(
        "--experiment-config-set",
        action="append",
        default=[],
        metavar="DOTTED.PATH=JSON_VALUE",
        help=(
            "Inline experiment-config override applied last; list indexes are supported "
            "and the option is repeatable."
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("reports/draco"))
    parser.add_argument(
        "--selection-plan-evidence-mode",
        choices=SUPPORTED_SELECTION_PLAN_EVIDENCE_MODES,
        default=DEFAULT_SELECTION_PLAN_EVIDENCE_MODE,
        help=(
            "Write content-addressed selection-plan pack evidence by default, or "
            "explicitly request legacy inline selection plans for compatibility."
        ),
    )
    parser.add_argument(
        "--groups",
        required=True,
        help="Comma-separated experiment groups to run, for example B0,B1,G3,G8.",
    )
    parser.add_argument(
        "--task-ids",
        action="append",
        default=[],
        metavar="TASK_ID",
        help=(
            "Run only the selected task ID after validating the complete reference input; "
            "repeatable."
        ),
    )
    parser.add_argument("--max-tasks", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--ensemble-proposer-timeout",
        type=float,
        default=None,
        help=(
            "Per proposer request timeout in seconds for ensemble profile groups. "
            "By default the profile/default proposer timeout is used and is not "
            "expanded to match --timeout."
        ),
    )
    parser.add_argument(
        "--ensemble-aggregator-timeout",
        type=float,
        default=None,
        help=(
            "Per aggregator/scorer request timeout in seconds for ensemble profile "
            "groups. By default the profile/default aggregator timeout is used and "
            "is not expanded to match --timeout."
        ),
    )
    parser.add_argument(
        "--ensemble-proposer-early-stop-success-count",
        type=int,
        default=None,
        help=(
            "For ensemble profile groups, stop waiting for remaining proposers once "
            "this many successful candidate responses are available. Omit to use "
            "the profile default; pass 0 to disable."
        ),
    )
    parser.add_argument(
        "--ensemble-proposer-early-stop-after",
        type=float,
        default=None,
        help=(
            "Minimum seconds to wait before applying proposer early-stop. Omit to "
            "use the profile default; pass 0 to stop as soon as the success quorum "
            "is reached."
        ),
    )
    parser.add_argument(
        "--expand-ensemble-timeouts-to-task-timeout",
        action="store_true",
        help=(
            "Legacy behavior: distribute spare --timeout budget into per-member "
            "ensemble proposer/aggregator timeouts. Leave off for lower tail latency."
        ),
    )
    parser.add_argument(
        "--runner-mode",
        choices=SUPPORTED_RUNNER_MODES,
        default=DEFAULT_DRACO_RUNNER_MODE,
        help=(
            "Generation runner. agent_loop runs the full Agent tool loop; "
            "provider runs one provider.chat call for provider-level ablations."
        ),
    )
    parser.add_argument(
        "--agent-max-iterations",
        type=int,
        default=DEFAULT_AGENT_MAX_ITERATIONS,
        help=(
            "Maximum Agent LLM/tool-loop iterations when --runner-mode=agent_loop; "
            "0 means unlimited."
        ),
    )
    parser.add_argument(
        "--deadline-wrapup-margin-seconds",
        type=int,
        default=DEFAULT_DEADLINE_WRAPUP_MARGIN_SECONDS,
        help=(
            "Reserve this many seconds before the Agent deadline for a finalization "
            "pass; 0 preserves the legacy hard-deadline behavior."
        ),
    )
    parser.add_argument(
        "--deadline-wrapup-disable-tools",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_DEADLINE_WRAPUP_DISABLE_TOOLS,
        help="Disable tools during the deadline-triggered finalization pass.",
    )
    parser.add_argument(
        "--deadline-thinking-off-margin-seconds",
        type=int,
        default=DEFAULT_DEADLINE_THINKING_OFF_MARGIN_SECONDS,
        help=(
            "Disable model thinking this many seconds before the Agent deadline; "
            "0 disables the transition."
        ),
    )
    parser.add_argument(
        "--max-iterations-includes-finalization",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_MAX_ITERATIONS_INCLUDES_FINALIZATION,
        help="Count the finalization pass against --agent-max-iterations.",
    )
    parser.add_argument(
        "--retrieval-loop-finalization-threshold",
        type=int,
        default=DEFAULT_RETRIEVAL_LOOP_FINALIZATION_THRESHOLD,
        help=(
            "Trigger finalization after this many consecutive retrieval-only Agent "
            "iterations; 0 disables retrieval-loop finalization."
        ),
    )
    parser.add_argument(
        "--finalization-aggregator-only",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_FINALIZATION_AGGREGATOR_ONLY,
        help="Use only the ensemble aggregator during Agent finalization.",
    )
    parser.add_argument(
        "--finalization-disable-thinking",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_FINALIZATION_DISABLE_THINKING,
        help="Disable model thinking during Agent finalization.",
    )
    parser.add_argument(
        "--require-clean-source",
        action="store_true",
        help="Refuse to start unless the benchmark source worktree is clean and identifiable.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--local-web-tools-smoke-only",
        action="store_true",
        help=(
            "Configure local benchmark web tools, run a real search/fetch preflight, "
            "then exit before any model or judge calls."
        ),
    )
    parser.add_argument("--judge-model", default="")
    parser.add_argument("--judge-repeats", type=int, default=3)
    parser.add_argument("--judge-concurrency", type=int, default=1)
    parser.add_argument(
        "--judge-max-attempts",
        type=int,
        default=JUDGE_MAX_ATTEMPTS,
        help=(
            "Cumulative physical attempts for each Judge criterion/repeat unit "
            "across campaign resume waves; capped at 3."
        ),
    )
    parser.add_argument("--judge-candidates", action="store_true")
    parser.add_argument(
        "--generation-max-attempts",
        type=int,
        default=GENERATION_MAX_ATTEMPTS,
        help=(
            "Cumulative answer-generation attempts per group/task across campaign "
            "resume waves; capped at 3."
        ),
    )
    parser.add_argument(
        "--generation-max-tokens",
        type=int,
        default=DEFAULT_GENERATION_MAX_TOKENS_OVERRIDE,
        help=(
            "Override max completion tokens for generation and ensemble members; "
            "0 preserves the existing 16384-token/default profile behavior."
        ),
    )
    parser.add_argument(
        "--generation-retry-backoff",
        type=float,
        default=DEFAULT_GENERATION_RETRY_BACKOFF_SECONDS,
        help="Initial seconds to wait before retrying answer generation; doubles each retry.",
    )
    parser.add_argument(
        "--tool-mode",
        choices=SUPPORTED_TOOL_MODES,
        default=TOOL_MODE_PROVIDER_ONLY,
        help=(
            "Benchmark tool mode. provider_only attaches no external tools; "
            "local_web_tools attaches executable web_search and web_fetch tools "
            "for the Agent loop; openrouter_server_tools attaches OpenRouter "
            "server tools for provider-level runs."
        ),
    )
    parser.add_argument(
        "--contamination-blocked-domains",
        default=",".join(DEFAULT_CONTAMINATION_BLOCKED_DOMAINS),
        help=(
            "Comma-separated benchmark leakage domains to exclude from web search "
            "and block from web fetch when research tools are wired."
        ),
    )
    parser.add_argument(
        "--local-web-search-provider",
        choices=SUPPORTED_LOCAL_WEB_SEARCH_PROVIDERS,
        default=DEFAULT_LOCAL_WEB_SEARCH_PROVIDER,
        help=("Provider for executable local web_search when --tool-mode=local_web_tools."),
    )
    parser.add_argument(
        "--local-web-search-api-key-env",
        default="",
        help=(
            "Environment variable that contains the local web_search provider API key. "
            "Use BRAVE_SEARCH_API_KEY for --local-web-search-provider=brave. "
            "The key value is never written to benchmark command/manifest files."
        ),
    )
    parser.add_argument(
        "--allow-firecrawl-web-fetch",
        action="store_true",
        help=(
            "Allow web_fetch to escalate short/failed local extraction to paid Firecrawl. "
            "Disabled by default so benchmark tool spend is reproducible."
        ),
    )
    parser.add_argument(
        "--require-openrouter-non-byok",
        action="store_true",
        help=(
            "Audit every OpenRouter generation and Judge request for "
            "is_byok=false evidence. Missing or conflicting evidence is "
            "reported separately and does not change task execution status."
        ),
    )
    parser.add_argument(
        "--continue-after-cost-audit-failure",
        action="store_true",
        help=(
            "Deprecated no-op retained for command compatibility; cost/BYOK "
            "audit findings are always recorded without stopping task execution."
        ),
    )
    parser.add_argument(
        "--openrouter-web-search-engine",
        default=DEFAULT_OPENROUTER_WEB_SEARCH_ENGINE,
        help="OpenRouter web_search engine used when --tool-mode=openrouter_server_tools.",
    )
    parser.add_argument(
        "--openrouter-web-search-max-results",
        type=int,
        default=DEFAULT_OPENROUTER_WEB_SEARCH_MAX_RESULTS,
        help="Maximum results per OpenRouter web_search call.",
    )
    parser.add_argument(
        "--openrouter-web-search-max-total-results",
        type=int,
        default=DEFAULT_OPENROUTER_WEB_SEARCH_MAX_TOTAL_RESULTS,
        help="Maximum total results across OpenRouter web_search calls.",
    )
    parser.add_argument(
        "--openrouter-web-search-context-size",
        choices=("low", "medium", "high"),
        default=DEFAULT_OPENROUTER_WEB_SEARCH_CONTEXT_SIZE,
        help="Search context size for OpenRouter web_search.",
    )
    parser.add_argument(
        "--openrouter-web-fetch-engine",
        default=DEFAULT_OPENROUTER_WEB_FETCH_ENGINE,
        help="OpenRouter web_fetch engine used when --tool-mode=openrouter_server_tools.",
    )
    parser.add_argument(
        "--openrouter-web-fetch-max-uses",
        type=int,
        default=DEFAULT_OPENROUTER_WEB_FETCH_MAX_USES,
        help="Maximum OpenRouter web_fetch uses per request.",
    )
    parser.add_argument(
        "--openrouter-web-fetch-max-content-tokens",
        type=int,
        default=DEFAULT_OPENROUTER_WEB_FETCH_MAX_CONTENT_TOKENS,
        help="Maximum content tokens returned by OpenRouter web_fetch.",
    )
    parser.add_argument(
        "--openrouter-fusion-analysis-models",
        default=",".join(DEFAULT_OPENROUTER_FUSION_ANALYSIS_MODELS),
        help=(
            "Comma-separated OpenRouter Fusion panel models. Used by B8; "
            "OpenRouter documents 1 to 8 models."
        ),
    )
    parser.add_argument(
        "--openrouter-fusion-model",
        default=DEFAULT_OPENROUTER_FUSION_MODEL,
        help="OpenRouter Fusion judge model. Used by B8.",
    )
    parser.add_argument(
        "--openrouter-fusion-max-tool-calls",
        type=int,
        default=DEFAULT_OPENROUTER_FUSION_MAX_TOOL_CALLS,
        help=(
            "Maximum OpenRouter web_search/web_fetch steps for each Fusion "
            "panel model and judge call; range 1 to 16."
        ),
    )
    parser.add_argument(
        "--openrouter-fusion-max-completion-tokens",
        type=int,
        default=DEFAULT_OPENROUTER_FUSION_MAX_COMPLETION_TOKENS,
        help="Maximum completion tokens for each inner Fusion panel/judge call.",
    )
    parser.add_argument(
        "--openrouter-fusion-reasoning-effort",
        default=DEFAULT_OPENROUTER_FUSION_REASONING_EFFORT,
        help="Reasoning effort forwarded to OpenRouter Fusion panel and judge calls.",
    )
    parser.add_argument(
        "--openrouter-fusion-temperature",
        type=float,
        default=DEFAULT_OPENROUTER_FUSION_TEMPERATURE,
        help="Temperature forwarded to OpenRouter Fusion panel calls.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.command_argv = [sys.executable, *sys.argv]
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
