#!/usr/bin/env python3
"""Finalize a complete DRACO Mini campaign using only sealed local artifacts.

This command deliberately performs no networking or model-provider calls.  It
validates immutable source shards, replays the pure frozen G1 ranker, selects
one completed result per expected group/task pair, rebuilds whole-campaign
spend from physical request receipts, binds incomplete metadata to account
reconciliation and policy evidence while preserving audit failures separately,
reseals results, rebuilds traces, and publishes one directory atomically.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import hmac
import json
import math
import os
import random
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from typing import Any

from opensquilla.eval.draco_artifact_io import (
    DRACO_DURABLE_RESULT_ROW_FIELD,
    DRACO_RUN_MANIFEST_SCHEMA_V2,
    DracoArtifactDurabilityError,
    durable_artifact_capability_contract,
    verify_durable_draco_artifacts,
)
from opensquilla.provider.protocol import (
    provider_retry_expanded_proposer_identities,
)
from opensquilla.provider.ranking_router import (
    canonical_json_bytes as ranking_canonical_json_bytes,
)
from opensquilla.provider.ranking_router import (
    canonical_json_sha256 as ranking_canonical_json_sha256,
)
from opensquilla.provider.types import REASONING_ONLY_LENGTH_STOP_REASONS
from opensquilla.usage_evidence import (
    MISSING_USAGE_PLACEHOLDER_ROLES,
    UsageEvidenceError,
    canonical_run_usage_units,
    canonicalize_run_usage,
    derive_physical_request_count,
    usage_units,
)

SUPPORTED_GROUPS = ("B0", "B1", "B2", "B4", "G1")
# Backwards-compatible public name used by existing callers and fixtures.
GROUPS = SUPPORTED_GROUPS
GROUP_SET = frozenset(SUPPORTED_GROUPS)
RESULT_EVIDENCE_SCHEMA = "opensquilla.draco.result-evidence/v1"
RESULT_EVIDENCE_SHA256_FIELD = "result_evidence_sha256"
RUNTIME_SCHEMA = "opensquilla.draco-runtime-environment/v1"
RECONCILIATION_SCHEMA = "opensquilla.openrouter-account-reconciliation/v1"
PROOF_SCHEMA = "opensquilla.draco.openrouter-non-byok-campaign-proof/v1"
LEDGER_SCHEMA = "opensquilla.draco.actual-spend-ledger/v1"
AUDIT_SCHEMA = "opensquilla.draco.campaign-final-audit/v1"
MANIFEST_SCHEMA = "opensquilla.draco.campaign-final-manifest/v1"
RESOLUTION_SCHEMA = "opensquilla.draco.openrouter-non-byok-resolution/v1"
GENERATION_ATTEMPT_EVIDENCE_SCHEMA = "opensquilla.draco-generation-attempt/v1"
JUDGE_ATTEMPT_EVIDENCE_SCHEMA = "opensquilla.draco-judge-attempt/v1"
ENSEMBLE_OUTPUT_BINDING_SCHEMA = "opensquilla.ensemble-output-binding/v1"
GENERATION_POSTPROCESSING_TERMINAL_SCHEMA = (
    "opensquilla.draco.generation-postprocessing-terminal/v1"
)
PROVIDER_NATIVE_PROPOSER_RECOVERY_TERMINAL_SCHEMA = (
    "opensquilla.draco.provider-native-proposer-recovery-terminal/v1"
)
THINKING_PHYSICAL_EVIDENCE_SCHEMA = "opensquilla.router-dynamic-thinking-physical-evidence/v1"
LEGACY_THINKING_RANKING_VERSION = "step2-ranking-v3"
LEGACY_MANAGED_V3_SOURCE_IDENTITY = {
    "git_head": "f39f7d5e529ce42a6149fc8af6be5a7d6e23ea6b",
    "source_tree_sha256": ("f44c3edc6db511639c6f7e8a411a47d5eff057eb4911ecc3aa6335967b2a993e"),
}
JUDGE_ATTEMPT_BUDGET_SCOPE = "criterion_repeat_campaign"
JUDGE_ATTEMPT_BUDGET_LIMIT = 3
JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR = "judge_attempt_budget_exhausted"
FINALIZER_VERSION = 9
RESUME_SCHEDULE_ACTIONS = frozenset(
    {
        "regenerate",
        "model_regenerate",
        "judge_only",
        "metadata_only",
        "audit_only",
    }
)
FROZEN_DRACO_MINI_TASK_COUNT = 10
FROZEN_DRACO_MINI_SHA256 = "1eb4e618c8df8e7f68bded3d2b6f77a541744aa1072eb338835b776183188a8d"
FORMAL_REQUIRED_STABLE_POLL_COUNT = 6
FORMAL_POLL_INTERVAL_SECONDS = 15
FORMAL_MINIMUM_SETTLEMENT_SECONDS = 180
FORMAL_MINIMUM_STABLE_TAIL_SECONDS = 75
FORMAL_G1_LEGACY_SOURCE_REGISTRY_SNAPSHOT_SHA256 = (
    "9f76c7f96e5cb22c05b615f69b71ca633965e5039fbec9673f0a5edf9b45078a"
)
# Backwards-compatible public name used by historical fixtures and callers.
FORMAL_G1_SOURCE_REGISTRY_SNAPSHOT_SHA256 = FORMAL_G1_LEGACY_SOURCE_REGISTRY_SNAPSHOT_SHA256
FORMAL_G1_FULL_REGISTRY_SNAPSHOT_VERSION = "curated-openrouter-step2-2026-07-31.1"
FORMAL_G1_FULL_REGISTRY_SNAPSHOT_SHA256 = (
    "b51b64d7880472e47f8a5f954b1a76eaee440d6cd59d28f9dc2579f876bac1ea"
)
# Serving aliases are part of the authenticated historical/full registry
# identities above. Keep this projection immutable so finalization never
# consults a newer packaged model profile when replaying paid runs.
FORMAL_OPENROUTER_SERVING_ALIASES = {
    "anthropic/claude-fable-5": "anthropic/claude-5-fable-20260609",
    "anthropic/claude-haiku-4.5": "anthropic/claude-4.5-haiku-20251001",
    "anthropic/claude-opus-4.8": "anthropic/claude-4.8-opus-20260528",
    "anthropic/claude-sonnet-5": "anthropic/claude-sonnet-5-20260630",
    "bytedance-seed/seed-2.0-lite": "bytedance-seed/seed-2.0-lite-20260309",
    "deepseek/deepseek-v3.2": "deepseek/deepseek-v3.2-20251201",
    "deepseek/deepseek-v4-flash": "deepseek/deepseek-v4-flash-20260423",
    "deepseek/deepseek-v4-pro": "deepseek/deepseek-v4-pro-20260423",
    "google/gemini-3-flash-preview": "google/gemini-3-flash-preview-20251217",
    "google/gemini-3.1-pro-preview": "google/gemini-3.1-pro-preview-20260219",
    "google/gemini-3.5-flash": "google/gemini-3.5-flash-20260519",
    "google/gemini-3.6-flash": "google/gemini-3.6-flash-20260721",
    "google/gemma-4-26b-a4b-it": "google/gemma-4-26b-a4b-it-20260403",
    "google/gemma-4-31b-it": "google/gemma-4-31b-it-20260402",
    "ibm-granite/granite-4.1-8b": "ibm-granite/granite-4.1-8b-20260429",
    "inclusionai/ling-2.6-1t": "inclusionai/ling-2.6-1t-20260423",
    "inclusionai/ring-2.6-1t": "inclusionai/ring-2.6-1t-20260508",
    "kwaipilot/kat-coder-air-v2.5": "kwaipilot/kat-coder-air-v2.5-20260710",
    "kwaipilot/kat-coder-pro-v2.5": "kwaipilot/kat-coder-pro-v2.5-20260710",
    "meituan/longcat-2.0": "meituan/longcat-2.0-20260720",
    "meta/muse-spark-1.1": "meta/muse-spark-1.1-20260709",
    "meta-llama/llama-4-maverick": "meta-llama/llama-4-maverick-17b-128e-instruct",
    "meta-llama/llama-4-scout": "meta-llama/llama-4-scout-17b-16e-instruct",
    "minimax/minimax-m2.7": "minimax/minimax-m2.7-20260318",
    "minimax/minimax-m3": "minimax/minimax-m3-20260531",
    "mistralai/mistral-medium-3-5": "mistralai/mistral-medium-3.5-20260430",
    "moonshotai/kimi-k2-thinking": "moonshotai/kimi-k2-thinking-20251106",
    "moonshotai/kimi-k2.5": "moonshotai/kimi-k2.5-0127",
    "moonshotai/kimi-k2.6": "moonshotai/kimi-k2.6-20260420",
    "moonshotai/kimi-k2.7-code": "moonshotai/kimi-k2.7-code-20260612",
    "moonshotai/kimi-k3": "moonshotai/kimi-k3-20260715",
    "nvidia/nemotron-3-super-120b-a12b": "nvidia/nemotron-3-super-120b-a12b-20230311",
    "nvidia/nemotron-3-ultra-550b-a55b": "nvidia/nemotron-3-ultra-550b-a55b-20260604",
    "openai/gpt-5.3-codex": "openai/gpt-5.3-codex-20260224",
    "openai/gpt-5.5": "openai/gpt-5.5-20260423",
    "openai/gpt-5.6-luna": "openai/gpt-5.6-luna-20260709",
    "openai/gpt-5.6-sol": "openai/gpt-5.6-sol-20260709",
    "openai/gpt-5.6-terra": "openai/gpt-5.6-terra-20260709",
    "poolside/laguna-s-2.1": "poolside/laguna-s-2.1-20260720",
    "poolside/laguna-xs-2.1": "poolside/laguna-xs-2.1-20260625",
    "qwen/qwen3-coder": "qwen/qwen3-coder-480b-a35b-07-25",
    "qwen/qwen3-coder-next": "qwen/qwen3-coder-next-2025-02-03",
    "qwen/qwen3-next-80b-a3b-thinking": "qwen/qwen3-next-80b-a3b-thinking-2509",
    "qwen/qwen3.5-122b-a10b": "qwen/qwen3.5-122b-a10b-20260224",
    "qwen/qwen3.5-397b-a17b": "qwen/qwen3.5-397b-a17b-20260216",
    "qwen/qwen3.6-27b": "qwen/qwen3.6-27b-20260422",
    "qwen/qwen3.6-35b-a3b": "qwen/qwen3.6-35b-a3b-20260415",
    "qwen/qwen3.7-max": "qwen/qwen3.7-max-20260520",
    "qwen/qwen3.7-plus": "qwen/qwen3.7-plus-20260602",
    "stepfun/step-3.7-flash": "stepfun/step-3.7-flash-20260528",
    "tencent/hy3": "tencent/hy3-20260706",
    "tencent/hy3-preview": "tencent/hy3-preview-20260421",
    "thinkingmachines/inkling": "thinkingmachines/inkling-20260715",
    "x-ai/grok-4.20": "x-ai/grok-4.20-20260309",
    "x-ai/grok-4.5": "x-ai/grok-4.5-20260708",
    "xiaomi/mimo-v2.5": "xiaomi/mimo-v2.5-20260422",
    "xiaomi/mimo-v2.5-pro": "xiaomi/mimo-v2.5-pro-20260422",
    "z-ai/glm-4.6v": "z-ai/glm-4.6-20251208",
    "z-ai/glm-4.7-flash": "z-ai/glm-4.7-flash-20260119",
    "z-ai/glm-5": "z-ai/glm-5-20260211",
    "z-ai/glm-5.1": "z-ai/glm-5.1-20260406",
    "z-ai/glm-5.2": "z-ai/glm-5.2-20260616",
}
FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION = "step2-ranking-config-v3"
FORMAL_G1_RANKING_CONFIG_VERSION = "step2-ranking-2026-08-02.2"
FORMAL_G1_RANKING_CONFIG_SHA256 = "71be283f94095bc3ced34d39ae9ed58abbaa7e4d273b0a074e7e8a4a6e4b5fc6"
HISTORICAL_G1_RANKING_CONFIG_IDENTITIES = frozenset(
    {
        (
            "step2-ranking-config-v3",
            "step2-ranking-2026-07-22.1",
            "a8addcdefa04349209c20e97ca5851ed0f5ca55646c9d0c5badc5d32dd7ef10c",
        ),
        (
            "step2-ranking-config-v3",
            "step2-ranking-2026-08-02.1",
            "d2a14525e11943ef98ebe3f08e99dfb64f6f5011e498770d3b54a50aba02188c",
        ),
    }
)
FORMAL_TASK_ANALYZER_EXECUTION_POLICY = {
    "protocol_version": "opus-4.8-json-v3",
    "provider": "openrouter",
    "model": "anthropic/claude-opus-4.8",
    "upstream_provider": "anthropic",
    "stream_close_timeout_seconds": 1.0,
    "timeout_seconds": 20.0,
    "max_retries": 3,
}
FORMAL_G1_PROPOSER_COUNT_MAX = 5
FORMAL_G1_REGISTRY_ALL_CANDIDATE_COUNT = 79
FORMAL_TASK_CONCURRENCY = 5
FORMAL_JUDGE_CONCURRENCY = 6
FORMAL_TASK_TIMEOUT_SECONDS = Decimal("10800")
FORMAL_PROPOSER_TIMEOUT_SECONDS = Decimal("907.5")
FORMAL_AGGREGATOR_TIMEOUT_SECONDS = Decimal("2662.5")
FORMAL_AGENT_MAX_ITERATIONS = 20
FORMAL_GENERATION_MAX_ATTEMPTS = 3
FORMAL_GENERATION_RETRY_BACKOFF_SECONDS = Decimal("2")
FORMAL_GENERATION_MAX_TOKENS = 16_384
FORMAL_AGGREGATOR_RECOVERY_POLICY = {
    "aggregator_recovery_mode": "experiment",
    "aggregator_recovery_top_k": 3,
    "aggregator_max_tokens_cap": 65_536,
    "aggregator_visible_answer_reserve_tokens": 8_192,
}
FORMAL_PROPOSER_RECOVERY_SCHEMA = "opensquilla.router-dynamic-proposer-recovery/v1"
FORMAL_PROPOSER_RECOVERY_POLICY = {
    "schema": FORMAL_PROPOSER_RECOVERY_SCHEMA,
    "configured_backup_count": 2,
    "effective_backup_count": 2,
    "max_additional_physical_requests": 3,
    "quorum_required": 2,
    "max_tokens_cap": 65_536,
    "visible_answer_reserve_tokens": 4_096,
    "thinking_downgrade_order": ["one_strictly_lower"],
    "transient_same_model_retries": 1,
    "backup_reasoning_downgrades": 1,
}


def analyzer_failure_fallback_proposer_recovery_policy(
    base_policy: Mapping[str, Any] = FORMAL_PROPOSER_RECOVERY_POLICY,
) -> dict[str, Any]:
    """Return the exact provider-owned recovery policy for fixed fallback."""

    return {
        **dict(base_policy),
        "configured_backup_count": 0,
        "effective_backup_count": 0,
        "max_additional_physical_requests": 0,
        "quorum_required": 1,
    }


def authenticated_analyzer_failure_fallback_plan(
    plan: Any,
    *,
    proposer_recovery_policy: Mapping[str, Any] = FORMAL_PROPOSER_RECOVERY_POLICY,
) -> bool:
    """Recognize the strict, self-contained fixed fallback execution policy."""

    if not isinstance(plan, Mapping):
        return False
    selected_p = plan.get("selected_P")
    proposer_models = plan.get("proposer_models")
    selected_a = str(plan.get("selected_A") or "")
    return bool(
        plan.get("analyzer_failure_fallback") is True
        and plan.get("analyzer_failure_fallback_schema")
        == "opensquilla.router-dynamic-analyzer-failure-fallback/v1"
        and plan.get("selection_mode") == "router_dynamic"
        and isinstance(selected_p, list)
        and len(selected_p) == 4
        and len(set(str(value) for value in selected_p)) == 4
        and isinstance(proposer_models, list)
        and len(proposer_models) == 4
        and plan.get("proposer_sample_count") == 4
        and plan.get("backup_P") == []
        and plan.get("configured_min_successful_proposers") == 1
        and plan.get("effective_min_successful_proposers") == 1
        and plan.get("N_min") == 1
        and plan.get("N_max") == 4
        and plan.get("complete_proposers_only") is True
        and plan.get("aggregator_max_recovery_actions") == 1
        and bool(selected_a)
        and plan.get("aggregator_candidates") == [selected_a]
        and plan.get("proposer_recovery_policy")
        == analyzer_failure_fallback_proposer_recovery_policy(proposer_recovery_policy)
    )


FORMAL_JUDGE_MAX_ATTEMPTS = 3
FORMAL_BLOCKED_DOMAINS = (
    "hf.co",
    "huggingface.co",
    "datasets-server.huggingface.co",
    "github.com",
    "raw.githubusercontent.com",
    "openrouter.ai",
    "perplexity.ai",
    "research.perplexity.ai",
)
FORMAL_AGENT_FINALIZATION_POLICY = {
    "deadline_wrapup_margin_seconds": 300,
    "deadline_wrapup_disable_tools": True,
    "deadline_thinking_off_margin_seconds": 0,
    "max_iterations_includes_finalization": False,
    "retrieval_loop_finalization_threshold": 0,
    "finalization_aggregator_only": False,
    "finalization_disable_thinking": False,
}
FORMAL_MODEL_THINKING_LEVELS = {
    "anthropic/claude-fable-5": "max",
    "anthropic/claude-opus-4.8": "max",
    "anthropic/claude-sonnet-5": "max",
    "deepseek/deepseek-v4-pro": "xhigh",
    "deepseek/deepseek-v4-flash": "xhigh",
    "google/gemini-3.1-pro-preview": "high",
    "google/gemini-3-flash-preview": "high",
    "google/gemini-3.5-flash": "high",
    "kwaipilot/kat-coder-air-v2.5": "off",
    "kwaipilot/kat-coder-pro-v2.5": "off",
    "meta-llama/llama-4-scout": "off",
    "minimax/minimax-m3": "high",
    "mistralai/mistral-medium-3-5": "high",
    "z-ai/glm-5.2": "xhigh",
    "moonshotai/kimi-k2.7-code": "high",
    "openai/gpt-5.5": "xhigh",
    "openai/gpt-5.5-pro": "xhigh",
    "openai/gpt-5.6-sol": "max",
    "poolside/laguna-xs-2.1": "high",
    "qwen/qwen3.7-max": "high",
    "qwen/qwen3.7-plus": "high",
    "qwen/qwen3.8-max": "high",
    "sakana/fugu-ultra": "max",
    "tencent/hy3": "high",
    "x-ai/grok-4.5": "high",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
SHA256_VALUE = re.compile(r"^sha256:[0-9a-f]{64}$")

B2_PROPOSERS = (
    "deepseek/deepseek-v4-pro",
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.7-code",
    "qwen/qwen3.7-max",
)
B2_AGGREGATOR = "z-ai/glm-5.2"
B0_MODEL = "anthropic/claude-fable-5"
B4_MODEL = "openai/gpt-5.6-sol"
TASK_ANALYZER_MODEL = "anthropic/claude-opus-4.8"
B1_TEXT_TIER_MODELS = {
    "c0": "deepseek/deepseek-v4-flash",
    "c1": "deepseek/deepseek-v4-pro",
    "c2": "z-ai/glm-5.2",
    "c3": "anthropic/claude-opus-4.8",
}
B1_TIER_MODELS = {
    **B1_TEXT_TIER_MODELS,
    "image_model": "moonshotai/kimi-k2.6",
}
FORMAL_UPSTREAM_PINS = {
    B0_MODEL: "amazonbedrock",
    B4_MODEL: "azure",
    TASK_ANALYZER_MODEL: "anthropic",
    "deepseek/deepseek-v4-flash": "deepseek",
    "deepseek/deepseek-v4-pro": "deepseek",
    "z-ai/glm-5.2": "z-ai",
    "moonshotai/kimi-k2.6": "moonshotai",
    "moonshotai/kimi-k2.7-code": "moonshotai",
    "qwen/qwen3.7-max": "alibaba",
    "google/gemini-3.1-pro-preview": "google-ai-studio",
}
JUDGE_MODEL = "google/gemini-3.1-pro-preview"
JUDGE_REPEATS = 3
ALLOWED_NON_GENERATION_ERRORS = frozenset(
    {
        "",
        "cost_metadata_incomplete",
        "judge_incomplete",
        "openrouter_non_byok_metadata_incomplete",
        "openrouter_non_byok_verification_failed",
    }
)
POLICY_VIOLATION_ERRORS = frozenset(
    {
        "openrouter_non_byok_policy_violation",
        "openrouter_byok_detected",
    }
)

# These failures describe policy, receipt, metering, or stream-finalization
# confidence.  They must remain visible in the published audit, but they do
# not erase a response that is already bound to a real request and has a
# non-empty, integrity-checked answer.
AUDIT_ONLY_GENERATION_REASONS = frozenset(
    {
        "openrouter_policy_violation",
        "selected_generation_degraded_success",
        "generation_accepted_as_degraded_success",
        "missing_generation_usage_route_evidence",
        "missing_successful_router_receipt",
        "successful_judge_has_unknown_usage",
        "aggregator_recovery_selected_stream_not_closed",
        "proposer_recovery_stream_not_closed",
    }
)

# The finalizer used to treat every route/identity/recovery discrepancy as if
# the model had produced no answer.  That conflates two independent questions:
# whether an ensemble produced a usable answer with quorum, and whether every
# piece of forensic metadata is exact.  These markers are deliberately applied
# only after ``row_has_bound_answer_and_proposer_quorum`` succeeds and the
# caller has verified task/prompt/compatibility integrity.  Missing candidates,
# sub-quorum execution, empty output, and request/answer integrity failures
# therefore remain blocking.
AUDIT_ONLY_EVIDENCE_REASON_MARKERS = (
    "identity",
    "model",
    "provider",
    "route",
    "routing",
    "selection",
    "ranker",
    "ranking",
    "registry",
    "recovery",
    "physical",
    "usage",
    "cost",
    "byok",
    "stream",
    "receipt",
    "output_binding",
    "output_component",
    "request_count",
    "cleanup",
    "provenance",
)
AUDIT_ONLY_EVIDENCE_REASONS = frozenset(
    {
        "selected_generation_not_successful",
        "generation_not_accepted",
        "wrong_executed_proposer_count",
        "successful_proposer_count_mismatch",
        "usable_proposer_count_mismatch",
        "invalid_successful_proposer_evidence",
        "invalid_dynamic_proposer_usable_quorum",
        "wrong_agent_llm_call_count",
        "untraced_agent_llm_calls",
        "invalid_agent_call_index_sequence",
    }
)

EXECUTION_BLOCKING_GENERATION_REASONS = frozenset(
    {
        "empty_final_text",
        "final_text_hash_mismatch",
        "final_text_length_mismatch",
        "prompt_hash_mismatch",
        "task_input_hash_mismatch",
        "run_compatibility_fingerprint_mismatch",
        "missing_ensemble_trace",
        "missing_ensemble_call_trace",
        "invalid_ensemble_call_trace",
        "missing_actual_proposer_candidates",
        "proposer_quorum_not_met",
        "insufficient_actual_proposer_quorum",
        "insufficient_selected_proposer_quorum",
        "final_request_not_aggregator",
        "generation_error",
        "generation_run_error",
        "physical_attempt_id_reused_across_generation_attempts",
        "duplicate_ensemble_physical_attempt_id",
        "duplicate_proposer_recovery_physical_attempt_id",
        "wrong_g1_registry_snapshot_hash",
        "g1_replay_registry_snapshot_hash_mismatch",
        "g1_physical_registry_snapshot_hash_differs_from_routing_trace",
        "g1_routing_plan_differs_from_physical_plan",
        "router_receipt_provider_not_bound_to_formal_route",
        "router_receipt_model_not_bound_to_formal_route",
        "router_receipt_request_not_bound_to_formal_route",
    }
)

AUDIT_ONLY_ERROR_CODES = frozenset(
    {
        "openrouter_non_byok_verification_failed",
        "openrouter_non_byok_metadata_incomplete",
        "openrouter_non_byok_policy_violation",
        "openrouter_byok_detected",
        "cost_metadata_incomplete",
        "usage_metadata_incomplete",
        "usage_unknown",
    }
)

DEGRADED_DELIVERY_ERROR_CODES = frozenset(
    {
        "incomplete_stream",
        "ensemble_aggregator_close_timeout",
        "ensemble_aggregator_timeout",
        "ensemble_aggregator_length_capped",
        "ensemble_aggregator_stream_error",
        "ensemble_aggregator_incomplete",
    }
)


def explicit_degraded_call_attempt(call: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the real failed physical attempt selected for degraded delivery."""

    recovery = call.get("aggregator_recovery")
    if not isinstance(recovery, Mapping):
        return None
    selected_attempt = recovery.get("selected_attempt")
    attempts = recovery.get("attempts")
    selected = (
        next(
            (
                attempt
                for attempt in attempts
                if isinstance(attempt, Mapping) and attempt.get("attempt") == selected_attempt
            ),
            None,
        )
        if isinstance(attempts, list)
        else None
    )
    assembled = call.get("assembled_output")
    visible = bool(
        isinstance(assembled, Mapping)
        and nonnegative_int(assembled.get("chars")) > 0
        and (
            bool(str(assembled.get("text") or "").strip())
            or HEX64.fullmatch(str(assembled.get("sha256") or "")) is not None
            or SHA256_VALUE.fullmatch(str(assembled.get("sha256") or "")) is not None
        )
    )
    run_outcome = str(call.get("run_outcome") or "")
    if not (
        call.get("execution_outcome") == "degraded_success"
        and call.get("delivery_outcome") == "degraded_success"
        and recovery.get("degraded") is True
        and recovery.get("success") is False
        and recovery.get("delivery_success") is True
        and recovery.get("delivery_outcome") == "degraded_success"
        and recovery.get("audit_outcome") == "incomplete"
        and recovery.get("recovery_skipped") is True
        and recovery.get("tools_disabled_after_partial_output") is True
        and recovery.get("selected_kind") == "degraded_delivery"
        and isinstance(selected_attempt, int)
        and not isinstance(selected_attempt, bool)
        and selected_attempt > 0
        and isinstance(selected, Mapping)
        and selected.get("outcome") == "failed"
        and selected.get("delivery_selected") is True
        and selected.get("visible_output_emitted") is True
        and selected.get("request_started") is True
        and isinstance(selected.get("physical_request_count"), int)
        and not isinstance(selected.get("physical_request_count"), bool)
        and selected.get("physical_request_count") > 0
        and str(selected.get("code") or "") == run_outcome
        and str(recovery.get("terminal_code") or "") == run_outcome
        and str(recovery.get("run_outcome") or "") == run_outcome
        and structural_aggregator_recovery_trigger(run_outcome)
        and visible
    ):
        return None
    return selected


def explicit_degraded_success(row: Mapping[str, Any]) -> bool:
    """Require a fully bound degraded-delivery receipt before accepting it."""

    execution_status = row.get("execution_status")
    if not (
        isinstance(execution_status, Mapping)
        and execution_status.get("status") == "degraded_success"
        and execution_status.get("success") is True
    ):
        return False
    trace = row.get("ensemble_trace")
    if not isinstance(trace, Mapping):
        return False
    traces = [trace]
    calls = trace.get("calls")
    if isinstance(calls, list):
        traces.extend(call for call in calls if isinstance(call, Mapping))
    return any(explicit_degraded_call_attempt(call) is not None for call in traces)


def evidence_reason_is_audit_only(reason: Any) -> bool:
    """Classify forensic discrepancies without weakening execution gates."""

    normalized = str(reason or "").strip().casefold()
    if not normalized or normalized in EXECUTION_BLOCKING_GENERATION_REASONS:
        return False
    return bool(
        normalized in AUDIT_ONLY_GENERATION_REASONS
        or normalized in AUDIT_ONLY_EVIDENCE_REASONS
        or any(marker in normalized for marker in AUDIT_ONLY_EVIDENCE_REASON_MARKERS)
    )


def row_has_bound_answer_and_proposer_quorum(row: Mapping[str, Any]) -> bool:
    """Prove the minimum execution facts needed to demote forensic failures.

    This intentionally does not trust declared proposer counters.  Every
    physical ensemble call must expose the authenticated execution quorum and
    a started aggregator request.  The ordinary quorum is two usable drafts;
    the fixed analyzer-failure fallback instead requires one fully completed
    draft.  Prompt/task/source bindings are checked by
    ``generation_reason_assessment`` before it enables audit demotion.
    """

    final_text = str(row.get("final_text") or "")
    if (
        not final_text.strip()
        or row.get("final_text_sha256") != text_sha256(final_text)
        or nonnegative_int(row.get("final_text_chars")) != len(final_text)
    ):
        return False
    trace = row.get("ensemble_trace")
    if not isinstance(trace, Mapping):
        return False
    calls, _ = ensemble_call_trace_sequence(trace)
    if not calls:
        return False
    # Earlier Agent-loop calls may legitimately be empty tool/fallback turns.
    # Execution is determined by the terminal call that supplied final_text.
    terminal = calls[-1]
    candidates = terminal.get("candidates")
    plan = terminal.get("selection_plan")
    fixed_complete_only = authenticated_analyzer_failure_fallback_plan(plan)
    required = 1 if fixed_complete_only else 2
    if not isinstance(candidates, list) or len(candidates) < required:
        return False
    proven = sum(
        1
        for candidate in candidates
        if (
            successful_candidate(candidate) and candidate.get("completion_outcome") == "complete"
            if fixed_complete_only
            else usable_candidate(candidate)
        )
    )
    if proven < required:
        return False
    final_request = terminal.get("final_request")
    if not (
        isinstance(final_request, Mapping)
        and final_request.get("request_started") is True
        and str(final_request.get("role") or "") == "aggregator"
    ):
        return False
    return True


def audit_only_error_text(
    value: Any,
    *,
    degraded_success: bool = False,
    evidence_proven: bool = False,
) -> bool:
    """Match only closed, explicit audit/degraded error codes.

    In particular, words such as ``usage``, ``receipt`` and ``stream`` are not
    sufficient: identity, route, protocol and tamper errors often contain the
    same words and must remain execution-invalid.
    """

    normalized = str(value or "").strip().casefold()
    if normalized in AUDIT_ONLY_ERROR_CODES:
        return True
    if degraded_success and normalized in DEGRADED_DELIVERY_ERROR_CODES:
        return True
    return evidence_proven and evidence_reason_is_audit_only(normalized)


def judge_evidence_error_is_audit_only(error: FinalizationError) -> bool:
    """Allow only missing/unknown Judge metering evidence to be non-blocking."""

    message = str(error).casefold()
    return any(
        marker in message
        for marker in (
            "does not represent every physical request in usage",
            "successful_judge_has_unknown_usage",
            "judge_usage_unknown",
            "judge_stream_not_closed",
        )
    )


def account_proof_error_is_audit_only(error: FinalizationError) -> bool:
    """Demote only settlement/cost incompleteness, never identity or tamper faults."""

    message = str(error).strip().casefold()
    return message in {
        "account reconciliation is not stable",
        "reconciliation lacks the formal account observation count",
        "reconciliation stable_poll_count is invalid",
        "formal account settlement window is too short",
        "formal stable account tail is too short",
    } or message.startswith(
        (
            "ledger recorded cost is ",
            "ledger exact cost is ",
            "cost reconciliation tolerance is ",
        )
    )


def partition_execution_and_audit_reasons(
    reasons: Iterable[str],
    *,
    evidence_proven: bool = False,
) -> tuple[list[str], list[str]]:
    """Split hard execution failures from publishable audit warnings."""

    blocking: list[str] = []
    warnings: list[str] = []
    for reason in dict.fromkeys(str(value) for value in reasons if str(value)):
        if (
            reason in AUDIT_ONLY_GENERATION_REASONS
            or reason.startswith("audit:")
            or (evidence_proven and evidence_reason_is_audit_only(reason))
        ):
            warnings.append(reason)
        else:
            blocking.append(reason)
    return blocking, warnings


USAGE_CONTRACT_KEYS = (
    "provider",
    "model",
    "requested_provider",
    "requested_model",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
TRACE_FIELDS = (
    "row_index",
    "task_id",
    "group",
    "domain",
    "runner_mode",
    "tools_enabled",
    "tool_policy",
    "generation_policy",
    "generation_config",
    "routing_trace",
    "started_at",
    "completed_at",
    "prompt_sha256",
    "task_input_sha256",
    "run_compatibility_fingerprint",
    "final_text_sha256",
    "final_text_chars",
    "error",
    "stream_tool_call_count",
    "server_tool_call_count",
    "server_tool_use",
    "total_tool_call_count",
    "trajectory_steps",
    "llm_request_count",
    "generation_attempt_count",
    "generation_max_attempts",
    "generation_retry_backoff_s",
    "generation_attempt_total_billed_cost",
    "generation_retry_reasons",
    "execution",
    "usage",
    "cost_accounting",
    "openrouter_non_byok_audit",
    "run_trace",
    "ensemble_trace",
    "fusion_delta",
)


class FinalizationError(ValueError):
    """A deterministic finalization gate failed."""


@dataclass(frozen=True)
class SourceRecord:
    path: Path
    source_index: int
    line: int
    row: dict[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        return (
            str(self.row.get("group") or ""),
            str(self.row.get("task_id") or ""),
        )

    @property
    def reference(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "source_index": self.source_index,
            "line": self.line,
        }


@dataclass
class LedgerEntry:
    identity: str
    scopes: set[str] = field(default_factory=set)
    units: list[dict[str, Any]] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    response_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class FinalizerExperimentPolicy:
    """Authenticated, cross-group experiment semantics used by finalization.

    The source manifests already authenticate each compatibility contract.  This
    projection makes the shared ``global_experiment_profile`` the authority for
    tunable experiment semantics and keeps operational trace fields as a
    reverse-checked projection rather than a second, hard-coded configuration.
    """

    profile: dict[str, Any]
    timeouts: dict[str, Any]
    runner: dict[str, Any]
    generation: dict[str, Any]
    judge: dict[str, Any]
    tools: dict[str, Any]
    aggregator_recovery: dict[str, Any]
    proposer_recovery: dict[str, Any]
    judge_provider_pin: str

    @property
    def generation_max_attempts(self) -> int:
        return int(self.generation["max_attempts"])

    @property
    def judge_model(self) -> str:
        return str(self.judge["model"])

    @property
    def judge_repeats(self) -> int:
        return int(self.judge["repeats"])

    @property
    def judge_max_attempts(self) -> int:
        return int(self.judge["max_attempts"])


def authenticated_generation_attempt_limit(
    requested: Any,
    policy: FinalizerExperimentPolicy,
) -> int:
    """Resolve an optional CLI assertion against authenticated run policy."""

    expected = policy.generation_max_attempts
    if requested is None:
        return expected
    if (
        isinstance(requested, bool)
        or not isinstance(requested, int)
        or requested < 1
        or requested != expected
    ):
        raise FinalizationError(
            "max generation attempts CLI assertion differs from authenticated "
            f"experiment policy: requested={requested!r}, expected={expected}"
        )
    return expected


def canonical_json_bytes(value: Any) -> bytes:
    """Use the runtime's canonical JSON encoding for offline verification."""

    return ranking_canonical_json_bytes(value)


def canonical_sha256(value: Any, *, prefix: bool = False) -> str:
    digest = ranking_canonical_json_sha256(value)
    return f"sha256:{digest}" if prefix else digest


G1_RANKING_CONFIG_RESOLUTION_FIELDS = frozenset(
    {
        "base_config",
        "override",
        "effective_config",
        "base_sha256",
        "override_sha256",
        "effective_sha256",
        "thinking_assignment_enabled",
    }
)
G1_BASELINE_RANKING_CONFIG_CONTRACT_FIELDS = (
    "baseline_expected_ranking_config_schema_version",
    "baseline_expected_ranking_config_version",
    "baseline_expected_ranking_config_sha256",
)


def _deep_merge_frozen_ranking_config(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge frozen public JSON without consulting packaged config state."""

    merged = copy.deepcopy(dict(base))
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge_frozen_ranking_config(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _validated_frozen_ranking_config(value: Any) -> dict[str, Any] | None:
    """Validate one supplied full config without loading packaged config data."""

    if not isinstance(value, Mapping):
        return None
    try:
        from opensquilla.provider.ranking_router import _validate_ranking_config

        validated = _validate_ranking_config(value)
    except (KeyError, TypeError, ValueError):
        return None
    payload = copy.deepcopy(dict(validated))
    return payload if canonical_json_bytes(payload) == canonical_json_bytes(value) else None


def _mapping_projection_matches(value: Any, projection: Any) -> bool:
    """Require every sparse override leaf in the frozen effective value."""

    if isinstance(projection, Mapping):
        return isinstance(value, Mapping) and all(
            key in value and _mapping_projection_matches(value[key], child)
            for key, child in projection.items()
        )
    return value == projection


def _frozen_task_analyzer_policy(config: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project analyzer policy from an already frozen full ranking config."""

    validated = _validated_frozen_ranking_config(config)
    analyzer = validated.get("task_analyzer") if isinstance(validated, Mapping) else None
    if not isinstance(analyzer, Mapping):
        return None
    public_fields = (
        "provider",
        "model",
        "upstream_provider",
        "stream_close_timeout_seconds",
    )
    has_public_policy = all(field in analyzer for field in public_fields)
    if any(field in analyzer for field in public_fields) and not has_public_policy:
        return None
    policy = dict(FORMAL_TASK_ANALYZER_EXECUTION_POLICY)
    if has_public_policy:
        policy.update({field: analyzer[field] for field in public_fields})
    policy.update(
        {
            "timeout_seconds": analyzer.get("timeout_seconds"),
            "max_retries": analyzer.get("max_retries"),
        }
    )
    return policy


def g1_ranking_config_identity(
    contract: Mapping[str, Any] | None,
) -> tuple[str, str, str] | None:
    """Authenticate the frozen G1 ranking resolution and return its identity.

    Historical manifests did not carry a resolution object and remain bound
    to the formal baseline constants. New manifests must make the base,
    sparse override, and effective object independently hash-verifiable, then
    reproduce the exact resolution with the runtime's canonical resolver.
    """

    if not isinstance(contract, Mapping):
        return None
    expected = (
        str(contract.get("expected_ranking_config_schema_version") or ""),
        str(contract.get("expected_ranking_config_version") or ""),
        str(contract.get("expected_ranking_config_sha256") or ""),
    )
    baseline = (
        FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION,
        FORMAL_G1_RANKING_CONFIG_VERSION,
        FORMAL_G1_RANKING_CONFIG_SHA256,
    )
    declared_baseline = tuple(
        str(contract.get(field) or "") for field in G1_BASELINE_RANKING_CONFIG_CONTRACT_FIELDS
    )
    resolution = contract.get("ranking_config_resolution")
    if resolution is None:
        if any(field in contract for field in G1_BASELINE_RANKING_CONFIG_CONTRACT_FIELDS):
            if declared_baseline not in {
                baseline,
                *HISTORICAL_G1_RANKING_CONFIG_IDENTITIES,
            }:
                return None
        return (
            expected
            if expected == baseline or expected in HISTORICAL_G1_RANKING_CONFIG_IDENTITIES
            else None
        )
    if (
        not isinstance(resolution, Mapping)
        or set(resolution) != G1_RANKING_CONFIG_RESOLUTION_FIELDS
        or declared_baseline != baseline
    ):
        return None

    base = resolution.get("base_config")
    override = resolution.get("override")
    effective = resolution.get("effective_config")
    if (
        not isinstance(base, Mapping)
        or not isinstance(effective, Mapping)
        or (override is not None and not isinstance(override, Mapping))
        or type(resolution.get("thinking_assignment_enabled")) is not bool
    ):
        return None
    base_sha256 = str(resolution.get("base_sha256") or "")
    raw_override_sha256 = resolution.get("override_sha256")
    override_sha256 = str(raw_override_sha256 or "")
    effective_sha256 = str(resolution.get("effective_sha256") or "")
    if (
        not HEX64.fullmatch(base_sha256)
        or not HEX64.fullmatch(effective_sha256)
        or canonical_sha256(base) != base_sha256
        or (override is None and raw_override_sha256 is not None)
        or (
            isinstance(override, Mapping)
            and (
                not HEX64.fullmatch(override_sha256)
                or canonical_sha256(override) != override_sha256
            )
        )
        or canonical_sha256(effective) != effective_sha256
        or base.get("schema_version") != baseline[0]
        or base.get("config_version") != baseline[1]
        or base_sha256 != baseline[2]
        or effective.get("schema_version") != expected[0]
        or effective.get("config_version") != expected[1]
        or effective_sha256 != expected[2]
        or not HEX64.fullmatch(expected[2])
    ):
        return None

    if (
        _validated_frozen_ranking_config(base) is None
        or _validated_frozen_ranking_config(effective) is None
    ):
        return None
    thinking_enabled = resolution["thinking_assignment_enabled"]
    effective_thinking = effective.get("thinking_assignment")
    if thinking_enabled:
        if (
            effective.get("schema_version") != "step2-ranking-config-v4"
            or not isinstance(effective_thinking, Mapping)
            or effective_thinking.get("enabled") is not True
        ):
            return None
    elif effective.get("schema_version") != FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION or (
        "thinking_assignment" in effective
    ):
        return None

    if override is None:
        if (
            raw_override_sha256 is not None
            or thinking_enabled
            or canonical_json_bytes(base) != canonical_json_bytes(effective)
            or base_sha256 != effective_sha256
        ):
            return None
    else:
        if not override or "schema_version" in override or "config_version" in override:
            return None
        expected_version = f"{base['config_version']}+override.{override_sha256[:12]}"
        if effective.get("config_version") != expected_version:
            return None
        nonthinking_override = {
            key: value for key, value in override.items() if key != "thinking_assignment"
        }
        expected_nonthinking = _deep_merge_frozen_ranking_config(
            base,
            nonthinking_override,
        )
        expected_nonthinking["config_version"] = expected_version
        effective_nonthinking = copy.deepcopy(dict(effective))
        if thinking_enabled:
            effective_nonthinking["schema_version"] = FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION
            effective_nonthinking.pop("thinking_assignment", None)
        if canonical_json_bytes(expected_nonthinking) != canonical_json_bytes(
            effective_nonthinking
        ):
            return None
        thinking_override = override.get("thinking_assignment")
        if thinking_override is not None:
            if not isinstance(thinking_override, Mapping):
                return None
            if thinking_enabled:
                if not _mapping_projection_matches(effective_thinking, thinking_override):
                    return None
            elif thinking_override.get("enabled") is not False:
                return None
    return expected


def g1_task_analyzer_execution_policy(
    contract: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the analyzer policy authenticated by a G1 ranking contract.

    Historical no-resolution contracts predate explicit analyzer identity and
    replay with the original OpenRouter/Opus/Anthropic policy. Current configs
    and sparse overrides must carry a policy exactly matching the authenticated
    effective ranking config.
    """

    identity = g1_ranking_config_identity(contract)
    if identity is None or not isinstance(contract, Mapping):
        return None
    resolution = contract.get("ranking_config_resolution")
    if isinstance(resolution, Mapping):
        effective = resolution.get("effective_config")
        if not isinstance(effective, Mapping):
            return None
        policy = _frozen_task_analyzer_policy(effective)
        requires_declared_policy = True
    else:
        policy = dict(FORMAL_TASK_ANALYZER_EXECUTION_POLICY)
        requires_declared_policy = identity == (
            FORMAL_G1_RANKING_CONFIG_SCHEMA_VERSION,
            FORMAL_G1_RANKING_CONFIG_VERSION,
            FORMAL_G1_RANKING_CONFIG_SHA256,
        )
    if policy is None:
        return None
    declared = contract.get("task_analyzer")
    if declared is None:
        return None if requires_declared_policy else policy
    if not isinstance(declared, Mapping) or dict(declared) != policy:
        return None
    return policy


def g1_task_analyzer_execution_contract(
    contract: Mapping[str, Any] | None,
    declared_execution_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the canonical execution contract shared by every audit path."""

    primary = g1_task_analyzer_execution_policy(contract)
    if primary is None or not isinstance(contract, Mapping):
        return None
    from opensquilla.eval.draco_task_analyzer_execution import (
        task_analyzer_execution_contract_from_g1_registry,
        validated_task_analyzer_execution_contract,
    )

    source_contract = copy.deepcopy(dict(contract))
    source_contract.setdefault("task_analyzer", copy.deepcopy(primary))
    try:
        derived = task_analyzer_execution_contract_from_g1_registry(
            source_contract
        )
    except (TypeError, ValueError):
        return None
    validated = validated_task_analyzer_execution_contract(derived)
    declared = declared_execution_contract
    if validated is None or (
        declared is not None
        and validated_task_analyzer_execution_contract(declared) != validated
    ):
        return None
    return validated


def g1_task_analyzer_execution_chain(
    contract: Mapping[str, Any] | None,
    declared_execution_contract: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]] | None:
    """Return the authenticated ordered live Analyzer route chain."""

    execution_contract = g1_task_analyzer_execution_contract(
        contract,
        declared_execution_contract,
    )
    return (
        copy.deepcopy(execution_contract["routes"])
        if execution_contract is not None
        else None
    )


def g1_task_analyzer_physical_routes_from_trace(
    plan: Mapping[str, Any],
    execution_contract: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Derive paid Analyzer routes through the shared fail-closed validator."""

    analyzer_trace = plan.get("task_analyzer")
    if not isinstance(analyzer_trace, Mapping):
        return [], ["missing_g1_task_analyzer_trace"]
    from opensquilla.eval.draco_task_analyzer_execution import (
        validate_task_analyzer_execution_trace,
    )

    validated, reasons = validate_task_analyzer_execution_trace(
        execution_contract=execution_contract,
        analyzer_trace=analyzer_trace,
        models_equivalent=_formal_openrouter_models_equivalent,
    )
    reason_aliases = {
        "invalid_task_analyzer_chain_trace": (
            "invalid_g1_task_analyzer_chain_trace"
        ),
        "invalid_task_analyzer_physical_attempts": (
            "invalid_g1_task_analyzer_attempt_sequence"
        ),
    }
    return (
        copy.deepcopy(validated["physical_routes"])
        if validated is not None
        else [],
        [reason_aliases.get(reason, reason) for reason in reasons],
    )


def g1_analyzer_failure_fallback_policy(
    contract: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the authenticated complete-only fixed ensemble fallback."""

    if not isinstance(contract, Mapping):
        return None
    raw = contract.get("analyzer_failure_fallback_ensemble")
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != {
        "proposers",
        "aggregator",
        "min_successful_proposers",
        "complete_proposers_only",
        "aggregator_max_recovery_actions",
    }:
        return None
    raw_proposers = raw.get("proposers")
    raw_aggregator = raw.get("aggregator")
    if (
        not isinstance(raw_proposers, list)
        or len(raw_proposers) != 4
        or not isinstance(raw_aggregator, Mapping)
        or raw.get("min_successful_proposers") != 1
        or raw.get("complete_proposers_only") is not True
        or raw.get("aggregator_max_recovery_actions") != 1
    ):
        return None

    def normalize_route(value: Any) -> dict[str, str] | None:
        if not isinstance(value, Mapping) or set(value) != {
            "provider",
            "model",
            "upstream_provider",
            "max_attempts",
        }:
            return None
        provider = str(value.get("provider") or "").strip().casefold()
        model = str(value.get("model") or "").strip()
        upstream = str(value.get("upstream_provider") or "").strip().casefold()
        if (
            provider != "openrouter"
            or not model
            or not upstream
            or upstream == "auto"
            or value.get("max_attempts") != 1
        ):
            return None
        return {
            "provider": provider,
            "model": model,
            "upstream_provider": upstream,
        }

    proposers = [normalize_route(route) for route in raw_proposers]
    aggregator = normalize_route(raw_aggregator)
    if aggregator is None or any(route is None for route in proposers):
        return None
    normalized_proposers = [route for route in proposers if route is not None]
    proposer_identities = [
        f"{route['provider']}:{route['model']}" for route in normalized_proposers
    ]
    if len(set(proposer_identities)) != len(proposer_identities):
        return None
    return {
        "proposers": normalized_proposers,
        "aggregator": aggregator,
        "selected_P": proposer_identities,
        "selected_A": f"{aggregator['provider']}:{aggregator['model']}",
        "min_successful_proposers": 1,
        "complete_proposers_only": True,
        "aggregator_max_recovery_actions": 1,
    }


def g1_ranking_proposer_max(contract: Mapping[str, Any] | None) -> int | None:
    """Return the authenticated effective proposer ceiling for a G1 arm."""

    if g1_ranking_config_identity(contract) is None or not isinstance(contract, Mapping):
        return None
    resolution = contract.get("ranking_config_resolution")
    if resolution is None:
        return (
            FORMAL_G1_PROPOSER_COUNT_MAX
            if contract.get("expected_proposer_count_max") == FORMAL_G1_PROPOSER_COUNT_MAX
            else None
        )
    if (
        not isinstance(resolution, Mapping)
        or contract.get("baseline_expected_proposer_count_max") != FORMAL_G1_PROPOSER_COUNT_MAX
    ):
        return None
    effective = resolution.get("effective_config")
    proposer_count = effective.get("proposer_count") if isinstance(effective, Mapping) else None
    by_tier = proposer_count.get("by_tier") if isinstance(proposer_count, Mapping) else None
    high_risk = proposer_count.get("high_risk") if isinstance(proposer_count, Mapping) else None
    if not isinstance(by_tier, Mapping) or not isinstance(high_risk, Mapping):
        return None
    raw_maxima = [row.get("max") for row in by_tier.values() if isinstance(row, Mapping)]
    raw_maxima.append(high_risk.get("max"))
    if not raw_maxima or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in raw_maxima
    ):
        return None
    effective_max = max(raw_maxima)
    return effective_max if contract.get("expected_proposer_count_max") == effective_max else None


def authenticated_registry_all_routes(
    contract: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """Validate the registry-all route projection frozen in a run contract."""

    if not isinstance(contract, Mapping):
        return None
    source_identity = g1_registry_source_identity(contract)
    routes = contract.get("expected_routes")
    routes_hash = str(contract.get("expected_routes_sha256") or "")
    expected_count = contract.get("expected_candidate_count")
    if (
        contract.get("candidate_scope") != "registry_all"
        or contract.get("policy") != "all_registry_models"
        or not str(contract.get("profile_id") or "").strip()
        or source_identity is None
        or (
            str(contract.get("source_registry_snapshot_version") or ""),
            str(contract.get("expected_source_registry_snapshot_sha256") or ""),
        )
        != source_identity
        or not isinstance(routes, Mapping)
        or isinstance(expected_count, bool)
        or expected_count != FORMAL_G1_REGISTRY_ALL_CANDIDATE_COUNT
        or len(routes) != expected_count
        or not HEX64.fullmatch(routes_hash)
        or canonical_sha256(routes) != routes_hash
    ):
        return None
    normalized: dict[str, str] = {}
    for raw_model, raw_provider in routes.items():
        model = str(raw_model)
        provider = str(raw_provider)
        if (
            model != model.strip().casefold()
            or "/" not in model
            or any(not segment for segment in model.split("/"))
            or provider != "auto"
            or model in normalized
        ):
            return None
        normalized[model] = provider
    return normalized


def g1_registry_source_identity(
    contract: Mapping[str, Any] | None,
) -> tuple[str, str] | None:
    """Return the immutable source identity selected by frozen ranking policy.

    Thinking-v4 campaigns were launched against the authenticated full-registry
    projection. Historical/no-resolution and thinking-disabled campaigns remain
    bound to the legacy projection. The decision is made solely from the frozen,
    self-validated ranking resolution carried by the run contract.
    """

    if g1_ranking_config_identity(contract) is None or not isinstance(contract, Mapping):
        return None
    resolution = contract.get("ranking_config_resolution")
    thinking_enabled = (
        isinstance(resolution, Mapping) and resolution.get("thinking_assignment_enabled") is True
    )
    source_version = str(contract.get("source_registry_snapshot_version") or "")
    return (
        (
            FORMAL_G1_FULL_REGISTRY_SNAPSHOT_VERSION
            if contract.get("candidate_scope") == "registry_all"
            else source_version
        ),
        (
            FORMAL_G1_FULL_REGISTRY_SNAPSHOT_SHA256
            if thinking_enabled
            else FORMAL_G1_LEGACY_SOURCE_REGISTRY_SNAPSHOT_SHA256
        ),
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def parse_maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def required_decimal(value: Any, *, label: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise FinalizationError(f"{label} is missing or non-numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinalizationError(f"{label} is not a valid decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise FinalizationError(f"{label} must be finite and non-negative")
    return parsed


def finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and math.isfinite(value):
        return max(0, int(value))
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return 0


def require_formal_fields(
    value: Any,
    expected: Mapping[str, Any],
    *,
    label: str,
) -> None:
    """Require a typed, recursively projected formal execution contract."""

    if not isinstance(value, Mapping):
        raise FinalizationError(f"{label} is missing or malformed")
    for field_name, expected_value in expected.items():
        field_label = f"{label}.{field_name}"
        actual_value = value.get(field_name)
        if isinstance(expected_value, Mapping):
            require_formal_fields(actual_value, expected_value, label=field_label)
        elif isinstance(expected_value, Decimal):
            if required_decimal(actual_value, label=field_label) != expected_value:
                raise FinalizationError(f"{field_label} differs from the formal value")
        elif isinstance(expected_value, bool):
            if actual_value is not expected_value:
                raise FinalizationError(f"{field_label} differs from the formal value")
        elif isinstance(expected_value, int):
            if (
                isinstance(actual_value, bool)
                or not isinstance(actual_value, int)
                or actual_value != expected_value
            ):
                raise FinalizationError(f"{field_label} differs from the formal value")
        elif isinstance(expected_value, (list, tuple)):
            if not isinstance(actual_value, list) or actual_value != list(expected_value):
                raise FinalizationError(f"{field_label} differs from the formal value")
        elif actual_value != expected_value:
            raise FinalizationError(f"{field_label} differs from the formal value")


def validate_formal_manifest_command(
    payload: Mapping[str, Any],
    *,
    path: Path,
    groups: Sequence[str] = GROUPS,
    expected_task_concurrency: int = FORMAL_TASK_CONCURRENCY,
    expected_judge_concurrency: int = FORMAL_JUDGE_CONCURRENCY,
) -> dict[str, int]:
    """Validate scheduling fields intentionally excluded from compatibility."""

    if (
        isinstance(expected_task_concurrency, bool)
        or not isinstance(expected_task_concurrency, int)
        or expected_task_concurrency < 1
    ):
        raise FinalizationError("expected task concurrency must be a positive integer")
    if (
        isinstance(expected_judge_concurrency, bool)
        or not isinstance(expected_judge_concurrency, int)
        or expected_judge_concurrency < 1
    ):
        raise FinalizationError("expected judge concurrency must be a positive integer")
    command = payload.get("command")
    parsed_args = command.get("parsed_args") if isinstance(command, Mapping) else None
    require_formal_fields(
        parsed_args,
        {
            "groups": ",".join(groups),
            "max_tasks": FROZEN_DRACO_MINI_TASK_COUNT,
            "concurrency": expected_task_concurrency,
            "judge_concurrency": expected_judge_concurrency,
            "require_clean_source": True,
            "dry_run": False,
        },
        label=f"{path} command.parsed_args",
    )
    return {
        "task_concurrency": expected_task_concurrency,
        "judge_concurrency": expected_judge_concurrency,
    }


def normalize_key_fingerprint(value: Any, *, label: str) -> str:
    fingerprint = str(value or "").strip().casefold()
    if fingerprint.startswith("sha256:"):
        fingerprint = fingerprint[7:]
    if not HEX64.fullmatch(fingerprint):
        raise FinalizationError(f"{label} is not a SHA-256 key fingerprint")
    return fingerprint


def parse_iso(value: Any, *, label: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise FinalizationError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinalizationError(f"{label} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise FinalizationError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def normalize_groups(value: str) -> tuple[str, ...]:
    raw_groups = tuple(item.strip() for item in value.split(","))
    if (
        not raw_groups
        or any(not group for group in raw_groups)
        or len(set(raw_groups)) != len(raw_groups)
        or any(group not in GROUP_SET for group in raw_groups)
        or raw_groups != tuple(group for group in SUPPORTED_GROUPS if group in raw_groups)
    ):
        raise FinalizationError(
            "formal finalization groups must be a non-empty, duplicate-free "
            f"canonical-order subset of {','.join(SUPPORTED_GROUPS)}"
        )
    return raw_groups


def require_regular_file(path: Path, *, owner_only: bool = True) -> Path:
    if path.is_symlink() or not path.is_file():
        raise FinalizationError(f"source is not a regular non-symlink file: {path}")
    resolved = path.resolve(strict=True)
    mode = resolved.stat().st_mode & 0o777
    if owner_only and mode & 0o077:
        raise FinalizationError(f"sensitive source is not owner-only: {path}")
    return resolved


def load_json(path: Path, *, owner_only: bool = True) -> dict[str, Any]:
    resolved = require_regular_file(path, owner_only=owner_only)
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalizationError(f"invalid JSON source {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise FinalizationError(f"JSON source is not an object: {resolved}")
    return value


def load_jsonl_rows(
    path: Path,
    *,
    owner_only: bool,
    source_label: str,
) -> list[tuple[int, Any]]:
    """Load JSONL records without treating Unicode line separators as row boundaries."""
    resolved = require_regular_file(path, owner_only=owner_only)
    rows: list[tuple[int, Any]] = []
    try:
        with resolved.open("r", encoding="utf-8", newline="\n") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise FinalizationError(
                        f"invalid {source_label} at {resolved}:{line_number}"
                    ) from exc
                rows.append((line_number, value))
    except (OSError, UnicodeError) as exc:
        raise FinalizationError(f"unable to read {source_label} {resolved}: {exc}") from exc
    return rows


def read_tasks(path: Path) -> list[dict[str, Any]]:
    resolved = require_regular_file(path, owner_only=False)
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, value in load_jsonl_rows(
        resolved,
        owner_only=False,
        source_label="benchmark JSONL",
    ):
        if not isinstance(value, dict):
            raise FinalizationError(f"benchmark row is not an object at {resolved}:{line_number}")
        task_id = str(value.get("id") or value.get("task_id") or "").strip()
        prompt = str(value.get("prompt") or value.get("problem") or "").strip()
        if not task_id or not prompt or task_id in seen:
            raise FinalizationError(
                f"benchmark row requires a unique id and prompt at {resolved}:{line_number}"
            )
        seen.add(task_id)
        value["id"] = task_id
        value["prompt"] = prompt
        if "rubric" in value:
            value["rubric"] = parse_maybe_json(value["rubric"])
        elif "answer" in value:
            value["rubric"] = parse_maybe_json(value["answer"])
        tasks.append(value)
    if not tasks:
        raise FinalizationError("benchmark contains no tasks")
    return tasks


def validate_frozen_draco_input(path: Path, tasks: Sequence[Mapping[str, Any]]) -> str:
    digest = file_sha256(path)
    if digest != FROZEN_DRACO_MINI_SHA256:
        raise FinalizationError("DRACO Mini input SHA256 differs from the frozen set")
    if len(tasks) != FROZEN_DRACO_MINI_TASK_COUNT:
        raise FinalizationError(
            f"DRACO Mini input must contain exactly {FROZEN_DRACO_MINI_TASK_COUNT} tasks"
        )
    task_ids = [str(task.get("id") or "") for task in tasks]
    if len(set(task_ids)) != FROZEN_DRACO_MINI_TASK_COUNT:
        raise FinalizationError("DRACO Mini input task IDs are not unique")
    return digest


def result_evidence_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": RESULT_EVIDENCE_SCHEMA,
        "result": {key: value for key, value in row.items() if key != RESULT_EVIDENCE_SHA256_FIELD},
    }


def verify_result_row_evidence(row: Mapping[str, Any]) -> bool:
    if row.get("result_evidence_schema") != RESULT_EVIDENCE_SCHEMA:
        return False
    actual = row.get(RESULT_EVIDENCE_SHA256_FIELD)
    if not isinstance(actual, str):
        return False
    try:
        expected = canonical_sha256(result_evidence_payload(row), prefix=True)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def seal_result_row(row: Mapping[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(dict(row))
    sealed["result_evidence_schema"] = RESULT_EVIDENCE_SCHEMA
    sealed.pop(RESULT_EVIDENCE_SHA256_FIELD, None)
    sealed[RESULT_EVIDENCE_SHA256_FIELD] = canonical_sha256(
        result_evidence_payload(sealed), prefix=True
    )
    return sealed


def read_source_rows(paths: Sequence[Path]) -> tuple[list[SourceRecord], dict[str, str]]:
    if not paths:
        raise FinalizationError("at least one --result source is required")
    records: list[SourceRecord] = []
    snapshots: dict[str, str] = {}
    for source_index, raw_path in enumerate(paths):
        path = require_regular_file(raw_path, owner_only=True)
        key = str(path)
        if key in snapshots:
            raise FinalizationError(f"duplicate result source: {path}")
        snapshots[key] = file_sha256(path)
        with path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise FinalizationError(
                        f"invalid result JSONL at {path}:{line_number}"
                    ) from exc
                if not isinstance(row, dict):
                    raise FinalizationError(f"result row is not an object at {path}:{line_number}")
                if not verify_result_row_evidence(row):
                    raise FinalizationError(
                        f"result row is not sealed or was mutated: {path}:{line_number}"
                    )
                records.append(SourceRecord(path, source_index, line_number, row))
    if not records:
        raise FinalizationError("result sources contain no sealed rows")
    return records, snapshots


def verify_source_snapshots(snapshots: Mapping[str, str]) -> None:
    for raw_path, expected in snapshots.items():
        path = require_regular_file(Path(raw_path), owner_only=False)
        actual = file_sha256(path)
        if not hmac.compare_digest(actual, expected):
            raise FinalizationError(f"source shard changed during finalization: {path}")


def validate_source_policy_history(
    records: Sequence[SourceRecord],
) -> list[dict[str, Any]]:
    """Return immutable policy failures for audit without erasing execution."""

    failures: list[dict[str, Any]] = []
    for record in records:
        row = record.row
        error = str(row.get("error") or "")
        audit = row.get("openrouter_non_byok_audit")
        audit_status = str(audit.get("status") or "") if isinstance(audit, Mapping) else ""
        unsafe = isinstance(audit, Mapping) and audit.get("policy_safe_to_continue") is False
        if (
            error in POLICY_VIOLATION_ERRORS
            or unsafe
            or audit_status
            in {
                "policy_violation",
                "explicit_byok",
                "provider_conflict",
                "receipt_conflict",
            }
        ):
            failures.append(
                record.reference
                | {
                    "group": record.key[0],
                    "task_id": record.key[1],
                    "error": error,
                    "audit_status": audit_status,
                }
            )
    return failures


def load_manifest_contracts(
    paths: Sequence[Path],
    *,
    result_paths: Sequence[Path],
    groups: Sequence[str],
    expected_task_concurrency: int = FORMAL_TASK_CONCURRENCY,
    expected_judge_concurrency: int = FORMAL_JUDGE_CONCURRENCY,
) -> tuple[dict[str, str], dict[str, dict[str, Any]], str, list[dict[str, Any]]]:
    if not paths:
        raise FinalizationError("at least one --manifest source is required")
    if len(paths) != len(result_paths):
        raise FinalizationError(
            "each result shard must have exactly one manifest in matching order"
        )
    allowed_statuses = {
        "complete",
        "metadata_incomplete",
        "cost_audit_failed",
        "audit_incomplete",
        "judge_incomplete",
        "result_incomplete",
        "resume_repair_incomplete",
    }
    audit_failure_markers = {
        "openrouter_non_byok_policy_violation",
        "openrouter_byok_detected",
        "cost_audit_failed",
    }
    authoritative_fingerprints: dict[str, str] | None = None
    authoritative_contracts: dict[str, dict[str, Any]] | None = None
    key_fingerprint = ""
    source_evidence: list[dict[str, Any]] = []
    prior_manifest_attempt_ids: set[str] = set()
    for raw_path, raw_result_path in zip(paths, result_paths, strict=True):
        path = require_regular_file(raw_path, owner_only=True)
        result_path = require_regular_file(raw_result_path, owner_only=True)
        payload = load_json(path)
        execution_scheduling = validate_formal_manifest_command(
            payload,
            path=path,
            groups=groups,
            expected_task_concurrency=expected_task_concurrency,
            expected_judge_concurrency=expected_judge_concurrency,
        )
        status = str(payload.get("status") or "")
        if status not in allowed_statuses:
            raise FinalizationError(
                f"source manifest status is not allowed for finalization: {status!r} at {path}"
            )
        failure_text = json.dumps(payload.get("failure"), ensure_ascii=False, sort_keys=True)
        manifest_audit_warnings = sorted(
            marker for marker in audit_failure_markers if marker in failure_text
        )
        if "preflight_failed" in failure_text:
            raise FinalizationError(f"source manifest contains a fatal preflight failure: {path}")
        tool_policy = payload.get("tool_policy")
        local_tools = (
            tool_policy.get("local_web_tools") if isinstance(tool_policy, Mapping) else None
        )
        preflight = local_tools.get("preflight") if isinstance(local_tools, Mapping) else None
        preflight_calls = (
            preflight.get("preflight_calls") if isinstance(preflight, Mapping) else None
        )
        preflight_status = (
            str(preflight.get("status") or "") if isinstance(preflight, Mapping) else ""
        )
        if (
            preflight_status not in {"passed", "skipped_not_required"}
            or not isinstance(preflight_calls, Mapping)
            or any(
                isinstance(preflight_calls.get(tool_name), bool)
                or not isinstance(preflight_calls.get(tool_name), int)
                or int(preflight_calls[tool_name]) < 0
                for tool_name in ("web_search", "web_fetch")
            )
        ):
            raise FinalizationError(
                f"live source manifest lacks a passed Web preflight ledger: {path}"
            )
        artifacts = payload.get("artifacts")
        declared_result = artifacts.get("results_jsonl") if isinstance(artifacts, Mapping) else None
        if (
            not isinstance(declared_result, str)
            or Path(declared_result).resolve(strict=False) != result_path
        ):
            raise FinalizationError(
                f"manifest results_jsonl is not bound to its result shard: {path}"
            )
        artifact_verification: dict[str, Any] | None = None
        declared_checkpoint = (
            artifacts.get("checkpoint_json") if isinstance(artifacts, Mapping) else None
        )
        artifact_recovery = payload.get("artifact_recovery")
        expected_durability = durable_artifact_capability_contract()
        manifest_schema = payload.get("schema")
        top_level_durability = payload.get("durable_artifact_capability")
        compatibility_preview = payload.get("run_compatibility")
        contracts_preview = (
            compatibility_preview.get("contracts")
            if isinstance(compatibility_preview, Mapping)
            else None
        )
        contract_durability = [
            (
                contracts_preview[group].get("durable_artifact_capability")
                if isinstance(contracts_preview, Mapping)
                and isinstance(contracts_preview.get(group), Mapping)
                else None
            )
            for group in groups
        ]
        durable_v2 = bool(
            manifest_schema == DRACO_RUN_MANIFEST_SCHEMA_V2
            or top_level_durability is not None
            or any(value is not None for value in contract_durability)
            or artifact_recovery is not None
        )
        if manifest_schema not in {None, DRACO_RUN_MANIFEST_SCHEMA_V2}:
            raise FinalizationError(
                f"source manifest uses an unsupported schema: {path}"
            )
        if durable_v2 and (
            manifest_schema != DRACO_RUN_MANIFEST_SCHEMA_V2
            or top_level_durability != expected_durability
            or any(value != expected_durability for value in contract_durability)
            or declared_checkpoint is None
        ):
            raise FinalizationError(
                f"manifest durable capability was downgraded or is incomplete: {path}"
            )
        if durable_v2:
            stamp = payload.get("stamp")
            if not isinstance(stamp, str) or not re.fullmatch(
                r"[0-9]{8}-[0-9]{6}", stamp
            ):
                raise FinalizationError(f"manifest durable stamp is malformed: {path}")
            expected_paths = {
                "results_jsonl": path.parent / f"draco_ensemble_{stamp}.jsonl",
                "trace_jsonl": path.parent / f"draco_run_{stamp}.trace.jsonl",
                "checkpoint_json": path.parent / f"draco_run_{stamp}.checkpoint.json",
                "manifest_json": path.parent / f"draco_run_{stamp}.manifest.json",
            }
            for artifact_key, expected_path in expected_paths.items():
                raw_artifact_path = (
                    artifacts.get(artifact_key)
                    if isinstance(artifacts, Mapping)
                    else None
                )
                if (
                    not isinstance(raw_artifact_path, str)
                    or not Path(raw_artifact_path).is_absolute()
                    or Path(os.path.abspath(raw_artifact_path)) != expected_path
                ):
                    raise FinalizationError(
                        f"manifest durable artifact path is not stamp-bound: {path}"
                    )
        recovered_resume_scheduled_pairs: list[dict[str, str]] | None = None
        recovery_prior_manifest: Mapping[str, Any] | None = None
        if declared_checkpoint is not None:
            declared_trace = (
                artifacts.get("trace_jsonl") if isinstance(artifacts, Mapping) else None
            )
            if not isinstance(declared_trace, str) or not isinstance(
                declared_checkpoint, str
            ):
                raise FinalizationError(
                    f"manifest durable artifact paths are malformed: {path}"
                )
            trace_path = require_regular_file(Path(declared_trace), owner_only=True)
            checkpoint_path = require_regular_file(
                Path(declared_checkpoint), owner_only=True
            )
            try:
                artifact_verification = verify_durable_draco_artifacts(
                    results_path=result_path,
                    trace_path=trace_path,
                    checkpoint_path=checkpoint_path,
                )
            except (OSError, UnicodeError, DracoArtifactDurabilityError) as exc:
                raise FinalizationError(
                    f"manifest durable artifact binding failed: {path}: {exc}"
                ) from exc
        if artifact_recovery is not None:
            if artifact_verification is None:
                raise FinalizationError(
                    f"recovered manifest lacks a durable checkpoint binding: {path}"
                )
            if (
                not isinstance(artifact_recovery, Mapping)
                or artifact_recovery.get("schema")
                != "opensquilla.draco-artifact-recovery/v1"
                or status != "result_incomplete"
                or artifact_recovery.get("status") != "sealed_after_unclean_exit"
                or artifact_recovery.get("prior_status") not in {"running", "aborted"}
                or artifact_recovery.get("physical_request_policy")
                != "no_model_or_provider_calls"
                or artifact_recovery.get("automatic_rerun_allowed") is not False
                or artifact_recovery.get("rows_written")
                != artifact_verification["rows_written"]
                or artifact_recovery.get("results_sha256")
                != artifact_verification["results_sha256"]
                or artifact_recovery.get("trace_sha256")
                != artifact_verification["trace_sha256"]
                or artifact_recovery.get("checkpoint_sha256")
                != artifact_verification["checkpoint_sha256"]
                or not HEX64.fullmatch(
                    str(artifact_recovery.get("prior_manifest_sha256") or "")
                )
                or not isinstance(artifact_recovery.get("prior_manifest_path"), str)
            ):
                raise FinalizationError(
                    f"manifest artifact recovery contract is malformed: {path}"
                )
            declared_prior_manifest = artifacts.get("pre_recovery_manifest_json")
            declared_recovery_prior_manifest = artifact_recovery[
                "prior_manifest_path"
            ]
            expected_prior_manifest_path = path.with_name(
                f"{path.name}.pre-recovery"
            )
            if (
                not isinstance(declared_prior_manifest, str)
                or Path(declared_prior_manifest).resolve(strict=False)
                != Path(declared_recovery_prior_manifest).resolve(strict=False)
                or Path(declared_prior_manifest).resolve(strict=False)
                != expected_prior_manifest_path.resolve(strict=False)
            ):
                raise FinalizationError(
                    f"manifest recovery sidecar path is not bound: {path}"
                )
            prior_manifest_path = require_regular_file(
                Path(declared_prior_manifest), owner_only=True
            )
            prior_manifest = load_json(prior_manifest_path)
            recovery_prior_manifest = prior_manifest
            prior_artifacts = prior_manifest.get("artifacts")
            if (
                file_sha256(prior_manifest_path)
                != artifact_recovery["prior_manifest_sha256"]
                or prior_manifest.get("status") != artifact_recovery["prior_status"]
                or prior_manifest.get("artifact_recovery") is not None
                or prior_manifest.get("schema") != DRACO_RUN_MANIFEST_SCHEMA_V2
                or prior_manifest.get("durable_artifact_capability")
                != expected_durability
                or prior_manifest.get("run_compatibility")
                != payload.get("run_compatibility")
                or not isinstance(prior_artifacts, Mapping)
                or Path(str(prior_artifacts.get("results_jsonl") or "")).resolve(
                    strict=False
                )
                != result_path
                or Path(str(prior_artifacts.get("trace_jsonl") or "")).resolve(
                    strict=False
                )
                != trace_path
                or Path(str(prior_artifacts.get("checkpoint_json") or "")).resolve(
                    strict=False
                )
                != checkpoint_path
                or Path(str(prior_artifacts.get("manifest_json") or "")).resolve(
                    strict=False
                )
                != path
            ):
                raise FinalizationError(
                    f"manifest recovery sidecar evidence is invalid: {path}"
                )
            raw_recovered_schedule = artifact_recovery.get(
                "durable_scheduled_pairs"
            )
            prior_resume = prior_manifest.get("resume_selection")
            prior_schedule = (
                prior_resume.get("scheduled_pairs", [])
                if isinstance(prior_resume, Mapping)
                else []
            )
            if not isinstance(raw_recovered_schedule, list) or not isinstance(
                prior_schedule, list
            ):
                raise FinalizationError(
                    f"manifest recovered resume schedule is malformed: {path}"
                )
            if not isinstance(prior_resume, Mapping):
                if raw_recovered_schedule:
                    raise FinalizationError(
                        f"manifest recovered a schedule absent from its prior manifest: {path}"
                    )
                recovered_resume_scheduled_pairs = None
                raw_recovered_schedule = []
            prior_schedule_values = {
                (
                    str(item.get("group") or ""),
                    str(item.get("task_id") or ""),
                    str(item.get("action") or ""),
                )
                for item in prior_schedule
                if isinstance(item, Mapping)
            }
            if isinstance(prior_resume, Mapping):
                recovered_resume_scheduled_pairs = []
            seen_recovered_keys: set[tuple[str, str]] = set()
            for item in raw_recovered_schedule:
                if not isinstance(item, Mapping):
                    raise FinalizationError(
                        f"manifest recovered scheduled pair is malformed: {path}"
                    )
                normalized = {
                    "group": str(item.get("group") or ""),
                    "task_id": str(item.get("task_id") or ""),
                    "action": str(item.get("action") or ""),
                }
                key = (normalized["group"], normalized["task_id"])
                if (
                    (*key, normalized["action"]) not in prior_schedule_values
                    or normalized["action"] not in RESUME_SCHEDULE_ACTIONS
                    or key in seen_recovered_keys
                ):
                    raise FinalizationError(
                        f"manifest recovered schedule is not a unique prior subset: {path}"
                    )
                seen_recovered_keys.add(key)
                assert recovered_resume_scheduled_pairs is not None
                recovered_resume_scheduled_pairs.append(normalized)
        result_rows = [
            value
            for _, value in load_jsonl_rows(
                result_path,
                owner_only=True,
                source_label="result JSONL",
            )
        ]
        durable_row_markers = [
            row.get(DRACO_DURABLE_RESULT_ROW_FIELD)
            for row in result_rows
            if isinstance(row, Mapping)
            and DRACO_DURABLE_RESULT_ROW_FIELD in row
        ]
        if durable_v2:
            if len(durable_row_markers) != len(result_rows) or any(
                marker != expected_durability for marker in durable_row_markers
            ):
                raise FinalizationError(
                    f"manifest durable row marker is missing or invalid: {path}"
                )
        elif durable_row_markers:
            raise FinalizationError(
                f"manifest durable capability was stripped from sealed rows: {path}"
            )
        if isinstance(artifact_recovery, Mapping):
            assert recovery_prior_manifest is not None
            prior_resume = recovery_prior_manifest.get("resume_selection")
            prior_scheduled = (
                prior_resume.get("scheduled_pairs")
                if isinstance(prior_resume, Mapping)
                else None
            )
            expected_pairs: set[tuple[str, str]] = set()
            if isinstance(prior_resume, Mapping) and not isinstance(
                prior_scheduled, list
            ):
                raise FinalizationError(
                    f"recovery prior schedule is malformed: {path}"
                )
            if isinstance(prior_scheduled, list):
                for item in prior_scheduled:
                    if (
                        not isinstance(item, Mapping)
                        or not isinstance(item.get("group"), str)
                        or not isinstance(item.get("task_id"), str)
                    ):
                        raise FinalizationError(
                            f"recovery prior schedule is malformed: {path}"
                        )
                    key = (item["group"], item["task_id"])
                    if key in expected_pairs:
                        raise FinalizationError(
                            f"recovery prior schedule contains duplicate pairs: {path}"
                        )
                    expected_pairs.add(key)
            else:
                prior_groups = recovery_prior_manifest.get("groups")
                prior_task_ids = recovery_prior_manifest.get("task_ids")
                if not isinstance(prior_groups, list) or not isinstance(
                    prior_task_ids, list
                ):
                    raise FinalizationError(
                        f"recovery prior manifest lacks its original schedule: {path}"
                    )
                expected_pairs = {
                    (str(group), str(task_id))
                    for group in prior_groups
                    for task_id in prior_task_ids
                }
            durable_pairs = {
                (str(row.get("group") or ""), str(row.get("task_id") or ""))
                for row in result_rows
                if isinstance(row, Mapping)
            }
            expected_ambiguous = [
                {"group": group, "task_id": task_id}
                for group, task_id in sorted(expected_pairs - durable_pairs)
            ]
            raw_ambiguous = artifact_recovery.get("ambiguous_pairs")
            ambiguous_count = artifact_recovery.get("ambiguous_pair_count")
            if (
                not durable_pairs <= expected_pairs
                or not isinstance(raw_ambiguous, list)
                or isinstance(ambiguous_count, bool)
                or ambiguous_count != len(raw_ambiguous)
                or raw_ambiguous != expected_ambiguous
            ):
                raise FinalizationError(
                    f"manifest recovery ambiguity ledger is not bound to its durable prefix: {path}"
                )
        shard_attempt_ids = {
            str(attempt.get("attempt_id") or "")
            for row in result_rows
            if isinstance(row, Mapping)
            for attempt in (
                row.get("execution", {}).get("generation_attempts", [])
                if isinstance(row.get("execution"), Mapping)
                else []
            )
            if isinstance(attempt, Mapping)
            and HEX32.fullmatch(str(attempt.get("attempt_id") or ""))
        }
        new_attempt_ids = shard_attempt_ids - prior_manifest_attempt_ids
        if preflight_status == "skipped_not_required":
            resume_selection = payload.get("resume_selection")
            if resume_selection is None and isinstance(artifact_recovery, Mapping):
                resume_selection = prior_manifest.get("resume_selection")
            if (
                any(
                    int(preflight_calls[tool_name]) != 0
                    for tool_name in (
                        "web_search",
                        "web_fetch",
                    )
                )
                or not isinstance(resume_selection, Mapping)
                or resume_selection.get("model_regenerate_pair_count") != 0
                or bool(new_attempt_ids)
                or not prior_manifest_attempt_ids
                or any(
                    not isinstance(row.get("execution"), Mapping)
                    or row["execution"].get("generation_reused") is not True
                    or str(row["execution"].get("resume_action") or "")
                    not in {"regenerate", "judge_only", "metadata_only", "audit_only"}
                    or (
                        str(row["execution"].get("resume_action") or "") == "regenerate"
                        and (
                            row["execution"].get("generation_auto_retry_blocked") is not True
                            or row["execution"].get("generation_model_started") is not False
                            or not blocked_regenerate_terminal_evidence(row["execution"])
                        )
                    )
                    or (
                        str(row["execution"].get("resume_action") or "") == "metadata_only"
                        and row["execution"].get("judge_reran") is True
                    )
                    or (
                        str(row["execution"].get("resume_action") or "") == "audit_only"
                        and (
                            row["execution"].get("judge_reran") is not False
                            or row["execution"].get("audit_only_recorded") is not True
                            or not isinstance(
                                row["execution"].get("audit_only_summary"),
                                Mapping,
                            )
                            or row["execution"]["audit_only_summary"].get("status") != "recorded"
                            or row["execution"]["audit_only_summary"].get("generation_called")
                            is not False
                            or row["execution"]["audit_only_summary"].get("judge_called")
                            is not False
                        )
                    )
                    for row in result_rows
                    if isinstance(row, Mapping)
                )
            ):
                raise FinalizationError(
                    f"skipped Web preflight is not bound to a no-generation repair shard: {path}"
                )
        prior_manifest_attempt_ids.update(shard_attempt_ids)
        if nonnegative_int(payload.get("rows_written")) != len(result_rows):
            raise FinalizationError(f"manifest rows_written differs from its result shard: {path}")
        if (
            artifact_verification is not None
            and artifact_verification["rows_written"] != len(result_rows)
        ):
            raise FinalizationError(
                f"manifest checkpoint rows differ from its result shard: {path}"
            )
        manifest_groups = payload.get("groups")
        manifest_task_ids = payload.get("task_ids")
        if (
            not isinstance(manifest_groups, list)
            or manifest_groups != list(groups)
            or not isinstance(manifest_task_ids, list)
            or any(
                str(row.get("group") or "") not in manifest_groups
                or str(row.get("task_id") or "") not in manifest_task_ids
                for row in result_rows
                if isinstance(row, Mapping)
            )
        ):
            raise FinalizationError(
                f"manifest groups/task_ids do not cover its result shard: {path}"
            )
        raw_resume_selection = payload.get("resume_selection")
        if raw_resume_selection is not None and not isinstance(raw_resume_selection, Mapping):
            raise FinalizationError(f"manifest resume_selection is malformed: {path}")
        raw_scheduled_pairs = (
            raw_resume_selection.get("scheduled_pairs", [])
            if isinstance(raw_resume_selection, Mapping)
            else []
        )
        if not isinstance(raw_scheduled_pairs, list):
            raise FinalizationError(f"manifest scheduled_pairs is malformed: {path}")
        resume_scheduled_pairs: list[dict[str, str]] = []
        resume_schedule_contract_verified = False
        seen_scheduled_pairs: set[tuple[str, str]] = set()
        for scheduled in raw_scheduled_pairs:
            if not isinstance(scheduled, Mapping):
                raise FinalizationError(f"manifest scheduled pair is malformed: {path}")
            scheduled_group = str(scheduled.get("group") or "")
            scheduled_task = str(scheduled.get("task_id") or "")
            scheduled_action = str(scheduled.get("action") or "")
            scheduled_key = (scheduled_group, scheduled_task)
            if (
                scheduled_group not in manifest_groups
                or scheduled_task not in manifest_task_ids
                or scheduled_action
                not in RESUME_SCHEDULE_ACTIONS
                or scheduled_key in seen_scheduled_pairs
            ):
                raise FinalizationError(
                    f"manifest scheduled pair is not uniquely bound to its shard: {path}"
                )
            seen_scheduled_pairs.add(scheduled_key)
            resume_scheduled_pairs.append(
                {
                    "group": scheduled_group,
                    "task_id": scheduled_task,
                    "action": scheduled_action,
                }
            )
        if resume_scheduled_pairs:
            assert isinstance(raw_resume_selection, Mapping)
            result_pairs = {
                (str(row.get("group") or ""), str(row.get("task_id") or ""))
                for row in result_rows
                if isinstance(row, Mapping)
            }
            scheduled_action_counts = Counter(
                (
                    "regenerate"
                    if scheduled["action"] in {"regenerate", "model_regenerate"}
                    else scheduled["action"]
                )
                for scheduled in resume_scheduled_pairs
            )
            declared_action_counts = raw_resume_selection.get("resume_action_counts")
            expected_schedule_counts = {
                "scheduled_pair_count": len(resume_scheduled_pairs),
                "regenerate_pair_count": scheduled_action_counts["regenerate"],
                "judge_only_pair_count": scheduled_action_counts["judge_only"],
                "metadata_only_pair_count": scheduled_action_counts["metadata_only"],
                "audit_only_pair_count": scheduled_action_counts["audit_only"],
                "policy_violation_pair_count": 0,
            }

            expected_resume_actions = {
                "policy_violation",
                "regenerate",
                "judge_only",
                "metadata_only",
                "audit_only",
                "complete",
            }
            normalized_resume_counts: dict[str, int] = {}
            counter_contract_valid = isinstance(declared_action_counts, Mapping)
            if counter_contract_valid:
                declared_keys = set(declared_action_counts)
                counter_contract_valid = bool(
                    declared_keys == expected_resume_actions
                    or declared_keys == expected_resume_actions - {"policy_violation"}
                )
                for action in expected_resume_actions:
                    raw_count = declared_action_counts.get(action)
                    if action == "policy_violation" and action not in declared_action_counts:
                        # Narrow legacy compatibility for the producer's one
                        # omitted reserved-zero key.
                        raw_count = 0
                    if (
                        isinstance(raw_count, bool)
                        or not isinstance(raw_count, int)
                        or raw_count < 0
                    ):
                        counter_contract_valid = False
                    else:
                        normalized_resume_counts[action] = raw_count

            selected_pair_count = raw_resume_selection.get("selected_pair_count")
            best_pair_count = raw_resume_selection.get("best_pair_count")
            strict_valid_pair_count = raw_resume_selection.get("strict_valid_pair_count")
            if (
                isinstance(selected_pair_count, bool)
                or not isinstance(selected_pair_count, int)
                or selected_pair_count < len(resume_scheduled_pairs)
                or isinstance(best_pair_count, bool)
                or not isinstance(best_pair_count, int)
                or best_pair_count < 0
                or isinstance(strict_valid_pair_count, bool)
                or not isinstance(strict_valid_pair_count, int)
                or strict_valid_pair_count < 0
            ):
                counter_contract_valid = False
            elif counter_contract_valid:
                completed_pair_count = normalized_resume_counts["complete"]
                counter_contract_valid = bool(
                    selected_pair_count == len(resume_scheduled_pairs) + strict_valid_pair_count
                    and completed_pair_count == strict_valid_pair_count
                    and best_pair_count
                    == sum(
                        normalized_resume_counts[action]
                        for action in expected_resume_actions
                        if action != "policy_violation"
                    )
                    and best_pair_count <= selected_pair_count
                    and normalized_resume_counts["policy_violation"] == 0
                    and normalized_resume_counts["judge_only"]
                    == scheduled_action_counts["judge_only"]
                    and normalized_resume_counts["metadata_only"]
                    == scheduled_action_counts["metadata_only"]
                    and normalized_resume_counts["audit_only"]
                    == scheduled_action_counts["audit_only"]
                    and normalized_resume_counts["regenerate"]
                    <= scheduled_action_counts["regenerate"]
                )

            model_regenerate_count = raw_resume_selection.get("model_regenerate_pair_count")
            regenerate_count = scheduled_action_counts["regenerate"]
            explicit_model_regenerate_count = sum(
                scheduled["action"] == "model_regenerate" for scheduled in resume_scheduled_pairs
            )
            if (
                isinstance(model_regenerate_count, bool)
                or not isinstance(model_regenerate_count, int)
                or not explicit_model_regenerate_count <= model_regenerate_count <= regenerate_count
            ):
                counter_contract_valid = False
            else:
                exhausted = raw_resume_selection.get("generation_budget_exhausted_pair_count")
                blocked = raw_resume_selection.get("generation_auto_retry_blocked_pair_count")
                if exhausted is None and blocked is None:
                    # Legacy manifests did not expose the non-model subsets.
                    counter_contract_valid = bool(
                        counter_contract_valid and model_regenerate_count == regenerate_count
                    )
                elif (
                    isinstance(exhausted, bool)
                    or not isinstance(exhausted, int)
                    or exhausted < 0
                    or isinstance(blocked, bool)
                    or not isinstance(blocked, int)
                    or blocked < 0
                ):
                    counter_contract_valid = False
                else:
                    non_model_regenerate_count = regenerate_count - model_regenerate_count
                    counter_contract_valid = bool(
                        counter_contract_valid
                        and max(exhausted, blocked)
                        <= non_model_regenerate_count
                        <= exhausted + blocked
                    )
            if (
                seen_scheduled_pairs != result_pairs
                or any(
                    isinstance(raw_resume_selection.get(field_name), bool)
                    or raw_resume_selection.get(field_name) != expected
                    for field_name, expected in expected_schedule_counts.items()
                )
                or not counter_contract_valid
            ):
                raise FinalizationError(
                    f"manifest resume schedule counters differ from its result shard: {path}"
                )
            resume_schedule_contract_verified = True
        if recovered_resume_scheduled_pairs is not None:
            result_pairs = {
                (str(row.get("group") or ""), str(row.get("task_id") or ""))
                for row in result_rows
                if isinstance(row, Mapping)
            }
            recovered_pairs = {
                (item["group"], item["task_id"])
                for item in recovered_resume_scheduled_pairs
            }
            if resume_scheduled_pairs or recovered_pairs != result_pairs:
                raise FinalizationError(
                    f"manifest recovered schedule differs from its result shard: {path}"
                )
            resume_scheduled_pairs = recovered_resume_scheduled_pairs
            resume_schedule_contract_verified = bool(resume_scheduled_pairs)
        compatibility = payload.get("run_compatibility")
        if not isinstance(compatibility, dict):
            raise FinalizationError(f"manifest lacks run compatibility: {path}")
        raw_fingerprints = compatibility.get("fingerprints")
        raw_contracts = compatibility.get("contracts")
        if not isinstance(raw_fingerprints, dict) or not isinstance(raw_contracts, dict):
            raise FinalizationError(f"manifest compatibility contract is incomplete: {path}")
        if set(raw_fingerprints) != set(groups) or set(raw_contracts) != set(groups):
            raise FinalizationError(
                f"manifest compatibility scope differs from active groups: {path}"
            )
        fingerprints: dict[str, str] = {}
        contracts: dict[str, dict[str, Any]] = {}
        for group in groups:
            fingerprint = str(raw_fingerprints.get(group) or "")
            contract = raw_contracts.get(group)
            if not SHA256_VALUE.fullmatch(fingerprint) or not isinstance(contract, dict):
                raise FinalizationError(
                    f"manifest lacks the {group} compatibility contract: {path}"
                )
            if canonical_sha256(contract, prefix=True) != fingerprint:
                raise FinalizationError(
                    f"manifest {group} compatibility fingerprint differs: {path}"
                )
            fingerprints[group] = fingerprint
            contracts[group] = contract
            runtime = contract.get("resolved_llm_runtime")
            candidate_key = (
                normalize_key_fingerprint(
                    runtime.get("api_key_sha256"),
                    label=f"{path} {group} runtime key",
                )
                if isinstance(runtime, dict)
                else ""
            )
            if not candidate_key:
                raise FinalizationError(f"manifest lacks a runtime key binding: {path}")
            if key_fingerprint and candidate_key != key_fingerprint:
                raise FinalizationError("source manifests use different OpenRouter keys")
            key_fingerprint = candidate_key
        if authoritative_fingerprints is None:
            authoritative_fingerprints = fingerprints
            authoritative_contracts = contracts
        elif authoritative_fingerprints != fingerprints or authoritative_contracts != contracts:
            raise FinalizationError("source manifests use different run contracts")
        source_record = {
            "path": str(path),
            "sha256": file_sha256(path),
            "status": status,
            "started_at": payload.get("started_at"),
            "finished_at": payload.get("finished_at"),
            "result_path": str(result_path),
            "result_sha256": file_sha256(result_path),
            "rows_written": len(result_rows),
            "execution_scheduling": execution_scheduling,
            "live_web_preflight": {
                "status": preflight_status,
                "preflight_calls": {
                    tool_name: int(preflight_calls[tool_name])
                    for tool_name in ("web_search", "web_fetch")
                },
            },
            "resume_scheduled_pairs": resume_scheduled_pairs,
            "resume_schedule_contract_verified": (resume_schedule_contract_verified),
            "audit_warnings": manifest_audit_warnings,
        }
        if artifact_verification is not None:
            source_record["durable_artifacts"] = artifact_verification
        if isinstance(artifact_recovery, Mapping):
            source_record["artifact_recovery"] = dict(artifact_recovery)
        source_evidence.append(source_record)
    assert authoritative_fingerprints is not None
    assert authoritative_contracts is not None
    return (
        authoritative_fingerprints,
        authoritative_contracts,
        key_fingerprint,
        source_evidence,
    )


_THINKING_SETTINGS = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh", "max", "adaptive"}
)
_AGENT_FINALIZATION_POLICY_FIELDS = (
    "deadline_wrapup_margin_seconds",
    "deadline_wrapup_disable_tools",
    "deadline_thinking_off_margin_seconds",
    "max_iterations_includes_finalization",
    "retrieval_loop_finalization_threshold",
    "finalization_aggregator_only",
    "finalization_disable_thinking",
)
_AGGREGATOR_RECOVERY_FIELDS = tuple(FORMAL_AGGREGATOR_RECOVERY_POLICY)
_PROPOSER_RECOVERY_FREEZE_FIELDS = (
    "proposer_backup_count",
    "proposer_recovery_max_additional_calls",
    "proposer_max_tokens_cap",
    "proposer_visible_answer_reserve_tokens",
)


def _require_exact_mapping_keys(
    value: Any,
    expected: set[str],
    *,
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise FinalizationError(f"{label} fields differ from the experiment schema")
    return value


def _require_policy_int(
    value: Any,
    *,
    label: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        bounds = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise FinalizationError(f"{label} must be an integer {bounds}")
    return value


def _require_policy_decimal(
    value: Any,
    *,
    label: str,
    minimum: Decimal = Decimal(0),
    positive: bool = False,
) -> Decimal:
    parsed = required_decimal(value, label=label)
    if parsed < minimum or (positive and parsed <= 0):
        comparator = "> 0" if positive else f">= {minimum}"
        raise FinalizationError(f"{label} must be {comparator}")
    return parsed


def _derive_finalizer_experiment_policy(
    contracts: Mapping[str, Mapping[str, Any]],
    *,
    groups: Sequence[str],
) -> FinalizerExperimentPolicy:
    """Derive one tunable policy from authenticated, cross-group contracts."""

    profiles: list[tuple[str, Mapping[str, Any]]] = []
    freezes: list[tuple[str, Mapping[str, Any]]] = []
    for group in groups:
        contract = contracts[group]
        profile = contract.get("global_experiment_profile")
        freeze = contract.get("formal_runtime_freeze")
        if not isinstance(profile, Mapping):
            raise FinalizationError(f"{group} global_experiment_profile is missing")
        if not isinstance(freeze, Mapping):
            raise FinalizationError(f"{group} formal_runtime_freeze is missing")
        profiles.append((group, profile))
        freezes.append((group, freeze))
    baseline_profile = profiles[0][1]
    baseline_freeze = freezes[0][1]
    for group, profile in profiles[1:]:
        if canonical_sha256(profile) != canonical_sha256(baseline_profile):
            raise FinalizationError(
                f"{group} global_experiment_profile differs across active groups"
            )
    recovery_freeze_fields = {
        field_name: baseline_freeze.get(field_name)
        for field_name in (*_AGGREGATOR_RECOVERY_FIELDS, *_PROPOSER_RECOVERY_FREEZE_FIELDS)
    }
    for group, freeze in freezes[1:]:
        candidate = {
            field_name: freeze.get(field_name)
            for field_name in (*_AGGREGATOR_RECOVERY_FIELDS, *_PROPOSER_RECOVERY_FREEZE_FIELDS)
        }
        if candidate != recovery_freeze_fields:
            raise FinalizationError(f"{group} recovery policy differs across active groups")

    profile = copy.deepcopy(dict(baseline_profile))
    if profile.get("schema_version") != 1 or not str(profile.get("profile_id") or "").strip():
        raise FinalizationError("global_experiment_profile identity is invalid")
    benchmark_input = profile.get("benchmark_input")
    require_formal_fields(
        benchmark_input,
        {
            "sha256": FROZEN_DRACO_MINI_SHA256,
            "task_count": FROZEN_DRACO_MINI_TASK_COUNT,
            "enforce_reference_input": True,
        },
        label="global_experiment_profile.benchmark_input",
    )

    raw_timeouts = _require_exact_mapping_keys(
        profile.get("timeouts"),
        {"task_seconds", "proposer_seconds", "aggregator_seconds", "task_margin_seconds"},
        label="global_experiment_profile.timeouts",
    )
    timeouts = {
        field_name: _require_policy_decimal(
            raw_timeouts.get(field_name),
            label=f"global_experiment_profile.timeouts.{field_name}",
            positive=field_name != "task_margin_seconds",
        )
        for field_name in raw_timeouts
    }
    if (
        timeouts["proposer_seconds"]
        + timeouts["aggregator_seconds"]
        + timeouts["task_margin_seconds"]
        > timeouts["task_seconds"]
    ):
        raise FinalizationError("global experiment timeout budgets exceed task_seconds")

    raw_runner = _require_exact_mapping_keys(
        profile.get("runner"),
        {"mode", "agent_max_iterations", *_AGENT_FINALIZATION_POLICY_FIELDS},
        label="global_experiment_profile.runner",
    )
    runner_mode = str(raw_runner.get("mode") or "")
    if runner_mode not in {"agent_loop", "provider"}:
        raise FinalizationError("global_experiment_profile.runner.mode is invalid")
    runner: dict[str, Any] = {
        "mode": runner_mode,
        "agent_max_iterations": _require_policy_int(
            raw_runner.get("agent_max_iterations"),
            label="global_experiment_profile.runner.agent_max_iterations",
        ),
    }
    boolean_runner_fields = {
        "deadline_wrapup_disable_tools",
        "max_iterations_includes_finalization",
        "finalization_aggregator_only",
        "finalization_disable_thinking",
    }
    for field_name in _AGENT_FINALIZATION_POLICY_FIELDS:
        value = raw_runner.get(field_name)
        if field_name in boolean_runner_fields:
            if type(value) is not bool:
                raise FinalizationError(
                    f"global_experiment_profile.runner.{field_name} must be boolean"
                )
            runner[field_name] = value
        else:
            runner[field_name] = _require_policy_int(
                value,
                label=f"global_experiment_profile.runner.{field_name}",
            )

    raw_generation = _require_exact_mapping_keys(
        profile.get("generation"),
        {
            "thinking_enabled",
            "thinking_budget_tokens",
            "default_thinking_level",
            "model_thinking_levels",
            "require_highest_thinking",
            "temperature",
            "max_tokens",
            "max_attempts",
            "retry_backoff_seconds",
        },
        label="global_experiment_profile.generation",
    )
    if type(raw_generation.get("thinking_enabled")) is not bool:
        raise FinalizationError("generation.thinking_enabled must be boolean")
    if type(raw_generation.get("require_highest_thinking")) is not bool:
        raise FinalizationError("generation.require_highest_thinking must be boolean")
    default_thinking = str(raw_generation.get("default_thinking_level") or "")
    thinking_levels = raw_generation.get("model_thinking_levels")
    if (
        default_thinking not in _THINKING_SETTINGS
        or not isinstance(thinking_levels, Mapping)
        or any(
            not isinstance(model, str) or not model or level not in _THINKING_SETTINGS
            for model, level in thinking_levels.items()
        )
    ):
        raise FinalizationError("global generation thinking policy is invalid")
    raw_temperature = raw_generation.get("temperature")
    temperature = (
        None
        if raw_temperature is None
        else required_decimal(raw_temperature, label="global generation temperature")
    )
    generation = {
        "thinking_enabled": raw_generation["thinking_enabled"],
        "thinking_budget_tokens": _require_policy_int(
            raw_generation.get("thinking_budget_tokens"),
            label="global generation thinking_budget_tokens",
            minimum=1,
        ),
        "default_thinking_level": default_thinking,
        "model_thinking_levels": copy.deepcopy(dict(thinking_levels)),
        "require_highest_thinking": raw_generation["require_highest_thinking"],
        "temperature": temperature,
        "max_tokens": _require_policy_int(
            raw_generation.get("max_tokens"),
            label="global generation max_tokens",
            minimum=1,
        ),
        "max_attempts": _require_policy_int(
            raw_generation.get("max_attempts"),
            label="global generation max_attempts",
            minimum=1,
            maximum=3,
        ),
        "retry_backoff_seconds": _require_policy_decimal(
            raw_generation.get("retry_backoff_seconds"),
            label="global generation retry_backoff_seconds",
        ),
    }

    raw_judge = _require_exact_mapping_keys(
        profile.get("judge"),
        {"model", "repeats", "max_attempts", "judge_candidates"},
        label="global_experiment_profile.judge",
    )
    judge_model = str(raw_judge.get("model") or "")
    if (
        not judge_model
        or judge_model != JUDGE_MODEL
        or judge_model != judge_model.strip().lower()
        or "/" not in judge_model
        or any(character.isspace() for character in judge_model)
        or type(raw_judge.get("judge_candidates")) is not bool
    ):
        raise FinalizationError("global Judge policy is invalid")
    judge = {
        "model": judge_model,
        "repeats": _require_policy_int(
            raw_judge.get("repeats"), label="global Judge repeats", minimum=1
        ),
        "max_attempts": _require_policy_int(
            raw_judge.get("max_attempts"),
            label="global Judge max_attempts",
            minimum=1,
            maximum=3,
        ),
        "judge_candidates": raw_judge["judge_candidates"],
    }

    raw_tools = _require_exact_mapping_keys(
        profile.get("tools"),
        {
            "mode",
            "sandbox_enabled",
            "contamination_blocked_domains",
            "web_search",
            "web_fetch",
        },
        label="global_experiment_profile.tools",
    )
    if raw_tools.get("mode") != "local_web_tools":
        raise FinalizationError("formal campaign requires local_web_tools")
    if raw_tools.get("sandbox_enabled") is not False:
        raise FinalizationError("formal campaign requires sandbox_enabled=false")
    if raw_tools.get("contamination_blocked_domains") != list(FORMAL_BLOCKED_DOMAINS):
        raise FinalizationError("formal contamination blocked domains differ")
    raw_search = _require_exact_mapping_keys(
        raw_tools.get("web_search"),
        {"provider", "api_key_env", "max_results"},
        label="global_experiment_profile.tools.web_search",
    )
    raw_fetch = _require_exact_mapping_keys(
        raw_tools.get("web_fetch"),
        {"max_content_tokens"},
        label="global_experiment_profile.tools.web_fetch",
    )
    search_provider = str(raw_search.get("provider") or "")
    search_api_key_env = str(raw_search.get("api_key_env") or "")
    if search_provider not in {"brave", "duckduckgo"}:
        raise FinalizationError("global web_search provider is invalid")
    if (
        search_provider == "brave"
        and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*",
            search_api_key_env,
        )
        is None
    ):
        raise FinalizationError("global web_search api_key_env is invalid")
    if search_provider == "duckduckgo" and search_api_key_env:
        raise FinalizationError("global DuckDuckGo web_search api_key_env must be empty")
    tools = {
        "mode": "local_web_tools",
        "sandbox_enabled": False,
        "contamination_blocked_domains": list(FORMAL_BLOCKED_DOMAINS),
        "web_search": {
            "provider": search_provider,
            "api_key_env": search_api_key_env,
            "max_results": _require_policy_int(
                raw_search.get("max_results"),
                label="global web_search max_results",
                minimum=1,
            ),
        },
        "web_fetch": {
            "max_content_tokens": _require_policy_int(
                raw_fetch.get("max_content_tokens"),
                label="global web_fetch max_content_tokens",
                minimum=1,
            )
        },
    }

    aggregator_recovery = {
        field_name: baseline_freeze.get(field_name) for field_name in _AGGREGATOR_RECOVERY_FIELDS
    }
    if aggregator_recovery["aggregator_recovery_mode"] not in {
        "off",
        "serving",
        "experiment",
    }:
        raise FinalizationError("aggregator recovery mode is invalid")
    _require_policy_int(
        aggregator_recovery["aggregator_recovery_top_k"],
        label="aggregator recovery top_k",
        minimum=1,
        maximum=3,
    )
    aggregator_cap = _require_policy_int(
        aggregator_recovery["aggregator_max_tokens_cap"],
        label="aggregator recovery max_tokens_cap",
        minimum=2,
    )
    aggregator_reserve = _require_policy_int(
        aggregator_recovery["aggregator_visible_answer_reserve_tokens"],
        label="aggregator recovery visible_answer_reserve_tokens",
        minimum=1,
    )
    if aggregator_reserve >= aggregator_cap:
        raise FinalizationError("aggregator recovery reserve must be below max_tokens_cap")

    backup_count = _require_policy_int(
        baseline_freeze.get("proposer_backup_count"),
        label="proposer recovery backup_count",
        maximum=2,
    )
    proposer_cap = _require_policy_int(
        baseline_freeze.get("proposer_max_tokens_cap"),
        label="proposer recovery max_tokens_cap",
        minimum=2,
    )
    proposer_reserve = _require_policy_int(
        baseline_freeze.get("proposer_visible_answer_reserve_tokens"),
        label="proposer recovery visible_answer_reserve_tokens",
        minimum=1,
    )
    if proposer_reserve >= proposer_cap:
        raise FinalizationError("proposer recovery reserve must be below max_tokens_cap")
    proposer_recovery = {
        "schema": FORMAL_PROPOSER_RECOVERY_SCHEMA,
        "configured_backup_count": backup_count,
        "effective_backup_count": backup_count,
        "max_additional_physical_requests": _require_policy_int(
            baseline_freeze.get("proposer_recovery_max_additional_calls"),
            label="proposer recovery max_additional_calls",
            maximum=3,
        ),
        "quorum_required": 2,
        "max_tokens_cap": proposer_cap,
        "visible_answer_reserve_tokens": proposer_reserve,
        "thinking_downgrade_order": ["one_strictly_lower"],
        "transient_same_model_retries": 1,
        "backup_reasoning_downgrades": 1,
    }

    judge_pins: set[str] = set()
    for group in groups:
        runtime = contracts[group].get("resolved_llm_runtime")
        pins = runtime.get("provider_routing") if isinstance(runtime, Mapping) else None
        judge_pin = (
            str(pins.get(judge_model) or "").strip().casefold() if isinstance(pins, Mapping) else ""
        )
        if not judge_pin or judge_pin == "auto":
            raise FinalizationError(f"{group} lacks a strict Judge upstream provider pin")
        judge_pins.add(judge_pin)
    if len(judge_pins) != 1:
        raise FinalizationError("Judge upstream provider pin differs across active groups")

    return FinalizerExperimentPolicy(
        profile=profile,
        timeouts=timeouts,
        runner=runner,
        generation=generation,
        judge=judge,
        tools=tools,
        aggregator_recovery=aggregator_recovery,
        proposer_recovery=proposer_recovery,
        judge_provider_pin=next(iter(judge_pins)),
    )


def contract_recovery_policies(
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project authenticated runtime-freeze recovery fields for row audits."""

    freeze = contract.get("formal_runtime_freeze")
    if not isinstance(freeze, Mapping):
        return dict(FORMAL_AGGREGATOR_RECOVERY_POLICY), dict(FORMAL_PROPOSER_RECOVERY_POLICY)
    aggregator = {field_name: freeze.get(field_name) for field_name in _AGGREGATOR_RECOVERY_FIELDS}
    if any(value is None for value in aggregator.values()):
        aggregator = dict(FORMAL_AGGREGATOR_RECOVERY_POLICY)
    proposer_fields_present = all(
        field_name in freeze for field_name in _PROPOSER_RECOVERY_FREEZE_FIELDS
    )
    if not proposer_fields_present:
        return aggregator, dict(FORMAL_PROPOSER_RECOVERY_POLICY)
    backup_count = freeze["proposer_backup_count"]
    proposer = {
        "schema": FORMAL_PROPOSER_RECOVERY_SCHEMA,
        "configured_backup_count": backup_count,
        "effective_backup_count": backup_count,
        "max_additional_physical_requests": freeze["proposer_recovery_max_additional_calls"],
        "quorum_required": 2,
        "max_tokens_cap": freeze["proposer_max_tokens_cap"],
        "visible_answer_reserve_tokens": freeze["proposer_visible_answer_reserve_tokens"],
        "thinking_downgrade_order": ["one_strictly_lower"],
        "transient_same_model_retries": 1,
        "backup_reasoning_downgrades": 1,
    }
    return aggregator, proposer


def validate_formal_campaign_contracts(
    contracts: Mapping[str, Mapping[str, Any]],
    *,
    groups: Sequence[str] = GROUPS,
) -> FinalizerExperimentPolicy:
    """Authenticate tunable semantics and pin non-negotiable safety contracts."""

    active_groups = tuple(groups)
    if (
        not active_groups
        or active_groups != tuple(group for group in SUPPORTED_GROUPS if group in active_groups)
        or set(contracts) != set(active_groups)
    ):
        raise FinalizationError(
            "formal compatibility contracts must cover exactly the active groups "
            f"{','.join(active_groups)}"
        )
    policy = _derive_finalizer_experiment_policy(
        contracts,
        groups=active_groups,
    )
    expected_timeouts = {
        field_name: policy.timeouts[field_name]
        for field_name in ("task_seconds", "proposer_seconds", "aggregator_seconds")
    }
    expected_judge = dict(policy.judge)
    runner_finalization_policy = {
        field_name: policy.runner[field_name] for field_name in _AGENT_FINALIZATION_POLICY_FIELDS
    }
    expected_operational_generation_policy = {
        "generation_thinking": "model_max",
        "temperature": policy.generation["temperature"],
        "thinking_enabled": policy.generation["thinking_enabled"],
        "thinking_level": "model-specific",
        "default_thinking_level": policy.generation["default_thinking_level"],
        "thinking_budget_tokens": policy.generation["thinking_budget_tokens"],
        "max_thinking_budget_tokens": policy.generation["thinking_budget_tokens"],
        "max_tokens": policy.generation["max_tokens"],
        "max_tokens_overridden": True,
        "model_thinking_levels": policy.generation["model_thinking_levels"],
        "require_highest_thinking": policy.generation["require_highest_thinking"],
        "applies_to": "single baselines and ensemble members",
    }
    expected_operational_tools = {
        "tool_mode": policy.tools["mode"],
        "tools_enabled": True,
        "tool_names": ["web_search", "web_fetch"],
        "local_web_tools": {
            "web_search": {
                "excluded_domains": FORMAL_BLOCKED_DOMAINS,
                **policy.tools["web_search"],
            },
            "web_fetch": {
                "blocked_domains": FORMAL_BLOCKED_DOMAINS,
                "max_content_tokens": policy.tools["web_fetch"]["max_content_tokens"],
                "max_content_chars": max(
                    100,
                    int(policy.tools["web_fetch"]["max_content_tokens"]) * 4,
                ),
                "allow_firecrawl": False,
            },
        },
        "contamination_blocked_domains": FORMAL_BLOCKED_DOMAINS,
        "contamination_controls": {
            "status": "enforced_by_local_web_tools",
            "web_search_field": "excluded_domains_query_and_result_filter",
            "web_fetch_field": "blocked_domains",
        },
    }
    for group in active_groups:
        contract = contracts[group]
        require_formal_fields(
            contract,
            {
                "schema": "opensquilla.draco.run-compatibility/v1",
                "benchmark": "DRACO",
                "group": group,
                "runner": {
                    "mode": policy.runner["mode"],
                    "agent_max_iterations": policy.runner["agent_max_iterations"],
                    "finalization_policy": runner_finalization_policy,
                },
                "tools": expected_operational_tools,
                "generation": {
                    "policy": expected_operational_generation_policy,
                    "max_attempts": policy.generation["max_attempts"],
                    "retry_backoff_seconds": policy.generation["retry_backoff_seconds"],
                },
                "judge": expected_judge,
                "timeouts": {
                    **expected_timeouts,
                    "proposer_early_stop_success_count": 0,
                    "proposer_early_stop_after_seconds": Decimal("0"),
                    "expand_to_task_timeout": False,
                },
                "resolved_llm_runtime": {
                    "provider": "openrouter",
                    "base_url": "https://openrouter.ai/api/v1",
                    "base_url_from_env": False,
                    "proxy": "",
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
                    **policy.profile,
                },
                "formal_runtime_freeze": {
                    "source": "experiment_config",
                    "sandbox_enabled": False,
                    "sandbox_security_grading_enabled": False,
                    **policy.aggregator_recovery,
                    "proposer_backup_count": policy.proposer_recovery["configured_backup_count"],
                    "proposer_recovery_max_additional_calls": policy.proposer_recovery[
                        "max_additional_physical_requests"
                    ],
                    "proposer_max_tokens_cap": policy.proposer_recovery["max_tokens_cap"],
                    "proposer_visible_answer_reserve_tokens": policy.proposer_recovery[
                        "visible_answer_reserve_tokens"
                    ],
                    "g1_user_profile_generation_enabled": False,
                    "g1_user_profile_enabled": False,
                },
                "dry_run": False,
            },
            label=f"{group} formal execution contract",
        )

    for group, expected_model in (("B0", B0_MODEL), ("B4", B4_MODEL)):
        if group not in active_groups:
            continue
        contract = contracts.get(group)
        spec = contract.get("group_spec") if isinstance(contract, Mapping) else None
        if (
            not isinstance(spec, Mapping)
            or spec.get("kind") != "single"
            or spec.get("model") != expected_model
        ):
            raise FinalizationError(f"{group} formal contract must use openrouter/{expected_model}")
    if "B1" in active_groups:
        b1 = contracts.get("B1")
        b1_spec = b1.get("group_spec") if isinstance(b1, Mapping) else None
        gateway = b1.get("gateway_execution") if isinstance(b1, Mapping) else None
        router = gateway.get("squilla_router") if isinstance(gateway, Mapping) else None
        tiers = router.get("tiers") if isinstance(router, Mapping) else None
        if not isinstance(b1_spec, Mapping) or b1_spec.get("kind") != "router_single":
            raise FinalizationError("B1 formal contract must be router_single")
        if not isinstance(tiers, Mapping) or set(tiers) != set(B1_TIER_MODELS):
            raise FinalizationError("B1 formal tier set differs from c0/c1/c2/c3/image_model")
        for tier, expected_model in B1_TIER_MODELS.items():
            value = tiers.get(tier)
            if (
                not isinstance(value, Mapping)
                or str(value.get("provider") or "").casefold() != "openrouter"
                or value.get("model") != expected_model
            ):
                raise FinalizationError(f"B1 {tier} must use openrouter/{expected_model}")
    if "B2" in active_groups:
        b2 = contracts.get("B2")
        b2_spec = b2.get("group_spec") if isinstance(b2, Mapping) else None
        if (
            not isinstance(b2_spec, Mapping)
            or b2_spec.get("kind") != "selection_mode"
            or b2_spec.get("selection_mode") != "static_openrouter_b5"
        ):
            raise FinalizationError("B2 formal contract must use static_openrouter_b5")
    routes: Mapping[str, Any] = {}
    g1_analyzer_policy: Mapping[str, Any] = {}
    if "G1" in active_groups:
        g1 = contracts.get("G1")
        g1_spec = g1.get("group_spec") if isinstance(g1, Mapping) else None
        registry = g1.get("g1_registry_contract") if isinstance(g1, Mapping) else None
        routes = registry.get("expected_routes") if isinstance(registry, Mapping) else None
        candidate_scope = (
            str(registry.get("candidate_scope") or "exact_routes")
            if isinstance(registry, Mapping)
            else ""
        )
        candidate_policy = (
            str(registry.get("policy") or "exact_openrouter_routes")
            if isinstance(registry, Mapping)
            else ""
        )
        expected_candidate_policy = (
            "all_registry_models"
            if candidate_scope == "registry_all"
            else "exact_openrouter_routes"
        )
        ranking_config_identity = (
            g1_ranking_config_identity(registry) if isinstance(registry, Mapping) else None
        )
        ranking_proposer_max = (
            g1_ranking_proposer_max(registry) if isinstance(registry, Mapping) else None
        )
        g1_analyzer_policy = (
            g1_task_analyzer_execution_policy(registry) if isinstance(registry, Mapping) else None
        ) or {}
        g1_analyzer_chain = (
            g1_task_analyzer_execution_chain(
                registry,
                g1.get("task_analyzer_execution_contract")
                if isinstance(g1, Mapping)
                and isinstance(
                    g1.get("task_analyzer_execution_contract"),
                    Mapping,
                )
                else None,
            )
            if isinstance(registry, Mapping)
            else None
        )
        g1_analyzer_fallback = (
            g1_analyzer_failure_fallback_policy(registry) if isinstance(registry, Mapping) else None
        )
        registry_source_identity = (
            g1_registry_source_identity(registry) if isinstance(registry, Mapping) else None
        )
        registry_all_routes_valid = candidate_scope != "registry_all" or (
            authenticated_registry_all_routes(registry) is not None
        )
        if (
            not isinstance(g1_spec, Mapping)
            or g1_spec.get("kind") != "selection_mode"
            or g1_spec.get("selection_mode") != "router_dynamic"
            or candidate_scope not in {"registry_all", "exact_routes"}
            or candidate_policy != expected_candidate_policy
            or not isinstance(routes, Mapping)
            or not routes
            or (
                candidate_scope == "exact_routes"
                and any(str(provider).strip().casefold() == "auto" for provider in routes.values())
            )
            or not registry_all_routes_valid
            or nonnegative_int(registry.get("expected_candidate_count")) != len(routes)
            or not str(registry.get("profile_id") or "").strip()
            or not str(registry.get("source_registry_snapshot_version") or "").strip()
            or not HEX64.fullmatch(str(registry.get("expected_routes_sha256") or ""))
            or canonical_sha256(routes) != str(registry.get("expected_routes_sha256") or "")
            or registry_source_identity is None
            or (
                str(registry.get("source_registry_snapshot_version") or ""),
                str(registry.get("expected_source_registry_snapshot_sha256") or ""),
            )
            != registry_source_identity
            or ranking_config_identity is None
            or ranking_proposer_max is None
            or not g1_analyzer_policy
            or g1_analyzer_chain is None
            or str(g1_analyzer_policy.get("provider") or "") != "openrouter"
            or str(g1_analyzer_policy.get("upstream_provider") or "").strip().casefold()
            in {"", "auto"}
            or registry.get("user_profile_enabled") is not False
        ):
            raise FinalizationError(
                "G1 formal contract must use router_dynamic with a resolved frozen candidate pool"
            )
        replay_contract = registry.get("task_analysis_execution")
        if replay_contract is not None:
            from opensquilla.provider.ranking_router import (
                frozen_task_analysis_contract_reasons,
            )

            benchmark = policy.profile.get("benchmark_input")
            task_ids = benchmark.get("task_ids") if isinstance(benchmark, Mapping) else None
            replay_reasons = frozen_task_analysis_contract_reasons(
                replay_contract,
                expected_task_ids=(
                    [str(task_id) for task_id in task_ids] if isinstance(task_ids, list) else []
                ),
            )
            if replay_reasons:
                raise FinalizationError(
                    "G1 frozen task analysis contract is invalid: " + ",".join(replay_reasons)
                )
    for group, contract in contracts.items():
        gateway = contract.get("gateway_execution")
        ensemble = gateway.get("llm_ensemble") if isinstance(gateway, Mapping) else None
        if not isinstance(ensemble, Mapping):
            raise FinalizationError(f"{group} formal contract lacks gateway llm_ensemble")
        require_formal_fields(
            ensemble,
            policy.aggregator_recovery,
            label=f"{group} gateway llm_ensemble",
        )
        runtime = contract.get("resolved_llm_runtime")
        pins = runtime.get("provider_routing") if isinstance(runtime, Mapping) else None
        if not isinstance(pins, Mapping):
            raise FinalizationError(f"{group} formal contract lacks provider_routing pins")
        g1_explicit_pins: dict[str, str] = {}
        if group == "G1":
            explicit_routes = list(g1_analyzer_chain or [])
            if g1_analyzer_fallback is not None:
                explicit_routes.extend(g1_analyzer_fallback["proposers"])
                explicit_routes.append(g1_analyzer_fallback["aggregator"])
            for route in explicit_routes:
                model = str(route["model"])
                upstream = str(route["upstream_provider"]).strip().casefold()
                prior = g1_explicit_pins.get(model)
                if prior is not None and prior != upstream:
                    raise FinalizationError(
                        f"G1 contains conflicting upstream provider pins for {model}"
                    )
                g1_explicit_pins[model] = upstream
        required_models = (
            {B0_MODEL}
            if group == "B0"
            else {B4_MODEL}
            if group == "B4"
            else set(B1_TIER_MODELS.values())
            if group == "B1"
            else {*B2_PROPOSERS, B2_AGGREGATOR, TASK_ANALYZER_MODEL}
            if group == "B2"
            else {*routes, *g1_explicit_pins}
        )
        for model in required_models:
            route_pin = (
                str(routes[model]).strip().casefold() if group == "G1" and model in routes else ""
            )
            if group == "G1" and route_pin == "auto" and model not in g1_explicit_pins:
                # ``registry_all`` deliberately leaves candidate upstream
                # selection to OpenRouter.  The fixed task analyzer remains
                # independently pinned below.
                continue
            expected_pin = (
                g1_explicit_pins[model]
                if group == "G1" and model in g1_explicit_pins
                else route_pin or FORMAL_UPSTREAM_PINS.get(model)
            )
            if not expected_pin or str(pins.get(model) or "").strip().casefold() != expected_pin:
                raise FinalizationError(f"{group} upstream provider pin differs for {model}")
        if group == "G1" and (
            str(runtime.get("provider") or "").strip().casefold()
            != str(g1_analyzer_policy["provider"])
            or str(pins.get(str(g1_analyzer_policy["model"])) or "").strip().casefold()
            != str(g1_analyzer_policy["upstream_provider"]).strip().casefold()
        ):
            raise FinalizationError("G1 task analyzer upstream provider pin differs")
    return policy


def usage_generation_contract(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    contract = {key: value.get(key) for key in USAGE_CONTRACT_KEYS if key in value}
    breakdown = value.get("model_usage_breakdown")
    if isinstance(breakdown, list):
        contract["model_usage_breakdown"] = [
            usage_generation_contract(item) for item in breakdown if isinstance(item, Mapping)
        ]
    return contract


def usage_generation_identity_contract(value: Any) -> Any:
    """Project only immutable generation data used to link repair waves.

    Requested/actual provider identity and cost receipt fields are deliberately
    excluded: a metadata-only repair is allowed to backfill those fields
    without becoming a new generation.  Token counts and breakdown shape stay
    bound to the accepted generation.
    """

    if not isinstance(value, Mapping):
        return None
    token_keys = USAGE_CONTRACT_KEYS[4:]
    contract = {key: value.get(key) for key in token_keys if key in value}
    if "physical_attempt_id" in value:
        contract["physical_attempt_id"] = value.get("physical_attempt_id")
    breakdown = value.get("model_usage_breakdown")
    if isinstance(breakdown, list):
        contract["model_usage_breakdown"] = [
            usage_generation_identity_contract(item)
            for item in breakdown
            if isinstance(item, Mapping)
        ]
    return contract


def generation_identity(row: Mapping[str, Any]) -> str:
    # Formal repair rows may either carry no attempt items (the current
    # contract) or repeat immutable prior items (legacy-compatible input).
    # Therefore attempt-list shape cannot be part of the cross-wave identity.
    # Physical attempts are independently authenticated and budgeted by
    # validate_generation_attempt_evidence().
    return canonical_sha256(
        {
            "group": row.get("group"),
            "task_id": row.get("task_id"),
            "prompt_sha256": row.get("prompt_sha256"),
            "started_at": row.get("started_at"),
            "generation_completed_at": row.get("generation_completed_at"),
            "final_text_sha256": row.get("final_text_sha256"),
            "llm_request_count": row.get("llm_request_count"),
            "usage": usage_generation_identity_contract(row.get("usage")),
        }
    )


def blocked_regenerate_terminal_evidence(execution: Mapping[str, Any]) -> bool:
    """Bind a no-model blocked regenerate row to producer terminal evidence."""

    if execution.get("generation_model_started") is not False:
        return False
    raw_attempts = execution.get("generation_attempts")
    if not isinstance(raw_attempts, list):
        return False
    generation_attempt_ids = {
        attempt_id
        for attempt in raw_attempts
        if isinstance(attempt, Mapping)
        and HEX32.fullmatch(attempt_id := str(attempt.get("attempt_id") or "")) is not None
    }
    if not generation_attempt_ids:
        return False

    observed_terminal = False
    for field_name, expected_schema in (
        (
            "generation_postprocessing_terminal",
            GENERATION_POSTPROCESSING_TERMINAL_SCHEMA,
        ),
        (
            "provider_native_proposer_recovery_terminal",
            PROVIDER_NATIVE_PROPOSER_RECOVERY_TERMINAL_SCHEMA,
        ),
    ):
        terminal = execution.get(field_name)
        if terminal is None:
            continue
        observed_terminal = True
        if not isinstance(terminal, Mapping):
            return False
        terminal_attempt_id = str(terminal.get("attempt_id") or "")
        if (
            terminal.get("schema") != expected_schema
            or terminal.get("automatic_generation_retry_allowed") is not False
            or HEX32.fullmatch(terminal_attempt_id) is None
            or terminal_attempt_id not in generation_attempt_ids
        ):
            return False
    return observed_terminal


def repair_evidence(row: Mapping[str, Any], execution: Mapping[str, Any]) -> bool:
    """Return whether a no-new-generation row is explicitly a repair."""

    if execution.get("generation_reused") is not True:
        return False
    action = str(execution.get("resume_action") or "")
    if action not in {"regenerate", "judge_only", "metadata_only", "audit_only"}:
        return False
    completion = row.get("resume_completion")
    if isinstance(completion, Mapping):
        if completion.get("generation_reused") is not True:
            return False
        completion_action = str(completion.get("action") or "")
        if completion_action and completion_action != action:
            return False
    if action == "regenerate":
        incomplete_reasons = (
            completion.get("incomplete_reasons") if isinstance(completion, Mapping) else None
        )
        if (
            execution.get("generation_auto_retry_blocked") is not True
            or execution.get("generation_model_started") is not False
            or not blocked_regenerate_terminal_evidence(execution)
            or not isinstance(completion, Mapping)
            or completion.get("status") != "incomplete"
            or completion.get("post_repair_action") != "regenerate"
            or completion.get("judge_reran") is not False
            or completion.get("metadata_repaired") is not False
            or not isinstance(incomplete_reasons, list)
            or "generation_auto_retry_blocked" not in incomplete_reasons
        ):
            return False
    if action == "audit_only":
        summary = execution.get("audit_only_summary")
        if (
            execution.get("audit_only_recorded") is not True
            or execution.get("judge_reran") is not False
            or not isinstance(summary, Mapping)
            or summary.get("status") != "recorded"
            or summary.get("generation_called") is not False
            or summary.get("judge_called") is not False
        ):
            return False
    # The action itself records the reason a repair wave was emitted.  The
    # booleans describe its outcome and may legitimately both be false when a
    # repair was attempted but could not fill the missing metadata.
    return True


def immutable_attempt_payload(attempt: Mapping[str, Any]) -> dict[str, Any]:
    """Project attempt fields a later receipt repair may not change.

    Legacy non-adaptive attempts retain their original v1 projection.  Once an
    attempt carries G1 adaptive-routing evidence, its complete decision and
    retry provenance are immutable across waves.
    """

    run = attempt.get("run")
    attempt_id = str(attempt.get("attempt_id") or "")
    canonical_run = (
        _canonicalized_run(run, identity_seed=f"generation-attempt:{attempt_id}")
        if isinstance(run, Mapping)
        else None
    )
    immutable_run = (
        {
            "error": canonical_run.get("error"),
            "final_text_sha256": canonical_run.get("final_text_sha256"),
            "llm_request_count": canonical_run.get("llm_request_count"),
            "usage": usage_generation_identity_contract(canonical_run.get("usage")),
        }
        if canonical_run is not None
        else None
    )
    payload = {
        "attempt_id": attempt.get("attempt_id"),
        "attempt_kind": attempt.get("attempt_kind"),
        "attempt": attempt.get("attempt"),
        "started_at": attempt.get("started_at"),
        "completed_at": attempt.get("completed_at"),
        "retryable": attempt.get("retryable"),
        "retry_reason": attempt.get("retry_reason"),
        "retry_suppressed_reason": attempt.get("retry_suppressed_reason"),
        "will_retry": attempt.get("will_retry"),
        "retry_backoff_s": attempt.get("retry_backoff_s"),
        "run": immutable_run,
    }
    selection_plan = attempt.get("selection_plan")
    adaptive_fields = (
        "selection_plan",
        "excluded_proposer_identities",
        "deterministic_proposer_failures",
        "retry_selection_plan",
        "retry_excluded_proposer_identities",
    )
    adaptive = any(field in attempt for field in adaptive_fields)
    if adaptive:
        routing = run.get("routing_trace") if isinstance(run, Mapping) else None
        routing_plan = routing.get("selection_plan") if isinstance(routing, Mapping) else None
        ensemble_trace = run.get("ensemble_trace") if isinstance(run, Mapping) else None
        ensemble_plans: list[Any] = []
        if isinstance(ensemble_trace, Mapping):
            calls, _ = ensemble_call_trace_sequence(ensemble_trace)
            ensemble_plans = [copy.deepcopy(call.get("selection_plan")) for call in calls]
        payload.update(
            {
                "selection_plan": copy.deepcopy(selection_plan),
                "excluded_proposer_identities": copy.deepcopy(
                    attempt.get("excluded_proposer_identities")
                ),
                "deterministic_proposer_failures": copy.deepcopy(
                    attempt.get("deterministic_proposer_failures")
                ),
                "retry_selection_plan": copy.deepcopy(attempt.get("retry_selection_plan")),
                "retry_excluded_proposer_identities": copy.deepcopy(
                    attempt.get("retry_excluded_proposer_identities")
                ),
                "run_routing_selection_plan": copy.deepcopy(routing_plan),
                "run_ensemble_selection_plans": ensemble_plans,
                # Setup usage and the aggregate usage breakdown are two
                # mirrors of the same paid analyzer calls.  Bind both source
                # shapes and their reconciled physical identities so a later
                # wave cannot hide retries by changing only setup_usage.
                "run_task_analyzer_setup_usage_evidence": (
                    _task_analyzer_setup_usage_contract(run) if isinstance(run, Mapping) else None
                ),
            }
        )
    return payload


def validate_generation_attempt_evidence(
    records: Sequence[SourceRecord],
    *,
    max_attempts: int,
) -> dict[str, Any]:
    """Validate immutable, cumulative attempt declarations across waves."""

    seen_payloads: dict[str, str] = {}
    attempt_owner: dict[str, tuple[str, str]] = {}
    state: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "seen_ids": set(),
            "attempt_ordinals": {},
            "budget_used": 0,
            "row_count": 0,
            "repair_row_count": 0,
        }
    )
    for record in sorted(records, key=lambda item: (item.source_index, item.line)):
        row = record.row
        if row.get("generation_attempt_evidence_schema") != (GENERATION_ATTEMPT_EVIDENCE_SCHEMA):
            raise FinalizationError(
                "formal campaign contains legacy or missing generation attempt "
                f"evidence at {record.path}:{record.line}"
            )
        execution = row.get("execution")
        attempts = execution.get("generation_attempts") if isinstance(execution, Mapping) else None
        if not isinstance(attempts, list):
            raise FinalizationError(
                f"formal result lacks a generation attempt list: {record.path}:{record.line}"
            )
        location = f"{record.path}:{record.line}"
        declared_count = _require_policy_int(
            row.get("generation_attempt_count"),
            label=f"generation_attempt_count at {location}",
        )
        if declared_count != len(attempts):
            raise FinalizationError(
                "generation_attempt_count differs from v1 attempt evidence at "
                f"{record.path}:{record.line}"
            )
        actual_spend_metrics = row.get("actual_spend_metrics")
        if not isinstance(actual_spend_metrics, Mapping):
            raise FinalizationError(
                f"actual-spend generation attempt metrics are missing at {location}"
            )
        actual_spend_attempt_count = _require_policy_int(
            actual_spend_metrics.get("generation_attempt_count"),
            label=f"actual-spend generation_attempt_count at {location}",
        )
        if actual_spend_attempt_count != len(attempts):
            raise FinalizationError(
                "actual-spend generation attempt count differs from v1 evidence "
                f"at {record.path}:{record.line}"
            )
        budget_limit = _require_policy_int(
            row.get("generation_attempt_budget_limit"),
            label=f"generation_attempt_budget_limit at {location}",
            minimum=1,
        )
        if budget_limit != max_attempts:
            raise FinalizationError(
                f"generation attempt budget limit differs at {record.path}:{record.line}"
            )
        key_state = state[record.key]
        new_ids: list[str] = []
        row_ids: set[str] = set()
        prior_budget = _require_policy_int(
            execution.get("prior_generation_attempts_used"),
            label=f"prior_generation_attempts_used at {location}",
        )
        declared_budget = _require_policy_int(
            row.get("generation_attempt_budget_used"),
            label=f"generation_attempt_budget_used at {location}",
            maximum=max_attempts,
        )
        if prior_budget != key_state["budget_used"]:
            raise FinalizationError(
                f"{record.key} prior generation attempt declaration is non-monotonic"
            )
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                raise FinalizationError("generation attempt evidence is not an object")
            attempt_id = str(attempt.get("attempt_id") or "")
            if not HEX32.fullmatch(attempt_id) or attempt_id in row_ids:
                raise FinalizationError("generation attempt id is invalid or duplicated")
            row_ids.add(attempt_id)
            attempt_ordinal = _require_policy_int(
                attempt.get("attempt"),
                label=f"generation attempt ordinal at {location}",
                minimum=1,
                maximum=max_attempts,
            )
            if attempt.get("attempt_kind") not in {
                "generation",
                "generation_pre_call_guard",
                "provider_build_after_paid_setup",
            }:
                raise FinalizationError("generation attempt kind is unsupported")
            started = attempt.get("started_at")
            completed = attempt.get("completed_at")
            if (
                not finite_number(started)
                or not finite_number(completed)
                or float(completed) < float(started)
            ):
                raise FinalizationError("generation attempt timestamps are invalid")
            run = attempt.get("run")
            if not isinstance(run, Mapping):
                raise FinalizationError("generation attempt lacks a run payload")
            canonical_run = _canonicalized_run(
                run,
                identity_seed=f"generation-attempt:{attempt_id}",
            )
            if attempt.get("attempt_kind") == "provider_build_after_paid_setup" and (
                run_expected_request_count(canonical_run) < 1
                or not canonical_run_usage_units(
                    canonical_run,
                    identity_seed=f"generation-attempt:{attempt_id}",
                )
            ):
                raise FinalizationError("paid provider-build attempt lacks physical setup evidence")
            payload_sha = canonical_sha256(immutable_attempt_payload(attempt))
            prior_sha = seen_payloads.get(attempt_id)
            if prior_sha is not None and prior_sha != payload_sha:
                raise FinalizationError(
                    f"generation attempt id {attempt_id} has conflicting payloads"
                )
            owner = attempt_owner.get(attempt_id)
            if owner is not None and owner != record.key:
                raise FinalizationError(
                    f"generation attempt id {attempt_id} is reused by another pair"
                )
            seen_payloads[attempt_id] = payload_sha
            attempt_owner[attempt_id] = record.key
            if attempt_id not in key_state["seen_ids"]:
                expected_ordinal = prior_budget + len(new_ids) + 1
                if attempt_ordinal != expected_ordinal:
                    raise FinalizationError(
                        "new generation attempt ordinal is not contiguous with "
                        "the cumulative cross-wave budget"
                    )
                new_ids.append(attempt_id)
                key_state["attempt_ordinals"][attempt_id] = attempt_ordinal
            elif key_state["attempt_ordinals"].get(attempt_id) != attempt_ordinal:
                raise FinalizationError(f"generation attempt id {attempt_id} changed ordinal")
        expected_budget = key_state["budget_used"] + len(new_ids)
        if declared_budget != expected_budget or declared_budget > max_attempts:
            raise FinalizationError(
                f"{record.key} cumulative generation attempt declaration differs "
                f"or exceeds {max_attempts}"
            )
        if not new_ids:
            if not key_state["seen_ids"] or not repair_evidence(row, execution):
                raise FinalizationError(
                    f"{record.key} no-new-attempt row lacks explicit repair evidence"
                )
            key_state["repair_row_count"] += 1
        elif len(new_ids) != len(attempts):
            raise FinalizationError(
                f"{record.key} generation row mixes prior and new attempt evidence"
            )
        elif execution.get("generation_reused") is True:
            raise FinalizationError(
                f"{record.key} new attempt row incorrectly claims generation reuse"
            )
        key_state["seen_ids"].update(new_ids)
        key_state["budget_used"] = declared_budget
        key_state["row_count"] += 1
    return {
        f"{key[0]}/{key[1]}": {
            "generation_attempt_budget_used": value["budget_used"],
            "unique_attempt_count": len(value["seen_ids"]),
            "source_row_count": value["row_count"],
            "repair_row_count": value["repair_row_count"],
        }
        for key, value in sorted(state.items())
    }


def generation_sort_key(record: SourceRecord) -> tuple[int, float, int, int]:
    row = record.row
    raw = row.get("generation_completed_at")
    if not finite_number(raw):
        raw = row.get("completed_at")
    if not finite_number(raw):
        raw = row.get("started_at")
    timestamp = float(raw) if finite_number(raw) else 0.0
    return (
        int(timestamp > 0.0),
        timestamp,
        record.source_index,
        record.line,
    )


def generation_attempt_count(row: Mapping[str, Any]) -> int:
    execution = row.get("execution")
    attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping) and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    count = max(nonnegative_int(row.get("generation_attempt_count")), len(attempts))
    if count == 0 and str(row.get("final_text") or "").strip():
        count = 1
    return count


def selected_usage_models(row: Mapping[str, Any]) -> set[str]:
    return {
        str(unit.get("model") or "").strip()
        for unit in usage_units(row.get("usage"))
        if str(unit.get("model") or "").strip()
    }


def contract_provider_pins(contract: Mapping[str, Any]) -> dict[str, str]:
    runtime = contract.get("resolved_llm_runtime")
    raw = runtime.get("provider_routing") if isinstance(runtime, Mapping) else None
    pins = (
        {
            str(model).strip().casefold(): str(provider).strip().casefold()
            for model, provider in raw.items()
            if str(model).strip() and str(provider).strip()
        }
        if isinstance(raw, Mapping)
        else {}
    )
    registry = contract.get("g1_registry_contract")
    routes = registry.get("expected_routes") if isinstance(registry, Mapping) else None
    if (
        isinstance(registry, Mapping)
        and registry.get("candidate_scope") == "registry_all"
        and isinstance(routes, Mapping)
    ):
        # ``auto`` means the candidate contract does not require an upstream
        # pin.  It does not cancel a concrete pin already frozen in the
        # resolved runtime; mirror the runner's audit projection by filling
        # only candidates that are otherwise unpinned.
        for model, provider in routes.items():
            normalized_model = str(model).strip().casefold()
            normalized_provider = str(provider).strip().casefold()
            if normalized_model and normalized_provider == "auto":
                pins.setdefault(normalized_model, "auto")
    return pins


def _is_unknown_task_analyzer_placeholder(
    unit: Mapping[str, Any],
    *,
    expected_provider: str = "openrouter",
    expected_model: str = TASK_ANALYZER_MODEL,
) -> bool:
    provider_usage = unit.get("provider_usage")
    return (
        str(unit.get("role") or "").strip().casefold() == "unknown_request"
        and str(unit.get("label") or "").strip().casefold() == "task_analyzer"
        and str(unit.get("provider") or "").strip() == ""
        and str(unit.get("model") or "").strip() == ""
        and str(unit.get("requested_provider") or "").strip().casefold() == expected_provider
        and str(unit.get("requested_model") or "").strip() == expected_model
        and isinstance(unit.get("attempt"), int)
        and not isinstance(unit.get("attempt"), bool)
        and int(unit["attempt"]) >= 1
        and HEX32.fullmatch(str(unit.get("physical_attempt_id") or "")) is not None
        and nonnegative_int(unit.get("input_tokens")) == 0
        and nonnegative_int(unit.get("output_tokens")) == 0
        and nonnegative_int(unit.get("reasoning_tokens")) == 0
        and nonnegative_int(unit.get("cached_tokens")) == 0
        and nonnegative_int(unit.get("cache_write_tokens")) == 0
        and finite_number(unit.get("billed_cost"))
        and float(unit["billed_cost"]) == 0.0
        and str(unit.get("cost_source") or "none").casefold() in {"none", "unavailable"}
        and isinstance(provider_usage, Mapping)
        and provider_usage.get("usage_unknown") is True
        and provider_usage.get("physical_attempt_id") == unit.get("physical_attempt_id")
    )


def _is_task_analyzer_evidence(unit: Mapping[str, Any]) -> bool:
    role = str(unit.get("role") or "").strip().casefold()
    return role == "task_analyzer" or (
        role == "unknown_request"
        and str(unit.get("label") or "").strip().casefold() == "task_analyzer"
    )


def _task_analyzer_source_rows(
    run: Mapping[str, Any],
    *,
    source: str,
) -> list[dict[str, Any]]:
    if source == "setup":
        raw_rows = run.get("setup_usage")
        rows = raw_rows if isinstance(raw_rows, list) else []
    elif source == "usage":
        usage = run.get("usage")
        rows = usage_units(usage)
    else:  # pragma: no cover - internal caller contract
        raise ValueError(f"unknown task analyzer evidence source: {source}")
    return [
        copy.deepcopy(dict(row))
        for row in rows
        if isinstance(row, Mapping) and _is_task_analyzer_evidence(row)
    ]


def _task_analyzer_physical_attempt_id(unit: Mapping[str, Any]) -> str:
    direct = str(unit.get("physical_attempt_id") or "").strip()
    provider_usage = unit.get("provider_usage")
    nested = (
        str(provider_usage.get("physical_attempt_id") or "").strip()
        if isinstance(provider_usage, Mapping)
        else ""
    )
    if direct and nested and direct != nested:
        raise FinalizationError("task analyzer evidence has conflicting physical_attempt_id fields")
    return direct or nested


_TASK_ANALYZER_MIRROR_FIELDS = (
    "role",
    "label",
    "attempt",
    "request_count",
    "provider",
    "model",
    "requested_provider",
    "requested_model",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def _task_analyzer_present_value(unit: Mapping[str, Any], field: str) -> Any | None:
    if field not in unit:
        return None
    value = unit.get(field)
    return None if value in (None, "", [], {}) else value


def _merge_task_analyzer_mirrors(
    setup: Mapping[str, Any],
    usage: Mapping[str, Any],
) -> dict[str, Any]:
    for field_name in _TASK_ANALYZER_MIRROR_FIELDS:
        setup_value = _task_analyzer_present_value(setup, field_name)
        usage_value = _task_analyzer_present_value(usage, field_name)
        if setup_value is not None and usage_value is not None and setup_value != usage_value:
            raise FinalizationError(f"task analyzer setup/usage evidence conflicts on {field_name}")
    setup_ids = response_ids(setup)
    usage_ids = response_ids(usage)
    if setup_ids and usage_ids and setup_ids != usage_ids:
        raise FinalizationError("task analyzer setup/usage evidence conflicts on response_id")
    merged = copy.deepcopy(dict(setup))
    for key, value in usage.items():
        if _task_analyzer_present_value(merged, str(key)) is None:
            merged[str(key)] = copy.deepcopy(value)
    return merged


def _normalized_task_analyzer_source(
    rows: Sequence[Mapping[str, Any]],
    *,
    source: str,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    identified: dict[str, dict[str, Any]] = {}
    unidentified: list[dict[str, Any]] = []
    for raw in rows:
        row = copy.deepcopy(dict(raw))
        request_count = row.get("request_count")
        if request_count is not None and nonnegative_int(request_count) != 1:
            raise FinalizationError(
                f"task analyzer {source} evidence must represent one physical request per row"
            )
        physical_id = _task_analyzer_physical_attempt_id(row)
        if not physical_id:
            unidentified.append(row)
            continue
        row["physical_attempt_id"] = physical_id
        prior = identified.get(physical_id)
        if prior is None:
            identified[physical_id] = row
        else:
            identified[physical_id] = _merge_task_analyzer_mirrors(
                prior,
                row,
            )
    return identified, unidentified


def _reconciled_task_analyzer_units(
    run: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Reconcile setup and aggregate usage as mirrors of physical calls."""

    setup_rows = _task_analyzer_source_rows(run, source="setup")
    usage_rows = _task_analyzer_source_rows(run, source="usage")
    setup_by_id, setup_without_id = _normalized_task_analyzer_source(
        setup_rows,
        source="setup",
    )
    usage_by_id, usage_without_id = _normalized_task_analyzer_source(
        usage_rows,
        source="usage",
    )

    if setup_rows and usage_rows:
        any_identified = bool(setup_by_id or usage_by_id)
        any_unidentified = bool(setup_without_id or usage_without_id)
        if any_identified and any_unidentified:
            raise FinalizationError(
                "task analyzer setup/usage evidence cannot mix identified "
                "and unidentified physical requests"
            )
        if any_identified:
            reconciled = [
                (
                    _merge_task_analyzer_mirrors(
                        setup_by_id[physical_id],
                        usage_by_id[physical_id],
                    )
                    if physical_id in setup_by_id and physical_id in usage_by_id
                    else copy.deepcopy(setup_by_id.get(physical_id) or usage_by_id[physical_id])
                )
                for physical_id in set(setup_by_id) | set(usage_by_id)
            ]
        else:
            if len(setup_without_id) != len(usage_without_id):
                raise FinalizationError(
                    "task analyzer setup/usage evidence has different physical-request multiplicity"
                )
            reconciled = [
                _merge_task_analyzer_mirrors(setup, usage)
                for setup, usage in zip(
                    setup_without_id,
                    usage_without_id,
                    strict=True,
                )
            ]
    elif setup_rows:
        reconciled = [*setup_by_id.values(), *setup_without_id]
    else:
        reconciled = [*usage_by_id.values(), *usage_without_id]

    reconciled.sort(
        key=lambda row: (
            nonnegative_int(row.get("attempt")),
            _task_analyzer_physical_attempt_id(row),
            canonical_sha256(row),
        )
    )
    return reconciled, setup_rows, usage_rows


def _task_analyzer_immutable_unit(unit: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "role",
        "label",
        "attempt",
        "physical_attempt_id",
        "request_count",
        "requested_provider",
        "requested_model",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    )
    projected = {field: copy.deepcopy(unit.get(field)) for field in fields if field in unit}
    physical_id = _task_analyzer_physical_attempt_id(unit)
    if physical_id:
        projected["physical_attempt_id"] = physical_id
    return projected


def _task_analyzer_setup_usage_contract(
    run: Mapping[str, Any],
) -> dict[str, Any]:
    reconciled, setup_rows, usage_rows = _reconciled_task_analyzer_units(run)
    return {
        "setup": [_task_analyzer_immutable_unit(row) for row in setup_rows],
        "usage": [_task_analyzer_immutable_unit(row) for row in usage_rows],
        "unique_physical": [_task_analyzer_immutable_unit(row) for row in reconciled],
    }


def _canonical_task_analyzer_setup_units(
    run: Mapping[str, Any],
    *,
    identity_seed: str,
) -> list[dict[str, Any]]:
    """Return each unique analyzer request after reconciling both mirrors."""

    del identity_seed  # Physical analyzer identity is carried by the evidence.
    reconciled, _, _ = _reconciled_task_analyzer_units(run)
    return reconciled


def _is_unknown_judge_placeholder(
    unit: Mapping[str, Any],
    *,
    judge_model: str = JUDGE_MODEL,
) -> bool:
    """Return whether *unit* is a fail-closed unknown Judge request.

    A transiently failed OpenRouter request has no actual serving identity or
    successful router receipt to authenticate.  It may still be represented
    in the physical ledger, but only when its frozen requested route and
    explicit unknown-usage provenance are present.
    """

    def explicit_zero(field: str, *, optional: bool = False) -> bool:
        if optional and field not in unit:
            return True
        value = unit.get(field)
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) == 0.0
        )

    provider_usage = unit.get("provider_usage")
    evidence_id = str(unit.get("usage_evidence_id") or "")
    return (
        str(unit.get("role") or "").strip().casefold() in MISSING_USAGE_PLACEHOLDER_ROLES
        and str(unit.get("provider") or "").strip() == ""
        and str(unit.get("model") or "").strip() == ""
        and str(unit.get("requested_provider") or "").strip().casefold() == "openrouter"
        and str(unit.get("requested_model") or "").strip() == judge_model
        and explicit_zero("input_tokens")
        and explicit_zero("output_tokens")
        and explicit_zero("reasoning_tokens")
        and explicit_zero("cached_tokens")
        and explicit_zero("cache_read_tokens", optional=True)
        and explicit_zero("cache_write_tokens")
        and finite_number(unit.get("billed_cost"))
        and float(unit["billed_cost"]) == 0.0
        and str(unit.get("cost_source") or "").strip().casefold() == "none"
        and unit.get("usage_unknown") is True
        and unit.get("usage_evidence_schema") == "opensquilla.usage-evidence/v1"
        and unit.get("usage_evidence_source") == "physical_request_counter_deficit"
        and SHA256_VALUE.fullmatch(evidence_id) is not None
        and isinstance(unit.get("physical_request_ordinal"), int)
        and not isinstance(unit.get("physical_request_ordinal"), bool)
        and int(unit["physical_request_ordinal"]) >= 1
        and isinstance(provider_usage, Mapping)
        and provider_usage.get("usage_unknown") is True
        and provider_usage.get("usage_evidence_schema") == "opensquilla.usage-evidence/v1"
        and provider_usage.get("usage_evidence_id") == evidence_id
        and not response_ids(unit)
        and not _successful_router_bindings(unit)
    )


def usage_route_reasons(
    usage: Any,
    *,
    allowed_models: set[str],
    provider_pins: Mapping[str, str] | None = None,
    role_model_pins: Mapping[str, str] | None = None,
    role_provider_pins: Mapping[str, str] | None = None,
    allow_unknown_task_analyzer_attempts: bool = False,
    allowed_task_analyzer_models: set[str] | None = None,
    allow_unknown_judge_attempts: bool = False,
    unknown_judge_model: str = JUDGE_MODEL,
) -> list[str]:
    reasons: list[str] = []
    units = usage_units(usage)
    if not units:
        return ["missing_generation_usage_route_evidence"]
    represented = 0
    for unit in units:
        role = str(unit.get("role") or "").strip().casefold()
        label = str(unit.get("label") or "").strip().casefold()
        route_role = (
            label
            if role in MISSING_USAGE_PLACEHOLDER_ROLES
            and role_model_pins is not None
            and label in role_model_pins
            else role
        )
        effective_allowed_models = (
            {str(role_model_pins[route_role])}
            if role_model_pins is not None and route_role in role_model_pins
            else allowed_models
        )

        def expected_provider_pin(model: str) -> str:
            role_pin = (
                str(role_provider_pins.get(route_role) or "").strip().casefold()
                if role_provider_pins is not None and route_role in role_provider_pins
                else ""
            )
            model_pin = (
                str(provider_pins.get(model) or "").strip().casefold()
                if provider_pins is not None
                else ""
            )
            pin = role_pin or model_pin
            return "" if pin == "auto" else pin

        if role in MISSING_USAGE_PLACEHOLDER_ROLES:
            analyzer_models = allowed_task_analyzer_models or {
                str((role_model_pins or {}).get("task_analyzer") or TASK_ANALYZER_MODEL)
            }
            unknown_analyzer_allowed = bool(
                allow_unknown_task_analyzer_attempts
                and any(
                    _is_unknown_task_analyzer_placeholder(
                        unit,
                        expected_provider="openrouter",
                        expected_model=model,
                    )
                    for model in analyzer_models
                )
            )
            unknown_judge_allowed = allow_unknown_judge_attempts and _is_unknown_judge_placeholder(
                unit,
                judge_model=unknown_judge_model,
            )
            provider = str(unit.get("provider") or "").strip().casefold()
            requested_provider = str(unit.get("requested_provider") or "").strip().casefold()
            model = str(unit.get("model") or "").strip()
            requested_model = str(unit.get("requested_model") or "").strip()
            provider_usage = unit.get("provider_usage")
            nested_requested_provider = (
                str(provider_usage.get("requested_provider") or "").strip().casefold()
                if isinstance(provider_usage, Mapping)
                else ""
            )
            nested_requested_model = (
                str(provider_usage.get("requested_model") or "").strip()
                if isinstance(provider_usage, Mapping)
                else ""
            )
            router_metadata = (
                provider_usage.get("router_metadata")
                if isinstance(provider_usage, Mapping)
                else None
            )
            router_requested_provider = (
                str(router_metadata.get("requested_provider") or "").strip().casefold()
                if isinstance(router_metadata, Mapping)
                else ""
            )
            router_requested_model = (
                str(router_metadata.get("requested") or "").strip()
                if isinstance(router_metadata, Mapping)
                else ""
            )
            known_providers = {
                value
                for value in (
                    provider,
                    requested_provider,
                    nested_requested_provider,
                    router_requested_provider,
                )
                if value
            }
            known_models = [
                value
                for value in (
                    model,
                    requested_model,
                    nested_requested_model,
                    router_requested_model,
                )
                if value
            ]
            if any(value != "openrouter" for value in known_providers):
                reasons.append("wrong_generation_provider_route")
            model_outside_contract = any(
                not any(
                    _formal_openrouter_models_equivalent(value, allowed_model)
                    for allowed_model in effective_allowed_models
                )
                for value in known_models
            )
            conflicting_known_models = bool(known_models) and any(
                not _formal_openrouter_models_equivalent(known_models[0], value)
                for value in known_models[1:]
            )
            if model_outside_contract or conflicting_known_models:
                reasons.append("wrong_generation_model_route")
            successful = _successful_router_bindings(unit)
            known_model = known_models[0] if known_models else ""
            if successful and (
                not known_model
                or any(
                    any(
                        not _formal_openrouter_models_equivalent(
                            upstream_model,
                            known_value,
                        )
                        for known_value in known_models
                    )
                    for _, upstream_model in successful
                )
            ):
                reasons.append("conflicting_successful_router_receipt")
            if provider_pins is not None and known_model:
                pin_model = next(
                    (
                        allowed_model
                        for allowed_model in effective_allowed_models
                        if _formal_openrouter_models_equivalent(
                            known_model,
                            allowed_model,
                        )
                    ),
                    "",
                )
                expected_pin = expected_provider_pin(pin_model)
                if (
                    not expected_pin
                    and str(provider_pins.get(pin_model) or "").strip().casefold() != "auto"
                ):
                    reasons.append("missing_formal_upstream_provider_pin")
                elif (
                    expected_pin
                    and successful
                    and any(
                        upstream_provider != _normalize_openrouter_provider_identity(expected_pin)
                        for upstream_provider, _ in successful
                    )
                ):
                    reasons.append("router_receipt_provider_not_bound_to_formal_route")
            if unknown_analyzer_allowed:
                represented += 1
            elif unknown_judge_allowed:
                represented += 1
            elif allow_unknown_judge_attempts:
                reasons.append("invalid_unknown_judge_usage_placeholder")
            # The runner emits an explicit placeholder for a physical request whose
            # response usage could not be recovered.  Preserve it for account-level
            # reconciliation.  Missing fields remain unknown, while every field and
            # router binding that is present must still satisfy the frozen route.
            continue
        represented += 1
        provider = str(unit.get("provider") or "").strip().casefold()
        requested_provider = str(unit.get("requested_provider") or "").strip().casefold()
        model = str(unit.get("model") or "").strip()
        requested_model = str(unit.get("requested_model") or "").strip()
        if provider != "openrouter" or requested_provider != "openrouter":
            reasons.append("wrong_generation_provider_route")
        if (
            not model
            or model not in effective_allowed_models
            or not requested_model
            or requested_model not in effective_allowed_models
            or not _formal_openrouter_models_equivalent(model, requested_model)
        ):
            reasons.append("wrong_generation_model_route")
        provider_usage = unit.get("provider_usage")
        nested_requested_provider = (
            str(provider_usage.get("requested_provider") or "").strip().casefold()
            if isinstance(provider_usage, Mapping)
            else ""
        )
        nested_requested_model = (
            str(provider_usage.get("requested_model") or "").strip()
            if isinstance(provider_usage, Mapping)
            else ""
        )
        router_metadata = (
            provider_usage.get("router_metadata") if isinstance(provider_usage, Mapping) else None
        )
        successful = _successful_router_bindings(unit)
        model_bound = {
            (upstream_provider, upstream_model)
            for upstream_provider, upstream_model in successful
            if _formal_openrouter_models_equivalent(upstream_model, model)
            and _formal_openrouter_models_equivalent(upstream_model, requested_model)
        }
        router_requested = (
            str(router_metadata.get("requested") or "").strip()
            if isinstance(router_metadata, Mapping)
            else ""
        )
        router_requested_provider = (
            str(router_metadata.get("requested_provider") or "").strip().casefold()
            if isinstance(router_metadata, Mapping)
            else ""
        )
        if any(
            value and value != "openrouter"
            for value in (nested_requested_provider, router_requested_provider)
        ):
            reasons.append("wrong_generation_provider_route")
        if nested_requested_model and not _formal_openrouter_models_equivalent(
            nested_requested_model,
            requested_model,
        ):
            reasons.append("wrong_generation_model_route")
        if not isinstance(router_metadata, Mapping) or not successful:
            reasons.append("missing_successful_router_receipt")
        elif not model_bound:
            reasons.append("router_receipt_model_not_bound_to_formal_route")
        elif model_bound != successful:
            reasons.append("conflicting_successful_router_receipt")
        if not _formal_openrouter_models_equivalent(router_requested, requested_model):
            reasons.append("router_receipt_request_not_bound_to_formal_route")
        expected_pin = expected_provider_pin(requested_model)
        raw_expected_pin = (
            str(provider_pins.get(requested_model) or "").strip().casefold()
            if provider_pins is not None
            else ""
        )
        role_has_explicit_pin = (
            role_provider_pins is not None
            and route_role in role_provider_pins
            and bool(str(role_provider_pins.get(route_role) or "").strip())
        )
        if (
            provider_pins is not None
            and not expected_pin
            and raw_expected_pin != "auto"
            and not role_has_explicit_pin
        ):
            reasons.append("missing_formal_upstream_provider_pin")
        elif expected_pin and any(
            upstream_provider != _normalize_openrouter_provider_identity(expected_pin)
            for upstream_provider, _ in successful
        ):
            reasons.append("router_receipt_provider_not_bound_to_formal_route")
    if represented <= 0:
        reasons.append("missing_generation_usage_route_evidence")
    return list(dict.fromkeys(reasons))


def _unit_present_value(unit: Mapping[str, Any], field: str) -> Any | None:
    value = unit.get(field)
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if field in {"provider", "requested_provider"}:
        return str(value).strip().casefold()
    if field in {"model", "requested_model", "role"}:
        return str(value).strip()
    return value


def _normalize_openrouter_provider_identity(value: Any) -> str:
    """Normalize OpenRouter provider slugs and display names for comparison."""
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


@cache
def _formal_openrouter_model_aliases() -> dict[str, frozenset[str]]:
    """Bind requested model ids to the frozen registry's serving-model aliases."""
    aliases: dict[str, set[str]] = {
        str(model).strip().casefold(): {str(model).strip().casefold()}
        for model in FORMAL_UPSTREAM_PINS
        if str(model).strip()
    }
    for raw_model, raw_version in FORMAL_OPENROUTER_SERVING_ALIASES.items():
        model = str(raw_model).strip().casefold()
        version = str(raw_version).strip().casefold()
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


def _successful_router_bindings(unit: Mapping[str, Any]) -> set[tuple[str, str]]:
    provider_usage = unit.get("provider_usage")
    router_metadata = (
        provider_usage.get("router_metadata") if isinstance(provider_usage, Mapping) else None
    )
    attempts = router_metadata.get("attempts") if isinstance(router_metadata, Mapping) else None
    bindings = {
        (
            _normalize_openrouter_provider_identity(attempt.get("provider")),
            str(attempt.get("model") or "").strip(),
        )
        for attempt in attempts or []
        if isinstance(attempt, Mapping)
        and isinstance(attempt.get("status"), int)
        and not isinstance(attempt.get("status"), bool)
        and 200 <= int(attempt["status"]) < 300
        and str(attempt.get("provider") or "").strip()
        and str(attempt.get("model") or "").strip()
    }
    endpoints = router_metadata.get("endpoints") if isinstance(router_metadata, Mapping) else None
    available = endpoints.get("available") if isinstance(endpoints, Mapping) else None
    bindings.update(
        (
            _normalize_openrouter_provider_identity(endpoint.get("provider")),
            str(endpoint.get("model") or "").strip(),
        )
        for endpoint in available or []
        if isinstance(endpoint, Mapping)
        and endpoint.get("selected") is True
        and str(endpoint.get("provider") or "").strip()
        and str(endpoint.get("model") or "").strip()
    )
    return bindings


def _canonicalized_run(
    run: Mapping[str, Any],
    *,
    identity_seed: str,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """Return an in-memory canonical run without changing sealed wave input."""

    normalized = copy.deepcopy(dict(run))
    try:
        normalized["usage"] = canonicalize_run_usage(
            run,
            identity_seed=identity_seed,
            requested_provider=requested_provider,
            requested_model=requested_model,
            role=role,
        )
    except UsageEvidenceError as exc:
        raise FinalizationError(
            f"{identity_seed} has invalid physical usage evidence: {exc}"
        ) from exc
    return normalized


def run_receipt_enrichment(
    run: Any,
    *,
    identity_seed: str,
    source_index: int,
    line: int,
) -> tuple[int, int, int]:
    units = (
        canonical_run_usage_units(run, identity_seed=identity_seed)
        if isinstance(run, Mapping)
        else []
    )
    score = 0
    for unit in units:
        score += len(response_ids(unit)) * 20
        score += sum(
            bool(_unit_present_value(unit, field)) * weight
            for field, weight in (
                ("provider", 2),
                ("model", 2),
                ("requested_provider", 2),
                ("requested_model", 2),
            )
        )
        score += int(unit.get("billed_cost") is not None) * 5
        provider_usage = unit.get("provider_usage")
        if isinstance(provider_usage, Mapping):
            score += int(provider_usage.get("provider_reported_cost") is not None) * 8
            score += int(provider_usage.get("is_byok") in {True, False}) * 8
            router = provider_usage.get("router_metadata")
            if isinstance(router, Mapping):
                score += int(router.get("is_byok") in {True, False}) * 8
                score += int(router_provider_metadata_complete(router)) * 8
    return score, source_index, line


def validate_and_select_monotonic_run_version(
    versions: Sequence[tuple[SourceRecord, Mapping[str, Any]]],
    *,
    label: str,
    identity_seed: str,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    role: str | None = None,
) -> tuple[SourceRecord, Mapping[str, Any]]:
    """Allow receipt backfill only; reject conflicting or regressive copies."""

    if not versions:
        raise FinalizationError(f"{label} has no physical run versions")
    ordered = sorted(
        (
            (
                record,
                _canonicalized_run(
                    run,
                    identity_seed=identity_seed,
                    requested_provider=requested_provider,
                    requested_model=requested_model,
                    role=role,
                ),
            )
            for record, run in versions
        ),
        key=lambda value: (value[0].source_index, value[0].line),
    )
    expected_request_count: int | None = None
    prior_units: list[dict[str, Any]] | None = None
    exact_costs_by_unit: dict[int, set[Decimal]] = defaultdict(set)
    for record, run in ordered:
        request_count = derive_physical_request_count(run)
        if expected_request_count is None:
            expected_request_count = request_count
        elif request_count != expected_request_count:
            raise FinalizationError(
                f"{label} changed its physical request count across receipt repairs"
            )
        units = canonical_run_usage_units(run, identity_seed=identity_seed)
        if len(units) != expected_request_count:
            raise FinalizationError(f"{label} does not represent every physical request in usage")
        if prior_units is not None and len(units) != len(prior_units):
            raise FinalizationError(f"{label} changed its physical usage-unit shape")
        for index, unit in enumerate(units):
            prior = prior_units[index] if prior_units is not None else None
            if prior is not None:
                for field in (
                    "role",
                    "label",
                    "attempt",
                    "physical_attempt_id",
                    "provider",
                    "model",
                    "requested_provider",
                    "requested_model",
                    *USAGE_CONTRACT_KEYS[4:],
                ):
                    old = _unit_present_value(prior, field)
                    new = _unit_present_value(unit, field)
                    if old is not None and new != old:
                        raise FinalizationError(f"{label} receipt repair conflicts on {field}")
                old_ids = response_ids(prior)
                new_ids = response_ids(unit)
                if old_ids and new_ids != old_ids:
                    raise FinalizationError(f"{label} receipt repair conflicts on response_id")
                old_usage_flags, old_router_flags = unit_non_byok_flags(prior)
                new_usage_flags, new_router_flags = unit_non_byok_flags(unit)
                for name, old_flags, new_flags in (
                    ("usage_is_byok", old_usage_flags, new_usage_flags),
                    ("router_is_byok", old_router_flags, new_router_flags),
                ):
                    if old_flags and new_flags != old_flags:
                        raise FinalizationError(f"{label} receipt repair conflicts on {name}")
                old_routes = _successful_router_bindings(prior)
                new_routes = _successful_router_bindings(unit)
                if old_routes and new_routes != old_routes:
                    raise FinalizationError(f"{label} receipt repair conflicts on router route")
                if unit_cost_is_exact(prior) and not unit_cost_is_exact(unit):
                    raise FinalizationError(f"{label} receipt repair regressed an exact cost")
            exact_cost: Decimal | None = None
            if unit_cost_is_exact(unit):
                provider_usage = unit.get("provider_usage")
                assert isinstance(provider_usage, Mapping)
                exact_cost = required_decimal(
                    provider_usage.get("provider_reported_cost"),
                    label=f"{label} exact receipt cost",
                ).quantize(Decimal("0.000000001"))
            if exact_cost is not None:
                exact_costs_by_unit[index].add(exact_cost)
        prior_units = units
    if any(len(values) > 1 for values in exact_costs_by_unit.values()):
        raise FinalizationError(f"{label} receipt repair conflicts on exact cost")
    selected_record, selected_run = max(
        ordered,
        key=lambda version: run_receipt_enrichment(
            version[1],
            identity_seed=identity_seed,
            source_index=version[0].source_index,
            line=version[0].line,
        ),
    )
    return selected_record, selected_run


def canonical_judge_run_route_reasons(
    run: Mapping[str, Any],
    *,
    attempt_id: str,
    judge_model: str = JUDGE_MODEL,
    judge_provider_pin: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Canonicalize one Judge run and apply the frozen route contract."""

    canonical_run = _canonicalized_run(
        run,
        identity_seed=f"judge-attempt:{attempt_id}",
        requested_provider="openrouter",
        requested_model=judge_model,
        role="unknown_request",
    )
    has_unknown_placeholders = any(
        str(unit.get("role") or "").strip().casefold() in MISSING_USAGE_PLACEHOLDER_ROLES
        for unit in canonical_run_usage_units(
            canonical_run,
            identity_seed=f"judge-attempt:{attempt_id}",
        )
    )
    reasons: list[str] = []
    if has_unknown_placeholders and not str(canonical_run.get("error") or "").strip():
        reasons.append("successful_judge_has_unknown_usage")
    reasons.extend(
        usage_route_reasons(
            canonical_run.get("usage"),
            allowed_models={judge_model},
            provider_pins={
                judge_model: (judge_provider_pin or FORMAL_UPSTREAM_PINS.get(judge_model, ""))
            },
            allow_unknown_judge_attempts=has_unknown_placeholders,
            unknown_judge_model=judge_model,
        )
    )
    return canonical_run, list(dict.fromkeys(reasons))


def immutable_judge_attempt_payload(
    attempt: Mapping[str, Any],
    *,
    judge_model: str = JUDGE_MODEL,
) -> dict[str, Any]:
    run = attempt.get("run")
    attempt_id = str(attempt.get("attempt_id") or "")
    canonical_run = (
        _canonicalized_run(
            run,
            identity_seed=f"judge-attempt:{attempt_id}",
            requested_provider="openrouter",
            requested_model=judge_model,
            role="unknown_request",
        )
        if isinstance(run, Mapping)
        else None
    )
    immutable_run = (
        {
            "error": canonical_run.get("error"),
            "final_text_sha256": canonical_run.get("final_text_sha256"),
            "llm_request_count": canonical_run.get("llm_request_count"),
            "usage": usage_generation_identity_contract(canonical_run.get("usage")),
        }
        if canonical_run is not None
        else None
    )
    return {
        "attempt_id": attempt.get("attempt_id"),
        "attempt": attempt.get("attempt"),
        "verdict": attempt.get("verdict"),
        "met": attempt.get("met"),
        "retry_suppressed_reason": attempt.get("retry_suppressed_reason"),
        "run": immutable_run,
    }


def _judge_run_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "error": value.get("error"),
        "final_text_sha256": value.get("final_text_sha256"),
        "llm_request_count": value.get("llm_request_count"),
        "usage": usage_generation_identity_contract(value.get("usage")),
    }


def validate_judge_attempt_evidence(
    records: Sequence[SourceRecord],
    *,
    judge_model: str = JUDGE_MODEL,
    judge_max_attempts: int = JUDGE_ATTEMPT_BUDGET_LIMIT,
    judge_provider_pin: str | None = None,
) -> dict[str, Any]:
    """Validate cumulative Judge budgets and every physical Judge attempt."""

    payloads: dict[str, str] = {}
    owners: dict[str, tuple[str, str, str, str, int]] = {}
    run_versions: dict[str, list[tuple[SourceRecord, Mapping[str, Any]]]] = defaultdict(list)
    state: dict[tuple[str, str, str, str, int], dict[str, Any]] = defaultdict(
        lambda: {
            "attempt_ids": (),
            "successful": False,
            "exhausted": False,
            "source_row_count": 0,
            "declaration_prior": None,
            "declaration_new": None,
        }
    )
    scope_count = 0
    for record in sorted(records, key=lambda item: (item.source_index, item.line)):
        scopes: list[tuple[str, Any]] = [("judge", record.row.get("judge"))]
        candidate_judges = record.row.get("candidate_judges")
        if candidate_judges is not None:
            if not isinstance(candidate_judges, list):
                raise FinalizationError("candidate_judges is not a list")
            scopes.extend(
                (f"candidate_judge/{index}", judge) for index, judge in enumerate(candidate_judges)
            )
        for scope_name, judge in scopes:
            if judge is None:
                continue
            if not isinstance(judge, Mapping):
                raise FinalizationError(f"{record.key} {scope_name} is not an object")
            scope_count += 1
            if (
                judge.get("judge_attempt_evidence_schema") != JUDGE_ATTEMPT_EVIDENCE_SCHEMA
                or judge.get("judge_attempt_budget_scope") != JUDGE_ATTEMPT_BUDGET_SCOPE
                or judge.get("judge_attempt_budget_limit_per_unit") != judge_max_attempts
                or judge.get("prior_judge_attempts")
            ):
                raise FinalizationError(
                    f"{record.key} {scope_name} lacks the formal Judge attempt contract"
                )
            judgments = judge.get("criterion_judgments")
            if not isinstance(judgments, list):
                raise FinalizationError(f"{record.key} {scope_name} lacks criterion Judge evidence")
            seen_units: set[tuple[str, int]] = set()
            top_attempt_count = 0
            top_new_count = 0
            top_exhausted_count = 0
            for judgment in judgments:
                if not isinstance(judgment, Mapping):
                    raise FinalizationError("Judge criterion evidence is not an object")
                criterion_id = str(judgment.get("id") or "")
                repeat_index = judgment.get("repeat_index")
                if (
                    not criterion_id
                    or isinstance(repeat_index, bool)
                    or not isinstance(repeat_index, int)
                    or repeat_index < 0
                    or (criterion_id, repeat_index) in seen_units
                ):
                    raise FinalizationError(f"{record.key} {scope_name} has an invalid Judge unit")
                seen_units.add((criterion_id, repeat_index))
                owner = (
                    record.key[0],
                    record.key[1],
                    scope_name,
                    criterion_id,
                    repeat_index,
                )
                unit_state = state[owner]
                attempts = judgment.get("judge_attempts")
                if not isinstance(attempts, list):
                    raise FinalizationError(
                        f"{record.key} {scope_name}/{criterion_id}/{repeat_index} "
                        "lacks cumulative Judge attempts"
                    )
                if len(attempts) > judge_max_attempts:
                    raise FinalizationError(f"Judge attempt budget exceeds {judge_max_attempts}")
                attempt_ids: list[str] = []
                for ordinal, attempt in enumerate(attempts, start=1):
                    if not isinstance(attempt, Mapping):
                        raise FinalizationError("Judge attempt is not an object")
                    attempt_id = str(attempt.get("attempt_id") or "")
                    if not HEX32.fullmatch(attempt_id) or attempt.get("attempt") != ordinal:
                        raise FinalizationError("Judge attempt identity or ordinal is invalid")
                    prior_owner = owners.get(attempt_id)
                    if prior_owner is not None and prior_owner != owner:
                        raise FinalizationError(
                            f"Judge attempt id {attempt_id} is reused by another unit"
                        )
                    payload = canonical_sha256(
                        immutable_judge_attempt_payload(
                            attempt,
                            judge_model=judge_model,
                        )
                    )
                    if attempt_id in payloads and payloads[attempt_id] != payload:
                        raise FinalizationError(
                            f"Judge attempt id {attempt_id} has conflicting payloads"
                        )
                    run = attempt.get("run")
                    if not isinstance(run, Mapping):
                        raise FinalizationError("Judge attempt lacks a physical run")
                    owners[attempt_id] = owner
                    payloads[attempt_id] = payload
                    run_versions[attempt_id].append((record, run))
                    attempt_ids.append(attempt_id)
                previous_ids = tuple(unit_state["attempt_ids"])
                current_ids = tuple(attempt_ids)
                if current_ids[: len(previous_ids)] != previous_ids:
                    raise FinalizationError(
                        f"{record.key} {scope_name}/{criterion_id}/{repeat_index} "
                        "Judge attempts are not cumulative"
                    )
                new_count = len(current_ids) - len(previous_ids)
                declared_prior = judgment.get("prior_judge_attempts_used")
                declared_new = judgment.get("judge_new_attempt_count")
                canonical_delta_declaration = (
                    declared_prior == len(previous_ids) and declared_new == new_count
                )
                replayed_snapshot_declaration = (
                    new_count == 0
                    and current_ids == previous_ids
                    and declared_prior == unit_state["declaration_prior"]
                    and declared_new == unit_state["declaration_new"]
                )
                if (
                    not (canonical_delta_declaration or replayed_snapshot_declaration)
                    or judgment.get("judge_attempt_count") != len(current_ids)
                    or judgment.get("judge_attempt_budget_used") != len(current_ids)
                    or judgment.get("judge_attempt_budget_remaining")
                    != judge_max_attempts - len(current_ids)
                    or judgment.get("judge_attempt_budget_limit") != judge_max_attempts
                    or judgment.get("judge_attempt_evidence_schema")
                    != JUDGE_ATTEMPT_EVIDENCE_SCHEMA
                    or judgment.get("judge_attempt_budget_scope") != JUDGE_ATTEMPT_BUDGET_SCOPE
                ):
                    raise FinalizationError(
                        f"{record.key} {scope_name}/{criterion_id}/{repeat_index} "
                        "Judge budget declarations differ"
                    )
                exhausted = judgment.get("judge_attempt_budget_exhausted") is True
                if (
                    exhausted != (judgment.get("error") == JUDGE_ATTEMPT_BUDGET_EXHAUSTED_ERROR)
                    or exhausted
                    and len(current_ids) != judge_max_attempts
                ):
                    raise FinalizationError(
                        f"{record.key} {scope_name}/{criterion_id}/{repeat_index} "
                        "Judge exhaustion declaration differs"
                    )
                if unit_state["successful"] and new_count or unit_state["exhausted"] and new_count:
                    raise FinalizationError(
                        f"{record.key} {scope_name}/{criterion_id}/{repeat_index} "
                        "spent a new Judge attempt after terminal state"
                    )
                successful = isinstance(judgment.get("met"), bool) and not judgment.get("error")
                if successful:
                    if not attempts:
                        raise FinalizationError("successful Judge unit lacks a physical attempt")
                    final_attempt = attempts[-1]
                    final_run = final_attempt.get("run")
                    final_met = final_attempt.get("met")
                    final_verdict = str(final_attempt.get("verdict") or "").strip().upper()
                    expected_verdict = "MET" if judgment.get("met") is True else "UNMET"
                    judge_run = judgment.get("judge_run")
                    if (
                        not isinstance(final_run, Mapping)
                        or str(final_run.get("error") or "")
                        or final_attempt.get("retry_suppressed_reason")
                        or final_met is not judgment.get("met")
                        or final_verdict != expected_verdict
                        or str(judgment.get("verdict") or "").strip().upper() != expected_verdict
                        or not isinstance(judge_run, Mapping)
                        or _judge_run_binding(judge_run) != _judge_run_binding(final_run)
                    ):
                        raise FinalizationError(
                            f"{record.key} {scope_name}/{criterion_id}/"
                            f"{repeat_index} result is not bound to its final "
                            "successful Judge attempt"
                        )
                unit_state["attempt_ids"] = current_ids
                unit_state["successful"] = successful
                unit_state["exhausted"] = exhausted
                unit_state["source_row_count"] += 1
                unit_state["declaration_prior"] = declared_prior
                unit_state["declaration_new"] = declared_new
                top_attempt_count += len(current_ids)
                top_new_count += int(declared_new)
                top_exhausted_count += int(exhausted)
            if (
                judge.get("judge_attempt_count") != top_attempt_count
                or judge.get("judge_new_attempt_count") != top_new_count
                or judge.get("judge_attempt_budget_exhausted_count") != top_exhausted_count
                or judge.get("judge_attempt_budget_exhausted") is not bool(top_exhausted_count)
            ):
                raise FinalizationError(f"{record.key} {scope_name} aggregate Judge budget differs")
    for attempt_id, versions in run_versions.items():
        _, run = validate_and_select_monotonic_run_version(
            versions,
            label=f"Judge attempt {attempt_id}",
            identity_seed=f"judge-attempt:{attempt_id}",
            requested_provider="openrouter",
            requested_model=judge_model,
            role="unknown_request",
        )
        _, route_failures = canonical_judge_run_route_reasons(
            run,
            attempt_id=attempt_id,
            judge_model=judge_model,
            judge_provider_pin=judge_provider_pin,
        )
        if route_failures:
            raise FinalizationError(
                f"Judge attempt {attempt_id} violates the frozen route: {route_failures}"
            )
    return {
        "schema": JUDGE_ATTEMPT_EVIDENCE_SCHEMA,
        "budget_scope": JUDGE_ATTEMPT_BUDGET_SCOPE,
        "budget_limit_per_unit": judge_max_attempts,
        "judge_scope_source_count": scope_count,
        "criterion_repeat_unit_count": len(state),
        "unique_physical_judge_attempt_count": len(payloads),
        "units": {
            "/".join((group, task, scope, criterion, str(repeat))): {
                "judge_attempt_budget_used": len(value["attempt_ids"]),
                "successful": value["successful"],
                "exhausted": value["exhausted"],
                "source_row_count": value["source_row_count"],
            }
            for (
                group,
                task,
                scope,
                criterion,
                repeat,
            ), value in sorted(state.items())
        },
    }


def ensemble_call_trace_sequence(
    trace: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[str]]:
    """Return physical ensemble calls from direct and Agent-loop traces."""

    calls = trace.get("calls")
    if calls is None and str(trace.get("mode") or "") != "agent_loop":
        return [trace], []
    if not isinstance(calls, list) or not calls:
        return [], ["missing_ensemble_call_trace"]
    if any(not isinstance(item, Mapping) for item in calls):
        return [], ["invalid_ensemble_call_trace"]
    call_traces = [item for item in calls if isinstance(item, Mapping)]
    reasons: list[str] = []
    raw_count = trace.get("agent_llm_call_count")
    if (
        not isinstance(raw_count, int)
        or isinstance(raw_count, bool)
        or raw_count != len(call_traces)
    ):
        reasons.append("wrong_agent_llm_call_count")
    if nonnegative_int(trace.get("untraced_agent_llm_call_count")) != 0:
        reasons.append("untraced_agent_llm_calls")
    indices = [item.get("agent_call_index") for item in call_traces]
    if indices != list(range(1, len(call_traces) + 1)):
        reasons.append("invalid_agent_call_index_sequence")
    return call_traces, reasons


_PARTIAL_PROPOSER_HARD_ERROR_CODES = frozenset(
    {
        "candidate_mode_contract_violation",
        "router_dynamic_proposer_recovery_plan_drift",
        "proposer_recovery_budget_overrun",
        "proposer_recovery_evidence_unproven",
        "quorum_cancelled",
        "quorum_unreachable",
        "soft_deadline",
    }
)


def _candidate_has_visible_content(candidate: Any) -> bool:
    content = candidate.get("content") if isinstance(candidate, Mapping) else None
    return bool(
        isinstance(content, Mapping)
        and nonnegative_int(content.get("chars")) > 0
        and (
            bool(str(content.get("text") or "").strip())
            or HEX64.fullmatch(str(content.get("sha256") or "")) is not None
            or SHA256_VALUE.fullmatch(str(content.get("sha256") or "")) is not None
        )
    )


def successful_candidate(candidate: Any) -> bool:
    """Validate a strict, physically successful proposer receipt."""

    return bool(
        isinstance(candidate, Mapping)
        and candidate.get("ok") is True
        and not candidate.get("error")
        and candidate.get("request_started") is True
        and isinstance(candidate.get("physical_request_count"), int)
        and not isinstance(candidate.get("physical_request_count"), bool)
        and candidate.get("physical_request_count") > 0
        and _candidate_has_visible_content(candidate)
    )


def partial_usable_candidate(candidate: Any) -> bool:
    """Validate the explicit provider receipt for one inert partial draft."""

    execution = candidate.get("execution") if isinstance(candidate, Mapping) else None
    error_code = str(candidate.get("error_code") or "") if isinstance(candidate, Mapping) else ""
    return bool(
        isinstance(candidate, Mapping)
        and candidate.get("ok") is False
        and candidate.get("usable_for_aggregation") is True
        and candidate.get("completion_outcome") == "partial_usable"
        and candidate.get("request_started") is True
        and isinstance(candidate.get("physical_request_count"), int)
        and not isinstance(candidate.get("physical_request_count"), bool)
        and candidate.get("physical_request_count") > 0
        and _candidate_has_visible_content(candidate)
        and error_code not in _PARTIAL_PROPOSER_HARD_ERROR_CODES
        and not (
            isinstance(execution, Mapping)
            and execution.get("candidate_mode_contract_violation") is True
        )
    )


def usable_candidate(candidate: Any) -> bool:
    return successful_candidate(candidate) or partial_usable_candidate(candidate)


def _strict_zero_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return Decimal(str(value)) == 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def _legacy_excluded_zero_request_candidate(
    candidate: Mapping[str, Any],
    *,
    expected_identity: str,
    excluded_identities: set[str],
) -> bool:
    """Recognize the one legacy zero-request receipt that omitted ``[]``.

    Older router_dynamic runs serialized locally excluded proposer slots without
    ``execution.physical_attempts``.  Accept only that exact, inert shape; any
    evidence of dispatch, output, usage, cost, or identity drift stays fatal.
    """

    execution = candidate.get("execution")
    content = candidate.get("content")
    requested_identity = _canonical_proposer_recovery_identity(
        f"{str(candidate.get('requested_provider') or '')}:"
        f"{str(candidate.get('requested_model') or '')}"
    )
    blocked_identity = (
        _canonical_proposer_recovery_identity(execution.get("blocked_identity"))
        if isinstance(execution, Mapping)
        else ""
    )
    if (
        not expected_identity
        or requested_identity != expected_identity
        or blocked_identity != expected_identity
        or expected_identity not in excluded_identities
        or not isinstance(execution, Mapping)
        or set(execution)
        != {
            "request_started",
            "stream_closed",
            "blocked_reason",
            "blocked_identity",
        }
        or execution.get("request_started") is not False
        or execution.get("stream_closed") is not True
        or execution.get("blocked_reason") != "scope_failed_identity"
        or "physical_attempts" in execution
        or candidate.get("error_code") != "proposer_recovery_identity_excluded"
        or candidate.get("error")
        != ("proposer identity was excluded after an earlier failure in this retry scope")
        or candidate.get("ok") is not False
        or candidate.get("usable_for_aggregation") is not False
        or candidate.get("completion_outcome") != "failed"
        or candidate.get("request_started") is not False
        or candidate.get("stream_closed") is not True
        or candidate.get("physical_request_count") != 0
        or candidate.get("usage_reported") is not False
        or candidate.get("usage_missing_count") != 0
        or str(candidate.get("provider") or "")
        or str(candidate.get("model") or "")
        or str(candidate.get("stop_reason") or "")
        or candidate.get("elapsed_ms") != 0
        or candidate.get("ttft_ms") is not None
        or not isinstance(content, Mapping)
        or content.get("text") != ""
        or content.get("chars") != 0
        or content.get("truncated") is not False
        or str(content.get("sha256") or "")
        or str(candidate.get("text") or "")
        or any(
            candidate.get(field_name, 0) != 0
            for field_name in (
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cached_tokens",
                "cache_write_tokens",
            )
        )
        or not _strict_zero_number(candidate.get("billed_cost"))
        or candidate.get("cost_source") != "none"
        or candidate.get("model_usage_breakdown") not in (None, [])
        or candidate.get("diagnostic_model_usage_breakdown") not in (None, [])
        or candidate.get("provider_usage") not in (None, {})
        or candidate.get("billing_receipt") is not None
    ):
        return False
    return True


def _trace_output_binding_reasons(
    output: Any,
    *,
    label: str,
    require_nonempty: bool = True,
) -> tuple[str, int, list[str]]:
    if not isinstance(output, Mapping):
        return "", 0, [f"missing_{label}_binding"]
    output_text = output.get("text")
    raw_chars = output.get("chars")
    if (
        not isinstance(output_text, str)
        or not isinstance(raw_chars, int)
        or isinstance(raw_chars, bool)
        or raw_chars < 0
        or (require_nonempty and (raw_chars <= 0 or not output_text.strip()))
    ):
        return "", 0, [f"missing_{label}_binding"]
    truncated = output.get("truncated")
    if not isinstance(truncated, bool):
        return output_text, raw_chars, [f"invalid_{label}_truncation"]
    if (truncated and len(output_text) > raw_chars) or (
        not truncated and len(output_text) != raw_chars
    ):
        return output_text, raw_chars, [f"wrong_{label}_length"]
    output_hash = str(output.get("sha256") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", output_hash) is None:
        return output_text, raw_chars, [f"missing_{label}_hash"]
    if not truncated and output_hash != text_sha256(output_text):
        return output_text, raw_chars, [f"wrong_{label}_hash"]
    return output_text, raw_chars, []


def aggregator_output_reasons(
    call: Mapping[str, Any],
    final_request: Mapping[str, Any],
    *,
    final_text: str,
) -> list[str]:
    """Validate physical output separately from the assembled user response."""

    reasons: list[str] = []
    if call.get("output_binding_schema") != ENSEMBLE_OUTPUT_BINDING_SCHEMA:
        reasons.append("missing_aggregator_output_binding_schema")

    assembled = call.get("assembled_output")
    assembled_text, assembled_chars, assembled_reasons = _trace_output_binding_reasons(
        assembled,
        label="assembled_aggregator_output",
    )
    reasons.extend(assembled_reasons)
    if not assembled_reasons:
        if assembled_chars > len(final_text):
            reasons.append("wrong_assembled_aggregator_output_length")
        else:
            final_output_tail = final_text[-assembled_chars:]
            if isinstance(assembled, Mapping) and assembled.get("truncated") is True:
                if not final_output_tail.startswith(assembled_text):
                    reasons.append("wrong_assembled_aggregator_output_binding")
            elif assembled_text != final_output_tail:
                reasons.append("wrong_assembled_aggregator_output_binding")

    physical_output = final_request.get("output")
    _, _, physical_reasons = _trace_output_binding_reasons(
        physical_output,
        label="aggregator_physical_output",
    )
    reasons.extend(physical_reasons)

    recovery = call.get("aggregator_recovery")
    attempts = recovery.get("attempts") if isinstance(recovery, Mapping) else None
    attempt_by_number = (
        {
            attempt.get("attempt"): attempt
            for attempt in attempts
            if isinstance(attempt, Mapping)
            and isinstance(attempt.get("attempt"), int)
            and not isinstance(attempt.get("attempt"), bool)
        }
        if isinstance(attempts, list)
        else {}
    )
    selected_attempt = recovery.get("selected_attempt") if isinstance(recovery, Mapping) else None
    selected_kind = (
        str(recovery.get("selected_kind") or "") if isinstance(recovery, Mapping) else ""
    )
    components = call.get("output_components")
    if not isinstance(components, list) or not components:
        reasons.append("missing_aggregator_output_components")
        return list(dict.fromkeys(reasons))

    expected_start = 0
    selected_component: Mapping[str, Any] | None = None
    for component in components:
        if not isinstance(component, Mapping):
            reasons.append("invalid_aggregator_output_component")
            continue
        attempt_number = component.get("attempt")
        attempt = attempt_by_number.get(attempt_number)
        if not isinstance(attempt, Mapping):
            reasons.append("unknown_aggregator_output_component_attempt")
        else:
            if attempt.get("visible_output_emitted") is not True:
                reasons.append("nonvisible_aggregator_output_component_attempt")
            if str(component.get("kind") or "") != str(attempt.get("kind") or ""):
                reasons.append("aggregator_output_component_kind_mismatch")
            if component.get("fallback_index") != attempt.get("fallback_index"):
                reasons.append("aggregator_output_component_fallback_index_mismatch")
            if (
                str(component.get("requested_provider") or "").strip().casefold()
                != str(attempt.get("requested_provider") or "").strip().casefold()
                or str(component.get("requested_model") or "").strip()
                != str(attempt.get("requested_model") or "").strip()
            ):
                reasons.append("aggregator_output_component_identity_mismatch")

        raw_start = component.get("assembled_start")
        raw_end = component.get("assembled_end")
        if (
            not isinstance(raw_start, int)
            or isinstance(raw_start, bool)
            or not isinstance(raw_end, int)
            or isinstance(raw_end, bool)
            or raw_start != expected_start
            or raw_end <= raw_start
        ):
            reasons.append("noncontiguous_aggregator_output_components")
            continue
        contribution = component.get("assembled_contribution")
        contribution_text, contribution_chars, contribution_reasons = _trace_output_binding_reasons(
            contribution,
            label="aggregator_output_component",
        )
        reasons.extend(contribution_reasons)
        if contribution_chars != raw_end - raw_start:
            reasons.append("aggregator_output_component_length_mismatch")
        if (
            not contribution_reasons
            and isinstance(assembled, Mapping)
            and assembled.get("truncated") is False
            and isinstance(contribution, Mapping)
            and contribution.get("truncated") is False
            and assembled_text[raw_start:raw_end] != contribution_text
        ):
            reasons.append("wrong_aggregator_output_component_binding")
        _, _, component_physical_reasons = _trace_output_binding_reasons(
            component.get("physical_output"),
            label="aggregator_component_physical_output",
        )
        reasons.extend(component_physical_reasons)
        prefix_hash = str(component.get("assembled_prefix_sha256") or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", prefix_hash) is None:
            reasons.append("missing_aggregator_output_component_prefix_hash")
        expected_start = raw_end
        if attempt_number == selected_attempt:
            selected_component = component

    if expected_start != assembled_chars:
        reasons.append("incomplete_aggregator_output_components")
    if (
        components
        and isinstance(components[-1], Mapping)
        and isinstance(assembled, Mapping)
        and str(components[-1].get("assembled_prefix_sha256") or "").strip().lower()
        != str(assembled.get("sha256") or "").strip().lower()
    ):
        reasons.append("wrong_aggregator_output_component_prefix_hash")
    if selected_component is None:
        reasons.append("missing_selected_aggregator_output_component")
    elif selected_component.get("physical_output") != physical_output:
        reasons.append("wrong_selected_aggregator_physical_output_binding")
    if selected_kind in {"continuation", "continuation_fallback"}:
        selected_start = (
            selected_component.get("assembled_start")
            if isinstance(selected_component, Mapping)
            else 0
        )
        if len(components) < 2 or not isinstance(selected_start, int) or selected_start <= 0:
            reasons.append("missing_prior_aggregator_output_component")
    return list(dict.fromkeys(reasons))


_STRUCTURAL_AGGREGATOR_RECOVERY_TRIGGERS = frozenset(
    {
        "incomplete_stream",
        "provider_build_failed",
        "member_unavailable",
        "reasoning_only_length",
        "empty_length",
        "reasoning_only_terminal",
        "empty_terminal",
        "visible_length",
        "visible_length_continuations_exhausted",
        "continuation_failed",
        "aggregator_error",
        "ensemble_aggregator_incomplete",
    }
)
_SEMANTIC_AGGREGATOR_FALLBACK_MARKERS = (
    "judge",
    "score",
    "quality",
    "rubric",
    "verdict",
    "reward",
)
_FORMAL_AGGREGATOR_SELECTED_KINDS = frozenset(
    {
        "primary",
        "continuation",
        "same_model_recovery",
        "model_fallback",
        "continuation_fallback",
        "partial_salvage",
        "degraded_delivery",
    }
)
_FORMAL_AGGREGATOR_ATTEMPT_OUTCOMES = frozenset(
    {
        "succeeded",
        "degraded_success",
        "partial_usable",
        "abandoned",
        "failed",
        "provider_build_failed",
        "member_unavailable",
    }
)


def structural_aggregator_recovery_trigger(value: Any) -> bool:
    """Return whether a fallback trigger describes an execution failure."""

    trigger = str(value or "").strip().casefold()
    if not trigger or any(marker in trigger for marker in _SEMANTIC_AGGREGATOR_FALLBACK_MARKERS):
        return False
    return bool(
        trigger in _STRUCTURAL_AGGREGATOR_RECOVERY_TRIGGERS
        or trigger.startswith(
            (
                "ensemble_aggregator_",
                "provider_",
                "stream_",
                "http_",
                "timeout",
            )
        )
        or re.fullmatch(r"[45]\d\d", trigger)
    )


def _request_identity_reasons(
    request: Mapping[str, Any],
    *,
    expected_identity: str,
    label: str,
    allow_unknown_usage: bool = False,
) -> list[str]:
    """Bind requested and actual request identity to one frozen candidate."""

    reasons: list[str] = []
    expected_provider, separator, expected_model = expected_identity.partition(":")
    if not separator or not expected_provider or not expected_model:
        return [f"invalid_{label}_expected_identity"]
    usage = request.get("usage")
    execution = request.get("execution")
    if not isinstance(usage, Mapping):
        reasons.append(f"missing_{label}_usage")
    if not isinstance(execution, Mapping):
        reasons.append(f"missing_{label}_execution")
    for source_name, source in (("usage", usage), ("execution", execution)):
        if not isinstance(source, Mapping):
            continue
        requested_provider = str(source.get("requested_provider") or "").strip().casefold()
        requested_model = str(source.get("requested_model") or "").strip()
        actual_provider = (
            str(source.get("provider") or source.get("actual_provider") or "").strip().casefold()
        )
        actual_model = str(source.get("model") or source.get("actual_model") or "").strip()
        if requested_provider != expected_provider.casefold() or requested_model != expected_model:
            reasons.append(f"wrong_{label}_{source_name}_requested_identity")
        provider_usage = source.get("provider_usage")
        explicit_unknown_usage = bool(
            allow_unknown_usage
            and source_name == "usage"
            and not actual_provider
            and not actual_model
            and str(source.get("role") or "").strip().casefold() in MISSING_USAGE_PLACEHOLDER_ROLES
            and source.get("usage_unknown") is True
            and isinstance(provider_usage, Mapping)
            and provider_usage.get("usage_unknown") is True
        )
        if not explicit_unknown_usage and (
            actual_provider != expected_provider.casefold() or actual_model != expected_model
        ):
            reasons.append(f"wrong_{label}_{source_name}_actual_identity")
    return reasons


def aggregator_recovery_execution_reasons(
    call: Mapping[str, Any],
    *,
    expected_aggregator: str,
    expected_policy: Mapping[str, Any] = FORMAL_AGGREGATOR_RECOVERY_POLICY,
) -> tuple[str, list[str]]:
    """Validate recovery evidence and return the physically executed model."""

    reasons: list[str] = []
    primary_identity = f"openrouter:{expected_aggregator}"
    plan = call.get("selection_plan")
    recovery = call.get("aggregator_recovery")
    raw_candidates = plan.get("aggregator_candidates") if isinstance(plan, Mapping) else None
    if raw_candidates is None and isinstance(recovery, Mapping):
        # Static B2 plans name one aggregator but historically store the
        # concrete serving chain in the execution receipt.  That receipt is
        # the authoritative physical evidence; dynamic G1 separately requires
        # its ranked candidate chain in the frozen selection plan.
        raw_candidates = recovery.get("candidate_ids")
    if raw_candidates is None:
        reasons.append("missing_aggregator_candidate_chain")
        candidate_chain = (primary_identity,)
    elif (
        not isinstance(raw_candidates, list)
        or not raw_candidates
        or any(not isinstance(item, str) or not item.strip() for item in raw_candidates)
    ):
        reasons.append("invalid_aggregator_candidate_chain")
        candidate_chain = (primary_identity,)
    else:
        candidate_chain = tuple(str(item).strip() for item in raw_candidates)
        if candidate_chain[0] != primary_identity:
            reasons.append("aggregator_candidate_chain_primary_mismatch")
        if len(candidate_chain) > 3 or len(set(candidate_chain)) != len(candidate_chain):
            reasons.append("invalid_aggregator_candidate_chain")

    fallback_used = call.get("fallback_used")
    if not isinstance(fallback_used, bool):
        reasons.append("aggregator_fallback_used_or_unknown")
        fallback_used = False
    executed_a = str(call.get("executed_A") or "").strip()
    if not isinstance(recovery, Mapping):
        reasons.append("missing_aggregator_recovery_evidence")
        if executed_a and executed_a != primary_identity:
            reasons.append("unexpected_aggregator_execution_identity")
        return expected_aggregator, reasons

    if recovery.get("schema") != "opensquilla.ensemble-aggregator-recovery/v1":
        reasons.append("wrong_aggregator_recovery_schema")
    if recovery.get("mode") != expected_policy.get("aggregator_recovery_mode"):
        reasons.append("wrong_aggregator_recovery_mode")
    if recovery.get("max_tokens_cap") != expected_policy.get("aggregator_max_tokens_cap"):
        reasons.append("wrong_aggregator_recovery_max_tokens_cap")
    if recovery.get("visible_answer_reserve_tokens") != expected_policy.get(
        "aggregator_visible_answer_reserve_tokens"
    ):
        reasons.append("wrong_aggregator_recovery_visible_answer_reserve_tokens")
    analyzer_failure_recovery_budget: int | None = None
    if isinstance(plan, Mapping) and plan.get("analyzer_failure_fallback") is True:
        analyzer_failure_recovery_budget = 1
        if plan.get(
            "aggregator_max_recovery_actions"
        ) != analyzer_failure_recovery_budget or recovery.get("max_recovery_actions") != plan.get(
            "aggregator_max_recovery_actions"
        ):
            reasons.append("aggregator_recovery_max_actions_mismatch")
    recovery_candidates = recovery.get("candidate_ids")
    if recovery_candidates != list(candidate_chain):
        reasons.append("aggregator_recovery_candidates_mismatch")
    if nonnegative_int(recovery.get("candidate_count")) != len(candidate_chain):
        reasons.append("wrong_aggregator_recovery_candidate_count")
    if recovery.get("proposer_reused") is not True:
        reasons.append("aggregator_recovery_did_not_reuse_proposers")
    assembled_output = call.get("assembled_output")
    delivery_outcome = str(call.get("delivery_outcome") or "")
    explicit_degraded_attempt = explicit_degraded_call_attempt(call)
    declared_degraded_delivery = explicit_degraded_attempt is not None
    degraded_visible_answer = bool(
        declared_degraded_delivery
        and isinstance(assembled_output, Mapping)
        and nonnegative_int(assembled_output.get("chars")) > 0
        and (
            bool(str(assembled_output.get("text") or "").strip())
            or HEX64.fullmatch(str(assembled_output.get("sha256") or "")) is not None
            or SHA256_VALUE.fullmatch(str(assembled_output.get("sha256") or "")) is not None
        )
    )
    if recovery.get("success") is not True and not degraded_visible_answer:
        reasons.append("aggregator_recovery_not_successful")
    if recovery.get("degraded") is True and not degraded_visible_answer:
        reasons.append("degraded_aggregator_recovery_not_formal")
    if delivery_outcome != "complete" and not (
        degraded_visible_answer and delivery_outcome in {"partial_usable", "degraded_success"}
    ):
        reasons.append("aggregator_delivery_not_complete")

    raw_fallback_index = recovery.get("fallback_index")
    if not isinstance(raw_fallback_index, int) or isinstance(raw_fallback_index, bool):
        reasons.append("invalid_aggregator_fallback_index")
    fallback_index = nonnegative_int(raw_fallback_index)
    if fallback_index >= len(candidate_chain):
        reasons.append("aggregator_fallback_index_outside_candidate_chain")
        fallback_index = 0
    if fallback_used != (fallback_index > 0):
        reasons.append("aggregator_fallback_flag_index_mismatch")
    if fallback_index > 2:
        reasons.append("aggregator_fallback_index_outside_top3")
    expected_identity = candidate_chain[fallback_index]
    expected_model = expected_identity.partition(":")[2] or expected_aggregator
    recovery_executed = str(recovery.get("executed_A") or "").strip()
    if executed_a != expected_identity or recovery_executed != expected_identity:
        reasons.append("aggregator_executed_identity_mismatch")

    fallback_reason = str(recovery.get("fallback_reason") or "").strip()
    call_fallback_reason = str(call.get("fallback_reason") or "").strip()
    selected_kind = str(recovery.get("selected_kind") or "").strip()
    if selected_kind not in _FORMAL_AGGREGATOR_SELECTED_KINDS:
        reasons.append("invalid_aggregator_recovery_selected_kind")
    expected_run_outcomes = (
        {str(recovery.get("run_outcome") or "")}
        if explicit_degraded_attempt is not None
        else {"success"}
        if selected_kind == "primary"
        else {"aggregator_recovered"}
    )
    if str(call.get("run_outcome") or "") not in expected_run_outcomes:
        reasons.append("aggregator_run_outcome_selected_kind_mismatch")
    continuation_count = recovery.get("continuation_count")
    same_model_recovery_count = recovery.get("same_model_recovery_count")
    if (
        not isinstance(continuation_count, int)
        or isinstance(continuation_count, bool)
        or continuation_count < 0
    ):
        reasons.append("invalid_aggregator_recovery_continuation_count")
    if (
        not isinstance(same_model_recovery_count, int)
        or isinstance(same_model_recovery_count, bool)
        or same_model_recovery_count < 0
    ):
        reasons.append("invalid_aggregator_same_model_recovery_count")
    if selected_kind == "continuation" and nonnegative_int(continuation_count) < 1:
        reasons.append("aggregator_continuation_count_selected_kind_mismatch")
    if selected_kind == "same_model_recovery" and nonnegative_int(same_model_recovery_count) < 1:
        reasons.append("aggregator_same_model_count_selected_kind_mismatch")
    if selected_kind == "model_fallback" and fallback_index == 0:
        reasons.append("aggregator_model_fallback_index_mismatch")
    if selected_kind == "continuation_fallback":
        if fallback_index == 0:
            reasons.append("aggregator_continuation_fallback_index_mismatch")
        if nonnegative_int(continuation_count) < 1:
            reasons.append("aggregator_continuation_fallback_count_mismatch")
    if (
        selected_kind != "primary"
        and not (
            degraded_visible_answer and selected_kind in {"partial_salvage", "degraded_delivery"}
        )
        and not structural_aggregator_recovery_trigger(fallback_reason)
    ):
        reasons.append("nonstructural_aggregator_recovery_trigger")
    if fallback_index > 0:
        if fallback_reason != call_fallback_reason:
            reasons.append("aggregator_fallback_reason_mismatch")
        if not structural_aggregator_recovery_trigger(fallback_reason):
            reasons.append("nonstructural_aggregator_fallback_trigger")
    elif call_fallback_reason:
        reasons.append("unexpected_aggregator_fallback_reason")

    attempts = recovery.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        reasons.append("missing_aggregator_recovery_attempts")
        attempts = []
    attempt_numbers: list[int] = []
    physical_attempt_spans: list[tuple[int, int]] = []
    failed_candidate_indexes: set[int] = set()
    for attempt_row in attempts:
        if not isinstance(attempt_row, Mapping):
            reasons.append("invalid_aggregator_recovery_attempt")
            continue
        raw_attempt_number = attempt_row.get("attempt")
        if (
            not isinstance(raw_attempt_number, int)
            or isinstance(raw_attempt_number, bool)
            or raw_attempt_number <= 0
        ):
            reasons.append("invalid_aggregator_recovery_attempt_number")
        else:
            attempt_numbers.append(raw_attempt_number)
        attempt_kind = str(attempt_row.get("kind") or "").strip()
        if attempt_kind not in _FORMAL_AGGREGATOR_SELECTED_KINDS:
            reasons.append("invalid_aggregator_recovery_attempt_kind")
        outcome = str(attempt_row.get("outcome") or "").strip()
        if outcome not in _FORMAL_AGGREGATOR_ATTEMPT_OUTCOMES:
            reasons.append("invalid_aggregator_recovery_attempt_outcome")
        raw_attempt_index = attempt_row.get("fallback_index")
        if not isinstance(raw_attempt_index, int) or isinstance(raw_attempt_index, bool):
            reasons.append("invalid_aggregator_recovery_attempt_fallback_index")
        attempt_index = nonnegative_int(raw_attempt_index)
        if attempt_index >= len(candidate_chain):
            reasons.append("aggregator_recovery_attempt_index_outside_candidate_chain")
            continue
        requested_identity = (
            f"{str(attempt_row.get('requested_provider') or '').strip().casefold()}:"
            f"{str(attempt_row.get('requested_model') or '').strip()}"
        )
        if requested_identity != candidate_chain[attempt_index]:
            reasons.append("aggregator_recovery_attempt_identity_mismatch")
        request_started = attempt_row.get("request_started")
        if not isinstance(request_started, bool):
            reasons.append("invalid_aggregator_recovery_attempt_started")
            request_started = False
        raw_physical_index = attempt_row.get("physical_attempt_index")
        raw_attempt_physical_count = attempt_row.get("physical_request_count")
        if request_started:
            if (
                not isinstance(raw_physical_index, int)
                or isinstance(raw_physical_index, bool)
                or raw_physical_index <= 0
            ):
                reasons.append("invalid_aggregator_physical_attempt_index")
            if (
                not isinstance(raw_attempt_physical_count, int)
                or isinstance(raw_attempt_physical_count, bool)
                or raw_attempt_physical_count <= 0
            ):
                reasons.append("invalid_aggregator_attempt_physical_request_count")
            elif isinstance(raw_physical_index, int) and not isinstance(raw_physical_index, bool):
                physical_attempt_spans.append((raw_physical_index, raw_attempt_physical_count))
        elif raw_physical_index is not None:
            reasons.append("unstarted_aggregator_attempt_has_physical_index")
        elif raw_attempt_physical_count != 0:
            reasons.append("unstarted_aggregator_attempt_has_physical_request")
        if outcome == "succeeded" and request_started is not True:
            reasons.append("unstarted_aggregator_attempt_succeeded")
        trigger = str(attempt_row.get("trigger") or "").strip()
        if outcome != "succeeded":
            if trigger and not structural_aggregator_recovery_trigger(trigger):
                reasons.append("nonstructural_aggregator_recovery_attempt")
            if outcome in {"abandoned", "failed", "provider_build_failed", "member_unavailable"}:
                failed_candidate_indexes.add(attempt_index)
    if (
        attempt_numbers != sorted(attempt_numbers)
        or len(set(attempt_numbers)) != len(attempt_numbers)
        or len(attempt_numbers) != len(attempts)
    ):
        reasons.append("invalid_aggregator_recovery_attempt_sequence")
    if analyzer_failure_recovery_budget is not None:
        started_physical_requests = sum(
            nonnegative_int(attempt.get("physical_request_count"))
            for attempt in attempts
            if isinstance(attempt, Mapping) and attempt.get("request_started") is True
        )
        declared_recovery_actions = nonnegative_int(continuation_count) + nonnegative_int(
            same_model_recovery_count
        )
        if (
            max(0, started_physical_requests - 1) > analyzer_failure_recovery_budget
            or declared_recovery_actions > analyzer_failure_recovery_budget
        ):
            reasons.append("aggregator_recovery_action_budget_exceeded")
    expected_physical_index = 1
    for physical_index, physical_count in physical_attempt_spans:
        if physical_index != expected_physical_index:
            reasons.append("invalid_aggregator_physical_attempt_sequence")
            break
        expected_physical_index += physical_count

    raw_selected_attempt = recovery.get("selected_attempt")
    if (
        not isinstance(raw_selected_attempt, int)
        or isinstance(raw_selected_attempt, bool)
        or raw_selected_attempt <= 0
    ):
        reasons.append("invalid_aggregator_recovery_selected_attempt")
    selected_attempt = nonnegative_int(raw_selected_attempt)
    selected_rows = [
        attempt
        for attempt in attempts
        if isinstance(attempt, Mapping)
        and attempt.get("attempt") == selected_attempt
        and nonnegative_int(attempt.get("fallback_index")) == fallback_index
        and (attempt.get("outcome") == "succeeded" or explicit_degraded_attempt is attempt)
        and attempt.get("request_started") is True
    ]
    usable_physical_rows = [
        attempt
        for attempt in attempts
        if isinstance(attempt, Mapping)
        and (attempt.get("outcome") == "succeeded" or explicit_degraded_attempt is attempt)
        and attempt.get("request_started") is True
    ]
    if len(usable_physical_rows) != 1:
        reasons.append("ambiguous_aggregator_recovery_successful_attempt")
    if len(selected_rows) != 1:
        reasons.append("ambiguous_aggregator_recovery_selected_attempt")
    else:
        selected_row = selected_rows[0]
        if selected_row.get("kind") != selected_kind and not (
            explicit_degraded_attempt is selected_row
            and selected_kind == "degraded_delivery"
            and selected_row.get("kind") in _FORMAL_AGGREGATOR_SELECTED_KINDS
        ):
            reasons.append("aggregator_recovery_selected_attempt_kind_mismatch")
        if selected_row.get("stream_closed") is not True:
            reasons.append("aggregator_recovery_selected_stream_not_closed")
        requested_identity = (
            f"{str(selected_row.get('requested_provider') or '').strip().casefold()}:"
            f"{str(selected_row.get('requested_model') or '').strip()}"
        )
        actual_identity = (
            f"{str(selected_row.get('actual_provider') or '').strip().casefold()}:"
            f"{str(selected_row.get('actual_model') or '').strip()}"
        )
        if requested_identity != expected_identity or actual_identity != expected_identity:
            reasons.append("aggregator_recovery_selected_attempt_identity_mismatch")
        selected_trigger = str(selected_row.get("trigger") or "").strip()
        if selected_kind != "primary" and selected_trigger != fallback_reason:
            reasons.append("aggregator_recovery_selected_attempt_trigger_mismatch")

    if fallback_index > 0:
        if 0 not in failed_candidate_indexes:
            reasons.append("missing_structural_primary_aggregator_failure")
        if any(index not in failed_candidate_indexes for index in range(fallback_index)):
            reasons.append("aggregator_fallback_skipped_ranked_candidate")

    final_request = call.get("final_request")
    if isinstance(final_request, Mapping):
        reasons.extend(
            _request_identity_reasons(
                final_request,
                expected_identity=expected_identity,
                label="final_aggregator",
                allow_unknown_usage=explicit_degraded_attempt is not None,
            )
        )
    return expected_model, list(dict.fromkeys(reasons))


def _canonical_proposer_recovery_identity(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    identity = value
    if (
        not identity
        or identity != identity.strip()
        or any(character.isspace() for character in identity)
    ):
        return ""
    provider, separator, model = identity.partition(":")
    if (
        not separator
        or not provider
        or provider != provider.casefold()
        or not model
        or any(not segment for segment in model.split(":"))
    ):
        return ""
    return identity


def expanded_proposer_slot_identities(
    plan: Mapping[str, Any],
) -> tuple[tuple[str, ...], list[str]]:
    """Resolve every runtime proposer sample slot to its frozen identity.

    ``selected_P`` lists distinct ranking choices, whereas ``proposer_models``
    lists physical proposer samples after each selected member's ``k`` was
    expanded. Recovery receipts are indexed by those physical sample slots.
    Require the exact block expansion emitted by the provider: every selected
    identity appears at least once, in ranking order.
    """

    expanded = provider_retry_expanded_proposer_identities(plan)
    if not expanded:
        return (), ["invalid_expanded_proposer_sample_roster"]
    return expanded, []


def proposer_recovery_execution_reasons(
    call: Mapping[str, Any],
    *,
    executed_plan: Mapping[str, Any],
    expected_policy: Mapping[str, Any] = FORMAL_PROPOSER_RECOVERY_POLICY,
) -> tuple[dict[int, str], set[str], list[str]]:
    """Validate one self-contained provider-owned recovery receipt."""

    policy = executed_plan.get("proposer_recovery_policy")
    receipt = call.get("proposer_recovery")
    expected_configured_backup_count = expected_policy.get("configured_backup_count")
    actual_effective_backup_count = (
        policy.get("effective_backup_count") if isinstance(policy, Mapping) else None
    )
    effective_backup_count_valid = bool(
        isinstance(expected_configured_backup_count, int)
        and not isinstance(expected_configured_backup_count, bool)
        and isinstance(actual_effective_backup_count, int)
        and not isinstance(actual_effective_backup_count, bool)
        and 0 <= actual_effective_backup_count <= expected_configured_backup_count
    )
    expected_plan_policy = dict(expected_policy)
    if effective_backup_count_valid:
        expected_plan_policy["effective_backup_count"] = actual_effective_backup_count
    expected_backup_count = actual_effective_backup_count if effective_backup_count_valid else -1
    expected_max_additional_requests = nonnegative_int(
        expected_policy.get("max_additional_physical_requests")
    )
    expected_quorum = nonnegative_int(expected_policy.get("quorum_required"))
    complete_proposers_only = bool(
        executed_plan.get("analyzer_failure_fallback") is True
        and executed_plan.get("complete_proposers_only") is True
        and expected_quorum == 1
    )

    def strict_successful_candidate(candidate: Any) -> bool:
        return bool(
            successful_candidate(candidate)
            and (not complete_proposers_only or candidate.get("completion_outcome") == "complete")
        )

    if policy is None:
        return (
            {},
            set(),
            ["unexpected_proposer_recovery_receipt"] if receipt is not None else [],
        )

    reasons: list[str] = []
    if not isinstance(policy, Mapping) or dict(policy) != expected_plan_policy:
        reasons.append("invalid_proposer_recovery_policy")
    selected = executed_plan.get("selected_P")
    backups = executed_plan.get("backup_P")
    aggregators = executed_plan.get("aggregator_candidates")
    selected_identities = (
        [_canonical_proposer_recovery_identity(value) for value in selected]
        if isinstance(selected, list)
        else []
    )
    backup_identities = (
        [_canonical_proposer_recovery_identity(value) for value in backups]
        if isinstance(backups, list)
        else []
    )
    aggregator_identities = (
        [_canonical_proposer_recovery_identity(value) for value in aggregators]
        if isinstance(aggregators, list)
        else []
    )
    if (
        not selected_identities
        or any(not identity for identity in selected_identities)
        or len(set(selected_identities)) != len(selected_identities)
        or len(backup_identities) != expected_backup_count
        or any(not identity for identity in backup_identities)
        or len(set(backup_identities)) != expected_backup_count
        or bool(set(selected_identities).intersection(backup_identities))
        or bool(set(aggregator_identities).intersection(backup_identities))
        or executed_plan.get("effective_min_successful_proposers") != expected_quorum
    ):
        reasons.append("invalid_proposer_recovery_roster")
    expanded_slot_identities, expanded_slot_reasons = expanded_proposer_slot_identities(
        executed_plan
    )
    reasons.extend(expanded_slot_reasons)

    try:
        from opensquilla.provider.protocol import (
            provider_retry_roster_fingerprint,
        )

        expected_fingerprint = provider_retry_roster_fingerprint(executed_plan)
    except (TypeError, ValueError):
        expected_fingerprint = ""
    if HEX64.fullmatch(expected_fingerprint) is None:
        reasons.append("invalid_proposer_recovery_fingerprint")

    if not isinstance(receipt, Mapping):
        reasons.append("missing_proposer_recovery_receipt")
        return {}, set(), list(dict.fromkeys(reasons))
    started_count = receipt.get("additional_physical_requests_started")
    remaining_count = receipt.get("remaining_additional_physical_requests")
    receipt_attempts = receipt.get("attempts")
    if (
        receipt.get("schema") != FORMAL_PROPOSER_RECOVERY_SCHEMA
        or receipt.get("selection_plan_fingerprint") != expected_fingerprint
        or receipt.get("scope") != "run_turn"
        or not str(receipt.get("scope_id") or "").strip()
        or receipt.get("max_additional_physical_requests") != expected_max_additional_requests
        or receipt.get("external_physical_requests_reserved") != 0
        or isinstance(started_count, bool)
        or not isinstance(started_count, int)
        or not 0 <= started_count <= expected_max_additional_requests
        or isinstance(remaining_count, bool)
        or not isinstance(remaining_count, int)
        or remaining_count != expected_max_additional_requests - started_count
        or receipt.get("quorum_required") != expected_quorum
        or type(receipt.get("quorum_reached")) is not bool
        or not isinstance(receipt_attempts, list)
    ):
        reasons.append("invalid_proposer_recovery_receipt")
        receipt_attempts = receipt_attempts if isinstance(receipt_attempts, list) else []
    declared_recovery_slot_by_physical_id = {
        physical_id: int(attempt["slot_index"])
        for attempt in receipt_attempts
        if isinstance(attempt, Mapping)
        and attempt.get("request_started") is True
        and isinstance(attempt.get("physical_request_count"), int)
        and not isinstance(attempt.get("physical_request_count"), bool)
        and attempt.get("physical_request_count") == 1
        and isinstance(attempt.get("slot_index"), int)
        and not isinstance(attempt.get("slot_index"), bool)
        and 0 <= int(attempt["slot_index"]) < len(expanded_slot_identities)
        and HEX32.fullmatch(physical_id := str(attempt.get("physical_attempt_id") or ""))
        is not None
    }

    cleanup_bypass = receipt.get("cleanup_quorum_bypass")
    cleanup_bypass_indexes: set[int] = set()
    cleanup_bypass_physical_ids: set[str] = set()
    if cleanup_bypass is not None:
        raw_indexes = (
            cleanup_bypass.get("candidate_indexes") if isinstance(cleanup_bypass, Mapping) else None
        )
        raw_physical_ids = (
            cleanup_bypass.get("physical_attempt_ids")
            if isinstance(cleanup_bypass, Mapping)
            else None
        )
        cleanup_bypass_indexes = (
            set(raw_indexes)
            if isinstance(raw_indexes, list)
            and all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in raw_indexes
            )
            else set()
        )
        cleanup_bypass_physical_ids = (
            set(raw_physical_ids)
            if isinstance(raw_physical_ids, list)
            and all(
                isinstance(value, str) and HEX32.fullmatch(value) is not None
                for value in raw_physical_ids
            )
            else set()
        )
        if (
            not isinstance(cleanup_bypass, Mapping)
            or cleanup_bypass.get("schema")
            != "opensquilla.router-dynamic-proposer-cleanup-quorum-bypass/v1"
            or cleanup_bypass.get("applied") is not True
            or cleanup_bypass.get("quorum_required") != expected_quorum
            or not isinstance(
                cleanup_bypass.get("successful_proposers"),
                int,
            )
            or isinstance(
                cleanup_bypass.get("successful_proposers"),
                bool,
            )
            or cleanup_bypass.get("successful_proposers") < expected_quorum
            or not isinstance(raw_indexes, list)
            or raw_indexes != sorted(cleanup_bypass_indexes)
            or not cleanup_bypass_indexes
            or not isinstance(raw_physical_ids, list)
            or raw_physical_ids != list(dict.fromkeys(raw_physical_ids))
            or not cleanup_bypass_physical_ids
            or cleanup_bypass.get("recovery_skipped") is not True
            or cleanup_bypass.get("aggregator_tools_disabled") is not True
            or receipt.get("terminal_code")
            or receipt.get("scope_terminal_code")
        ):
            reasons.append("invalid_proposer_cleanup_quorum_bypass")

    candidates = call.get("candidates")
    final_identity_by_slot: dict[int, str] = {}
    physical_identity_by_id: dict[str, str] = {}
    candidate_slot_by_physical_id: dict[str, int] = {}
    all_candidate_ids: list[str] = []
    candidate_physical_attempts_by_slot: dict[
        int,
        list[Mapping[str, Any]],
    ] = {}
    raw_excluded_identities = receipt.get("cumulative_excluded_identities")
    excluded_identities = (
        {
            identity
            for value in raw_excluded_identities
            if (identity := _canonical_proposer_recovery_identity(value))
        }
        if isinstance(raw_excluded_identities, list)
        else set()
    )
    observed_unclosed_indexes: set[int] = set()
    observed_unclosed_physical_ids: set[str] = set()
    if not isinstance(candidates, list) or len(candidates) != len(expanded_slot_identities):
        reasons.append("invalid_proposer_recovery_candidate_slots")
        candidates = candidates if isinstance(candidates, list) else []
    for slot_index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            reasons.append("invalid_proposer_recovery_candidate")
            continue
        requested_identity = _canonical_proposer_recovery_identity(
            f"{str(candidate.get('requested_provider') or '')}:"
            f"{str(candidate.get('requested_model') or '')}"
        )
        allowed_slot_identities = (
            {
                expanded_slot_identities[slot_index],
                *backup_identities,
            }
            if slot_index < len(expanded_slot_identities)
            else set(backup_identities)
        )
        execution = candidate.get("execution")
        physical_attempts = (
            execution.get("physical_attempts") if isinstance(execution, Mapping) else None
        )
        physical_count = candidate.get("physical_request_count")
        request_started = candidate.get("request_started")
        if not isinstance(physical_attempts, list) and _legacy_excluded_zero_request_candidate(
            candidate,
            expected_identity=requested_identity,
            excluded_identities=excluded_identities.intersection(allowed_slot_identities),
        ):
            physical_attempts = []
        if (
            type(request_started) is not bool
            or isinstance(physical_count, bool)
            or not isinstance(physical_count, int)
            or physical_count < 0
            or not isinstance(physical_attempts, list)
            or len(physical_attempts) != physical_count
            or request_started is not bool(physical_count)
        ):
            reasons.append("invalid_proposer_recovery_physical_ledger")
            continue
        slot_identities: list[str] = []
        valid_slot_attempts: list[Mapping[str, Any]] = []
        for ordinal, physical in enumerate(physical_attempts, start=1):
            physical_id = (
                str(physical.get("physical_attempt_id") or "")
                if isinstance(physical, Mapping)
                else ""
            )
            identity = (
                _canonical_proposer_recovery_identity(physical.get("identity"))
                if isinstance(physical, Mapping)
                else ""
            )
            stream_closed = physical.get("stream_closed") if isinstance(physical, Mapping) else None
            physical_attempt_ordinal = (
                physical.get("attempt") if isinstance(physical, Mapping) else None
            )
            receipt_bound_local_ordinal = bool(
                isinstance(physical_attempt_ordinal, int)
                and not isinstance(physical_attempt_ordinal, bool)
                and physical_attempt_ordinal == 1
                and declared_recovery_slot_by_physical_id.get(physical_id) == slot_index
            )
            quarantined_unclosed = bool(
                isinstance(physical, Mapping)
                and stream_closed is not True
                and slot_index in cleanup_bypass_indexes
                and physical_id in cleanup_bypass_physical_ids
                and not successful_candidate(candidate)
                and candidate.get("error_code") == "ensemble_proposer_close_timeout"
                and candidate.get("stream_closed") is False
                and physical.get("outcome") in {"interrupted", "failed", "cleanup_unproven"}
            )
            if stream_closed is not True and not quarantined_unclosed:
                reasons.append("proposer_recovery_stream_not_closed")
            if (
                not isinstance(physical, Mapping)
                or not isinstance(physical_attempt_ordinal, int)
                or isinstance(physical_attempt_ordinal, bool)
                or (physical_attempt_ordinal != ordinal and not receipt_bound_local_ordinal)
                or physical.get("request_started") is not True
                or stream_closed is not True
                and not quarantined_unclosed
                or HEX32.fullmatch(physical_id) is None
                or not identity
            ):
                reasons.append("invalid_proposer_recovery_physical_attempt")
                continue
            if quarantined_unclosed:
                observed_unclosed_indexes.add(slot_index)
                observed_unclosed_physical_ids.add(physical_id)
            slot_identities.append(identity)
            all_candidate_ids.append(physical_id)
            physical_identity_by_id[physical_id] = identity
            candidate_slot_by_physical_id[physical_id] = slot_index
            valid_slot_attempts.append(physical)
        candidate_physical_attempts_by_slot[slot_index] = valid_slot_attempts
        actual_identity = _canonical_proposer_recovery_identity(
            f"{str(candidate.get('provider') or '')}:{str(candidate.get('model') or '')}"
        )
        strict_candidate = strict_successful_candidate(candidate)
        partial_candidate = partial_usable_candidate(candidate)
        if (
            not requested_identity
            or requested_identity not in allowed_slot_identities
            or (strict_candidate and actual_identity != requested_identity)
            or (partial_candidate and actual_identity and actual_identity != requested_identity)
            or (partial_candidate and not actual_identity and not slot_identities)
            or (slot_identities and slot_identities[-1] != requested_identity)
        ):
            reasons.append("wrong_proposer_recovery_final_identity")
        elif slot_index < len(expanded_slot_identities):
            final_identity_by_slot[slot_index] = requested_identity
    if len(all_candidate_ids) != len(set(all_candidate_ids)) or len(physical_identity_by_id) != len(
        all_candidate_ids
    ):
        reasons.append("duplicate_proposer_recovery_physical_attempt_id")
    if any(
        physical_id in candidate_slot_by_physical_id
        and candidate_slot_by_physical_id[physical_id] != expected_slot
        for physical_id, expected_slot in declared_recovery_slot_by_physical_id.items()
    ):
        reasons.append("proposer_recovery_candidate_slot_mismatch")
    if cleanup_bypass is not None and (
        observed_unclosed_indexes != cleanup_bypass_indexes
        or observed_unclosed_physical_ids != cleanup_bypass_physical_ids
    ):
        reasons.append("proposer_cleanup_quorum_bypass_mismatch")

    normalized_attempts: list[Mapping[str, Any]] = []
    receipt_started_total = 0
    receipt_physical_ids: list[str] = []
    backup_targets: list[str] = []
    for sequence, attempt in enumerate(receipt_attempts, start=1):
        if not isinstance(attempt, Mapping):
            reasons.append("invalid_proposer_recovery_attempt")
            continue
        normalized_attempts.append(attempt)
        slot_index = attempt.get("slot_index")
        kind = str(attempt.get("kind") or "")
        source_identity = _canonical_proposer_recovery_identity(attempt.get("source_identity"))
        target_identity = _canonical_proposer_recovery_identity(attempt.get("target_identity"))
        request_started = attempt.get("request_started")
        physical_count = attempt.get("physical_request_count")
        physical_id = str(attempt.get("physical_attempt_id") or "")
        outcome = str(attempt.get("outcome") or "")
        if (
            attempt.get("sequence") != sequence
            or isinstance(slot_index, bool)
            or not isinstance(slot_index, int)
            or not 0 <= slot_index < len(expanded_slot_identities)
            or kind
            not in {
                "thinking_downgrade",
                "transient_retry",
                "backup_replacement",
            }
            or not source_identity
            or not target_identity
            or not str(attempt.get("failure_kind") or "").strip()
            or not str(attempt.get("reason") or "").strip()
            or type(request_started) is not bool
        ):
            reasons.append("invalid_proposer_recovery_attempt")
            continue
        if request_started:
            receipt_started_total += nonnegative_int(physical_count)
            if attempt.get("stream_closed") is not True:
                reasons.append("proposer_recovery_stream_not_closed")
            if (
                isinstance(physical_count, bool)
                or not isinstance(physical_count, int)
                or physical_count != 1
                or HEX32.fullmatch(physical_id) is None
                or outcome
                not in {
                    "succeeded",
                    "failed",
                    "budget_overrun",
                    "evidence_unproven",
                }
                or (
                    attempt.get("usage_reported") is True
                    and attempt.get("usage_missing_count") != 0
                )
                or (
                    attempt.get("usage_reported") is not True
                    and attempt.get("usage_missing_count") != 1
                )
            ):
                reasons.append("invalid_proposer_recovery_attempt")
            else:
                receipt_physical_ids.append(physical_id)
        elif physical_count != 0 or physical_id or outcome != "not_started":
            reasons.append("invalid_proposer_recovery_unstarted_attempt")
        if kind == "thinking_downgrade":
            if (
                target_identity != source_identity
                or not str(attempt.get("thinking_before") or "")
                or not str(attempt.get("thinking_after") or "")
                or attempt.get("thinking_before") == attempt.get("thinking_after")
            ):
                reasons.append("invalid_proposer_thinking_downgrade")
        elif kind == "transient_retry":
            backoff = attempt.get("backoff_s")
            if (
                target_identity != source_identity
                or isinstance(backoff, bool)
                or not isinstance(backoff, int | float)
                or float(backoff) < 0
            ):
                reasons.append("invalid_proposer_transient_retry")
        else:
            if target_identity not in backup_identities:
                reasons.append("invalid_proposer_backup_replacement")
            else:
                backup_targets.append(target_identity)
        if (
            request_started
            and physical_id in physical_identity_by_id
            and physical_identity_by_id[physical_id] != target_identity
        ):
            reasons.append("proposer_recovery_physical_identity_mismatch")

    if receipt_started_total != started_count or len(receipt_physical_ids) != len(
        set(receipt_physical_ids)
    ):
        reasons.append("invalid_proposer_recovery_budget")

    receipt_physical_id_set = set(receipt_physical_ids)
    current_recovery_ids: list[str] = []
    primary_success_slots: set[int] = set()
    primary_identity_by_slot: dict[int, str] = {}
    strict_successful_slots = {
        slot_index
        for slot_index, candidate in enumerate(candidates)
        if strict_successful_candidate(candidate)
    }
    final_physical_id_by_slot: dict[int, str] = {}
    for slot_index, physical_attempts in candidate_physical_attempts_by_slot.items():
        candidate_ids = [
            str(physical.get("physical_attempt_id") or "") for physical in physical_attempts
        ]
        candidate_recovery_ids = [
            physical_id for physical_id in candidate_ids if physical_id in receipt_physical_id_set
        ]
        if candidate_ids:
            final_physical_id_by_slot[slot_index] = candidate_ids[-1]
        current_recovery_ids.extend(candidate_recovery_ids)
        primary_rows = [
            physical
            for physical in physical_attempts
            if str(physical.get("physical_attempt_id") or "") not in receipt_physical_id_set
        ]
        if len(primary_rows) > 1 or (
            primary_rows and physical_attempts and primary_rows[0] is not physical_attempts[0]
        ):
            reasons.append("invalid_proposer_recovery_primary_physical_ledger")
        elif primary_rows:
            primary_identity = _canonical_proposer_recovery_identity(
                primary_rows[0].get("identity")
            )
            primary_identity_by_slot[slot_index] = primary_identity
            if (
                primary_rows[0].get("outcome") == "succeeded"
                and slot_index in strict_successful_slots
                and not candidate_recovery_ids
            ):
                primary_success_slots.add(slot_index)
        expected_slot_recovery_ids = [
            str(attempt.get("physical_attempt_id") or "")
            for attempt in normalized_attempts
            if attempt.get("request_started") is True
            and attempt.get("slot_index") == slot_index
            and str(attempt.get("physical_attempt_id") or "") in set(candidate_ids)
        ]
        if candidate_recovery_ids != expected_slot_recovery_ids:
            reasons.append("proposer_recovery_candidate_receipt_order_mismatch")

    if backup_targets != backup_identities[: len(backup_targets)]:
        reasons.append("proposer_recovery_skipped_ordered_backup")
    visited = receipt.get("visited_identities")
    if not isinstance(visited, list) or visited != sorted(set(backup_targets)):
        reasons.append("wrong_proposer_recovery_visited_identities")
    exclusions = receipt.get("cumulative_excluded_identities")
    if (
        not isinstance(exclusions, list)
        or exclusions != sorted(set(str(value) for value in exclusions))
        or any(
            _canonical_proposer_recovery_identity(value)
            not in {*selected_identities, *backup_identities}
            for value in exclusions
        )
    ):
        reasons.append("invalid_proposer_recovery_exclusions")

    before = receipt.get("executed_proposer_roster_before")
    after = receipt.get("executed_proposer_roster_after")
    normalized_before = (
        [_canonical_proposer_recovery_identity(value) for value in before]
        if isinstance(before, list)
        else []
    )
    normalized_after = (
        [_canonical_proposer_recovery_identity(value) for value in after]
        if isinstance(after, list)
        else []
    )
    allowed_identities_by_slot = [
        {slot_identity, *backup_identities} for slot_identity in expanded_slot_identities
    ]

    def valid_executed_roster(roster: list[str]) -> bool:
        return (
            len(roster) == len(expanded_slot_identities)
            and all(roster)
            and all(
                identity in allowed_identities_by_slot[slot_index]
                for slot_index, identity in enumerate(roster)
            )
        )

    derived_after = list(normalized_before)
    current_recovery_set = set(current_recovery_ids)
    for attempt in normalized_attempts:
        physical_id = str(attempt.get("physical_attempt_id") or "")
        slot_index = attempt.get("slot_index")
        if (
            attempt.get("request_started") is True
            and physical_id in current_recovery_set
            and attempt.get("outcome") == "succeeded"
            and isinstance(slot_index, int)
            and not isinstance(slot_index, bool)
            and 0 <= slot_index < len(derived_after)
        ):
            derived_after[slot_index] = _canonical_proposer_recovery_identity(
                attempt.get("target_identity")
            )
    if (
        not valid_executed_roster(normalized_before)
        or not valid_executed_roster(normalized_after)
        or normalized_after != derived_after
        or any(
            primary_identity != normalized_before[slot_index]
            for slot_index, primary_identity in (primary_identity_by_slot.items())
            if slot_index < len(normalized_before)
        )
    ):
        reasons.append("invalid_proposer_recovery_executed_roster")

    if any(
        Counter(receipt_physical_ids)[physical_id] != Counter(current_recovery_ids)[physical_id]
        for physical_id in current_recovery_ids
    ):
        reasons.append("proposer_recovery_candidate_receipt_mismatch")
    successful_slots = set(primary_success_slots)
    for attempt in normalized_attempts:
        physical_id = str(attempt.get("physical_attempt_id") or "")
        if attempt.get("request_started") is not True or physical_id not in current_recovery_set:
            continue
        if len(successful_slots) >= expected_quorum:
            reasons.append("proposer_recovery_continued_after_quorum")
        slot_index = nonnegative_int(attempt.get("slot_index"))
        if (
            attempt.get("outcome") == "succeeded"
            and slot_index in strict_successful_slots
            and final_physical_id_by_slot.get(slot_index) == physical_id
        ):
            successful_slots.add(slot_index)
    actual_strict_successful = (
        sum(strict_successful_candidate(candidate) for candidate in candidates)
        if isinstance(candidates, list)
        else 0
    )
    actual_usable = (
        sum(usable_candidate(candidate) for candidate in candidates)
        if isinstance(candidates, list)
        else 0
    )
    if (
        isinstance(cleanup_bypass, Mapping)
        and cleanup_bypass.get("successful_proposers") != actual_strict_successful
    ):
        reasons.append("proposer_cleanup_quorum_bypass_success_mismatch")
    if (
        isinstance(cleanup_bypass, Mapping)
        and cleanup_bypass.get("usable_proposers") != actual_usable
    ):
        reasons.append("proposer_cleanup_quorum_bypass_usable_mismatch")
    if (
        call.get("successful_proposers") != actual_strict_successful
        or receipt.get("strict_successful_proposers") != actual_strict_successful
        or receipt.get("usable_proposers") != actual_usable
        or receipt.get("quorum_reached")
        is not (
            actual_strict_successful >= expected_quorum
            if complete_proposers_only
            else actual_usable >= expected_quorum
        )
        or len(successful_slots) != actual_strict_successful
    ):
        reasons.append("proposer_recovery_success_count_mismatch")
    return (
        final_identity_by_slot,
        set(current_recovery_ids),
        list(dict.fromkeys(reasons)),
    )


def ensemble_physical_call_reasons(
    call: Mapping[str, Any],
    *,
    expected_proposers: Sequence[str],
    expected_aggregator: str,
    final_text: str,
    require_output_binding: bool,
    aggregator_recovery_policy: Mapping[str, Any] = FORMAL_AGGREGATOR_RECOVERY_POLICY,
    proposer_recovery_policy: Mapping[str, Any] = FORMAL_PROPOSER_RECOVERY_POLICY,
) -> list[str]:
    """Validate one physical ensemble call from candidate evidence."""

    reasons: list[str] = []
    if str(call.get("request_outcome") or "llm_response") != "llm_response":
        reasons.append("aggregator_call_error")
    if str(call.get("final_request_role") or "") != "aggregator":
        reasons.append("final_request_not_aggregator")

    total = call.get("total_candidates")
    successful = call.get("successful_proposers")
    expected_total = len(expected_proposers)
    executed_plan = call.get("selection_plan")
    complete_only_analyzer_fallback = bool(
        isinstance(executed_plan, Mapping)
        and executed_plan.get("analyzer_failure_fallback") is True
        and executed_plan.get("complete_proposers_only") is True
        and executed_plan.get("effective_min_successful_proposers") == 1
    )
    dynamic_partial_quorum = bool(
        isinstance(executed_plan, Mapping)
        and not complete_only_analyzer_fallback
        and executed_plan.get("proposer_recovery_policy") is not None
        and (
            executed_plan.get("selection_mode") == "router_dynamic"
            or executed_plan.get("strategy") == "router_dynamic"
        )
    )
    usable = call.get("usable_proposers") if dynamic_partial_quorum else successful
    required_successful_proposers = 1 if complete_only_analyzer_fallback else min(2, expected_total)
    effective_proposer_recovery_policy = (
        analyzer_failure_fallback_proposer_recovery_policy(proposer_recovery_policy)
        if complete_only_analyzer_fallback
        else proposer_recovery_policy
    )
    successful_count_valid = (
        isinstance(successful, int)
        and not isinstance(successful, bool)
        and 0 <= successful <= expected_total
    )
    if not isinstance(total, int) or isinstance(total, bool) or total != expected_total:
        reasons.append("wrong_executed_proposer_count")
    if not successful_count_valid or (
        not dynamic_partial_quorum and successful < required_successful_proposers
    ):
        reasons.append("proposer_quorum_not_met")
    if dynamic_partial_quorum:
        usable_count_valid = (
            isinstance(usable, int)
            and not isinstance(usable, bool)
            and 0 <= usable <= expected_total
        )
        if not usable_count_valid or usable < required_successful_proposers:
            reasons.append("proposer_quorum_not_met")
        if (
            not successful_count_valid
            or not usable_count_valid
            or successful > usable
            or call.get("partial_proposers") != usable - successful
            or call.get("execution_quorum_required") != required_successful_proposers
            or call.get("execution_quorum_met") is not (usable >= required_successful_proposers)
            or call.get("strict_quorum_met") is not (successful >= required_successful_proposers)
        ):
            reasons.append("invalid_dynamic_proposer_usable_quorum")

    strict_physical_evidence = False
    recovered_identity_by_slot: dict[int, str] = {}
    recovery_physical_ids: set[str] = set()
    receipt_bound_local_recovery_ids: set[str] = set()
    initial_identity_by_slot: tuple[str, ...] = ()
    if not isinstance(executed_plan, Mapping):
        reasons.append("missing_executed_selection_plan")
    else:
        physical_schema = executed_plan.get("thinking_physical_evidence_schema")
        if physical_schema is not None:
            if physical_schema != THINKING_PHYSICAL_EVIDENCE_SCHEMA:
                reasons.append("unknown_thinking_physical_evidence_schema")
            else:
                strict_physical_evidence = True
        raw_models = executed_plan.get("proposer_models")
        models = tuple(str(item) for item in raw_models) if isinstance(raw_models, list) else ()
        if models != tuple(expected_proposers):
            reasons.append("wrong_proposer_models")
        if str(executed_plan.get("aggregator_model") or "") != expected_aggregator:
            reasons.append("wrong_aggregator_model")
        if executed_plan.get("proposer_recovery_policy") is not None:
            strict_physical_evidence = True
            (
                initial_identity_by_slot,
                expanded_slot_reasons,
            ) = expanded_proposer_slot_identities(executed_plan)
            reasons.extend(expanded_slot_reasons)
            if len(initial_identity_by_slot) != expected_total:
                reasons.append("wrong_proposer_recovery_sample_slots")
            (
                recovered_identity_by_slot,
                recovery_physical_ids,
                proposer_recovery_reasons,
            ) = proposer_recovery_execution_reasons(
                call,
                executed_plan=executed_plan,
                expected_policy=effective_proposer_recovery_policy,
            )
            reasons.extend(proposer_recovery_reasons)
            if not proposer_recovery_reasons:
                # These IDs are the strict intersection of this call's
                # candidate ledger and valid, slot-bound receipt entries.
                receipt_bound_local_recovery_ids = set(recovery_physical_ids)
        elif call.get("proposer_recovery") is not None:
            reasons.append("unexpected_proposer_recovery_receipt")
    executed_aggregator, recovery_reasons = aggregator_recovery_execution_reasons(
        call,
        expected_aggregator=expected_aggregator,
        expected_policy=aggregator_recovery_policy,
    )
    reasons.extend(recovery_reasons)

    candidates = call.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != expected_total:
        reasons.append("missing_actual_proposer_candidates")
    else:
        strict_proven = [
            successful_candidate(candidate)
            and (
                not complete_only_analyzer_fallback
                or candidate.get("completion_outcome") == "complete"
            )
            for candidate in candidates
        ]
        proven = [
            usable_candidate(candidate) if dynamic_partial_quorum else strict
            for candidate, strict in zip(candidates, strict_proven, strict=True)
        ]
        if any(
            isinstance(candidate, Mapping)
            and candidate.get("ok") is True
            and not successful_candidate(candidate)
            for candidate in candidates
        ):
            reasons.append("invalid_successful_proposer_evidence")
        actual_strict_successful = sum(strict_proven)
        actual_usable = sum(proven)
        if actual_strict_successful != successful:
            reasons.append("successful_proposer_count_mismatch")
        if dynamic_partial_quorum and actual_usable != usable:
            reasons.append("usable_proposer_count_mismatch")
        if actual_usable < required_successful_proposers:
            reasons.append("insufficient_actual_proposer_quorum")
        for slot_index, (
            candidate,
            expected_model,
            candidate_proven,
            candidate_strict,
        ) in enumerate(
            zip(
                candidates,
                expected_proposers,
                proven,
                strict_proven,
                strict=True,
            )
        ):
            if not isinstance(candidate, Mapping):
                continue
            expected_identity = recovered_identity_by_slot.get(
                slot_index,
                (
                    initial_identity_by_slot[slot_index]
                    if slot_index < len(initial_identity_by_slot)
                    else f"openrouter:{expected_model}"
                ),
            )
            expected_provider, _, expected_candidate_model = expected_identity.partition(":")
            execution = candidate.get("execution")
            actual_provider = (
                str(
                    candidate.get("provider")
                    or (execution.get("actual_provider") if isinstance(execution, Mapping) else "")
                    or ""
                )
                .strip()
                .casefold()
            )
            requested_provider = (
                str(
                    candidate.get("requested_provider")
                    or (
                        execution.get("requested_provider")
                        if isinstance(execution, Mapping)
                        else ""
                    )
                    or ""
                )
                .strip()
                .casefold()
            )
            actual_model = str(
                candidate.get("model")
                or (execution.get("actual_model") if isinstance(execution, Mapping) else "")
                or ""
            ).strip()
            requested_model = str(
                candidate.get("requested_model")
                or (execution.get("requested_model") if isinstance(execution, Mapping) else "")
                or ""
            ).strip()
            attempts = (
                execution.get("physical_attempts") if isinstance(execution, Mapping) else None
            )
            physical_count = candidate.get("physical_request_count")
            final_physical_identity = (
                _canonical_proposer_recovery_identity(attempts[-1].get("identity"))
                if isinstance(attempts, list)
                and isinstance(physical_count, int)
                and not isinstance(physical_count, bool)
                and physical_count > 0
                and len(attempts) == physical_count
                and isinstance(attempts[-1], Mapping)
                else ""
            )
            partial_actual_identity_omission_proven = bool(
                partial_usable_candidate(candidate)
                and not actual_provider
                and not actual_model
                and final_physical_identity == expected_identity
            )
            if (
                requested_provider != expected_provider
                or (actual_provider and actual_provider != expected_provider)
                or (
                    candidate_proven
                    and not actual_provider
                    and not partial_actual_identity_omission_proven
                )
                or (candidate_strict and not actual_provider)
            ):
                reasons.append("wrong_actual_proposer_provider")
            if (
                requested_model != expected_candidate_model
                or (actual_model and actual_model != expected_candidate_model)
                or (
                    candidate_proven
                    and not actual_model
                    and not partial_actual_identity_omission_proven
                )
                or (candidate_strict and not actual_model)
            ):
                reasons.append("wrong_actual_proposer_model")

    if str(call.get("final_request_role") or "") == "aggregator":
        proposer_physical_count = 0
        strict_physical_ids: list[str] = []
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    continue
                request_started = candidate.get("request_started")
                physical_count = candidate.get("physical_request_count")
                if not isinstance(request_started, bool):
                    reasons.append("invalid_proposer_request_started_evidence")
                    continue
                if request_started:
                    if (
                        not isinstance(physical_count, int)
                        or isinstance(physical_count, bool)
                        or physical_count <= 0
                    ):
                        reasons.append("invalid_proposer_physical_request_count")
                    else:
                        proposer_physical_count += physical_count
                        if strict_physical_evidence:
                            execution = candidate.get("execution")
                            attempts = (
                                execution.get("physical_attempts")
                                if isinstance(execution, Mapping)
                                else None
                            )
                            if not isinstance(attempts, list) or len(attempts) != physical_count:
                                reasons.append("invalid_proposer_physical_attempt_ledger")
                            else:
                                for ordinal, attempt in enumerate(
                                    attempts,
                                    start=1,
                                ):
                                    attempt_id = (
                                        str(attempt.get("physical_attempt_id") or "")
                                        if isinstance(attempt, Mapping)
                                        else ""
                                    )
                                    attempt_ordinal = (
                                        attempt.get("attempt")
                                        if isinstance(attempt, Mapping)
                                        else None
                                    )
                                    receipt_bound_local_ordinal = bool(
                                        isinstance(attempt_ordinal, int)
                                        and not isinstance(attempt_ordinal, bool)
                                        and attempt_ordinal == 1
                                        and attempt_id in receipt_bound_local_recovery_ids
                                    )
                                    if (
                                        not isinstance(attempt, Mapping)
                                        or not isinstance(attempt_ordinal, int)
                                        or isinstance(attempt_ordinal, bool)
                                        or (
                                            attempt_ordinal != ordinal
                                            and not receipt_bound_local_ordinal
                                        )
                                        or attempt.get("request_started") is not True
                                        or HEX32.fullmatch(attempt_id) is None
                                    ):
                                        reasons.append("invalid_proposer_physical_attempt")
                                    else:
                                        strict_physical_ids.append(attempt_id)
                elif physical_count not in {None, 0}:
                    reasons.append("unstarted_proposer_has_physical_request")
                elif strict_physical_evidence:
                    execution = candidate.get("execution")
                    attempts = (
                        execution.get("physical_attempts") if isinstance(execution, Mapping) else []
                    )
                    if attempts not in (None, []):
                        reasons.append("unstarted_proposer_has_physical_attempt")
        recovery = call.get("aggregator_recovery")
        recovery_attempts = recovery.get("attempts") if isinstance(recovery, Mapping) else []
        aggregator_physical_count = (
            sum(
                int(attempt.get("physical_request_count") or 0)
                for attempt in recovery_attempts
                if (
                    isinstance(attempt, Mapping)
                    and attempt.get("request_started") is True
                    and isinstance(attempt.get("physical_request_count"), int)
                    and not isinstance(attempt.get("physical_request_count"), bool)
                )
            )
            if isinstance(recovery_attempts, list)
            else 0
        )
        if strict_physical_evidence and isinstance(
            recovery_attempts,
            list,
        ):
            for recovery_attempt in recovery_attempts:
                if not isinstance(recovery_attempt, Mapping):
                    continue
                started = recovery_attempt.get("request_started") is True
                attempt_id = str(recovery_attempt.get("physical_attempt_id") or "")
                if started:
                    if (
                        recovery_attempt.get("physical_request_count") != 1
                        or HEX32.fullmatch(attempt_id) is None
                    ):
                        reasons.append("invalid_aggregator_physical_attempt_identity")
                    else:
                        strict_physical_ids.append(attempt_id)
                elif attempt_id:
                    reasons.append("unstarted_aggregator_attempt_has_physical_identity")
        raw_llm_request_count = call.get("llm_request_count")
        raw_physical_request_count = call.get("physical_request_count")
        if (
            not isinstance(raw_llm_request_count, int)
            or isinstance(raw_llm_request_count, bool)
            or raw_llm_request_count <= 0
        ):
            reasons.append("invalid_ensemble_llm_request_count")
        if (
            not isinstance(raw_physical_request_count, int)
            or isinstance(raw_physical_request_count, bool)
            or raw_physical_request_count <= 0
        ):
            reasons.append("invalid_ensemble_physical_request_count")
        if raw_llm_request_count != raw_physical_request_count:
            reasons.append("ensemble_request_count_mismatch")
        minimum_request_count = proposer_physical_count + aggregator_physical_count
        if isinstance(raw_physical_request_count, int):
            if strict_physical_evidence and raw_physical_request_count != minimum_request_count:
                reasons.append("ensemble_physical_request_count_not_exact")
            elif raw_physical_request_count < minimum_request_count:
                reasons.append("ensemble_physical_request_count_undercounted")
        if strict_physical_evidence:
            if len(strict_physical_ids) != len(set(strict_physical_ids)):
                reasons.append("duplicate_ensemble_physical_attempt_id")
            if not recovery_physical_ids.issubset(set(strict_physical_ids)):
                reasons.append("proposer_recovery_physical_attempt_set_mismatch")
            if isinstance(raw_physical_request_count, int) and raw_physical_request_count != len(
                strict_physical_ids
            ):
                reasons.append("ensemble_physical_attempt_set_count_mismatch")
            selected_id = ""
            if isinstance(recovery, Mapping):
                selected_attempt = recovery.get("selected_attempt")
                selected_rows = [
                    row
                    for row in recovery_attempts
                    if isinstance(row, Mapping)
                    and row.get("attempt") == selected_attempt
                    and row.get("request_started") is True
                    and row.get("outcome") == "succeeded"
                ]
                if len(selected_rows) == 1:
                    selected_id = str(selected_rows[0].get("physical_attempt_id") or "")
            final_request = call.get("final_request")
            final_usage = final_request.get("usage") if isinstance(final_request, Mapping) else None
            final_provider_usage = (
                final_usage.get("provider_usage") if isinstance(final_usage, Mapping) else None
            )
            final_id = (
                str(final_usage.get("physical_attempt_id") or "")
                if isinstance(final_usage, Mapping)
                else ""
            )
            nested_final_id = (
                str(final_provider_usage.get("physical_attempt_id") or "")
                if isinstance(final_provider_usage, Mapping)
                else ""
            )
            if not selected_id or final_id != selected_id or nested_final_id != selected_id:
                reasons.append("final_aggregator_physical_attempt_id_mismatch")

    final_request = call.get("final_request")
    if (
        not isinstance(final_request, Mapping)
        or final_request.get("request_started") is not True
        or str(final_request.get("role") or "") != "aggregator"
        or final_request.get("error")
        or call.get("aggregator_error")
    ):
        reasons.append("aggregator_request_incomplete")
    else:
        usage = final_request.get("usage")
        execution = final_request.get("execution")
        actual_model = (
            str(usage.get("model") or usage.get("requested_model") or "")
            if isinstance(usage, Mapping)
            else str(
                execution.get("actual_model")
                or execution.get("requested_model")
                or execution.get("model")
                or ""
            )
            if isinstance(execution, Mapping)
            else ""
        )
        if actual_model != executed_aggregator:
            reasons.append("wrong_actual_aggregator_model")
        actual_provider = (
            str(usage.get("provider") or "").strip().casefold()
            if isinstance(usage, Mapping)
            else str(execution.get("actual_provider") or "").strip().casefold()
            if isinstance(execution, Mapping)
            else ""
        )
        requested_provider = (
            str(usage.get("requested_provider") or "").strip().casefold()
            if isinstance(usage, Mapping)
            else str(execution.get("requested_provider") or "").strip().casefold()
            if isinstance(execution, Mapping)
            else ""
        )
        requested_model = (
            str(usage.get("requested_model") or "").strip()
            if isinstance(usage, Mapping)
            else str(execution.get("requested_model") or "").strip()
            if isinstance(execution, Mapping)
            else ""
        )
        if actual_provider != "openrouter" or requested_provider != "openrouter":
            reasons.append("wrong_actual_aggregator_provider")
        if requested_model != executed_aggregator:
            reasons.append("wrong_requested_aggregator_model")
        if require_output_binding:
            reasons.extend(
                aggregator_output_reasons(
                    call,
                    final_request,
                    final_text=final_text,
                )
            )
    return list(dict.fromkeys(reasons))


_ADMISSIBLE_NONTERMINAL_FALLBACK_REASONS = frozenset(
    {
        "aggregator_fallback_used_or_unknown",
        "missing_aggregator_recovery_evidence",
        "final_request_not_aggregator",
        "proposer_quorum_not_met",
        "insufficient_actual_proposer_quorum",
        "aggregator_request_incomplete",
    }
)


def admissible_empty_nonterminal_fallback_reasons(
    call: Mapping[str, Any],
    *,
    expected_proposers: Sequence[str],
    executed_plan: Mapping[str, Any],
) -> list[str]:
    """Validate an outputless nonterminal fallback against frozen routes."""

    reasons: list[str] = []
    if str(call.get("request_outcome") or "llm_response") != "llm_response":
        reasons.append("invalid_intermediate_fallback_outcome")
    if call.get("fallback_used") is not True:
        reasons.append("invalid_intermediate_fallback_flag")
    if str(call.get("final_request_role") or "") != "fallback_single":
        reasons.append("invalid_intermediate_fallback_role")
    total = call.get("total_candidates")
    successful = call.get("successful_proposers")
    expected_total = len(expected_proposers)
    required_quorum = (
        2
        if executed_plan.get("proposer_recovery_policy") is not None
        else math.ceil(2 * expected_total / 3)
    )
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or total != expected_total
        or not isinstance(successful, int)
        or isinstance(successful, bool)
        or not 0 <= successful < required_quorum
    ):
        reasons.append("invalid_intermediate_fallback_quorum")
    candidates = call.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != expected_total:
        reasons.append("invalid_intermediate_fallback_candidates")
    elif sum(successful_candidate(candidate) for candidate in candidates) != successful:
        reasons.append("intermediate_fallback_candidate_count_mismatch")
    final_request = call.get("final_request")
    if (
        not isinstance(final_request, Mapping)
        or final_request.get("request_started") is not True
        or str(final_request.get("role") or "") != "fallback_single"
        or final_request.get("error")
        or call.get("aggregator_error")
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
    execution = final_request.get("execution")
    if not isinstance(usage, Mapping):
        reasons.append("missing_intermediate_fallback_usage")
        return list(dict.fromkeys(reasons))
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
    if (
        not actual_model
        or not requested_model
        or not any(
            _formal_openrouter_models_equivalent(actual_model, expected)
            and _formal_openrouter_models_equivalent(requested_model, expected)
            and all(
                _formal_openrouter_models_equivalent(execution_model, expected)
                for execution_model in execution_models
            )
            for expected in expected_proposers
        )
    ):
        reasons.append("wrong_intermediate_fallback_model")
    if str(execution.get("role") or "") != "fallback_single":
        reasons.append("wrong_intermediate_fallback_execution_role")
    if not isinstance(usage, Mapping) or not str(usage.get("stop_reason") or "").strip():
        reasons.append("missing_intermediate_fallback_stop_reason")
    return list(dict.fromkeys(reasons))


def agent_call_output_sequence_reasons(
    calls: Sequence[Mapping[str, Any]],
    *,
    final_text: str,
) -> list[str]:
    """Bind every Agent-loop response segment to the stored final answer."""

    if len(calls) <= 1:
        return []
    reasons: list[str] = []
    offset = 0
    for call in calls:
        final_request = call.get("final_request")
        output = (
            call.get("assembled_output")
            if call.get("output_binding_schema") == ENSEMBLE_OUTPUT_BINDING_SCHEMA
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
        # Trace text may be clipped, while chars/sha256 intentionally bind the
        # complete contribution.  Validate the hash against the reconstructed
        # full segment rather than the clipped prefix.
        if output.get("sha256") not in {None, "", text_sha256(segment)}:
            reasons.append("wrong_agent_call_output_hash")
        if output.get("truncated") is True:
            if not segment.startswith(output_text):
                reasons.append("wrong_agent_call_output_binding")
        elif len(output_text) != output_chars or output_text != segment:
            reasons.append("wrong_agent_call_output_binding")
        offset += output_chars
    if offset != len(final_text):
        reasons.append("incomplete_agent_call_output_binding")
    return list(dict.fromkeys(reasons))


def ensemble_gate(
    row: Mapping[str, Any],
    *,
    expected_proposers: Sequence[str] | None = None,
    expected_aggregator: str | None = None,
    allowed_models: set[str] | None = None,
    aggregator_recovery_policy: Mapping[str, Any] = FORMAL_AGGREGATOR_RECOVERY_POLICY,
    proposer_recovery_policy: Mapping[str, Any] = FORMAL_PROPOSER_RECOVERY_POLICY,
) -> list[str]:
    trace = row.get("ensemble_trace")
    if not isinstance(trace, Mapping):
        return ["missing_ensemble_trace"]
    routing = row.get("routing_trace")
    selection_plan = (
        routing.get("selection_plan")
        if isinstance(routing, Mapping) and isinstance(routing.get("selection_plan"), Mapping)
        else {}
    )
    planned_prompt = selection_plan.get("aggregator_prompt")
    if planned_prompt is not None and trace.get("aggregator_prompt") != planned_prompt:
        reasons = ["g1_aggregator_prompt_trace_mismatch"]
    else:
        reasons = []
    raw_proposers = selection_plan.get("proposer_models")
    planned_proposers = (
        tuple(str(item) for item in raw_proposers) if isinstance(raw_proposers, list) else ()
    )
    planned_aggregator = str(selection_plan.get("aggregator_model") or "")
    proposers = tuple(expected_proposers) if expected_proposers is not None else planned_proposers
    aggregator = expected_aggregator or planned_aggregator
    if not proposers:
        reasons.append("missing_proposer_models")
    if not aggregator:
        reasons.append("missing_aggregator_model")
    if expected_proposers is not None and planned_proposers != proposers:
        reasons.append("wrong_proposer_models")
    if expected_aggregator is not None and planned_aggregator != aggregator:
        reasons.append("wrong_aggregator_model")
    if allowed_models is not None:
        if any(model not in allowed_models for model in proposers):
            reasons.append("proposer_model_outside_frozen_routes")
        if aggregator not in allowed_models:
            reasons.append("aggregator_model_outside_frozen_routes")
    calls, sequence_reasons = ensemble_call_trace_sequence(trace)
    reasons.extend(sequence_reasons)
    reasons.extend(
        agent_call_output_sequence_reasons(
            calls,
            final_text=str(row.get("final_text") or ""),
        )
    )
    if proposers and aggregator:
        for index, call in enumerate(calls):
            call_reasons = ensemble_physical_call_reasons(
                call,
                expected_proposers=proposers,
                expected_aggregator=aggregator,
                final_text=str(row.get("final_text") or ""),
                require_output_binding=index == len(calls) - 1,
                aggregator_recovery_policy=aggregator_recovery_policy,
                proposer_recovery_policy=proposer_recovery_policy,
            )
            if index < len(calls) - 1 and call.get("fallback_used") is True:
                fallback_reasons = admissible_empty_nonterminal_fallback_reasons(
                    call,
                    expected_proposers=proposers,
                    executed_plan=selection_plan,
                )
                reasons.extend(fallback_reasons)
                if not fallback_reasons:
                    call_reasons = [
                        reason
                        for reason in call_reasons
                        if reason not in _ADMISSIBLE_NONTERMINAL_FALLBACK_REASONS
                    ]
            reasons.extend(call_reasons)
    return list(dict.fromkeys(reasons))


def g1_recomputed_proposer_bounds(
    plan: Mapping[str, Any],
) -> tuple[int, int, list[str]] | None:
    config = plan.get("ranking_parameters")
    task_profile = plan.get("task_profile")
    if not isinstance(config, Mapping) or not isinstance(task_profile, Mapping):
        return None
    routing_tiers = config.get("routing_tiers")
    proposer_count = config.get("proposer_count")
    if not isinstance(routing_tiers, Mapping) or not isinstance(proposer_count, Mapping):
        return None
    tier_mapping = routing_tiers.get("mapping")
    tier_dist = task_profile.get("tier_dist")
    by_tier = proposer_count.get("by_tier")
    if (
        not isinstance(tier_mapping, Mapping)
        or not isinstance(tier_dist, Mapping)
        or not isinstance(by_tier, Mapping)
    ):
        return None
    try:
        tier_values = [int(value) for value in tier_mapping.values()]
        expected_tier = sum(int(tier) * float(weight) for tier, weight in tier_dist.items())
        effective_tier = max(
            min(tier_values),
            min(
                max(tier_values),
                math.floor(expected_tier + float(proposer_count["effective_tier_rounding_offset"])),
            ),
        )
        tier_bounds = by_tier[str(effective_tier)]
        minimum = int(tier_bounds["min"])
        maximum = int(tier_bounds["max"])
    except (KeyError, TypeError, ValueError):
        return None
    constraints = task_profile.get("constraints")
    constraints = constraints if isinstance(constraints, Mapping) else {}
    reasons = [f"tier_{effective_tier}"]
    if str(constraints.get("risk") or "low") == "high":
        high_risk = proposer_count.get("high_risk")
        if not isinstance(high_risk, Mapping):
            return None
        try:
            minimum = max(minimum, int(high_risk["min"]))
            maximum = max(maximum, int(high_risk["max"]))
        except (KeyError, TypeError, ValueError):
            return None
        reasons.append("high_risk_cross_validation")
    constrained = str(constraints.get("cost")) in {
        str(value) for value in proposer_count.get("constrained_cost_values") or []
    } or str(constraints.get("latency")) in {
        str(value) for value in proposer_count.get("constrained_latency_values") or []
    }
    if constrained:
        try:
            maximum = min(maximum, int(proposer_count["constrained_max"]))
        except (KeyError, TypeError, ValueError):
            return None
        minimum = min(minimum, maximum)
        reasons.append("cost_or_latency_constrained")
    proposer_policy = plan.get("proposer_recovery_policy")
    explicit_quorum = (
        proposer_policy.get("quorum_required") if isinstance(proposer_policy, Mapping) else None
    )
    if (
        isinstance(explicit_quorum, int)
        and not isinstance(explicit_quorum, bool)
        and explicit_quorum > 0
        and (explicit_quorum > minimum or explicit_quorum > maximum)
    ):
        minimum = max(minimum, explicit_quorum)
        maximum = max(maximum, explicit_quorum)
        reasons.append("proposer_recovery_quorum")
    return minimum, maximum, reasons


def legacy_managed_v3_source_authenticated(
    contract: Mapping[str, Any] | None,
) -> bool:
    """Authorize the historical managed-v3 shape only for its exact source."""

    source_identity = contract.get("source_identity") if isinstance(contract, Mapping) else None
    return (
        isinstance(source_identity, Mapping)
        and dict(source_identity) == LEGACY_MANAGED_V3_SOURCE_IDENTITY
    )


def g1_aggregator_prompt_plan_reason(plan: Mapping[str, Any]) -> str:
    """Authenticate the exact prompt policy selected by the frozen ranker."""

    from opensquilla.provider.aggregator_prompt import (
        AGGREGATOR_PROMPT_VERSION_CURRENT,
        valid_aggregator_prompt_evidence,
    )

    ranking_parameters = plan.get("ranking_parameters")
    aggregator_policy = (
        ranking_parameters.get("aggregator") if isinstance(ranking_parameters, Mapping) else None
    )
    configured_version = (
        aggregator_policy.get("prompt_version") if isinstance(aggregator_policy, Mapping) else None
    )
    evidence = plan.get("aggregator_prompt")
    # Archived pre-P0-35 plans did not record this additive field. New plans
    # always include it, including the byte-equivalent v1 baseline.
    if evidence is None and configured_version is None:
        return ""
    expected_version = configured_version or AGGREGATOR_PROMPT_VERSION_CURRENT
    return (
        ""
        if valid_aggregator_prompt_evidence(
            evidence,
            expected_version=expected_version,
        )
        else "wrong_g1_aggregator_prompt"
    )


def g1_registry_plan_reasons(
    plan: Any,
    *,
    contract: Mapping[str, Any],
    allow_legacy_managed_v3: bool = False,
    aggregator_recovery_policy: Mapping[str, Any] = FORMAL_AGGREGATOR_RECOVERY_POLICY,
    proposer_recovery_policy: Mapping[str, Any] = FORMAL_PROPOSER_RECOVERY_POLICY,
) -> tuple[list[str], tuple[str, ...], str]:
    """Bind a G1 physical plan to its frozen registry and exact P/A choice."""

    reasons: list[str] = []
    if not isinstance(plan, Mapping):
        return ["missing_g1_selection_plan"], (), ""
    if plan.get("analyzer_failure_fallback") is True:
        fallback = g1_analyzer_failure_fallback_policy(contract)
        ranking_identity = g1_ranking_config_identity(contract)
        if (
            fallback is None
            or ranking_identity is None
            or contract.get("selection_mode") != "router_dynamic"
            or contract.get("user_profile_enabled") is not False
        ):
            return ["invalid_g1_analyzer_failure_fallback_contract"], (), ""
        expected_p = list(fallback["selected_P"])
        expected_a = str(fallback["selected_A"])
        expected_models = [identity.partition(":")[2] for identity in expected_p]
        expected_proposer_recovery = analyzer_failure_fallback_proposer_recovery_policy(
            proposer_recovery_policy
        )
        if plan.get("analyzer_failure_fallback_schema") != (
            "opensquilla.router-dynamic-analyzer-failure-fallback/v1"
        ):
            reasons.append("wrong_g1_analyzer_failure_fallback_schema")
        if plan.get("selected_P") != expected_p:
            reasons.append("wrong_g1_selected_proposers")
        if plan.get("selected_A") != expected_a:
            reasons.append("wrong_g1_selected_aggregator")
        if (
            plan.get("proposer_models") != expected_models
            or nonnegative_int(plan.get("proposer_count")) != len(expected_p)
            or nonnegative_int(plan.get("proposer_sample_count")) != len(expected_p)
            or plan.get("aggregator_model") != expected_a.partition(":")[2]
        ):
            reasons.append("wrong_g1_physical_selection_plan")
        if (
            plan.get("complete_proposers_only") is not True
            or plan.get("configured_min_successful_proposers") != 1
            or plan.get("effective_min_successful_proposers") != 1
            or plan.get("N_min") != 1
            or plan.get("N_max") != len(expected_p)
        ):
            reasons.append("wrong_g1_analyzer_failure_fallback_quorum")
        if plan.get("proposer_recovery_policy") != expected_proposer_recovery:
            reasons.append("wrong_g1_proposer_recovery_policy")
        if (
            plan.get("aggregator_max_recovery_actions") != 1
            or plan.get("backup_P") != []
            or plan.get("configured_proposer_backup_count") != 0
            or plan.get("aggregator_candidates") != [expected_a]
        ):
            reasons.append("wrong_g1_analyzer_failure_fallback_recovery")
        for field, expected in aggregator_recovery_policy.items():
            if plan.get(field) != expected:
                reasons.append(f"wrong_g1_{field}")
        try:
            from opensquilla.provider.protocol import (
                provider_retry_roster_fingerprint,
            )

            proposer_recovery_fingerprint = provider_retry_roster_fingerprint(plan)
        except (TypeError, ValueError):
            proposer_recovery_fingerprint = ""
        if HEX64.fullmatch(proposer_recovery_fingerprint) is None:
            reasons.append("invalid_g1_proposer_recovery_fingerprint")
        analyzer_trace = plan.get("task_analyzer")
        analyzer_chain_trace = (
            analyzer_trace.get("chain") if isinstance(analyzer_trace, Mapping) else None
        )
        if (
            not isinstance(analyzer_trace, Mapping)
            or analyzer_trace.get("schema_valid") is not False
            or not isinstance(analyzer_chain_trace, Mapping)
            or analyzer_chain_trace.get("exhausted") is not True
        ):
            reasons.append("wrong_g1_analyzer_failure_fallback_activation")
        ranking_schema, ranking_version, ranking_hash = ranking_identity
        ranking_parameters = plan.get("ranking_parameters")
        if (
            plan.get("ranking_config_schema_version") != ranking_schema
            or plan.get("ranking_config_version") != ranking_version
            or plan.get("ranking_config_hash") != ranking_hash
            or not isinstance(ranking_parameters, Mapping)
            or canonical_sha256(ranking_parameters) != ranking_hash
        ):
            reasons.append("wrong_g1_ranking_config")
        return (
            list(dict.fromkeys(reasons)),
            tuple(expected_models),
            expected_a.partition(":")[2],
        )
    profile_id = str(contract.get("profile_id") or "").strip()
    source_version = str(contract.get("source_registry_snapshot_version") or "").strip()
    routes_hash = str(contract.get("expected_routes_sha256") or "").strip()
    ranking_config_identity = g1_ranking_config_identity(contract)
    ranking_config_schema_version, ranking_config_version, ranking_config_hash = (
        ranking_config_identity if ranking_config_identity is not None else ("", "", "")
    )
    source_registry_snapshot_hash = str(
        contract.get("expected_source_registry_snapshot_sha256") or ""
    ).strip()
    registry_source_identity = g1_registry_source_identity(contract)
    analyzer_policy = g1_task_analyzer_execution_policy(contract)
    formal_n_max = g1_ranking_proposer_max(contract)
    routes = contract.get("expected_routes")
    expected_count = nonnegative_int(contract.get("expected_candidate_count"))
    candidate_scope = str(contract.get("candidate_scope") or "exact_routes")
    candidate_policy = str(contract.get("policy") or "exact_openrouter_routes")
    expected_candidate_policy = (
        "all_registry_models" if candidate_scope == "registry_all" else "exact_openrouter_routes"
    )
    expected_identities = (
        {f"openrouter:{str(model).strip().lower()}" for model in routes}
        if isinstance(routes, Mapping)
        else set()
    )
    if (
        not profile_id
        or contract.get("selection_mode") != "router_dynamic"
        or candidate_scope not in {"registry_all", "exact_routes"}
        or candidate_policy != expected_candidate_policy
        or not source_version
        or not HEX64.fullmatch(routes_hash)
        or canonical_sha256(routes) != routes_hash
        or ranking_config_identity is None
        or analyzer_policy is None
        or registry_source_identity is None
        or (source_version, source_registry_snapshot_hash) != registry_source_identity
        or formal_n_max is None
        or contract.get("user_profile_enabled") is not False
        or expected_count <= 0
        or len(expected_identities) != expected_count
        or (
            candidate_scope == "exact_routes"
            and isinstance(routes, Mapping)
            and any(str(provider).strip().casefold() == "auto" for provider in routes.values())
        )
        or (
            candidate_scope == "registry_all"
            and authenticated_registry_all_routes(contract) is None
        )
    ):
        return ["invalid_g1_registry_contract"], (), ""
    filtered_version = f"{source_version}+{profile_id}+{routes_hash[:12]}"
    allowlist = plan.get("candidate_allowlist")
    expected_allowlist = {
        "policy": expected_candidate_policy,
        "profile_id": profile_id,
        "source_registry_snapshot_version": source_version,
        "expected_source_registry_snapshot_sha256": (source_registry_snapshot_hash),
        "filtered_registry_snapshot_version": filtered_version,
        "expected_routes_sha256": routes_hash,
        "expected_candidate_count": expected_count,
        "candidate_count": expected_count,
    }
    if not isinstance(allowlist, Mapping):
        reasons.append("missing_g1_candidate_allowlist")
    else:
        for field, expected in expected_allowlist.items():
            if allowlist.get(field) != expected:
                reasons.append(f"wrong_g1_candidate_allowlist_{field}")
        identities = allowlist.get("expected_identities")
        if (
            not isinstance(identities, list)
            or len(identities) != expected_count
            or set(str(value) for value in identities) != expected_identities
        ):
            reasons.append("wrong_g1_candidate_allowlist_identities")
    pool = plan.get("candidate_pool")
    pool_identities = (
        [str(item.get("identity") or "") for item in pool if isinstance(item, Mapping)]
        if isinstance(pool, list)
        else []
    )
    if (
        nonnegative_int(plan.get("candidate_pool_size")) != expected_count
        or len(pool_identities) != expected_count
        or len(set(pool_identities)) != expected_count
        or set(pool_identities) != expected_identities
    ):
        reasons.append("wrong_g1_candidate_pool")
    if plan.get("registry_snapshot_version") != filtered_version:
        reasons.append("wrong_g1_registry_snapshot_version")
    if not HEX64.fullmatch(str(plan.get("registry_snapshot_hash") or "")):
        reasons.append("wrong_g1_registry_snapshot_hash")
    if (
        plan.get("ranking_config_hash") != ranking_config_hash
        or plan.get("ranking_config_schema_version") != ranking_config_schema_version
        or plan.get("ranking_config_version") != ranking_config_version
        or not isinstance(plan.get("ranking_parameters"), Mapping)
        or canonical_sha256(plan.get("ranking_parameters")) != ranking_config_hash
    ):
        reasons.append("wrong_g1_ranking_config")
    ranking_parameters = plan.get("ranking_parameters")
    prompt_reason = g1_aggregator_prompt_plan_reason(plan)
    if prompt_reason:
        reasons.append(prompt_reason)
    ranking_parameters = ranking_parameters if isinstance(ranking_parameters, Mapping) else {}
    proposer_count_policy = ranking_parameters.get("proposer_count")
    proposer_count_policy = (
        proposer_count_policy if isinstance(proposer_count_policy, Mapping) else {}
    )
    aggregator_policy = ranking_parameters.get("aggregator")
    aggregator_policy = aggregator_policy if isinstance(aggregator_policy, Mapping) else {}
    configured_backup_count = proposer_count_policy.get("backup_count")
    configured_aggregator_candidate_count = aggregator_policy.get("candidate_count")
    if (
        isinstance(configured_backup_count, bool)
        or not isinstance(configured_backup_count, int)
        or not 0 <= configured_backup_count <= 2
        or isinstance(configured_aggregator_candidate_count, bool)
        or not isinstance(configured_aggregator_candidate_count, int)
        or not 1 <= configured_aggregator_candidate_count <= 3
    ):
        reasons.append("wrong_g1_ranking_roster_policy")
        configured_backup_count = -1
        configured_aggregator_candidate_count = -1
    n_min = plan.get("N_min")
    n_max = plan.get("N_max")
    recomputed_bounds = g1_recomputed_proposer_bounds(plan)
    if (
        isinstance(n_min, bool)
        or not isinstance(n_min, int)
        or isinstance(n_max, bool)
        or not isinstance(n_max, int)
        or not 1 <= n_min <= n_max <= formal_n_max
    ):
        reasons.append("wrong_g1_proposer_bounds")
    if (
        recomputed_bounds is None
        or (n_min, n_max) != recomputed_bounds[:2]
        or plan.get("bound_reasons") != recomputed_bounds[2]
    ):
        reasons.append("g1_proposer_bounds_not_recomputed")
    selected_p = plan.get("selected_P")
    if (
        not isinstance(selected_p, list)
        or not selected_p
        or len(set(str(value) for value in selected_p)) != len(selected_p)
        or any(str(value) not in expected_identities for value in selected_p)
    ):
        reasons.append("wrong_g1_selected_proposers")
        selected_proposer_models: tuple[str, ...] = ()
    else:
        selected_proposer_models = tuple(str(identity).partition(":")[2] for identity in selected_p)
        if not (
            isinstance(n_min, int)
            and not isinstance(n_min, bool)
            and isinstance(n_max, int)
            and not isinstance(n_max, bool)
            and n_min <= len(selected_proposer_models) <= n_max
        ):
            reasons.append("g1_selected_proposer_count_outside_bounds")
    expanded_slot_identities, expanded_slot_reasons = expanded_proposer_slot_identities(plan)
    reasons.extend(expanded_slot_reasons)
    proposer_models = tuple(identity.partition(":")[2] for identity in expanded_slot_identities)
    selected_a = str(plan.get("selected_A") or "")
    aggregator_model = selected_a.partition(":")[2] if selected_a in expected_identities else ""
    if not aggregator_model:
        reasons.append("wrong_g1_selected_aggregator")
    backup_p = plan.get("backup_P")
    plan_proposer_recovery_policy = plan.get("proposer_recovery_policy")
    if backup_p is None and plan_proposer_recovery_policy is None:
        # Historical plans predate provider-owned proposer recovery.  They
        # remain auditable under their original frozen contract.
        pass
    elif not isinstance(plan_proposer_recovery_policy, Mapping):
        reasons.append("invalid_g1_proposer_recovery_policy")
    else:
        normalized_backups = (
            [str(identity or "") for identity in backup_p] if isinstance(backup_p, list) else []
        )
        aggregator_candidates = plan.get("aggregator_candidates")
        normalized_aggregators = (
            [str(identity or "") for identity in aggregator_candidates]
            if isinstance(aggregator_candidates, list)
            else []
        )
        expected_proposer_recovery_policy = {
            **proposer_recovery_policy,
            "configured_backup_count": configured_backup_count,
        }
        effective_backup_count = plan_proposer_recovery_policy.get("effective_backup_count")
        effective_backup_count_valid = bool(
            isinstance(effective_backup_count, int)
            and not isinstance(effective_backup_count, bool)
            and 0 <= effective_backup_count <= configured_backup_count
        )
        if effective_backup_count_valid:
            expected_proposer_recovery_policy["effective_backup_count"] = effective_backup_count
        if dict(plan_proposer_recovery_policy) != expected_proposer_recovery_policy:
            reasons.append("wrong_g1_proposer_recovery_policy")
        if (
            not effective_backup_count_valid
            or not isinstance(backup_p, list)
            or len(normalized_backups) != effective_backup_count
            or len(set(normalized_backups)) != effective_backup_count
            or any(identity not in expected_identities for identity in normalized_backups)
            or bool(set(normalized_backups).intersection(str(value) for value in selected_p or []))
            or bool(set(normalized_backups).intersection(normalized_aggregators))
            or plan.get("configured_proposer_backup_count") != configured_backup_count
            or plan.get("effective_proposer_backup_count") != effective_backup_count
            or plan.get("effective_min_successful_proposers") != 2
        ):
            reasons.append("wrong_g1_proposer_recovery_roster")
        try:
            from opensquilla.provider.protocol import (
                provider_retry_roster_fingerprint,
            )

            fingerprint = provider_retry_roster_fingerprint(plan)
        except (TypeError, ValueError):
            fingerprint = ""
        if HEX64.fullmatch(fingerprint) is None:
            reasons.append("invalid_g1_proposer_recovery_fingerprint")
    recovery_fields_present = any(
        field in plan
        for field in (
            "aggregator_candidates",
            *aggregator_recovery_policy,
        )
    )
    if not recovery_fields_present:
        reasons.append("missing_g1_aggregator_recovery_policy")
    if recovery_fields_present:
        for field, expected in aggregator_recovery_policy.items():
            if plan.get(field) != expected:
                reasons.append(f"wrong_g1_{field}")
        aggregator_trace = plan.get("aggregator")
        score_rows = (
            aggregator_trace.get("scores") if isinstance(aggregator_trace, Mapping) else None
        )
        frozen_score_identities = (
            [str(row.get("identity") or "") for row in score_rows if isinstance(row, Mapping)]
            if isinstance(score_rows, list)
            else []
        )
        expected_chain = frozen_score_identities[:configured_aggregator_candidate_count]
        candidate_chain = plan.get("aggregator_candidates")
        required_candidate_count = configured_aggregator_candidate_count
        if (
            len(expected_chain) != required_candidate_count
            or not isinstance(candidate_chain, list)
            or len(candidate_chain) != required_candidate_count
            or candidate_chain != expected_chain
            or len(set(str(value) for value in candidate_chain)) != len(candidate_chain)
            or str(candidate_chain[0] if candidate_chain else "") != selected_a
            or plan.get("configured_aggregator_candidate_count")
            != configured_aggregator_candidate_count
            or plan.get("effective_aggregator_candidate_count") != required_candidate_count
        ):
            reasons.append("wrong_g1_aggregator_candidate_chain")
    if (
        nonnegative_int(plan.get("proposer_sample_count")) != len(proposer_models)
        or tuple(str(value) for value in plan.get("proposer_models") or []) != proposer_models
        or str(plan.get("aggregator_model") or "") != aggregator_model
    ):
        reasons.append("wrong_g1_physical_selection_plan")
    selection_steps = plan.get("selection_steps")
    if (
        not isinstance(selection_steps, list)
        or len(selection_steps) != len(selected_proposer_models)
        or any(
            not isinstance(step, Mapping)
            or step.get("step") != index
            or str(step.get("selected") or "") != selected_p[index - 1]
            for index, step in enumerate(selection_steps, start=1)
        )
        or plan.get("proposer_count") != len(selected_proposer_models)
    ):
        reasons.append("wrong_g1_selection_steps")
    try:
        from opensquilla.provider.ranking_router import (
            ranking_trace_replay_reasons,
        )

        reasons.extend(
            ranking_trace_replay_reasons(
                plan,
                allow_legacy_managed_v3=allow_legacy_managed_v3,
            )
        )
    except Exception:  # noqa: BLE001 - malformed evidence must fail closed
        reasons.append("g1_frozen_ranker_replay_failed")
    replay_contract = contract.get("task_analysis_execution")
    if replay_contract is not None:
        from opensquilla.provider.ranking_router import (
            frozen_task_analysis_plan_reasons,
        )

        reasons.extend(frozen_task_analysis_plan_reasons(plan, replay_contract))
    return list(dict.fromkeys(reasons)), proposer_models, aggregator_model


_G1_LIFECYCLE_PLAN_MATCH_FIELDS = (
    "decision_id",
    "registry_snapshot_hash",
    "ranking_config_hash",
    "selected_P",
    "backup_P",
    "selected_A",
    "proposer_models",
    "aggregator_model",
    "aggregator_candidates",
    "configured_proposer_backup_count",
    "effective_proposer_backup_count",
    "proposer_recovery_policy",
    "aggregator_recovery_mode",
    "aggregator_recovery_top_k",
    "aggregator_max_tokens_cap",
    "aggregator_visible_answer_reserve_tokens",
    "aggregator_prompt",
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
    "N_min",
    "N_max",
    "bound_reasons",
    "retry_parent_decision_id",
    "retry_excluded_proposer_identities",
    "task_analysis_reused",
    "task_analysis_reuse",
    "retry_routing",
)


def matching_saved_generation_attempts(
    row: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    final_sha = str(row.get("final_text_sha256") or "")
    selected_usage = usage_generation_identity_contract(row.get("usage"))
    execution = row.get("execution")
    attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping) and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    return [
        attempt
        for attempt in attempts
        if isinstance(attempt, Mapping)
        and isinstance(attempt.get("run"), Mapping)
        and str(attempt["run"].get("final_text_sha256") or "") == final_sha
        and usage_generation_identity_contract(attempt["run"].get("usage")) == selected_usage
    ]


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


def _g1_reasoning_only_length_failures(
    run: Mapping[str, Any],
) -> list[dict[str, Any]]:
    trace = run.get("ensemble_trace")
    calls, _ = ensemble_call_trace_sequence(trace if isinstance(trace, Mapping) else {})
    failures: list[dict[str, Any]] = []
    for call_index, call in enumerate(calls, start=1):
        candidates = call.get("candidates")
        if not isinstance(candidates, list):
            continue
        for candidate_index, candidate in enumerate(candidates):
            if not isinstance(candidate, Mapping) or candidate.get("ok") is True:
                continue
            stop_reason = str(candidate.get("stop_reason") or "").strip().casefold()
            if stop_reason not in REASONING_ONLY_LENGTH_STOP_REASONS:
                continue
            content = candidate.get("content")
            content_chars = content.get("chars") if isinstance(content, Mapping) else None
            visible_chars = (
                nonnegative_int(content_chars)
                if isinstance(content_chars, int | float) and not isinstance(content_chars, bool)
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
            reasoning_tokens = nonnegative_int(candidate.get("reasoning_tokens"))
            output_tokens = nonnegative_int(candidate.get("output_tokens"))
            if reasoning_tokens <= 0:
                for field in (
                    "model_usage_breakdown",
                    "diagnostic_model_usage_breakdown",
                ):
                    breakdown = candidate.get(field)
                    if isinstance(breakdown, list):
                        reasoning_tokens += sum(
                            nonnegative_int(unit.get("reasoning_tokens"))
                            for unit in breakdown
                            if isinstance(unit, Mapping)
                        )
            if output_tokens <= 0:
                breakdown = candidate.get("model_usage_breakdown")
                if isinstance(breakdown, list):
                    output_tokens = sum(
                        nonnegative_int(unit.get("output_tokens"))
                        for unit in breakdown
                        if isinstance(unit, Mapping)
                    )
            provider = (
                str(candidate.get("requested_provider") or candidate.get("provider") or "")
                .strip()
                .lower()
            )
            model = (
                str(candidate.get("requested_model") or candidate.get("model") or "")
                .strip()
                .lower()
            )
            if (
                visible_chars == 0
                and reasoning_tokens > 0
                and candidate.get("request_started") is True
                and nonnegative_int(candidate.get("physical_request_count")) > 0
                and provider
                and model
            ):
                actual_provider = str(candidate.get("provider") or provider).strip().lower()
                actual_model = str(candidate.get("model") or model).strip().lower()
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
                        "physical_request_count": nonnegative_int(
                            candidate.get("physical_request_count")
                        ),
                        "usage_reported": candidate.get("usage_reported") is True,
                        "usage_missing_count": nonnegative_int(
                            candidate.get("usage_missing_count")
                        ),
                        "stop_reason": stop_reason,
                        "visible_output_chars": visible_chars,
                        "input_tokens": nonnegative_int(candidate.get("input_tokens")),
                        "output_tokens": output_tokens,
                        "reasoning_tokens": reasoning_tokens,
                        "error": str(candidate.get("error") or ""),
                        "error_code": str(candidate.get("error_code") or ""),
                    }
                )
    return failures


def _g1_reasoning_only_length_failure_identities(run: Mapping[str, Any]) -> set[str]:
    return {
        str(failure.get("identity") or "") for failure in _g1_reasoning_only_length_failures(run)
    }


def _g1_task_analyzer_decision_projection(value: Any) -> Any:
    """Normalize receipt representation while retaining analyzer usage evidence."""

    from opensquilla.provider.thinking_execution import (
        immutable_task_analyzer_payload,
    )

    return immutable_task_analyzer_payload(value)


def _g1_lifecycle_plan_field(plan: Mapping[str, Any], field: str) -> Any:
    value = plan.get(field)
    if field == "task_analyzer":
        return _g1_task_analyzer_decision_projection(value)
    return value


def _g1_plans_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(
        _g1_lifecycle_plan_field(left, field) == _g1_lifecycle_plan_field(right, field)
        for field in _G1_LIFECYCLE_PLAN_MATCH_FIELDS
    )


def _g1_full_plans_match(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    """Compare every immutable field while excluding execution fallback receipts."""

    try:
        from opensquilla.provider.thinking_execution import (
            immutable_selection_plan_payload,
        )

        return canonical_sha256(immutable_selection_plan_payload(left)) == canonical_sha256(
            immutable_selection_plan_payload(right)
        )
    except (TypeError, ValueError):
        return False


def _g1_execution_plan_mutation_reasons(
    expected_plan: Mapping[str, Any],
    observed_plan: Mapping[str, Any],
) -> list[str]:
    """Validate provider-execution receipts excluded from decision-plan hashing."""

    from opensquilla.provider.thinking_execution import (
        validate_thinking_execution_plan_mutation,
    )

    return (
        ["invalid_g1_thinking_execution_plan_mutation"]
        if validate_thinking_execution_plan_mutation(
            expected_plan,
            observed_plan,
        )
        else []
    )


def _g1_retry_plan_provenance_reasons(
    plan: Mapping[str, Any],
    *,
    initial_plan: Mapping[str, Any],
    initial_decision_id: str,
    exclusions: set[str],
) -> list[str]:
    """Require bound-v2 provenance for every adaptive retry plan."""

    reasons: list[str] = []
    provenance_fields = (
        "retry_parent_decision_id",
        "retry_excluded_proposer_identities",
        "task_analysis_reused",
        "task_analysis_reuse",
        "retry_routing",
    )
    if not exclusions:
        if any(field in plan for field in provenance_fields):
            reasons.append("unexpected_g1_initial_retry_provenance")
        return reasons
    expected_exclusions = sorted(exclusions)
    if plan.get("retry_parent_decision_id") != initial_decision_id:
        reasons.append("wrong_g1_retry_parent_decision_id")
    if not str(plan.get("decision_id") or "") or plan.get("decision_id") == initial_decision_id:
        reasons.append("wrong_g1_retry_decision_id")
    if plan.get("retry_excluded_proposer_identities") != expected_exclusions:
        reasons.append("wrong_g1_retry_plan_exclusions")
    if plan.get("task_analysis_reused") is not True:
        reasons.append("missing_g1_retry_task_analysis_reuse")
    retry_routing = plan.get("retry_routing")
    if not isinstance(retry_routing, Mapping):
        reasons.append("missing_g1_retry_routing_provenance")
        return reasons
    retry_schema = retry_routing.get("schema")
    if retry_schema != "opensquilla.router-dynamic-retry-routing/v2":
        reasons.append("wrong_g1_retry_routing_schema")
    if retry_routing.get("reason") != "prior_attempt_reasoning_only_length":
        reasons.append("wrong_g1_retry_routing_reason")
    if retry_routing.get("parent_decision_id") != initial_decision_id:
        reasons.append("wrong_g1_retry_routing_parent_decision_id")
    if retry_routing.get("excluded_proposer_identities") != expected_exclusions:
        reasons.append("wrong_g1_retry_routing_exclusions")
    if retry_routing.get("task_analysis_reused") is not True:
        reasons.append("missing_g1_retry_routing_task_analysis_reuse")
    if retry_schema == "opensquilla.router-dynamic-retry-routing/v2":
        from opensquilla.provider.ranking_router import (
            router_dynamic_task_analysis_reuse_reasons,
        )

        reasons.extend(
            router_dynamic_task_analysis_reuse_reasons(
                initial_plan,
                plan,
            )
        )
        binding = plan.get("task_analysis_reuse")
        binding_hash = (
            str(binding.get("projection_sha256") or "") if isinstance(binding, Mapping) else ""
        )
        if retry_routing.get("task_analysis_source_decision_id") != initial_decision_id:
            reasons.append("wrong_g1_retry_routing_task_analysis_source_decision")
        if retry_routing.get("task_analysis_reuse_sha256") != binding_hash:
            reasons.append("wrong_g1_retry_routing_task_analysis_reuse_hash")
    return list(dict.fromkeys(reasons))


def _adaptive_g1_lifecycle_routing(
    row: Mapping[str, Any],
    *,
    registry: Mapping[str, Any],
    physical_plans: Sequence[Mapping[str, Any]],
    initial_reasons: Sequence[str],
    allow_legacy_managed_v3: bool = False,
    aggregator_recovery_policy: Mapping[str, Any] = FORMAL_AGGREGATOR_RECOVERY_POLICY,
    proposer_recovery_policy: Mapping[str, Any] = FORMAL_PROPOSER_RECOVERY_POLICY,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Validate per-attempt G1 retry plans without requiring identical rosters."""

    reasons = list(initial_reasons)
    matching = matching_saved_generation_attempts(row)
    if len(matching) != 1:
        reasons.append("ambiguous_g1_selected_generation_attempt")
    selected = matching[0] if len(matching) == 1 else None
    selected_attempt_id = (
        str(selected.get("attempt_id") or "") if isinstance(selected, Mapping) else ""
    )
    selected_ordinal = (
        nonnegative_int(selected.get("attempt")) if isinstance(selected, Mapping) else 0
    )
    execution = row.get("execution")
    attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping) and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    expected_exclusions: set[str] = set()
    selected_plan: Mapping[str, Any] | None = None
    selected_routing: Mapping[str, Any] | None = None
    validated_plan_count = 0
    validated_physical_plan_count = 0
    previous_ordinal = 0
    previous_plan: Mapping[str, Any] | None = None
    pending_retry_plan: Mapping[str, Any] | None = None
    pending_retry_exclusions: set[str] | None = None
    initial_plan: Mapping[str, Any] | None = None
    initial_decision_id = ""
    thinking_plan_prefix_by_decision: dict[str, Mapping[str, Any]] = {}
    thinking_execution_history: list[dict[str, Any]] = []
    provider_native_terminal_seen = False
    validated_provider_native_receipt_count = 0
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            reasons.append("invalid_g1_attempt_evidence")
            continue
        ordinal = nonnegative_int(attempt.get("attempt"))
        if ordinal <= 0:
            reasons.append("invalid_g1_attempt_ordinal_sequence")
            continue
        if ordinal <= previous_ordinal:
            reasons.append("invalid_g1_attempt_ordinal_sequence")
        previous_ordinal = ordinal
        run = attempt.get("run")
        run_map = run if isinstance(run, Mapping) else {}
        attempt_has_ensemble_requests = run_expected_ensemble_request_count(run_map) > 0
        if provider_native_terminal_seen and attempt_has_ensemble_requests:
            reasons.append("g1_provider_native_outer_retry_forbidden")
        routing = run_map.get("routing_trace")
        routing_map = routing if isinstance(routing, Mapping) else {}
        routed_plan = routing_map.get("selection_plan")
        declared_plan = attempt.get("selection_plan")
        if not isinstance(declared_plan, Mapping):
            reasons.append("missing_g1_attempt_selection_plan")
            continue
        plan = declared_plan
        if not isinstance(routed_plan, Mapping):
            reasons.append("missing_g1_attempt_routing_plan")
        validated_plan_count += 1
        plan_reasons, _, _ = g1_registry_plan_reasons(
            plan,
            contract=registry,
            allow_legacy_managed_v3=allow_legacy_managed_v3,
            aggregator_recovery_policy=aggregator_recovery_policy,
            proposer_recovery_policy=proposer_recovery_policy,
        )
        reasons.extend(plan_reasons)
        if isinstance(routed_plan, Mapping):
            routed_reasons, _, _ = g1_registry_plan_reasons(
                routed_plan,
                contract=registry,
                allow_legacy_managed_v3=allow_legacy_managed_v3,
                aggregator_recovery_policy=aggregator_recovery_policy,
                proposer_recovery_policy=proposer_recovery_policy,
            )
            reasons.extend(routed_reasons)
            if not _g1_full_plans_match(declared_plan, routed_plan):
                reasons.append("g1_attempt_selection_plan_differs_from_routing")
            reasons.extend(
                _g1_execution_plan_mutation_reasons(
                    declared_plan,
                    routed_plan,
                )
            )

        provider_native_policy = plan.get("proposer_recovery_policy") is not None
        if provider_native_policy:
            if attempt.get("proposer_recovery_owner") != "provider":
                reasons.append("missing_g1_provider_native_recovery_owner")
        elif attempt.get("proposer_recovery_owner") is not None:
            reasons.append("unexpected_g1_provider_native_recovery_owner")

        if not initial_decision_id:
            initial_decision_id = str(plan.get("decision_id") or "")
            initial_plan = plan

        exclusion_sources: list[tuple[set[str], bool]] = [
            _normalized_g1_retry_identities(attempt.get("excluded_proposer_identities"))
        ]
        if "retry_excluded_proposer_identities" in plan:
            exclusion_sources.append(
                _normalized_g1_retry_identities(plan.get("retry_excluded_proposer_identities"))
            )
        retry_routing = plan.get("retry_routing")
        if isinstance(retry_routing, Mapping):
            exclusion_sources.append(
                _normalized_g1_retry_identities(retry_routing.get("excluded_proposer_identities"))
            )
        if any(not valid for _, valid in exclusion_sources):
            reasons.append("invalid_g1_retry_excluded_proposer_identities")
        current_exclusions = exclusion_sources[0][0]
        attempt_exclusions_raw = attempt.get("excluded_proposer_identities")
        if attempt_exclusions_raw != sorted(current_exclusions):
            reasons.append("noncanonical_g1_attempt_exclusions")
        if any(values != current_exclusions for values, _ in exclusion_sources[1:]):
            reasons.append("conflicting_g1_retry_excluded_proposer_identities")
        if pending_retry_plan is not None:
            if not _g1_full_plans_match(plan, pending_retry_plan):
                reasons.append("g1_retry_selection_plan_not_used_by_next_attempt")
            reasons.extend(
                _g1_execution_plan_mutation_reasons(
                    pending_retry_plan,
                    plan,
                )
            )
            if current_exclusions != (pending_retry_exclusions or set()):
                reasons.append("g1_retry_exclusions_not_used_by_next_attempt")
            pending_retry_plan = None
            pending_retry_exclusions = None
        elif previous_plan is not None:
            if not _g1_full_plans_match(plan, previous_plan):
                reasons.append("g1_attempt_plan_changed_without_retry_selection")
            reasons.extend(
                _g1_execution_plan_mutation_reasons(
                    previous_plan,
                    plan,
                )
            )
        if not expected_exclusions.issubset(current_exclusions):
            reasons.append("nonmonotonic_g1_retry_exclusions")
        if current_exclusions != expected_exclusions:
            reasons.append("wrong_g1_retry_exclusion_evolution")
        selected_p = {
            str(identity or "").strip().lower() for identity in plan.get("selected_P") or []
        }
        if selected_p & current_exclusions:
            reasons.append("g1_retry_selected_excluded_proposer")
        reasons.extend(
            _g1_retry_plan_provenance_reasons(
                plan,
                initial_plan=initial_plan or plan,
                initial_decision_id=initial_decision_id,
                exclusions=current_exclusions,
            )
        )

        attempt_trace = run_map.get("ensemble_trace")
        attempt_calls: list[Mapping[str, Any]] = []
        if isinstance(attempt_trace, Mapping) and attempt_trace:
            attempt_calls, attempt_sequence_reasons = ensemble_call_trace_sequence(attempt_trace)
            reasons.extend(attempt_sequence_reasons)
            if not attempt_calls:
                reasons.append("missing_g1_attempt_physical_selection_plan")
            decision_key = str(plan.get("decision_id") or "")
            previous_thinking_plan = thinking_plan_prefix_by_decision.get(decision_key)
            if previous_thinking_plan is None:
                previous_thinking_plan = plan
            for call in attempt_calls:
                physical_plan = call.get("selection_plan")
                physical_reasons, _, _ = g1_registry_plan_reasons(
                    physical_plan,
                    contract=registry,
                    allow_legacy_managed_v3=allow_legacy_managed_v3,
                    aggregator_recovery_policy=aggregator_recovery_policy,
                    proposer_recovery_policy=proposer_recovery_policy,
                )
                reasons.extend(physical_reasons)
                if not isinstance(physical_plan, Mapping):
                    reasons.append("missing_g1_attempt_physical_selection_plan")
                    continue
                validated_physical_plan_count += 1
                if not _g1_full_plans_match(plan, physical_plan):
                    reasons.append("g1_attempt_selection_plan_differs_from_physical_plan")
                reasons.extend(
                    _g1_execution_plan_mutation_reasons(
                        plan,
                        physical_plan,
                    )
                )
                from opensquilla.provider.thinking_execution import (
                    validate_thinking_execution_call,
                )

                validated_thinking_plan, execution_reason = validate_thinking_execution_call(
                    previous_thinking_plan,
                    call,
                )
                if execution_reason:
                    reasons.append("invalid_g1_physical_thinking_execution")
                else:
                    previous_thinking_plan = validated_thinking_plan
                if not isinstance(previous_thinking_plan, Mapping):
                    reasons.append("invalid_g1_physical_thinking_execution")
                if isinstance(routed_plan, Mapping) and not _g1_full_plans_match(
                    routed_plan,
                    physical_plan,
                ):
                    reasons.append("g1_attempt_routing_plan_differs_from_physical_plan")
                if isinstance(routed_plan, Mapping):
                    reasons.extend(
                        _g1_execution_plan_mutation_reasons(
                            routed_plan,
                            physical_plan,
                        )
                    )
                if provider_native_policy:
                    _, _, provider_recovery_reasons = proposer_recovery_execution_reasons(
                        call,
                        executed_plan=physical_plan,
                        expected_policy=proposer_recovery_policy,
                    )
                    reasons.extend(provider_recovery_reasons)
                    validated_provider_native_receipt_count += 1
                thinking_execution_history.append(copy.deepcopy(dict(physical_plan)))
            thinking_plan_prefix_by_decision[decision_key] = previous_thinking_plan
        elif attempt_has_ensemble_requests:
            reasons.append("missing_g1_attempt_ensemble_trace")

        if provider_native_policy:
            if attempt.get("deterministic_proposer_failures") != []:
                reasons.append("unexpected_g1_provider_native_outer_failure_evidence")
            if attempt.get("excluded_proposer_identities") != [] or current_exclusions:
                reasons.append("unexpected_g1_provider_native_outer_exclusions")
            if any(
                field in attempt
                for field in (
                    "retry_selection_plan",
                    "retry_excluded_proposer_identities",
                    "retry_deferred_to_next_wave",
                    "thinking_execution_projection",
                )
            ):
                reasons.append("unexpected_g1_provider_native_outer_retry_plan")
            retry_backoff = attempt.get("retry_backoff_s")
            if (
                attempt.get("will_retry") is not False
                or isinstance(retry_backoff, bool)
                or not isinstance(retry_backoff, int | float)
                or float(retry_backoff) != 0.0
            ):
                reasons.append("g1_provider_native_outer_retry_not_suppressed")
            if (
                attempt.get("retry_reason")
                and not str(attempt.get("retry_suppressed_reason") or "").strip()
            ):
                reasons.append("g1_provider_native_terminal_reason_not_suppressed")
            if attempt_has_ensemble_requests:
                provider_native_terminal_seen = True
                reasons.extend(g1_thinking_physical_usage_binding_reasons(run_map))
            expected_exclusions = current_exclusions
            if selected is not None and str(attempt.get("attempt_id") or "") == selected_attempt_id:
                selected_plan = plan
                selected_routing = routing_map
            previous_plan = plan
            continue

        derived_failure_rows = _g1_reasoning_only_length_failures(run_map)
        derived_failures = {str(failure.get("identity") or "") for failure in derived_failure_rows}
        recorded_failures = attempt.get("deterministic_proposer_failures")
        normalized_recorded_failures = (
            [dict(failure) for failure in recorded_failures]
            if isinstance(recorded_failures, list)
            and all(isinstance(failure, Mapping) for failure in recorded_failures)
            else None
        )
        if normalized_recorded_failures != derived_failure_rows:
            reasons.append("wrong_g1_deterministic_proposer_failure_evidence")
        if not derived_failures.issubset(selected_p):
            reasons.append("g1_retry_failure_outside_attempt_roster")
        next_exclusions = current_exclusions | derived_failures

        retry_selection_plan = attempt.get("retry_selection_plan")
        retry_exclusions_raw = attempt.get("retry_excluded_proposer_identities")
        retry_deferred = attempt.get("retry_deferred_to_next_wave")
        if retry_deferred is not None and type(retry_deferred) is not bool:
            reasons.append("invalid_g1_retry_deferred_marker")
        if isinstance(retry_selection_plan, Mapping):
            if (initial_plan or plan).get(
                "ranking_thinking_assignment_enabled"
            ) is True or retry_selection_plan.get("ranking_thinking_assignment_enabled") is True:
                from opensquilla.provider.thinking_execution import (
                    validate_thinking_execution_history_closure,
                )

                projected_retry_plan, projection_audit, projection_reason = (
                    validate_thinking_execution_history_closure(
                        thinking_execution_history,
                        retry_selection_plan,
                    )
                )
                if projection_reason:
                    reasons.append("invalid_g1_thinking_execution_projection:" + projection_reason)
                elif retry_selection_plan.get(
                    "executed_thinking_assignment"
                ) != projected_retry_plan.get(
                    "executed_thinking_assignment"
                ) or retry_selection_plan.get(
                    "thinking_execution_fallbacks", []
                ) != projected_retry_plan.get("thinking_execution_fallbacks", []):
                    reasons.append("g1_retry_thinking_execution_projection_differs")
                if attempt.get("thinking_execution_projection") != projection_audit:
                    reasons.append("wrong_g1_thinking_execution_projection_audit")
            retry_plan_reasons, _, _ = g1_registry_plan_reasons(
                retry_selection_plan,
                contract=registry,
                allow_legacy_managed_v3=allow_legacy_managed_v3,
                aggregator_recovery_policy=aggregator_recovery_policy,
                proposer_recovery_policy=proposer_recovery_policy,
            )
            reasons.extend(retry_plan_reasons)
            retry_exclusions, retry_exclusions_valid = _normalized_g1_retry_identities(
                retry_exclusions_raw
            )
            if not retry_exclusions_valid:
                reasons.append("invalid_g1_retry_next_exclusions")
            if retry_exclusions_raw != sorted(retry_exclusions):
                reasons.append("invalid_g1_retry_next_exclusions")
            if retry_exclusions != next_exclusions:
                reasons.append("wrong_g1_retry_next_exclusion_evolution")
            retry_selected_p = {
                str(identity or "").strip().lower()
                for identity in retry_selection_plan.get("selected_P") or []
            }
            if retry_selected_p & retry_exclusions:
                reasons.append("g1_retry_selection_selected_excluded_proposer")
            reasons.extend(
                _g1_retry_plan_provenance_reasons(
                    retry_selection_plan,
                    initial_plan=initial_plan or plan,
                    initial_decision_id=initial_decision_id,
                    exclusions=retry_exclusions,
                )
            )
            if (attempt.get("will_retry") is True) == (
                retry_deferred is True
            ) or not derived_failures:
                reasons.append("unexpected_g1_retry_selection_plan")
            pending_retry_plan = retry_selection_plan
            pending_retry_exclusions = retry_exclusions
        elif retry_selection_plan is not None:
            reasons.append("invalid_g1_retry_selection_plan")
        elif retry_exclusions_raw not in (None, []):
            reasons.append("g1_retry_exclusions_without_selection_plan")
        elif retry_deferred is True:
            reasons.append("g1_retry_deferred_without_selection_plan")
        elif attempt.get("will_retry") is True and derived_failures:
            reasons.append("missing_g1_retry_selection_plan")

        expected_exclusions = next_exclusions

        if selected is not None and str(attempt.get("attempt_id") or "") == selected_attempt_id:
            selected_plan = plan
            selected_routing = routing_map
        previous_plan = plan

    if validated_plan_count <= 0:
        reasons.append("missing_g1_attempt_selection_plan")
    if pending_retry_plan is not None:
        reasons.append("dangling_g1_retry_selection_plan")
    if selected_plan is None:
        reasons.append("missing_g1_selected_attempt_selection_plan")
        return {}, {}, list(dict.fromkeys(reasons))
    for physical in physical_plans:
        if not _g1_full_plans_match(selected_plan, physical):
            reasons.append("g1_selected_attempt_plan_differs_from_physical_plan")
        reasons.extend(
            _g1_execution_plan_mutation_reasons(
                selected_plan,
                physical,
            )
        )

    top_routing = row.get("routing_trace")
    top_routing_map = top_routing if isinstance(top_routing, Mapping) else {}
    top_plan = top_routing_map.get("selection_plan")
    if isinstance(top_plan, Mapping):
        top_reasons, _, _ = g1_registry_plan_reasons(
            top_plan,
            contract=registry,
            allow_legacy_managed_v3=allow_legacy_managed_v3,
            aggregator_recovery_policy=aggregator_recovery_policy,
            proposer_recovery_policy=proposer_recovery_policy,
        )
        reasons.extend(top_reasons)
        if not _g1_full_plans_match(selected_plan, top_plan):
            reasons.append("g1_routing_plan_differs_from_selected_attempt")
        reasons.extend(
            _g1_execution_plan_mutation_reasons(
                selected_plan,
                top_plan,
            )
        )
        effective_routing = dict(top_routing_map)
    elif top_routing_map:
        reasons.append("invalid_g1_top_routing_trace")
        effective_routing = {}
    elif isinstance(selected_routing, Mapping) and selected_routing:
        effective_routing = dict(selected_routing)
    else:
        effective_routing = {"selection_plan": dict(selected_plan)}

    evidence = {
        "schema": "opensquilla.draco.g1-provider-lifecycle-routing-recovery/v1",
        "source_attempt_id": selected_attempt_id,
        "source_attempt": selected_ordinal,
        "selected_attempt_id": selected_attempt_id,
        "selected_attempt": selected_ordinal,
        "decision_id": str(selected_plan.get("decision_id") or ""),
        "registry_snapshot_hash": str(selected_plan.get("registry_snapshot_hash") or ""),
        "ranking_config_hash": str(selected_plan.get("ranking_config_hash") or ""),
        "validated_attempt_plan_count": validated_plan_count,
        "validated_attempt_physical_plan_count": validated_physical_plan_count,
        "validated_provider_native_receipt_count": (validated_provider_native_receipt_count),
    }
    return effective_routing, evidence, list(dict.fromkeys(reasons))


def effective_g1_lifecycle_routing(
    row: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Resolve one G1 plan from the same row/provider lifecycle."""

    allow_legacy_managed_v3 = legacy_managed_v3_source_authenticated(contract)
    registry = contract.get("g1_registry_contract")
    if not isinstance(registry, Mapping):
        return {}, {}, ["invalid_g1_registry_contract"]
    aggregator_recovery_policy, proposer_recovery_policy = contract_recovery_policies(contract)
    reasons: list[str] = []
    trace = row.get("ensemble_trace")
    calls, sequence_reasons = ensemble_call_trace_sequence(
        trace if isinstance(trace, Mapping) else {}
    )
    reasons.extend(sequence_reasons)
    physical_plans: list[Mapping[str, Any]] = []
    for call in calls:
        plan = call.get("selection_plan")
        plan_reasons, _, _ = g1_registry_plan_reasons(
            plan,
            contract=registry,
            allow_legacy_managed_v3=allow_legacy_managed_v3,
            aggregator_recovery_policy=aggregator_recovery_policy,
            proposer_recovery_policy=proposer_recovery_policy,
        )
        reasons.extend(plan_reasons)
        if isinstance(plan, Mapping):
            physical_plans.append(plan)
    if not physical_plans:
        reasons.append("missing_g1_physical_selection_plan")
        return {}, {}, list(dict.fromkeys(reasons))
    execution = row.get("execution")
    generation_attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping) and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    if any(
        isinstance(attempt, Mapping)
        and (
            "selection_plan" in attempt
            or "excluded_proposer_identities" in attempt
            or "deterministic_proposer_failures" in attempt
            or "retry_selection_plan" in attempt
            or "retry_excluded_proposer_identities" in attempt
        )
        for attempt in generation_attempts
    ):
        return _adaptive_g1_lifecycle_routing(
            row,
            registry=registry,
            physical_plans=physical_plans,
            initial_reasons=reasons,
            allow_legacy_managed_v3=allow_legacy_managed_v3,
            aggregator_recovery_policy=aggregator_recovery_policy,
            proposer_recovery_policy=proposer_recovery_policy,
        )
    physical = physical_plans[0]
    for plan in physical_plans[1:]:
        if not _g1_plans_match(plan, physical):
            reasons.append("conflicting_g1_physical_selection_plans")

    routing = row.get("routing_trace")
    top_routing = dict(routing) if isinstance(routing, Mapping) else {}
    top_plan = top_routing.get("selection_plan")
    if isinstance(top_plan, Mapping):
        top_reasons, _, _ = g1_registry_plan_reasons(
            top_plan,
            contract=registry,
            allow_legacy_managed_v3=allow_legacy_managed_v3,
            aggregator_recovery_policy=aggregator_recovery_policy,
            proposer_recovery_policy=proposer_recovery_policy,
        )
        reasons.extend(top_reasons)
        if not _g1_plans_match(top_plan, physical):
            reasons.append("g1_routing_plan_differs_from_physical_plan")
        return top_routing, {}, list(dict.fromkeys(reasons))
    if top_routing:
        reasons.append("invalid_g1_top_routing_trace")
        return {}, {}, list(dict.fromkeys(reasons))

    matching = matching_saved_generation_attempts(row)
    if len(matching) != 1:
        reasons.append("ambiguous_g1_selected_generation_attempt")
        return {}, {}, list(dict.fromkeys(reasons))
    selected = matching[0]
    selected_ordinal = nonnegative_int(selected.get("attempt"))
    execution = row.get("execution")
    attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping) and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    candidates: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        run = attempt.get("run")
        attempt_routing = run.get("routing_trace") if isinstance(run, Mapping) else None
        plan = (
            attempt_routing.get("selection_plan") if isinstance(attempt_routing, Mapping) else None
        )
        plan_reasons, _, _ = g1_registry_plan_reasons(
            plan,
            contract=registry,
            allow_legacy_managed_v3=allow_legacy_managed_v3,
            aggregator_recovery_policy=aggregator_recovery_policy,
            proposer_recovery_policy=proposer_recovery_policy,
        )
        if plan_reasons:
            if isinstance(plan, Mapping):
                reasons.extend(plan_reasons)
            continue
        if not isinstance(plan, Mapping) or not _g1_plans_match(plan, physical):
            reasons.append("g1_lifecycle_plan_differs_from_physical_plan")
            continue
        if nonnegative_int(attempt.get("attempt")) > selected_ordinal:
            continue
        candidates.append((attempt, attempt_routing))
    unique = {
        canonical_sha256(dict(candidate_routing)): (attempt, candidate_routing)
        for attempt, candidate_routing in candidates
    }
    if len(unique) != 1:
        reasons.append("ambiguous_g1_lifecycle_routing_plan")
        return {}, {}, list(dict.fromkeys(reasons))
    attempt, recovered = next(iter(unique.values()))
    evidence = {
        "schema": "opensquilla.draco.g1-provider-lifecycle-routing-recovery/v1",
        "source_attempt_id": str(attempt.get("attempt_id") or ""),
        "source_attempt": nonnegative_int(attempt.get("attempt")),
        "selected_attempt_id": str(selected.get("attempt_id") or ""),
        "selected_attempt": selected_ordinal,
        "decision_id": str(physical.get("decision_id") or ""),
        "registry_snapshot_hash": str(physical.get("registry_snapshot_hash") or ""),
        "ranking_config_hash": str(physical.get("ranking_config_hash") or ""),
    }
    return dict(recovered), evidence, list(dict.fromkeys(reasons))


def validate_g1_paid_attempt_plan_history(
    records: Sequence[SourceRecord],
    *,
    contracts: Mapping[str, Mapping[str, Any]],
) -> None:
    """Make every paid adaptive G1 attempt auditable across all source waves."""

    group_contract = contracts.get("G1")
    allow_legacy_managed_v3 = legacy_managed_v3_source_authenticated(group_contract)
    registry = (
        group_contract.get("g1_registry_contract") if isinstance(group_contract, Mapping) else None
    )
    if not isinstance(registry, Mapping):
        raise FinalizationError("G1 paid-attempt audit lacks a registry contract")
    aggregator_recovery_policy, proposer_recovery_policy = contract_recovery_policies(
        group_contract
    )
    analyzer_policy = g1_task_analyzer_execution_policy(registry)
    if analyzer_policy is None:
        raise FinalizationError("G1 paid-attempt audit lacks an authenticated analyzer policy")
    declared_analyzer_execution_contract = group_contract.get(
        "task_analyzer_execution_contract"
    )
    analyzer_chain = g1_task_analyzer_execution_chain(
        registry,
        declared_analyzer_execution_contract
        if isinstance(declared_analyzer_execution_contract, Mapping)
        else None,
    )
    if analyzer_chain is None:
        raise FinalizationError("G1 paid-attempt audit lacks an authenticated analyzer chain")
    analyzer_execution_contract = g1_task_analyzer_execution_contract(
        registry,
        declared_analyzer_execution_contract
        if isinstance(declared_analyzer_execution_contract, Mapping)
        else None,
    )
    if analyzer_execution_contract is None:
        raise FinalizationError(
            "G1 paid-attempt audit lacks an authenticated analyzer execution contract"
        )
    replay_contract = registry.get("task_analysis_execution")
    if replay_contract is not None:
        from opensquilla.provider.ranking_router import (
            frozen_task_analysis_plan_reasons,
        )
    else:
        frozen_task_analysis_plan_reasons = None
    violations: list[dict[str, Any]] = []
    campaign_analyzer_physical_ids: set[str] = set()
    selected_only_reasons = {
        "ambiguous_g1_selected_generation_attempt",
        "missing_g1_selected_attempt_selection_plan",
    }
    task_records: dict[tuple[str, str], list[SourceRecord]] = {}
    for record in records:
        if record.key[0] != "G1":
            continue
        task_records.setdefault(record.key, []).append(record)

    for task_key, task_history in task_records.items():
        attempts_by_id: dict[str, dict[str, Any]] = {}
        attempt_payload_hashes: dict[str, str] = {}
        attempt_references: dict[str, dict[str, Any]] = {}
        history_is_adaptive = False
        duplicate_conflict = False
        for record in task_history:
            execution = record.row.get("execution")
            raw_attempts = (
                execution.get("generation_attempts")
                if isinstance(execution, Mapping)
                and isinstance(execution.get("generation_attempts"), list)
                else []
            )
            for attempt in raw_attempts:
                if not isinstance(attempt, Mapping):
                    continue
                run = attempt.get("run")
                run_map = run if isinstance(run, Mapping) else {}
                trace_events = run_map.get("trace_events")
                routing = run_map.get("routing_trace")
                if any(
                    isinstance(event, Mapping) and event.get("code") == "g1_pre_call_guard_failed"
                    for event in (trace_events if isinstance(trace_events, list) else [])
                ) or (
                    isinstance(routing, Mapping)
                    and isinstance(
                        routing.get("pre_call_guard"),
                        Mapping,
                    )
                ):
                    violations.append(
                        record.reference
                        | {
                            "group": task_key[0],
                            "task_id": task_key[1],
                            "attempt_id": str(attempt.get("attempt_id") or ""),
                            "reasons": ["g1_pre_call_guard_failed"],
                        }
                    )
                    duplicate_conflict = True
                    continue
                routed_plan = (
                    routing.get("selection_plan") if isinstance(routing, Mapping) else None
                )
                trace = run_map.get("ensemble_trace")
                physical_calls, _ = ensemble_call_trace_sequence(
                    trace if isinstance(trace, Mapping) else {}
                )
                paid_analyzer_setup = bool(
                    attempt.get("attempt_kind") == "provider_build_after_paid_setup"
                )
                managed_runtime_plans = [
                    plan
                    for plan in (
                        routed_plan,
                        *(call.get("selection_plan") for call in physical_calls),
                    )
                    if isinstance(plan, Mapping)
                    and plan.get("ranking_thinking_assignment_enabled") is True
                ]
                current_adaptive_runtime_plans = [
                    plan
                    for plan in managed_runtime_plans
                    if not (
                        allow_legacy_managed_v3
                        and plan.get("ranking_version") == LEGACY_THINKING_RANKING_VERSION
                        and not plan.get("thinking_physical_evidence_schema")
                    )
                ]
                runtime_requires_adaptive = bool(
                    paid_analyzer_setup or current_adaptive_runtime_plans
                )
                adaptive = any(
                    field in attempt
                    for field in (
                        "selection_plan",
                        "excluded_proposer_identities",
                        "deterministic_proposer_failures",
                        "retry_selection_plan",
                        "retry_excluded_proposer_identities",
                    )
                )
                history_is_adaptive = history_is_adaptive or adaptive or runtime_requires_adaptive
                if runtime_requires_adaptive and not all(
                    field in attempt
                    for field in (
                        "selection_plan",
                        "excluded_proposer_identities",
                        "deterministic_proposer_failures",
                    )
                ):
                    violations.append(
                        record.reference
                        | {
                            "group": task_key[0],
                            "task_id": task_key[1],
                            "attempt_id": str(attempt.get("attempt_id") or ""),
                            "reasons": ["missing_adaptive_g1_attempt_evidence"],
                        }
                    )
                    duplicate_conflict = True
                    continue
                if not adaptive:
                    continue
                attempt_id = str(attempt.get("attempt_id") or "")
                ordinal = nonnegative_int(attempt.get("attempt"))
                if not attempt_id or ordinal <= 0:
                    violations.append(
                        record.reference
                        | {
                            "group": task_key[0],
                            "task_id": task_key[1],
                            "reasons": ["invalid_g1_attempt_identity_or_ordinal"],
                        }
                    )
                    duplicate_conflict = True
                    continue
                normalized = copy.deepcopy(dict(attempt))
                payload_hash = canonical_sha256(immutable_attempt_payload(normalized))
                previous_hash = attempt_payload_hashes.get(attempt_id)
                if previous_hash is not None and previous_hash != payload_hash:
                    violations.append(
                        record.reference
                        | {
                            "group": task_key[0],
                            "task_id": task_key[1],
                            "attempt_id": attempt_id,
                            "reasons": ["conflicting_cross_wave_g1_attempt_evidence"],
                        }
                    )
                    duplicate_conflict = True
                    continue
                attempts_by_id.setdefault(attempt_id, normalized)
                attempt_payload_hashes[attempt_id] = payload_hash
                attempt_references.setdefault(attempt_id, record.reference)
        if duplicate_conflict or not history_is_adaptive or not attempts_by_id:
            continue

        ordered_attempts = sorted(
            attempts_by_id.values(),
            key=lambda attempt: (
                nonnegative_int(attempt.get("attempt")),
                str(attempt.get("attempt_id") or ""),
            ),
        )
        ordinals = [nonnegative_int(attempt.get("attempt")) for attempt in ordered_attempts]
        expected_ordinals = list(range(1, len(ordered_attempts) + 1))
        if ordinals != expected_ordinals:
            violations.append(
                task_history[-1].reference
                | {
                    "group": task_key[0],
                    "task_id": task_key[1],
                    "attempt_ordinals": ordinals,
                    "reasons": ["noncontiguous_cross_wave_g1_attempt_history"],
                }
            )
            continue

        paid_attempt_count = sum(
            1
            for attempt in ordered_attempts
            if isinstance(attempt.get("run"), Mapping)
            and run_expected_request_count(attempt["run"]) > 0
        )
        if paid_attempt_count <= 0:
            continue

        analyzer_occurrences: list[tuple[int, Mapping[str, Any]]] = []
        for attempt in ordered_attempts:
            run = attempt.get("run")
            if not isinstance(run, Mapping) or run_expected_request_count(run) <= 0:
                continue
            attempt_id = str(attempt.get("attempt_id") or "")
            ordinal = nonnegative_int(attempt.get("attempt"))
            for unit in _canonical_task_analyzer_setup_units(
                run,
                identity_seed=f"generation-attempt:{attempt_id}",
            ):
                analyzer_occurrences.append((ordinal, unit))
        analyzer_reasons: list[str] = []
        first_plan = ordered_attempts[0].get("selection_plan")
        if replay_contract is not None:
            if analyzer_occurrences:
                analyzer_reasons.append("unexpected_g1_task_analyzer_request_in_frozen_replay")
            assert frozen_task_analysis_plan_reasons is not None
            source_row = task_history[-1].row
            analyzer_reasons.extend(
                frozen_task_analysis_plan_reasons(
                    first_plan,
                    replay_contract,
                    expected_task_id=task_key[1],
                    expected_task_input_sha256=str(source_row.get("task_input_sha256") or ""),
                    expected_prompt_sha256=str(source_row.get("prompt_sha256") or ""),
                )
            )
        else:
            analyzer_attempt_routes, chain_trace_reasons = (
                g1_task_analyzer_physical_routes_from_trace(
                    first_plan if isinstance(first_plan, Mapping) else {},
                    analyzer_execution_contract,
                )
            )
            analyzer_reasons.extend(chain_trace_reasons)
            analyzer_trace = (
                first_plan.get("task_analyzer") if isinstance(first_plan, Mapping) else None
            )
            has_chain_trace = bool(
                isinstance(analyzer_trace, Mapping)
                and isinstance(analyzer_trace.get("chain"), Mapping)
            )
            if not analyzer_occurrences and not (has_chain_trace and not analyzer_attempt_routes):
                analyzer_reasons.append("missing_g1_task_analyzer_request")
            elif analyzer_occurrences:
                if any(ordinal != 1 for ordinal, _ in analyzer_occurrences):
                    analyzer_reasons.append("g1_task_analyzer_not_in_first_attempt")
                analyzer_attempts = [
                    nonnegative_int(analyzer.get("attempt")) for _, analyzer in analyzer_occurrences
                ]
                if analyzer_attempts != list(range(1, len(analyzer_occurrences) + 1)):
                    analyzer_reasons.append("invalid_g1_task_analyzer_retry_sequence")
                analyzer_budget = len(analyzer_attempt_routes)
                if (
                    len(analyzer_occurrences) != analyzer_budget
                    if has_chain_trace
                    else len(analyzer_occurrences) > analyzer_budget
                ):
                    analyzer_reasons.append("g1_task_analyzer_retry_budget_exceeded")
                physical_attempt_ids = [
                    str(analyzer.get("physical_attempt_id") or "")
                    for _, analyzer in analyzer_occurrences
                ]
                if any(not HEX32.fullmatch(value) for value in physical_attempt_ids) or len(
                    physical_attempt_ids
                ) != len(set(physical_attempt_ids)):
                    analyzer_reasons.append("invalid_g1_task_analyzer_physical_attempt_identity")
                elif any(
                    physical_id in campaign_analyzer_physical_ids
                    for physical_id in physical_attempt_ids
                ):
                    analyzer_reasons.append(
                        "duplicate_cross_task_g1_task_analyzer_physical_attempt_identity"
                    )
                else:
                    campaign_analyzer_physical_ids.update(physical_attempt_ids)
                requested_routes = [
                    (
                        str(analyzer.get("requested_provider") or "").strip().casefold(),
                        str(analyzer.get("requested_model") or "").strip(),
                    )
                    for _, analyzer in analyzer_occurrences
                ]
                expected_route_prefix = [
                    (str(route["provider"]), str(route["model"]))
                    for route in analyzer_attempt_routes[: len(requested_routes)]
                ]
                if len(requested_routes) > len(analyzer_attempt_routes) or any(
                    actual_provider != expected_provider
                    or not _formal_openrouter_models_equivalent(
                        actual_model,
                        expected_model,
                    )
                    for (
                        actual_provider,
                        actual_model,
                    ), (
                        expected_provider,
                        expected_model,
                    ) in zip(
                        requested_routes,
                        expected_route_prefix,
                    )
                ):
                    analyzer_reasons.append("wrong_g1_task_analyzer_route")
                for index, (_, analyzer) in enumerate(
                    analyzer_occurrences[: len(analyzer_attempt_routes)]
                ):
                    expected_route = analyzer_attempt_routes[index]
                    if not _is_unknown_task_analyzer_placeholder(
                        analyzer,
                        expected_provider=str(expected_route["provider"]),
                        expected_model=str(expected_route["model"]),
                    ) and (
                        str(analyzer.get("provider") or "").strip().casefold()
                        != str(expected_route["provider"])
                        or not _formal_openrouter_models_equivalent(
                            analyzer.get("model"),
                            expected_route["model"],
                        )
                    ):
                        analyzer_reasons.append("wrong_g1_task_analyzer_route")
                        break
        if analyzer_reasons:
            violations.append(
                task_history[-1].reference
                | {
                    "group": task_key[0],
                    "task_id": task_key[1],
                    "paid_attempt_count": paid_attempt_count,
                    "reasons": analyzer_reasons,
                }
            )

        # Audit one campaign-wide chain. A failed wave may end immediately
        # after publishing retry_selection_plan; the next wave consumes that
        # plan, so validating each SourceRecord independently would either
        # fabricate a dangling retry or allow the lifecycle to reset.
        audit_row = copy.deepcopy(task_history[-1].row)
        audit_execution = (
            dict(audit_row.get("execution"))
            if isinstance(audit_row.get("execution"), Mapping)
            else {}
        )
        audit_execution["generation_attempts"] = ordered_attempts
        audit_row["execution"] = audit_execution
        audit_row["final_text_sha256"] = "__campaign_paid_g1_history_has_no_selected_answer__"
        _, _, audit_reasons = _adaptive_g1_lifecycle_routing(
            audit_row,
            registry=registry,
            physical_plans=(),
            initial_reasons=(),
            allow_legacy_managed_v3=allow_legacy_managed_v3,
            aggregator_recovery_policy=aggregator_recovery_policy,
            proposer_recovery_policy=proposer_recovery_policy,
        )
        fatal_reasons = [reason for reason in audit_reasons if reason not in selected_only_reasons]
        if fatal_reasons:
            violations.append(
                task_history[-1].reference
                | {
                    "group": task_key[0],
                    "task_id": task_key[1],
                    "paid_attempt_count": paid_attempt_count,
                    "attempt_ids": [
                        str(attempt.get("attempt_id") or "") for attempt in ordered_attempts
                    ],
                    "reasons": fatal_reasons,
                }
            )
    if violations:
        raise FinalizationError(
            "campaign source history contains invalid paid G1 attempt "
            f"plan/provenance evidence: {violations[:5]}"
        )


def g1_provider_lifecycle_analyzer_reasons(
    row: Mapping[str, Any],
    *,
    allow_unknown_placeholder: bool = False,
    analyzer_policy: Mapping[str, Any] | None = None,
    replay_contract: Mapping[str, Any] | None = None,
) -> list[str]:
    """Require the setup-bearing attempt to contain one frozen task analyzer."""

    from opensquilla.provider.ranking_router import frozen_task_analysis_plan_reasons

    analyzer_provider = str((analyzer_policy or {}).get("provider") or "openrouter")
    analyzer_model = str((analyzer_policy or {}).get("model") or TASK_ANALYZER_MODEL)
    execution = row.get("execution")
    attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping) and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    request_attempts = [
        attempt
        for attempt in attempts
        if isinstance(attempt, Mapping)
        and isinstance(attempt.get("run"), Mapping)
        and run_expected_request_count(attempt["run"]) > 0
    ]
    if not request_attempts:
        return ["missing_g1_provider_lifecycle_attempt"]
    if replay_contract is not None:
        first_routing = request_attempts[0]["run"].get("routing_trace")
        first_plan = (
            first_routing.get("selection_plan") if isinstance(first_routing, Mapping) else None
        )
        reasons = frozen_task_analysis_plan_reasons(
            first_plan,
            replay_contract,
            expected_task_id=str(row.get("task_id") or ""),
            expected_task_input_sha256=str(row.get("task_input_sha256") or ""),
            expected_prompt_sha256=str(row.get("prompt_sha256") or ""),
        )
        for attempt in request_attempts:
            try:
                analyzers = _canonical_task_analyzer_setup_units(
                    attempt["run"],
                    identity_seed="finalizer-frozen-task-analysis",
                )
            except FinalizationError:
                reasons.append("invalid_g1_task_analyzer_replay_usage_evidence")
                continue
            if analyzers:
                reasons.append("unexpected_g1_task_analyzer_request_in_frozen_replay")
        return list(dict.fromkeys(reasons))
    first_attempt_id = str(request_attempts[0].get("attempt_id") or "")
    try:
        first_units = _canonical_task_analyzer_setup_units(
            request_attempts[0]["run"],
            identity_seed=f"generation-attempt:{first_attempt_id}",
        )
    except FinalizationError:
        return ["inconsistent_g1_task_analyzer_setup_usage_evidence"]
    analyzers = [
        unit
        for unit in first_units
        if str(unit.get("role") or "").strip().casefold() == "task_analyzer"
    ]
    placeholders = [
        unit
        for unit in first_units
        if allow_unknown_placeholder
        and _is_unknown_task_analyzer_placeholder(
            unit,
            expected_provider=analyzer_provider,
            expected_model=analyzer_model,
        )
    ]
    if not analyzers and not placeholders:
        return ["missing_g1_task_analyzer_request"]
    if analyzers:
        for analyzer in analyzers:
            if (
                str(analyzer.get("provider") or "").strip().casefold() != analyzer_provider
                or str(analyzer.get("requested_provider") or "").strip().casefold()
                != analyzer_provider
                or not _formal_openrouter_models_equivalent(
                    analyzer.get("model"),
                    analyzer_model,
                )
                or not _formal_openrouter_models_equivalent(
                    analyzer.get("requested_model"),
                    analyzer_model,
                )
            ):
                return ["wrong_g1_task_analyzer_route"]
    for attempt in request_attempts[1:]:
        attempt_id = str(attempt.get("attempt_id") or "")
        try:
            later_units = _canonical_task_analyzer_setup_units(
                attempt["run"],
                identity_seed=f"generation-attempt:{attempt_id}",
            )
        except FinalizationError:
            return ["inconsistent_g1_task_analyzer_setup_usage_evidence"]
        if any(
            str(unit.get("role") or "").strip().casefold() == "task_analyzer"
            or _is_unknown_task_analyzer_placeholder(
                unit,
                expected_provider=analyzer_provider,
                expected_model=analyzer_model,
            )
            for unit in later_units
        ):
            return ["repeated_g1_task_analyzer_request"]
    return []


def route_reasons(
    row: Mapping[str, Any],
    *,
    group: str,
    contract: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    aggregator_recovery_policy, proposer_recovery_policy = contract_recovery_policies(contract)
    routing = row.get("routing_trace")
    routing = routing if isinstance(routing, Mapping) else {}
    models = selected_usage_models(row)
    group_spec = contract.get("group_spec")
    group_spec = group_spec if isinstance(group_spec, Mapping) else {}
    provider_pins = contract_provider_pins(contract)
    if group in {"B0", "B4"}:
        expected = str(group_spec.get("model") or "")
        if not expected or routing.get("model") != expected or models and models != {expected}:
            reasons.append("wrong_fixed_model")
        if expected:
            reasons.extend(
                usage_route_reasons(
                    row.get("usage"),
                    allowed_models={expected},
                    provider_pins=provider_pins,
                )
            )
    elif group == "B1":
        applied = str(routing.get("applied_model") or routing.get("routed_model") or "")
        allowed = set(B1_TEXT_TIER_MODELS.values())
        if (
            routing.get("routing_applied") is not True
            or not applied
            or applied not in allowed
            or models
            and models != {applied}
        ):
            reasons.append("wrong_router_single_model")
        if applied:
            reasons.extend(
                usage_route_reasons(
                    row.get("usage"),
                    allowed_models={applied},
                    provider_pins=provider_pins,
                )
            )
    elif group == "B2":
        b2_models = {*B2_PROPOSERS, B2_AGGREGATOR}
        reasons.extend(
            usage_route_reasons(
                row.get("usage"),
                allowed_models=b2_models,
                provider_pins=provider_pins,
            )
        )
        if any(
            str(unit.get("role") or "").strip().casefold() == "task_analyzer"
            for unit in usage_units(row.get("usage"))
        ):
            reasons.append("unexpected_b2_task_analyzer_request")
        reasons.extend(
            ensemble_gate(
                row,
                expected_proposers=B2_PROPOSERS,
                expected_aggregator=B2_AGGREGATOR,
                aggregator_recovery_policy=aggregator_recovery_policy,
                proposer_recovery_policy=proposer_recovery_policy,
            )
        )
    elif group == "G1":
        allow_legacy_managed_v3 = legacy_managed_v3_source_authenticated(contract)
        registry = contract.get("g1_registry_contract")
        if not isinstance(registry, Mapping):
            profile = contract.get("global_experiment_profile")
            registry = (
                profile.get("g1_routing")
                if isinstance(profile, Mapping) and isinstance(profile.get("g1_routing"), Mapping)
                else {}
            )
        routes = registry.get("expected_routes")
        allowed = set(routes) if isinstance(routes, Mapping) else set()
        analyzer_policy = g1_task_analyzer_execution_policy(registry)
        declared_analyzer_execution_contract = contract.get(
            "task_analyzer_execution_contract"
        )
        analyzer_chain = g1_task_analyzer_execution_chain(
            registry,
            declared_analyzer_execution_contract
            if isinstance(declared_analyzer_execution_contract, Mapping)
            else None,
        )
        analyzer_fallback = g1_analyzer_failure_fallback_policy(registry)
        if analyzer_chain is not None:
            allowed.update(str(route["model"]) for route in analyzer_chain)
        if analyzer_fallback is not None:
            allowed.update(str(route["model"]) for route in analyzer_fallback["proposers"])
            allowed.add(str(analyzer_fallback["aggregator"]["model"]))
        if (
            registry.get("selection_mode") != "router_dynamic"
            or not allowed
            or analyzer_policy is None
            or analyzer_chain is None
        ):
            reasons.append("invalid_g1_registry_contract")
        effective_routing, _, lifecycle_routing_reasons = effective_g1_lifecycle_routing(
            row,
            contract=contract,
        )
        reasons.extend(lifecycle_routing_reasons)
        routing = effective_routing
        if allowed and analyzer_policy is not None and analyzer_chain is not None:
            single_analyzer_route = len(analyzer_chain) == 1
            reasons.extend(
                usage_route_reasons(
                    row.get("usage"),
                    allowed_models=allowed,
                    provider_pins=provider_pins,
                    role_model_pins=(
                        {
                            "task_analyzer": str(analyzer_policy["model"]),
                        }
                        if single_analyzer_route
                        else None
                    ),
                    role_provider_pins=(
                        {
                            "task_analyzer": str(analyzer_policy["upstream_provider"]),
                        }
                        if single_analyzer_route
                        else None
                    ),
                    allow_unknown_task_analyzer_attempts=True,
                    allowed_task_analyzer_models={str(route["model"]) for route in analyzer_chain},
                )
            )
        row_plan = routing.get("selection_plan")
        replay_contract = registry.get("task_analysis_execution")
        if replay_contract is not None and isinstance(row_plan, Mapping):
            from opensquilla.provider.ranking_router import (
                frozen_task_analysis_plan_reasons,
            )

            reasons.extend(
                frozen_task_analysis_plan_reasons(
                    row_plan,
                    replay_contract,
                    expected_task_id=str(row.get("task_id") or ""),
                    expected_task_input_sha256=str(row.get("task_input_sha256") or ""),
                    expected_prompt_sha256=str(row.get("prompt_sha256") or ""),
                )
            )
            reasons.extend(
                g1_provider_lifecycle_analyzer_reasons(
                    row,
                    replay_contract=replay_contract,
                )
            )
        plan_reasons, proposers, aggregator = g1_registry_plan_reasons(
            row_plan,
            contract=registry,
            allow_legacy_managed_v3=allow_legacy_managed_v3,
            aggregator_recovery_policy=aggregator_recovery_policy,
            proposer_recovery_policy=proposer_recovery_policy,
        )
        reasons.extend(plan_reasons)
        if proposers and aggregator:
            row_registry_snapshot_hash = str(row_plan.get("registry_snapshot_hash") or "")
            trace = row.get("ensemble_trace")
            calls, call_sequence_reasons = ensemble_call_trace_sequence(
                trace if isinstance(trace, Mapping) else {}
            )
            reasons.extend(call_sequence_reasons)
            for call in calls:
                if replay_contract is not None:
                    reasons.extend(
                        frozen_task_analysis_plan_reasons(
                            call.get("selection_plan"),
                            replay_contract,
                            expected_task_id=str(row.get("task_id") or ""),
                            expected_task_input_sha256=str(row.get("task_input_sha256") or ""),
                            expected_prompt_sha256=str(row.get("prompt_sha256") or ""),
                        )
                    )
                physical_reasons, physical_p, physical_a = g1_registry_plan_reasons(
                    call.get("selection_plan"),
                    contract=registry,
                    allow_legacy_managed_v3=allow_legacy_managed_v3,
                    aggregator_recovery_policy=aggregator_recovery_policy,
                    proposer_recovery_policy=proposer_recovery_policy,
                )
                reasons.extend(physical_reasons)
                if physical_p != proposers or physical_a != aggregator:
                    reasons.append("g1_physical_plan_differs_from_routing_trace")
                physical_plan = call.get("selection_plan")
                physical_registry_snapshot_hash = (
                    str(physical_plan.get("registry_snapshot_hash") or "")
                    if isinstance(physical_plan, Mapping)
                    else ""
                )
                if physical_registry_snapshot_hash != row_registry_snapshot_hash:
                    reasons.append("g1_physical_registry_snapshot_hash_differs_from_routing_trace")
            gate_row = dict(row)
            gate_row["routing_trace"] = routing
            reasons.extend(
                ensemble_gate(
                    gate_row,
                    expected_proposers=proposers,
                    expected_aggregator=aggregator,
                    allowed_models=allowed,
                    aggregator_recovery_policy=aggregator_recovery_policy,
                    proposer_recovery_policy=proposer_recovery_policy,
                )
            )
    return reasons


def _strict_g1_physical_generation_usage_ids(
    run: Mapping[str, Any],
    *,
    identity_seed: str,
) -> list[str]:
    """Return generation IDs for every strict G1 physical ledger."""

    trace = run.get("ensemble_trace")
    calls, _ = ensemble_call_trace_sequence(trace if isinstance(trace, Mapping) else {})
    if not any(
        isinstance(call.get("selection_plan"), Mapping)
        and (
            call["selection_plan"].get("thinking_physical_evidence_schema")
            == THINKING_PHYSICAL_EVIDENCE_SCHEMA
            or call["selection_plan"].get("proposer_recovery_policy") is not None
        )
        for call in calls
    ):
        return []
    generation_ids = [
        str(unit.get("physical_attempt_id") or "")
        for unit in canonical_run_usage_units(
            run,
            identity_seed=identity_seed,
        )
        if not _is_task_analyzer_evidence(unit)
    ]
    return [
        physical_attempt_id
        for physical_attempt_id in generation_ids
        if HEX32.fullmatch(physical_attempt_id) is not None
    ]


def register_physical_attempt_owners(
    physical_attempt_ids: Sequence[str],
    *,
    owner: tuple[tuple[str, str], str, str],
    owners: dict[str, tuple[tuple[str, str], str, str]],
) -> list[str]:
    """Reject one physical request identity owned by two logical request legs."""

    reasons: list[str] = []
    for physical_attempt_id in dict.fromkeys(physical_attempt_ids):
        previous_owner = owners.get(physical_attempt_id)
        if previous_owner is not None and previous_owner != owner:
            reasons.append("physical_attempt_id_reused_across_generation_attempts")
            continue
        owners[physical_attempt_id] = owner
    return list(dict.fromkeys(reasons))


def validate_physical_generation_routes(
    records: Sequence[SourceRecord],
    *,
    contracts: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    physical_attempt_owners: dict[
        str,
        tuple[tuple[str, str], str, str],
    ] = {}
    attempt_versions: dict[
        tuple[tuple[str, str], str],
        list[
            tuple[
                SourceRecord,
                Mapping[str, Any],
                set[str],
                Mapping[str, str],
                Mapping[str, str] | None,
                Mapping[str, str] | None,
            ]
        ],
    ] = defaultdict(list)
    for record in records:
        group = record.key[0]
        contract = contracts.get(group) or {}
        provider_pins: Mapping[str, str] | None = contract_provider_pins(contract)
        role_model_pins: Mapping[str, str] | None = None
        role_provider_pins: Mapping[str, str] | None = None
        routing = record.row.get("routing_trace")
        routing = routing if isinstance(routing, Mapping) else {}
        if group in {"B0", "B4"}:
            spec = contract.get("group_spec")
            allowed = {str(spec.get("model") or "")} if isinstance(spec, Mapping) else set()
        elif group == "B1":
            applied = str(routing.get("applied_model") or routing.get("routed_model") or "")
            allowed = {applied} if applied in set(B1_TEXT_TIER_MODELS.values()) else set()
        elif group == "B2":
            allowed = {*B2_PROPOSERS, B2_AGGREGATOR}
        else:
            allowed: set[str] = set()
            registry = contract.get("g1_registry_contract")
            routes = registry.get("expected_routes") if isinstance(registry, Mapping) else None
            analyzer_policy = g1_task_analyzer_execution_policy(registry)
            if isinstance(routes, Mapping) and analyzer_policy is not None:
                allowed.update(str(model) for model in routes)
                role_model_pins = {
                    "task_analyzer": str(analyzer_policy["model"]),
                }
                role_provider_pins = {
                    "task_analyzer": str(analyzer_policy["upstream_provider"]),
                }
        allowed.discard("")
        execution = record.row.get("execution")
        attempts = (
            execution.get("generation_attempts")
            if isinstance(execution, Mapping)
            and isinstance(execution.get("generation_attempts"), list)
            else []
        )
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                continue
            attempt_id = str(attempt.get("attempt_id") or "")
            run = attempt.get("run")
            if not isinstance(run, Mapping):
                continue
            if run_expected_request_count(run) <= 0:
                continue
            attempt_versions[(record.key, attempt_id)].append(
                (
                    record,
                    run,
                    allowed,
                    provider_pins,
                    role_model_pins,
                    role_provider_pins,
                )
            )
    for (attempt_owner_key, attempt_id), versions in attempt_versions.items():
        record, run = validate_and_select_monotonic_run_version(
            [(version[0], version[1]) for version in versions],
            label=(f"generation attempt {attempt_owner_key}:{attempt_id}"),
            identity_seed=f"generation-attempt:{attempt_id}",
        )
        selected_versions = [version for version in versions if version[0] is record]
        if not selected_versions:
            raise FinalizationError(
                f"generation attempt {attempt_id} selected an unknown route version"
            )
        (
            _,
            _,
            allowed,
            provider_pins,
            role_model_pins,
            role_provider_pins,
        ) = selected_versions[-1]
        reasons = usage_route_reasons(
            run.get("usage"),
            allowed_models=allowed,
            provider_pins=provider_pins,
            role_model_pins=role_model_pins,
            role_provider_pins=role_provider_pins,
            allow_unknown_task_analyzer_attempts=record.key[0] == "G1",
        )
        canonical_units = canonical_run_usage_units(
            run,
            identity_seed=f"generation-attempt:{attempt_id}",
        )
        if record.key[0] == "G1":
            reasons.extend(g1_thinking_physical_usage_binding_reasons(run))
            analyzer_ids = [
                _task_analyzer_physical_attempt_id(unit)
                for unit in _canonical_task_analyzer_setup_units(
                    run,
                    identity_seed=f"generation-attempt:{attempt_id}",
                )
            ]
            reasons.extend(
                register_physical_attempt_owners(
                    [
                        physical_attempt_id
                        for physical_attempt_id in analyzer_ids
                        if HEX32.fullmatch(physical_attempt_id) is not None
                    ],
                    owner=(record.key, attempt_id, "task_analyzer"),
                    owners=physical_attempt_owners,
                )
            )
            generation_ids = _strict_g1_physical_generation_usage_ids(
                run,
                identity_seed=f"generation-attempt:{attempt_id}",
            )
            generation_role = "managed_generation"
        else:
            generation_ids = [
                str(unit.get("physical_attempt_id") or "")
                for unit in canonical_units
                if not _is_task_analyzer_evidence(unit)
                and HEX32.fullmatch(str(unit.get("physical_attempt_id") or "")) is not None
            ]
            generation_role = "generation"
        reasons.extend(
            register_physical_attempt_owners(
                generation_ids,
                owner=(record.key, attempt_id, generation_role),
                owners=physical_attempt_owners,
            )
        )
        roles = {str(unit.get("role") or "").strip().casefold() for unit in canonical_units}
        if record.key[0] == "B2" and "task_analyzer" in roles:
            reasons.append("unexpected_b2_task_analyzer_request")
        blocking_reasons, audit_reasons = partition_execution_and_audit_reasons(
            reasons,
            evidence_proven=row_has_bound_answer_and_proposer_quorum(record.row),
        )
        if audit_reasons:
            warnings.append(
                record.reference
                | {
                    "group": record.key[0],
                    "task_id": record.key[1],
                    "attempt_id": attempt_id,
                    "reasons": audit_reasons,
                }
            )
        if blocking_reasons:
            violations.append(
                record.reference
                | {
                    "group": record.key[0],
                    "task_id": record.key[1],
                    "attempt_id": attempt_id,
                    "reasons": blocking_reasons,
                }
            )
    if violations:
        raise FinalizationError(
            f"physical generation route evidence violates frozen contracts: {violations[:5]}"
        )
    return warnings


def generation_reason_assessment(
    record: SourceRecord,
    *,
    task: Mapping[str, Any],
    expected_fingerprint: str,
    contract: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    row = record.row
    reasons: list[str] = []
    final_text = str(row.get("final_text") or "")
    degraded_success = explicit_degraded_success(row)
    if not final_text.strip():
        reasons.append("empty_final_text")
    if row.get("final_text_sha256") != text_sha256(final_text):
        reasons.append("final_text_hash_mismatch")
    if nonnegative_int(row.get("final_text_chars")) != len(final_text):
        reasons.append("final_text_length_mismatch")
    prompt = str(task.get("prompt") or "")
    if row.get("prompt_sha256") != text_sha256(prompt):
        reasons.append("prompt_hash_mismatch")
    if row.get("task_input_sha256") != canonical_sha256(task, prefix=True):
        reasons.append("task_input_hash_mismatch")
    if row.get("run_compatibility_fingerprint") != expected_fingerprint:
        reasons.append("run_compatibility_fingerprint_mismatch")
    execution_evidence_proven = bool(
        row_has_bound_answer_and_proposer_quorum(row)
        and row.get("prompt_sha256") == text_sha256(prompt)
        and row.get("task_input_sha256") == canonical_sha256(task, prefix=True)
        and row.get("run_compatibility_fingerprint") == expected_fingerprint
    )
    error = str(row.get("error") or "")
    if error in POLICY_VIOLATION_ERRORS:
        reasons.append("openrouter_policy_violation")
    elif error not in ALLOWED_NON_GENERATION_ERRORS:
        reasons.append(
            f"audit:row_error:{error}"
            if final_text.strip()
            and audit_only_error_text(
                error,
                degraded_success=degraded_success,
                evidence_proven=execution_evidence_proven,
            )
            else "generation_error"
        )
    execution = row.get("execution")
    run_error = str(execution.get("run_error") or "") if isinstance(execution, Mapping) else ""
    if run_error:
        reasons.append(
            f"audit:run_error:{run_error}"
            if final_text.strip()
            and audit_only_error_text(
                run_error,
                degraded_success=degraded_success,
                evidence_proven=execution_evidence_proven,
            )
            else "generation_run_error"
        )
    if row.get("selected_generation_succeeded") is not True:
        reasons.append(
            "selected_generation_degraded_success"
            if degraded_success
            else "selected_generation_not_successful"
        )
    completion = row.get("completion_status")
    if isinstance(completion, Mapping) and completion.get("generation_accepted") is False:
        reasons.append(
            "generation_accepted_as_degraded_success"
            if degraded_success
            else "generation_not_accepted"
        )
    reasons.extend(
        route_reasons(
            row,
            group=str(row.get("group") or ""),
            contract=contract,
        )
    )
    return partition_execution_and_audit_reasons(
        reasons,
        evidence_proven=execution_evidence_proven,
    )


def generation_reasons(
    record: SourceRecord,
    *,
    task: Mapping[str, Any],
    expected_fingerprint: str,
    contract: Mapping[str, Any],
) -> list[str]:
    """Return only reasons that invalidate the already-produced execution."""

    blocking, _ = generation_reason_assessment(
        record,
        task=task,
        expected_fingerprint=expected_fingerprint,
        contract=contract,
    )
    return blocking


def generation_audit_reasons(
    record: SourceRecord,
    *,
    task: Mapping[str, Any],
    expected_fingerprint: str,
    contract: Mapping[str, Any],
) -> list[str]:
    """Return policy/receipt/settlement warnings for a usable execution."""

    _, warnings = generation_reason_assessment(
        record,
        task=task,
        expected_fingerprint=expected_fingerprint,
        contract=contract,
    )
    return warnings


def task_rubric_criteria(task: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    rubric = task.get("rubric")
    if not isinstance(rubric, Mapping):
        return "", []
    rows: list[dict[str, Any]] = []
    for section in rubric.get("sections") or []:
        if not isinstance(section, Mapping):
            continue
        for criterion in section.get("criteria") or []:
            if not isinstance(criterion, Mapping):
                continue
            rows.append(
                {
                    "id": str(criterion.get("id") or ""),
                    "section_id": str(section.get("id") or "rubric"),
                    "weight": Decimal(str(criterion.get("weight") or 0)),
                }
            )
    return str(rubric.get("id") or task.get("id") or ""), rows


def recompute_judge_quality(judgments: Sequence[Mapping[str, Any]]) -> Decimal | None:
    if not judgments or any(not isinstance(item.get("met"), bool) for item in judgments):
        return None
    positive_total = sum(
        max(Decimal(0), Decimal(str(item.get("weight") or 0))) for item in judgments
    )
    if positive_total <= 0:
        return None
    raw_score = sum(
        Decimal(str(item.get("weight") or 0)) for item in judgments if item.get("met") is True
    )
    return max(Decimal(0), min(Decimal(100), raw_score / positive_total * 100))


def judge_reasons(
    row: Mapping[str, Any],
    *,
    task: Mapping[str, Any] | None = None,
    judge_model: str = JUDGE_MODEL,
    judge_repeats: int = JUDGE_REPEATS,
    judge_max_attempts: int = JUDGE_ATTEMPT_BUDGET_LIMIT,
    judge_provider_pin: str | None = None,
) -> list[str]:
    judge = row.get("judge")
    if not isinstance(judge, Mapping):
        return ["missing_judge"]
    reasons: list[str] = []
    if judge.get("score_status") != "complete":
        reasons.append("judge_incomplete")
    if nonnegative_int(judge.get("judge_error_count")) != 0:
        reasons.append("judge_errors")
    quality = row.get("quality_total")
    if not finite_number(quality):
        reasons.append("missing_quality_total")
    if (
        judge.get("judge_attempt_evidence_schema") != JUDGE_ATTEMPT_EVIDENCE_SCHEMA
        or judge.get("judge_attempt_budget_scope") != JUDGE_ATTEMPT_BUDGET_SCOPE
        or judge.get("judge_attempt_budget_limit_per_unit") != judge_max_attempts
        or judge.get("judge_attempt_budget_exhausted") is not False
        or nonnegative_int(judge.get("judge_attempt_budget_exhausted_count")) != 0
    ):
        reasons.append("judge_attempt_contract_mismatch")
    if task is None:
        return reasons
    rubric_id, rubric_criteria = task_rubric_criteria(task)
    judgments = judge.get("criterion_judgments")
    judgments = (
        [item for item in judgments if isinstance(item, Mapping)]
        if isinstance(judgments, list)
        else []
    )
    expected_count = len(rubric_criteria) * judge_repeats
    if (
        judge.get("mode") != "draco_criterion_judgments"
        or str(judge.get("judge_model") or "") != judge_model
        or nonnegative_int(judge.get("judge_repeats")) != judge_repeats
        or str(judge.get("rubric_id") or "") != rubric_id
        or nonnegative_int(judge.get("rubric_criteria_count")) != len(rubric_criteria)
        or nonnegative_int(judge.get("criteria_count")) != expected_count
        or len(judgments) != expected_count
        or nonnegative_int(judge.get("valid_criteria_count")) != expected_count
        or nonnegative_int(judge.get("invalid_criteria_count")) != 0
    ):
        reasons.append("judge_contract_mismatch")
    expected_occurrences = Counter(
        (criterion["id"], repeat)
        for criterion in rubric_criteria
        for repeat in range(judge_repeats)
    )
    observed_occurrences = Counter(
        (str(item.get("id") or ""), nonnegative_int(item.get("repeat_index"))) for item in judgments
    )
    if observed_occurrences != expected_occurrences:
        reasons.append("judge_rubric_binding_mismatch")
    criteria_by_id = {item["id"]: item for item in rubric_criteria}
    for judgment in judgments:
        expected = criteria_by_id.get(str(judgment.get("id") or ""))
        if (
            expected is None
            or str(judgment.get("section_id") or "") != expected["section_id"]
            or Decimal(str(judgment.get("weight") or 0)) != expected["weight"]
            or not isinstance(judgment.get("met"), bool)
            or judgment.get("error")
        ):
            reasons.append("judge_criterion_evidence_mismatch")
        attempts = judgment.get("judge_attempts")
        if not isinstance(attempts, list) or not attempts:
            reasons.append("missing_judge_physical_attempt")
            continue
        for attempt in attempts:
            run = attempt.get("run") if isinstance(attempt, Mapping) else None
            if not isinstance(run, Mapping):
                reasons.append("missing_judge_physical_attempt")
                continue
            attempt_id = str(attempt.get("attempt_id") or "")
            _, route_failures = canonical_judge_run_route_reasons(
                run,
                attempt_id=attempt_id,
                judge_model=judge_model,
                judge_provider_pin=judge_provider_pin,
            )
            blocking_route_failures, _ = partition_execution_and_audit_reasons(route_failures)
            reasons.extend(
                "wrong_judge_model_route" for reason in blocking_route_failures if reason
            )
        final_attempt = attempts[-1] if attempts and isinstance(attempts[-1], Mapping) else {}
        final_run = final_attempt.get("run")
        expected_verdict = (
            "MET"
            if judgment.get("met") is True
            else "UNMET"
            if judgment.get("met") is False
            else ""
        )
        if (
            not isinstance(final_run, Mapping)
            or str(final_run.get("error") or "")
            or final_attempt.get("retry_suppressed_reason")
            or final_attempt.get("met") is not judgment.get("met")
            or str(final_attempt.get("verdict") or "").strip().upper() != expected_verdict
            or str(judgment.get("verdict") or "").strip().upper() != expected_verdict
            or not isinstance(judgment.get("judge_run"), Mapping)
            or _judge_run_binding(judgment["judge_run"]) != _judge_run_binding(final_run)
        ):
            reasons.append("judge_result_not_bound_to_successful_attempt")
    recomputed = recompute_judge_quality(judgments)
    normalized = judge.get("normalized_score")
    if (
        recomputed is None
        or not finite_number(normalized)
        or not finite_number(quality)
        or abs(Decimal(str(normalized)) - recomputed) > Decimal("0.000000001")
        or abs(Decimal(str(quality)) - recomputed) > Decimal("0.000000001")
    ):
        reasons.append("quality_total_mismatch")
    recomputed_pass_rate = row_pass_rate(row) * Decimal(100)
    if (
        not finite_number(judge.get("pass_rate"))
        or not finite_number(judge.get("valid_pass_rate"))
        or abs(Decimal(str(judge.get("pass_rate"))) - recomputed_pass_rate) > Decimal("0.000000001")
        or abs(Decimal(str(judge.get("valid_pass_rate"))) - recomputed_pass_rate)
        > Decimal("0.000000001")
    ):
        reasons.append("judge_pass_rate_mismatch")
    return reasons


RETROSPECTIVE_RECLASSIFICATION_RECOVERY_SCHEMA = (
    "opensquilla.draco.retrospective-reclassification-recovery/v1"
)


def source_manifest_resume_action(
    manifest_sources: Sequence[Mapping[str, Any]],
    *,
    source_index: int,
    group: str,
    task_id: str,
) -> str:
    if source_index < 0 or source_index >= len(manifest_sources):
        return ""
    if manifest_sources[source_index].get("resume_schedule_contract_verified") is not True:
        return ""
    scheduled = manifest_sources[source_index].get("resume_scheduled_pairs")
    if not isinstance(scheduled, list):
        return ""
    matches = [
        str(pair.get("action") or "")
        for pair in scheduled
        if isinstance(pair, Mapping)
        and str(pair.get("group") or "") == group
        and str(pair.get("task_id") or "") == task_id
    ]
    return matches[0] if len(matches) == 1 else ""


def g1_retrospective_reclassification_recovery(
    record: SourceRecord,
    *,
    pre_retry_record: SourceRecord,
    contract: Mapping[str, Any],
    post_accept_invalid_rows: Sequence[Mapping[str, Any]],
    max_attempts: int,
) -> dict[str, Any]:
    """Authenticate one legacy retry caused by a later G1 reclassification."""

    row = record.row
    execution = row.get("execution")
    if (
        record.key[0] != "G1"
        or not isinstance(execution, Mapping)
        or not repair_evidence(row, execution)
        or execution.get("resume_action") != "metadata_only"
        or execution.get("judge_reran") is not False
        or execution.get("metadata_repair_attempted") is not True
        or execution.get("metadata_repaired") is not True
        or len(post_accept_invalid_rows) != 1
        or pre_retry_record.key != record.key
    ):
        return {}
    stored = execution.get("g1_provider_lifecycle_routing_recovery")
    restored_routing = row.get("routing_trace")
    replay_row = copy.deepcopy(row)
    replay_row["routing_trace"] = {}
    recovered_routing, recomputed, reasons = effective_g1_lifecycle_routing(
        replay_row,
        contract=contract,
    )
    if (
        reasons
        or not isinstance(restored_routing, Mapping)
        or dict(restored_routing) != recovered_routing
        or not isinstance(stored, Mapping)
        or not recomputed
        or dict(stored) != recomputed
    ):
        return {}
    pre_routing = pre_retry_record.row.get("routing_trace")
    pre_recovered, pre_recomputed, pre_reasons = effective_g1_lifecycle_routing(
        pre_retry_record.row,
        contract=contract,
    )
    if (
        not isinstance(pre_routing, Mapping)
        or bool(pre_routing)
        or pre_reasons
        or not pre_recomputed
        or pre_recovered != recovered_routing
        or pre_recomputed != recomputed
        or pre_retry_record.row.get("ensemble_trace") != row.get("ensemble_trace")
        or generation_identity(pre_retry_record.row) != generation_identity(row)
        or (pre_retry_record.source_index, pre_retry_record.line)
        >= (
            nonnegative_int(post_accept_invalid_rows[0].get("source_index")),
            nonnegative_int(post_accept_invalid_rows[0].get("line")),
        )
    ):
        return {}
    matching = matching_saved_generation_attempts(row)
    if len(matching) != 1:
        return {}
    selected = matching[0]
    selected_attempt_id = str(selected.get("attempt_id") or "")
    selected_attempt = nonnegative_int(selected.get("attempt"))
    if (
        recomputed.get("selected_attempt_id") != selected_attempt_id
        or nonnegative_int(recomputed.get("selected_attempt")) != selected_attempt
    ):
        return {}
    resume_completion = row.get("resume_completion")
    if (
        not isinstance(resume_completion, Mapping)
        or resume_completion.get("action") != "metadata_only"
        or resume_completion.get("generation_reused") is not True
        or resume_completion.get("metadata_repaired") is not True
        or resume_completion.get("judge_reran") is not False
    ):
        return {}

    repair_attempts = (
        execution.get("generation_attempts")
        if isinstance(execution.get("generation_attempts"), list)
        else []
    )
    repair_ids = {
        str(attempt.get("attempt_id") or "")
        for attempt in repair_attempts
        if isinstance(attempt, Mapping)
    }
    post_attempts = [
        attempt
        for invalid in post_accept_invalid_rows
        for attempt in invalid.get("new_attempts", [])
        if isinstance(attempt, Mapping)
    ]
    post_ids = {str(attempt.get("attempt_id") or "") for attempt in post_attempts}
    post_row = post_accept_invalid_rows[0]
    post_attempt = post_attempts[0] if len(post_attempts) == 1 else {}
    post_error = str(post_row.get("row_error") or "")
    post_run = post_attempt.get("run")
    post_run_error = str(post_run.get("error") or "") if isinstance(post_run, Mapping) else ""
    budget_used = nonnegative_int(row.get("generation_attempt_budget_used"))
    ordinals = {
        nonnegative_int(attempt.get("attempt"))
        for attempt in (*repair_attempts, *post_attempts)
        if isinstance(attempt, Mapping)
    }
    if (
        not selected_attempt_id
        or len(post_attempts) != 1
        or post_row.get("resume_manifest_action") != "regenerate"
        or post_row.get("selected_generation_succeeded") is not False
        or post_error != "aggregator_fallback_used_or_unknown"
        or str(post_attempt.get("attempt_kind") or "") != "generation"
        or not isinstance(post_attempt.get("run"), Mapping)
        or post_run_error != post_error
        or str(post_attempt.get("retry_reason") or "") != post_error
        or post_attempt.get("will_retry") is not False
        or selected_attempt_id in post_ids
        or repair_ids & post_ids
        or not post_ids
        or nonnegative_int(post_attempt.get("attempt")) != selected_attempt + 1
        or nonnegative_int(post_attempt.get("attempt")) != max_attempts
        or any(HEX32.fullmatch(attempt_id) is None for attempt_id in repair_ids | post_ids)
        or len(repair_ids) != len(repair_attempts)
        or len(post_ids) != len(post_attempts)
        or len(ordinals) != len(repair_attempts) + len(post_attempts)
        or budget_used <= 0
        or budget_used > max_attempts
        or nonnegative_int(row.get("generation_max_attempts")) != max_attempts
        or nonnegative_int(execution.get("generation_max_attempts")) != max_attempts
        or nonnegative_int(row.get("generation_attempt_count")) != len(repair_attempts)
        or nonnegative_int(execution.get("prior_generation_attempts_used")) != budget_used
        or len(repair_attempts) + len(post_attempts) != budget_used
        or ordinals != set(range(1, budget_used + 1))
    ):
        return {}
    invalid_sources = [
        {
            **{key: value.get(key) for key in ("path", "source_index", "line")},
            "new_attempt_ids": sorted(
                str(attempt.get("attempt_id") or "")
                for attempt in value.get("new_attempts", [])
                if isinstance(attempt, Mapping)
            ),
            "reasons": list(value.get("reasons") or []),
            "resume_manifest_action": value.get("resume_manifest_action"),
        }
        for value in post_accept_invalid_rows
    ]
    return {
        "schema": RETROSPECTIVE_RECLASSIFICATION_RECOVERY_SCHEMA,
        "status": "accepted",
        "policy": "g1-provider-lifecycle-retrospective-reclassification/v1",
        "selected_attempt_id": selected_attempt_id,
        "selected_attempt": selected_attempt,
        "generation_attempt_budget_used": budget_used,
        "invalid_post_accept_attempt_ids": sorted(post_ids),
        "invalid_post_accept_sources": invalid_sources,
        "lifecycle_reclassification": recomputed,
    }


def select_results(
    records: Sequence[SourceRecord],
    *,
    tasks: Sequence[dict[str, Any]],
    groups: Sequence[str],
    fingerprints: Mapping[str, str],
    contracts: Mapping[str, Mapping[str, Any]],
    max_attempts: int,
    experiment_policy: FinalizerExperimentPolicy | None = None,
    manifest_sources: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[SourceRecord], dict[str, Any]]:
    expected_keys = {(group, str(task["id"])) for task in tasks for group in groups}
    by_key: dict[tuple[str, str], list[SourceRecord]] = defaultdict(list)
    unexpected: list[dict[str, Any]] = []
    for record in records:
        if record.key not in expected_keys:
            unexpected.append(record.reference | {"key": list(record.key)})
        else:
            by_key[record.key].append(record)
    if unexpected:
        raise FinalizationError(f"result sources contain unexpected rows: {unexpected[:5]}")
    missing = sorted(expected_keys - set(by_key))
    if missing:
        raise FinalizationError(f"result sources miss expected pairs: {missing[:5]}")

    selected: list[SourceRecord] = []
    pair_audit: dict[str, Any] = {}
    for task in tasks:
        task_id = str(task["id"])
        for group in groups:
            key = (group, task_id)
            candidates = by_key[key]
            cumulative_used = max(
                (
                    nonnegative_int(record.row.get("generation_attempt_budget_used"))
                    for record in candidates
                ),
                default=0,
            )
            identities: dict[str, list[SourceRecord]] = defaultdict(list)
            identity_attempts: dict[str, int] = {}
            invalid_rows: list[dict[str, Any]] = []
            audit_warning_rows: list[dict[str, Any]] = []
            seen_pair_attempt_ids: set[str] = set()
            g1_generation_attempt_prefix: list[dict[str, Any]] = []
            g1_generation_attempt_ids: set[str] = set()
            accepted_generation_seen = False
            post_accept_invalid_rows: list[dict[str, Any]] = []
            for record in sorted(candidates, key=lambda item: (item.source_index, item.line)):
                execution = record.row.get("execution")
                attempts = (
                    execution.get("generation_attempts")
                    if isinstance(execution, Mapping)
                    and isinstance(execution.get("generation_attempts"), list)
                    else []
                )
                row_attempt_ids = {
                    str(attempt.get("attempt_id") or "")
                    for attempt in attempts
                    if isinstance(attempt, Mapping)
                }
                new_attempt_ids = row_attempt_ids - seen_pair_attempt_ids
                validation_record = record
                if group == "G1":
                    for attempt in attempts:
                        if not isinstance(attempt, Mapping):
                            continue
                        attempt_id = str(attempt.get("attempt_id") or "")
                        if attempt_id in g1_generation_attempt_ids:
                            continue
                        g1_generation_attempt_ids.add(attempt_id)
                        g1_generation_attempt_prefix.append(copy.deepcopy(dict(attempt)))
                    validation_row = copy.deepcopy(record.row)
                    validation_execution = (
                        dict(validation_row.get("execution"))
                        if isinstance(
                            validation_row.get("execution"),
                            Mapping,
                        )
                        else {}
                    )
                    validation_execution["generation_attempts"] = copy.deepcopy(
                        g1_generation_attempt_prefix
                    )
                    validation_row["execution"] = validation_execution
                    validation_record = SourceRecord(
                        path=record.path,
                        source_index=record.source_index,
                        line=record.line,
                        row=validation_row,
                    )
                reasons = generation_reasons(
                    validation_record,
                    task=task,
                    expected_fingerprint=fingerprints[group],
                    contract=contracts[group],
                )
                audit_reasons = generation_audit_reasons(
                    validation_record,
                    task=task,
                    expected_fingerprint=fingerprints[group],
                    contract=contracts[group],
                )
                if audit_reasons:
                    audit_warning_rows.append(record.reference | {"reasons": audit_reasons})
                if accepted_generation_seen and new_attempt_ids:
                    if not reasons:
                        raise FinalizationError(
                            f"{group}/{task_id} started a new generation attempt after "
                            "an already valid generation"
                        )
                    post_accept_invalid_rows.append(
                        record.reference
                        | {
                            "new_attempts": [
                                dict(attempt)
                                for attempt in attempts
                                if isinstance(attempt, Mapping)
                                and str(attempt.get("attempt_id") or "") in new_attempt_ids
                            ],
                            "reasons": reasons,
                            "resume_manifest_action": source_manifest_resume_action(
                                manifest_sources,
                                source_index=record.source_index,
                                group=group,
                                task_id=task_id,
                            ),
                            "selected_generation_succeeded": record.row.get(
                                "selected_generation_succeeded"
                            ),
                            "row_error": record.row.get("error"),
                        }
                    )
                identity = generation_identity(record.row)
                identity_attempts[identity] = max(
                    identity_attempts.get(identity, 0),
                    generation_attempt_count(record.row),
                )
                if reasons:
                    invalid_rows.append(record.reference | {"reasons": reasons})
                else:
                    identities[identity].append(record)
                    accepted_generation_seen = True
                seen_pair_attempt_ids.update(row_attempt_ids)
            legacy_used = sum(identity_attempts.values())
            budget_used = cumulative_used if cumulative_used else legacy_used
            if budget_used > max_attempts:
                raise FinalizationError(
                    f"{group}/{task_id} used {budget_used} generation attempts; "
                    f"limit is {max_attempts}"
                )
            if not identities:
                raise FinalizationError(
                    f"{group}/{task_id} has no valid generation: {invalid_rows[-3:]}"
                )
            latest_identity, identity_rows = max(
                identities.items(),
                key=lambda item: max(generation_sort_key(record) for record in item[1]),
            )
            repaired = max(
                identity_rows,
                key=lambda record: (record.source_index, record.line),
            )
            retrospective_recovery: dict[str, Any] = {}
            if post_accept_invalid_rows:
                repaired_order = (repaired.source_index, repaired.line)
                if len(identities) != 1 or any(
                    (
                        nonnegative_int(value.get("source_index")),
                        nonnegative_int(value.get("line")),
                    )
                    >= repaired_order
                    for value in post_accept_invalid_rows
                ):
                    raise FinalizationError(
                        f"{group}/{task_id} started a new generation attempt after "
                        "an already valid generation"
                    )
                pre_retry_candidates = [
                    candidate
                    for candidate in identity_rows
                    if (candidate.source_index, candidate.line)
                    < (
                        nonnegative_int(post_accept_invalid_rows[0].get("source_index")),
                        nonnegative_int(post_accept_invalid_rows[0].get("line")),
                    )
                ]
                if len(pre_retry_candidates) != 1:
                    raise FinalizationError(
                        f"{group}/{task_id} lacks one unique pre-retry valid generation record"
                    )
                pre_retry_record = pre_retry_candidates[0]
                retrospective_recovery = g1_retrospective_reclassification_recovery(
                    repaired,
                    pre_retry_record=pre_retry_record,
                    contract=contracts[group],
                    post_accept_invalid_rows=post_accept_invalid_rows,
                    max_attempts=max_attempts,
                )
                if not retrospective_recovery:
                    raise FinalizationError(
                        f"{group}/{task_id} started a new generation attempt after "
                        "an already valid generation"
                    )
            judge_failures = judge_reasons(
                repaired.row,
                task=task,
                judge_model=(
                    experiment_policy.judge_model if experiment_policy is not None else JUDGE_MODEL
                ),
                judge_repeats=(
                    experiment_policy.judge_repeats
                    if experiment_policy is not None
                    else JUDGE_REPEATS
                ),
                judge_max_attempts=(
                    experiment_policy.judge_max_attempts
                    if experiment_policy is not None
                    else JUDGE_ATTEMPT_BUDGET_LIMIT
                ),
                judge_provider_pin=(
                    experiment_policy.judge_provider_pin if experiment_policy is not None else None
                ),
            )
            if judge_failures:
                raise FinalizationError(
                    f"{group}/{task_id} latest repair lacks a complete Judge: {judge_failures}"
                )
            selected.append(repaired)
            selection_audit = {
                "source": repaired.reference,
                "generation_identity_sha256": latest_identity,
                "candidate_row_count": len(candidates),
                "valid_generation_row_count": sum(len(value) for value in identities.values()),
                "distinct_generation_count": len(identity_attempts),
                "generation_attempt_budget_used": budget_used,
                "invalid_row_count": len(invalid_rows),
                "warnings": audit_warning_rows,
            }
            if retrospective_recovery:
                selection_audit["retrospective_reclassification_recovery"] = retrospective_recovery
            pair_audit[f"{group}/{task_id}"] = selection_audit
    return selected, pair_audit


GENERATION_TERMINAL_RECLASSIFICATION_SCHEMA = (
    "opensquilla.draco.generation-terminal-reclassification/v1"
)
LEGACY_TERMINAL_POLICY_ERROR = "aggregator_fallback_used_or_unknown"


def selected_legacy_attempt_error_is_reclassified(
    selected_row: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> bool:
    """Allow one immutable legacy policy marker after independent terminal proof."""

    if (
        str(selected_row.get("group") or "") != "B2"
        or selected_row.get("selected_generation_succeeded") is not True
        or str(selected_row.get("error") or "") not in ALLOWED_NON_GENERATION_ERRORS
    ):
        return False
    execution = selected_row.get("execution")
    provenance = (
        execution.get("generation_terminal_reclassification")
        if isinstance(execution, Mapping)
        else None
    )
    run = attempt.get("run")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("schema") != GENERATION_TERMINAL_RECLASSIFICATION_SCHEMA
        or provenance.get("policy") != "terminal_aggregator_with_empty_intermediate_fallback/v1"
        or provenance.get("original_error") != LEGACY_TERMINAL_POLICY_ERROR
        or not isinstance(run, Mapping)
        or str(run.get("error") or "") != LEGACY_TERMINAL_POLICY_ERROR
        or str(attempt.get("retry_reason") or "") != LEGACY_TERMINAL_POLICY_ERROR
        or str(provenance.get("selected_attempt_id") or "") != str(attempt.get("attempt_id") or "")
        or nonnegative_int(provenance.get("selected_attempt"))
        != nonnegative_int(attempt.get("attempt"))
        or nonnegative_int(execution.get("selected_generation_attempt"))
        != nonnegative_int(attempt.get("attempt"))
        or str(execution.get("run_error") or "")
    ):
        return False
    completion = selected_row.get("completion_status")
    if not isinstance(completion, Mapping) or completion.get("generation_accepted") is not True:
        return False
    trace = selected_row.get("ensemble_trace")
    calls, sequence_reasons = ensemble_call_trace_sequence(
        trace if isinstance(trace, Mapping) else {}
    )
    if sequence_reasons or not calls:
        return False
    expected_intermediate = [
        nonnegative_int(call.get("agent_call_index"))
        for call in calls[:-1]
        if call.get("fallback_used") is True
    ]
    terminal = calls[-1]
    if (
        not expected_intermediate
        or provenance.get("intermediate_fallback_call_indexes") != expected_intermediate
        or nonnegative_int(provenance.get("terminal_call_index"))
        != nonnegative_int(terminal.get("agent_call_index"))
        or terminal.get("fallback_used") is not False
        or str(terminal.get("final_request_role") or "") != "aggregator"
    ):
        return False
    return not ensemble_gate(
        selected_row,
        expected_proposers=B2_PROPOSERS,
        expected_aggregator=B2_AGGREGATOR,
    )


def bind_selected_generation_attempts(
    records: Sequence[SourceRecord],
    selected: Sequence[SourceRecord],
) -> dict[str, str]:
    """Bind every selected answer to exactly one successful physical attempt."""

    bindings: dict[str, str] = {}
    for selected_record in selected:
        selected_identity = generation_identity(selected_record.row)
        selected_final_sha = str(selected_record.row.get("final_text_sha256") or "")
        selected_usage = usage_generation_identity_contract(selected_record.row.get("usage"))
        matching_ids: set[str] = set()
        for candidate in records:
            if (
                candidate.key != selected_record.key
                or generation_identity(candidate.row) != selected_identity
            ):
                continue
            execution = candidate.row.get("execution")
            attempts = (
                execution.get("generation_attempts")
                if isinstance(execution, Mapping)
                and isinstance(execution.get("generation_attempts"), list)
                else []
            )
            for attempt in attempts:
                if not isinstance(attempt, Mapping):
                    continue
                run = attempt.get("run")
                attempt_id = str(attempt.get("attempt_id") or "")
                canonical_run = (
                    _canonicalized_run(
                        run,
                        identity_seed=f"generation-attempt:{attempt_id}",
                    )
                    if isinstance(run, Mapping)
                    else None
                )
                if (
                    canonical_run is None
                    or str(canonical_run.get("final_text_sha256") or "") != selected_final_sha
                    or usage_generation_identity_contract(canonical_run.get("usage"))
                    != selected_usage
                    or run_expected_request_count(canonical_run) <= 0
                    or len(
                        canonical_run_usage_units(
                            canonical_run,
                            identity_seed=f"generation-attempt:{attempt_id}",
                        )
                    )
                    != run_expected_request_count(canonical_run)
                ):
                    continue
                run_error = str(canonical_run.get("error") or "")
                if (
                    run_error
                    and not audit_only_error_text(
                        run_error,
                        degraded_success=explicit_degraded_success(selected_record.row),
                    )
                    and not selected_legacy_attempt_error_is_reclassified(
                        selected_record.row,
                        attempt,
                    )
                ):
                    continue
                if HEX32.fullmatch(attempt_id):
                    matching_ids.add(attempt_id)
        pair = f"{selected_record.key[0]}/{selected_record.key[1]}"
        if len(matching_ids) != 1:
            raise FinalizationError(
                f"{pair} selected final answer is not bound to exactly one "
                f"successful physical generation attempt: {sorted(matching_ids)}"
            )
        bindings[pair] = next(iter(matching_ids))
    if len(bindings) != len(selected):
        raise FinalizationError("selected generation attempt binding is incomplete")
    return bindings


def response_ids(unit: Mapping[str, Any]) -> set[str]:
    values: list[Any] = []
    if unit.get("response_id") is not None:
        values.append(unit.get("response_id"))
    provider_usage = unit.get("provider_usage")
    if isinstance(provider_usage, Mapping):
        raw_ids = provider_usage.get("response_ids")
        if isinstance(raw_ids, (list, tuple, set, frozenset)):
            values.extend(raw_ids)
        elif raw_ids is not None:
            values.append(raw_ids)
        if provider_usage.get("response_id") is not None:
            values.append(provider_usage.get("response_id"))
    return {str(value).strip() for value in values if str(value).strip()}


def run_expected_request_count(run: Mapping[str, Any]) -> int:
    try:
        return derive_physical_request_count(run)
    except UsageEvidenceError as exc:
        raise FinalizationError(f"invalid physical usage evidence: {exc}") from exc


def run_expected_ensemble_request_count(run: Mapping[str, Any]) -> int:
    """Exclude provable setup-only analyzer calls from ensemble trace demand."""

    total = run_expected_request_count(run)
    analyzer_count = sum(
        1
        for unit in _canonical_task_analyzer_setup_units(
            run,
            identity_seed="expected-ensemble-request-count",
        )
    )
    return max(0, total - analyzer_count)


def g1_thinking_physical_usage_binding_reasons(
    run: Mapping[str, Any],
) -> list[str]:
    """Bind every managed/recovered physical request to one usage unit."""

    trace = run.get("ensemble_trace")
    calls, call_reasons = ensemble_call_trace_sequence(trace if isinstance(trace, Mapping) else {})
    strict_calls = [
        call
        for call in calls
        if isinstance(call.get("selection_plan"), Mapping)
        and (
            call["selection_plan"].get("thinking_physical_evidence_schema")
            == THINKING_PHYSICAL_EVIDENCE_SCHEMA
            or call["selection_plan"].get("proposer_recovery_policy") is not None
        )
    ]
    if not strict_calls:
        return []
    reasons = list(call_reasons)
    if len(strict_calls) != len(calls):
        reasons.append("mixed_g1_thinking_physical_evidence_schema")

    ledger_ids: list[str] = []
    prior_recovery_attempts: list[dict[str, Any]] = []
    prior_recovery_started = 0
    prior_recovery_roster_after: list[str] | None = None
    recovery_scope_id = ""
    recovery_fingerprint = ""
    for call in strict_calls:
        plan = call.get("selection_plan")
        provider_recovery_policy = (
            plan.get("proposer_recovery_policy") if isinstance(plan, Mapping) else None
        )
        candidates = call.get("candidates")
        current_candidate_ids: list[str] = []
        if isinstance(candidates, list):
            for candidate in candidates:
                execution = candidate.get("execution") if isinstance(candidate, Mapping) else None
                attempts = (
                    execution.get("physical_attempts") if isinstance(execution, Mapping) else None
                )
                if isinstance(attempts, list):
                    candidate_ids = [
                        str(attempt.get("physical_attempt_id") or "")
                        for attempt in attempts
                        if isinstance(attempt, Mapping) and attempt.get("request_started") is True
                    ]
                    ledger_ids.extend(candidate_ids)
                    current_candidate_ids.extend(candidate_ids)
        recovery = call.get("aggregator_recovery")
        attempts = recovery.get("attempts") if isinstance(recovery, Mapping) else None
        if isinstance(attempts, list):
            ledger_ids.extend(
                str(attempt.get("physical_attempt_id") or "")
                for attempt in attempts
                if isinstance(attempt, Mapping) and attempt.get("request_started") is True
            )

        if provider_recovery_policy is None:
            if call.get("proposer_recovery") is not None:
                reasons.append("unexpected_proposer_recovery_receipt")
            continue
        receipt = call.get("proposer_recovery")
        receipt_attempts = receipt.get("attempts") if isinstance(receipt, Mapping) else None
        if not isinstance(receipt, Mapping) or not isinstance(
            receipt_attempts,
            list,
        ):
            reasons.append("missing_proposer_recovery_receipt")
            continue
        normalized_receipts = [
            dict(attempt) for attempt in receipt_attempts if isinstance(attempt, Mapping)
        ]
        if len(normalized_receipts) != len(receipt_attempts):
            reasons.append("invalid_proposer_recovery_attempt")
            continue
        started = receipt.get("additional_physical_requests_started")
        started_total = sum(
            nonnegative_int(attempt.get("physical_request_count"))
            for attempt in normalized_receipts
            if attempt.get("request_started") is True
        )
        if (
            isinstance(started, bool)
            or not isinstance(started, int)
            or started != started_total
            or started < prior_recovery_started
            or len(normalized_receipts) < len(prior_recovery_attempts)
            or normalized_receipts[: len(prior_recovery_attempts)] != prior_recovery_attempts
        ):
            reasons.append("proposer_recovery_receipt_prefix_mismatch")
        new_receipts = normalized_receipts[len(prior_recovery_attempts) :]
        new_recovery_ids = [
            str(attempt.get("physical_attempt_id") or "")
            for attempt in new_receipts
            if attempt.get("request_started") is True
        ]
        new_recovery_id_set = set(new_recovery_ids)
        current_recovery_ids = [
            physical_id
            for physical_id in current_candidate_ids
            if physical_id in new_recovery_id_set
        ]
        if Counter(new_recovery_ids) != Counter(current_recovery_ids):
            reasons.append("proposer_recovery_incremental_physical_set_mismatch")
        scope_id = str(receipt.get("scope_id") or "")
        fingerprint = str(receipt.get("selection_plan_fingerprint") or "")
        roster_before = receipt.get("executed_proposer_roster_before")
        roster_after = receipt.get("executed_proposer_roster_after")
        normalized_before = (
            [str(identity or "") for identity in roster_before]
            if isinstance(roster_before, list)
            else []
        )
        normalized_after = (
            [str(identity or "") for identity in roster_after]
            if isinstance(roster_after, list)
            else []
        )
        if recovery_scope_id and scope_id != recovery_scope_id:
            reasons.append("proposer_recovery_scope_changed")
        if recovery_fingerprint and fingerprint != recovery_fingerprint:
            reasons.append("proposer_recovery_fingerprint_changed")
        initial_slot_identities, initial_slot_reasons = (
            expanded_proposer_slot_identities(plan)
            if isinstance(plan, Mapping)
            else ((), ["invalid_expanded_proposer_sample_roster"])
        )
        reasons.extend(initial_slot_reasons)
        if prior_recovery_roster_after is None and normalized_before != list(
            initial_slot_identities
        ):
            reasons.append("proposer_recovery_roster_prefix_changed")
        elif (
            prior_recovery_roster_after is not None
            and normalized_before != prior_recovery_roster_after
        ):
            reasons.append("proposer_recovery_roster_prefix_changed")
        recovery_scope_id = scope_id
        recovery_fingerprint = fingerprint
        prior_recovery_attempts = normalized_receipts
        prior_recovery_started = nonnegative_int(started)
        prior_recovery_roster_after = normalized_after

    if any(HEX32.fullmatch(attempt_id) is None for attempt_id in ledger_ids) or len(
        ledger_ids
    ) != len(set(ledger_ids)):
        reasons.append("invalid_g1_thinking_physical_attempt_set")

    try:
        units = canonical_run_usage_units(
            run,
            identity_seed="g1-thinking-physical-usage-binding",
        )
    except (FinalizationError, UsageEvidenceError, TypeError):
        reasons.extend(
            (
                "g1_thinking_physical_usage_set_mismatch",
                "g1_thinking_physical_usage_multiplicity_mismatch",
            )
        )
        return list(dict.fromkeys(reasons))
    analyzer_ids = {
        _task_analyzer_physical_attempt_id(unit)
        for unit in _canonical_task_analyzer_setup_units(
            run,
            identity_seed="g1-thinking-physical-usage-binding",
        )
    }
    analyzer_ids.discard("")
    generation_units = [
        unit
        for unit in units
        if str(unit.get("role") or "").strip().casefold()
        not in {"task_analyzer", "task_analyzer_attempt"}
        and str(unit.get("physical_attempt_id") or "") not in analyzer_ids
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
        if HEX32.fullmatch(attempt_id) is None or nested_id != attempt_id:
            reasons.append("invalid_g1_thinking_usage_physical_attempt_id")
            continue
        usage_ids.append(attempt_id)

    if Counter(usage_ids) != Counter(ledger_ids):
        reasons.append("g1_thinking_physical_usage_set_mismatch")
    try:
        expected = run_expected_ensemble_request_count(run)
    except FinalizationError:
        reasons.append("g1_thinking_physical_usage_multiplicity_mismatch")
        return list(dict.fromkeys(reasons))
    if len(ledger_ids) != expected or len(generation_units) != expected:
        reasons.append("g1_thinking_physical_usage_multiplicity_mismatch")
    return list(dict.fromkeys(reasons))


def iter_judge_runs(judge: Any) -> Iterable[tuple[str, Mapping[str, Any]]]:
    if not isinstance(judge, Mapping):
        return
    prior = judge.get("prior_judge_attempts")
    if isinstance(prior, list):
        for index, attempt in enumerate(prior):
            run = attempt.get("run") if isinstance(attempt, Mapping) else None
            if isinstance(run, Mapping):
                yield f"prior/{index}", run
    criteria = judge.get("criterion_judgments")
    if isinstance(criteria, list):
        for criterion_index, criterion in enumerate(criteria):
            attempts = criterion.get("judge_attempts") if isinstance(criterion, Mapping) else None
            if not isinstance(attempts, list):
                continue
            for attempt_index, attempt in enumerate(attempts):
                run = attempt.get("run") if isinstance(attempt, Mapping) else None
                if isinstance(run, Mapping):
                    criterion_id = str(criterion.get("id") or criterion_index)
                    repeat_index = nonnegative_int(criterion.get("repeat_index"))
                    attempt_id = str(attempt.get("attempt_id") or attempt_index)
                    yield (
                        f"criterion/{criterion_id}/{repeat_index}/{attempt_id}",
                        run,
                    )
        return
    attempts = judge.get("judge_attempts")
    if isinstance(attempts, list):
        for index, attempt in enumerate(attempts):
            run = attempt.get("run") if isinstance(attempt, Mapping) else None
            if isinstance(run, Mapping):
                yield f"attempt/{index}", run


def proof_only_usage_evidence_reasons(
    row: Mapping[str, Any],
    *,
    judge_model: str = JUDGE_MODEL,
    judge_provider_pin: str | None = None,
) -> list[str]:
    """Check that a campaign-proof-only row can enter canonical finalization.

    This deliberately uses the same canonicalizer and strict Judge route
    validator as finalization.  The campaign gate calls it only after the
    resume classifier has established valid generation and complete Judge
    semantics.
    """

    reasons: list[str] = []
    execution = row.get("execution")
    generation_attempts = (
        execution.get("generation_attempts")
        if isinstance(execution, Mapping) and isinstance(execution.get("generation_attempts"), list)
        else []
    )
    for index, attempt in enumerate(generation_attempts):
        if not isinstance(attempt, Mapping):
            reasons.append(f"generation_attempt/{index}/not_object")
            continue
        attempt_id = str(attempt.get("attempt_id") or "")
        run = attempt.get("run")
        if HEX32.fullmatch(attempt_id) is None or not isinstance(run, Mapping):
            reasons.append(f"generation_attempt/{index}/invalid_identity_or_run")
            continue
        try:
            canonical = _canonicalized_run(
                run,
                identity_seed=f"generation-attempt:{attempt_id}",
            )
            units = canonical_run_usage_units(
                canonical,
                identity_seed=f"generation-attempt:{attempt_id}",
            )
            if len(units) != run_expected_request_count(canonical):
                reasons.append(f"generation_attempt/{attempt_id}/usage_shape")
        except (FinalizationError, UsageEvidenceError, TypeError) as exc:
            reasons.append(f"generation_attempt/{attempt_id}/{type(exc).__name__}")

    judge_scopes: list[tuple[str, Any]] = [("judge", row.get("judge"))]
    candidate_judges = row.get("candidate_judges")
    if candidate_judges is not None:
        if not isinstance(candidate_judges, list):
            reasons.append("candidate_judges/not_list")
        else:
            judge_scopes.extend(
                (f"candidate_judge/{index}", judge) for index, judge in enumerate(candidate_judges)
            )
    for scope, judge in judge_scopes:
        if judge is None:
            continue
        if not isinstance(judge, Mapping):
            reasons.append(f"{scope}/not_object")
            continue
        for path, run in iter_judge_runs(judge):
            attempt_id = path.rsplit("/", 1)[-1]
            if HEX32.fullmatch(attempt_id) is None:
                reasons.append(f"{scope}/{path}/invalid_attempt_id")
                continue
            try:
                _, route_reasons = canonical_judge_run_route_reasons(
                    run,
                    attempt_id=attempt_id,
                    judge_model=judge_model,
                    judge_provider_pin=judge_provider_pin,
                )
            except (FinalizationError, UsageEvidenceError, TypeError) as exc:
                reasons.append(f"{scope}/{path}/{type(exc).__name__}")
                continue
            reasons.extend(f"{scope}/{path}/{reason}" for reason in route_reasons)
    return list(dict.fromkeys(reasons))


def unit_identity_signature(unit: Mapping[str, Any]) -> str:
    """Cost-repair-stable identity for a no-response-id physical unit."""

    return canonical_sha256(
        {
            "usage_contract": usage_generation_contract(unit),
            "role": unit.get("role"),
        }
    )


def _record_unit(
    entries: dict[str, LedgerEntry],
    response_id_bindings: dict[str, dict[str, Any]],
    *,
    identity: str,
    logical_physical_identity: str,
    unit: Mapping[str, Any],
    scope: str,
    reference: Mapping[str, Any],
) -> None:
    ids = response_ids(unit)
    reused = {
        response_id: response_id_bindings[response_id]
        for response_id in ids
        if response_id in response_id_bindings
    }
    if reused:
        raise FinalizationError(
            "provider response_id is reused across logical physical requests: "
            f"current={logical_physical_identity}/{scope}/{dict(reference)}, "
            f"first={reused}"
        )
    if ids:
        identity = f"response:{canonical_sha256(sorted(ids))}"
    entry = entries.setdefault(identity, LedgerEntry(identity))
    entry.scopes.add(scope)
    entry.units.append(copy.deepcopy(dict(unit)))
    entry.references.append(dict(reference))
    entry.response_ids.update(ids)
    for response_id in ids:
        response_id_bindings[response_id] = {
            "ledger_identity": identity,
            "logical_physical_identity": logical_physical_identity,
            "scope": scope,
            "reference": dict(reference),
        }


def _record_run(
    entries: dict[str, LedgerEntry],
    response_id_bindings: dict[str, dict[str, Any]],
    *,
    run: Mapping[str, Any],
    scope: str,
    base_identity: str,
    reference: Mapping[str, Any],
    occurrence_counter: Counter[str],
) -> None:
    units = canonical_run_usage_units(run, identity_seed=base_identity)
    expected = derive_physical_request_count(run)
    for unit in units:
        ids = response_ids(unit)
        signature = unit_identity_signature(unit)
        occurrence = occurrence_counter[signature]
        occurrence_counter[signature] += 1
        identity = (
            f"response:{canonical_sha256(sorted(ids))}"
            if ids
            else f"{base_identity}:unit:{signature}:{occurrence}"
        )
        logical_physical_identity = f"{base_identity}:unit:{signature}:{occurrence}"
        _record_unit(
            entries,
            response_id_bindings,
            identity=identity,
            logical_physical_identity=logical_physical_identity,
            unit=unit,
            scope=scope,
            reference=reference,
        )
    if len(units) != expected:
        raise FinalizationError(
            f"{base_identity} canonical usage shape does not match its physical request count"
        )


def build_actual_spend_ledger(
    records: Sequence[SourceRecord],
    *,
    selected: Sequence[SourceRecord] = (),
    selected_attempt_bindings: Mapping[str, str] | None = None,
    judge_model: str = JUDGE_MODEL,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rebuild physical spend from every wave; never trust a row-level total."""

    entries: dict[str, LedgerEntry] = {}
    response_id_bindings: dict[str, dict[str, Any]] = {}
    seen_generation_attempts: set[str] = set()
    generation_attempt_versions: dict[str, list[tuple[SourceRecord, Mapping[str, Any], int]]] = (
        defaultdict(list)
    )
    bindings = (
        dict(selected_attempt_bindings)
        if selected_attempt_bindings is not None
        else bind_selected_generation_attempts(records, selected)
        if selected
        else {}
    )
    if selected and len(bindings) != len(selected):
        raise FinalizationError(
            "selected generation attempt binding does not cover every selected pair"
        )
    selected_attempt_ids = set(bindings.values())
    judge_run_versions: dict[str, list[tuple[SourceRecord, str, Mapping[str, Any], str]]] = (
        defaultdict(list)
    )

    for record in records:
        row = record.row
        execution = row.get("execution")
        attempts = (
            execution.get("generation_attempts")
            if isinstance(execution, Mapping)
            and isinstance(execution.get("generation_attempts"), list)
            else []
        )
        for fallback_index, attempt in enumerate(attempts, start=1):
            if not isinstance(attempt, Mapping):
                continue
            attempt_id = str(attempt.get("attempt_id") or "")
            seen_generation_attempts.add(attempt_id)
            generation_attempt_versions[attempt_id].append((record, attempt, fallback_index))

        for judge_scope, judge in (
            ("judge", row.get("judge")),
            *(
                (f"candidate_judge/{index}", item)
                for index, item in enumerate(row.get("candidate_judges") or [])
            ),
        ):
            for path, run in iter_judge_runs(judge):
                identity = f"judge:{record.key[0]}:{record.key[1]}:{judge_scope}:{path}"
                judge_run_versions[identity].append((record, judge_scope, run, path))

    # A copied repair can add receipt/provider/cost metadata to the same
    # immutable attempt.  Retain exactly one physical request set, but choose
    # the most enriched monotonic copy instead of freezing the first wave.
    for attempt_id, versions in generation_attempt_versions.items():
        physical_record = min(
            (version[0] for version in versions),
            key=lambda candidate: (candidate.source_index, candidate.line),
        )
        record, run = validate_and_select_monotonic_run_version(
            [
                (candidate_record, attempt.get("run"))
                for candidate_record, attempt, _ in versions
                if isinstance(attempt.get("run"), Mapping)
            ],
            label=f"generation attempt {attempt_id}",
            identity_seed=f"generation-attempt:{attempt_id}",
        )
        matching_versions = [version for version in versions if version[0] is record]
        if not matching_versions:
            raise FinalizationError(
                f"generation attempt {attempt_id} selected an unknown receipt version"
            )
        _, attempt, fallback_index = matching_versions[-1]
        attempt_index = nonnegative_int(attempt.get("attempt")) or fallback_index
        _record_run(
            entries,
            response_id_bindings,
            run=run,
            scope="generation",
            base_identity=f"generation-attempt:{attempt_id}",
            reference=record.reference
            | {
                "group": record.key[0],
                "task_id": record.key[1],
                "phase": "generation",
                "attempt": attempt_index,
                "attempt_id": attempt_id,
                "attempt_kind": attempt.get("attempt_kind"),
                "attempt_outcome": ("failed" if str(run.get("error") or "") else "successful"),
                "selected_generation": attempt_id in selected_attempt_ids,
                "receipt_version_count": len(versions),
                "receipt_version_selected": True,
                "physical_source_index": physical_record.source_index,
                "physical_source_path": str(physical_record.path),
                "physical_source_line": physical_record.line,
                "receipt_source_index": record.source_index,
                "receipt_source_path": str(record.path),
                "receipt_source_line": record.line,
            },
            occurrence_counter=Counter(),
        )

    # Judge repair rows copy earlier logical attempt paths.  Select the most
    # enriched monotonic copy for each path; newly appended retry paths remain
    # independent physical calls.
    for identity, versions in judge_run_versions.items():
        physical_record = min(
            (version[0] for version in versions),
            key=lambda candidate: (candidate.source_index, candidate.line),
        )
        judge_path = versions[0][3]
        attempt_id = judge_path.rsplit("/", 1)[-1]
        if HEX32.fullmatch(attempt_id) is None:
            raise FinalizationError(f"{identity} lacks a stable Judge attempt identity")
        record, run = validate_and_select_monotonic_run_version(
            [(version[0], version[2]) for version in versions],
            label=identity,
            identity_seed=f"judge-attempt:{attempt_id}",
            requested_provider="openrouter",
            requested_model=judge_model,
            role="unknown_request",
        )
        matching_versions = [version for version in versions if version[0] is record]
        if not matching_versions:
            raise FinalizationError(f"{identity} selected an unknown receipt version")
        _, judge_scope, _, path = matching_versions[-1]
        _record_run(
            entries,
            response_id_bindings,
            run=run,
            scope=judge_scope,
            base_identity=identity,
            reference=record.reference
            | {
                "group": record.key[0],
                "task_id": record.key[1],
                "phase": judge_scope,
                "judge_path": path,
                "receipt_version_count": len(versions),
                "receipt_version_selected": True,
                "physical_source_index": physical_record.source_index,
                "physical_source_path": str(physical_record.path),
                "physical_source_line": physical_record.line,
                "receipt_source_index": record.source_index,
                "receipt_source_path": str(record.path),
                "receipt_source_line": record.line,
            },
            occurrence_counter=Counter(),
        )

    ledger_rows = [ledger_entry_payload(entry) for entry in entries.values()]
    ledger_rows.sort(key=lambda value: value["ledger_id"])
    category_counts = Counter(str(row["non_byok_evidence"]) for row in ledger_rows)
    scope_counts: Counter[str] = Counter()
    scope_costs: dict[str, Decimal] = defaultdict(Decimal)
    scope_exact_counts: Counter[str] = Counter()
    scope_non_exact_counts: Counter[str] = Counter()
    scope_unknown_counts: Counter[str] = Counter()
    disposition_counts: Counter[str] = Counter()
    disposition_costs: dict[str, Decimal] = defaultdict(Decimal)
    recorded_cost = Decimal(0)
    exact_cost = Decimal(0)
    unknown_cost_count = 0
    non_exact_cost_count = 0
    for row in ledger_rows:
        for scope in row["scopes"]:
            scope_counts[scope] += 1
        disposition = str(row.get("generation_disposition") or "")
        if disposition:
            disposition_counts[disposition] += 1
        cost = row.get("recorded_cost_usd")
        if cost is None:
            unknown_cost_count += 1
            for scope in row["scopes"]:
                scope_unknown_counts[scope] += 1
        else:
            parsed = required_decimal(cost, label="ledger cost")
            recorded_cost += parsed
            if row.get("cost_precision") == "exact":
                exact_cost += parsed
                for scope in row["scopes"]:
                    scope_exact_counts[scope] += 1
            else:
                non_exact_cost_count += 1
                for scope in row["scopes"]:
                    scope_non_exact_counts[scope] += 1
            for scope in row["scopes"]:
                scope_costs[scope] += parsed
            if disposition:
                disposition_costs[disposition] += parsed
    summary = {
        "schema": LEDGER_SCHEMA,
        "physical_request_count": len(ledger_rows),
        "scope_request_counts": dict(sorted(scope_counts.items())),
        "scope_recorded_cost_usd": {key: str(value) for key, value in sorted(scope_costs.items())},
        "scope_cost_precision_counts": {
            scope: {
                "exact": scope_exact_counts[scope],
                "non_exact": scope_non_exact_counts[scope],
                "unknown": scope_unknown_counts[scope],
            }
            for scope in sorted(scope_counts)
        },
        "generation_disposition_request_counts": dict(sorted(disposition_counts.items())),
        "generation_disposition_recorded_cost_usd": {
            key: str(value) for key, value in sorted(disposition_costs.items())
        },
        "non_byok_evidence_counts": dict(sorted(category_counts.items())),
        "recorded_cost_usd": str(recorded_cost),
        "exact_cost_usd": str(exact_cost),
        "unknown_cost_request_count": unknown_cost_count,
        "non_exact_cost_request_count": non_exact_cost_count,
        "source_row_count": len(records),
        "distinct_generation_attempt_count": len(seen_generation_attempts),
        "selected_generation_pair_count": len(bindings),
        "selected_generation_attempt_count": len(selected_attempt_ids),
        "note": (
            "Built from all source-wave generation attempts and Judge attempts; "
            "copied repairs are deduplicated by stable response id or retained "
            "run-occurrence identity. Failed and replaced generation attempts remain."
        ),
    }
    return ledger_rows, summary


def attach_retrospective_recovery_spend(
    pair_audit: Mapping[tuple[str, str] | str, Any],
    ledger_rows: Sequence[Mapping[str, Any]],
) -> None:
    """Bind any accepted post-valid retry exception to its actual failed spend."""

    for selection in pair_audit.values():
        if not isinstance(selection, dict):
            continue
        recovery = selection.get("retrospective_reclassification_recovery")
        if not isinstance(recovery, dict):
            continue
        invalid_ids = {
            str(value)
            for value in recovery.get("invalid_post_accept_attempt_ids", [])
            if HEX32.fullmatch(str(value))
        }
        if not invalid_ids:
            raise FinalizationError("retrospective recovery lacks invalid attempt identifiers")
        matched: list[Mapping[str, Any]] = []
        matched_ids: set[str] = set()
        for ledger_row in ledger_rows:
            references = ledger_row.get("source_references")
            row_ids = {
                str(reference.get("attempt_id") or "")
                for reference in references or []
                if isinstance(reference, Mapping)
            }
            overlap = row_ids & invalid_ids
            if not overlap:
                continue
            if ledger_row.get("generation_disposition") != "failed":
                raise FinalizationError(
                    "retrospective invalid attempt is not failed in the actual-spend ledger"
                )
            matched.append(ledger_row)
            matched_ids.update(overlap)
        if matched_ids != invalid_ids or not matched:
            raise FinalizationError(
                "retrospective invalid attempts are not fully represented in the ledger"
            )
        known_costs = [
            required_decimal(
                row.get("recorded_cost_usd"),
                label="retrospective failed-attempt ledger cost",
            )
            for row in matched
            if row.get("recorded_cost_usd") is not None
        ]
        precision_counts = Counter(str(row.get("cost_precision") or "unknown") for row in matched)
        recovery["invalid_post_accept_spend"] = {
            "physical_request_count": len(matched),
            "recorded_cost_usd": str(sum(known_costs, Decimal(0))),
            "exact_request_count": precision_counts["exact"],
            "non_exact_request_count": len(matched)
            - precision_counts["exact"]
            - precision_counts["unknown"],
            "unknown_cost_request_count": precision_counts["unknown"],
            "cost_complete": len(known_costs) == len(matched),
            "cost_exact": precision_counts["exact"] == len(matched),
        }


def ledger_model_metrics(
    ledger_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in ledger_rows:
        scopes = row.get("scopes")
        phase = (
            "generation"
            if isinstance(scopes, list) and "generation" in scopes
            else "judge"
            if isinstance(scopes, list) and any("judge" in str(scope) for scope in scopes)
            else "other"
        )
        grouped[
            (
                phase,
                str(row.get("provider") or "<unknown>"),
                str(row.get("model") or "<unknown>"),
            )
        ].append(row)
    metrics: list[dict[str, Any]] = []
    for (phase, provider, model), rows in sorted(grouped.items()):
        known_costs = [
            required_decimal(
                row.get("recorded_cost_usd"),
                label="model ledger cost",
            )
            for row in rows
            if row.get("recorded_cost_usd") is not None
        ]
        metrics.append(
            {
                "phase": phase,
                "provider": provider,
                "model": model,
                "calls": len(rows),
                "input_tokens": sum(nonnegative_int(row.get("input_tokens")) for row in rows),
                "output_tokens": sum(nonnegative_int(row.get("output_tokens")) for row in rows),
                "recorded_cost_usd": str(sum(known_costs, Decimal(0))),
                "exact_request_count": sum(row.get("cost_precision") == "exact" for row in rows),
                "estimated_or_recorded_request_count": sum(
                    row.get("recorded_cost_usd") is not None
                    and row.get("cost_precision") != "exact"
                    for row in rows
                ),
                "unknown_request_count": sum(row.get("recorded_cost_usd") is None for row in rows),
                "upstream_providers": sorted(
                    {str(value) for row in rows for value in row.get("upstream_providers") or []}
                ),
                "upstream_models": sorted(
                    {str(value) for row in rows for value in row.get("upstream_models") or []}
                ),
                "roles": sorted({str(value) for row in rows for value in row.get("roles") or []}),
                "cost_sources": sorted(
                    {str(value) for row in rows for value in row.get("cost_sources") or []}
                ),
            }
        )
    return metrics


def paid_external_tool_path(row: Mapping[str, Any]) -> tuple[bool, set[str]]:
    policy = row.get("tool_policy")
    policy = policy if isinstance(policy, Mapping) else {}
    local = policy.get("local_web_tools")
    local = local if isinstance(local, Mapping) else {}
    search = local.get("web_search")
    search = search if isinstance(search, Mapping) else {}
    fetch = local.get("web_fetch")
    fetch = fetch if isinstance(fetch, Mapping) else {}
    providers: set[str] = set()
    search_provider = str(search.get("provider") or "").strip().casefold()
    if search_provider:
        providers.add(search_provider)
    if fetch.get("allow_firecrawl") is True:
        providers.add("firecrawl")
    paid = str(policy.get("tool_mode") or "") == "local_web_tools" and (
        "brave" in providers or "firecrawl" in providers
    )
    return paid, providers


def attempt_tool_call_count(run: Mapping[str, Any]) -> int:
    declared = run.get("total_tool_call_count")
    if isinstance(declared, int) and not isinstance(declared, bool):
        return max(0, declared)
    return max(
        0,
        nonnegative_int(run.get("tool_call_count")),
        nonnegative_int(run.get("stream_tool_call_count"))
        + nonnegative_int(run.get("server_tool_call_count")),
    )


def parse_external_scope(
    value: Any,
    *,
    expected_calls: int,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    declared_calls = value.get("tool_call_count")
    if (
        not isinstance(declared_calls, int)
        or isinstance(declared_calls, bool)
        or declared_calls != expected_calls
    ):
        return None

    def optional_cost(field_name: str) -> Decimal | None:
        raw = value.get(field_name)
        if raw is None or isinstance(raw, bool):
            return None
        try:
            parsed = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return parsed if parsed.is_finite() and parsed >= 0 else None

    recorded = optional_cost("recorded_cost_usd")
    estimated = optional_cost("estimated_cost_usd")
    complete = value.get("cost_complete") is True
    exact = value.get("cost_exact") is True
    upper = nonnegative_int(value.get("potentially_unpriced_tool_call_count_upper_bound"))
    if exact and (not complete or recorded is None or upper):
        return None
    return {
        "tool_call_count": expected_calls,
        "recorded_cost": recorded,
        "estimated_cost": estimated,
        "potentially_unpriced_tool_call_count_upper_bound": upper,
        "cost_complete": complete,
        "cost_exact": exact,
    }


def derived_external_scope(
    *,
    tool_call_count: int,
    paid_path: bool,
) -> dict[str, Any]:
    unknown = paid_path and tool_call_count > 0
    return {
        "tool_call_count": tool_call_count,
        "recorded_cost": None if unknown else Decimal(0),
        "estimated_cost": None,
        "potentially_unpriced_tool_call_count_upper_bound": (tool_call_count if unknown else 0),
        "cost_complete": not unknown,
        "cost_exact": not unknown,
    }


def build_external_tool_cost_summary(
    records: Sequence[SourceRecord],
    *,
    manifest_sources: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Aggregate Web/Brave spend separately, deduplicated by attempt ID."""

    seen_attempts: set[str] = set()
    scopes: list[dict[str, Any]] = []
    providers: set[str] = set()
    for record in sorted(records, key=lambda item: (item.source_index, item.line)):
        execution = record.row.get("execution")
        attempts = (
            execution.get("generation_attempts")
            if isinstance(execution, Mapping)
            and isinstance(execution.get("generation_attempts"), list)
            else []
        )
        new_attempts: list[Mapping[str, Any]] = []
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                continue
            attempt_id = str(attempt.get("attempt_id") or "")
            if attempt_id in seen_attempts:
                continue
            seen_attempts.add(attempt_id)
            new_attempts.append(attempt)
        if not new_attempts:
            continue
        paid_path, row_providers = paid_external_tool_path(record.row)
        providers.update(row_providers)
        attempt_runs = [
            attempt.get("run")
            for attempt in new_attempts
            if isinstance(attempt.get("run"), Mapping)
        ]
        call_count = sum(
            attempt_tool_call_count(run) for run in attempt_runs if isinstance(run, Mapping)
        )
        run_scopes: list[dict[str, Any]] = []
        for run in attempt_runs:
            if not isinstance(run, Mapping):
                continue
            accounting = run.get("cost_accounting")
            external = (
                accounting.get("actual_external_tools") or accounting.get("external_tools")
                if isinstance(accounting, Mapping)
                else None
            )
            parsed = parse_external_scope(
                external,
                expected_calls=attempt_tool_call_count(run),
            )
            if parsed is None:
                run_scopes = []
                break
            run_scopes.append(parsed)
        if run_scopes and sum(scope["tool_call_count"] for scope in run_scopes) == call_count:
            scopes.extend(run_scopes)
            continue
        accounting = record.row.get("cost_accounting")
        row_external = (
            accounting.get("actual_external_tools") if isinstance(accounting, Mapping) else None
        )
        parsed_row = parse_external_scope(
            row_external,
            expected_calls=call_count,
        )
        scopes.append(
            parsed_row
            if parsed_row is not None
            else derived_external_scope(
                tool_call_count=call_count,
                paid_path=paid_path,
            )
        )

    task_tool_calls = sum(scope["tool_call_count"] for scope in scopes)
    task_upper_bound = sum(
        scope["potentially_unpriced_tool_call_count_upper_bound"] for scope in scopes
    )
    preflight_by_tool: Counter[str] = Counter()
    for source in manifest_sources:
        preflight = source.get("live_web_preflight")
        calls = preflight.get("preflight_calls") if isinstance(preflight, Mapping) else None
        if not isinstance(calls, Mapping):
            raise FinalizationError("manifest source lacks live Web preflight evidence")
        for tool_name in ("web_search", "web_fetch"):
            value = calls.get(tool_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise FinalizationError("manifest Web preflight call count is invalid")
            preflight_by_tool[tool_name] += value
    preflight_tool_calls = sum(preflight_by_tool.values())
    tool_calls = task_tool_calls + preflight_tool_calls
    # Live preflight happens outside generation/Judge traces and has no
    # provider-dollar receipt.  It is campaign overhead, never zero-cost.
    upper_bound = task_upper_bound + preflight_tool_calls
    exact = bool(scopes) and all(scope["cost_exact"] for scope in scopes)
    complete = bool(scopes) and all(scope["cost_complete"] for scope in scopes)
    if preflight_tool_calls:
        exact = False
        complete = False
    known_lower_bound = sum(
        (scope["recorded_cost"] if isinstance(scope.get("recorded_cost"), Decimal) else Decimal(0))
        for scope in scopes
    )
    has_unknown = preflight_tool_calls > 0 or any(
        not scope["cost_complete"] and scope.get("estimated_cost") is None for scope in scopes
    )
    estimated_total: Decimal | None = None
    if scopes and not has_unknown:
        estimated_total = sum(
            (
                scope["recorded_cost"]
                if scope["cost_exact"] and isinstance(scope.get("recorded_cost"), Decimal)
                else scope["estimated_cost"]
                if isinstance(scope.get("estimated_cost"), Decimal)
                else Decimal(0)
            )
            for scope in scopes
        )
    status = "exact" if exact else "estimated" if estimated_total is not None else "unknown"
    return {
        "scope": "campaign_actual_external_tools",
        "providers": sorted(providers),
        "distinct_generation_attempt_count": len(seen_attempts),
        "tool_call_count": tool_calls,
        "task_generation_tool_call_count": task_tool_calls,
        "live_preflight_tool_call_count": preflight_tool_calls,
        "live_preflight_calls_by_tool": dict(sorted(preflight_by_tool.items())),
        "live_preflight_manifest_count": len(manifest_sources),
        "recorded_cost_usd": str(known_lower_bound) if complete else None,
        "recorded_cost_usd_lower_bound": str(known_lower_bound),
        "estimated_cost_usd": (
            str(estimated_total) if estimated_total is not None and not exact else None
        ),
        "potentially_unpriced_tool_call_count_upper_bound": upper_bound,
        "cost_complete": complete,
        "cost_exact": exact,
        "cost_status": status,
        "cost_precision": status,
        "recorded_cost_usd_is_lower_bound": not complete,
        "separate_from_openrouter_account_delta": True,
        "deduplication": (
            "task calls: unique generation attempt_id across every source wave; "
            "live preflight: once per supplied source manifest"
        ),
        "note": (
            "Unknown Brave/Firecrawl calls are not reported as zero-dollar spend "
            "and are never mixed into the OpenRouter LLM account delta."
        ),
    }


def stable_receipt_conflicts(units: Sequence[Mapping[str, Any]]) -> set[str]:
    conflicts: set[str] = set()
    providers: set[str] = set()
    models: set[str] = set()
    costs: set[Decimal] = set()
    token_values: dict[str, set[int]] = defaultdict(set)
    for unit in units:
        provider = str(unit.get("provider") or "").strip().casefold()
        model = str(unit.get("model") or "").strip()
        if provider:
            providers.add(provider)
        if model:
            models.add(model)
        provider_usage = unit.get("provider_usage")
        provider_reported = (
            provider_usage.get("provider_reported_cost")
            if isinstance(provider_usage, Mapping)
            else None
        )
        cost_source = str(unit.get("cost_source") or "").casefold()
        raw_costs = [provider_reported]
        if "estimate" not in cost_source:
            raw_costs.append(unit.get("billed_cost"))
        for raw_cost in raw_costs:
            if raw_cost is not None and not isinstance(raw_cost, bool):
                try:
                    cost = Decimal(str(raw_cost))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if cost.is_finite() and cost >= 0:
                    costs.add(cost.quantize(Decimal("0.000000001")))
        if isinstance(provider_usage, Mapping):
            evidence = provider_usage.get("stable_receipt_evidence")
            if isinstance(evidence, Mapping):
                if evidence.get("receipt_conflict") is True:
                    conflicts.add("inherited_receipt_conflict")
                conflicts.update(
                    str(value) for value in evidence.get("conflict_fields") or [] if str(value)
                )
        for key in USAGE_CONTRACT_KEYS[4:]:
            raw = unit.get(key)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                token_values[key].add(raw)
    if len(providers) > 1:
        conflicts.add("provider")
    if len(models) > 1:
        conflicts.add("model")
    if len(costs) > 1:
        conflicts.add("cost")
    for key, values in token_values.items():
        if len(values) > 1:
            conflicts.add(key)
    return conflicts


def router_provider_metadata_complete(router_metadata: Mapping[str, Any]) -> bool:
    attempts = router_metadata.get("attempts")
    if isinstance(attempts, list):
        if any(
            isinstance(attempt, Mapping)
            and str(attempt.get("provider") or "").strip()
            and str(attempt.get("model") or "").strip()
            and isinstance(attempt.get("status"), int)
            and not isinstance(attempt.get("status"), bool)
            and 200 <= int(attempt["status"]) < 300
            for attempt in attempts
        ):
            return True
    endpoints = router_metadata.get("endpoints")
    available = endpoints.get("available") if isinstance(endpoints, Mapping) else None
    return isinstance(available, list) and any(
        isinstance(endpoint, Mapping)
        and endpoint.get("selected") is True
        and str(endpoint.get("provider") or "").strip()
        and str(endpoint.get("model") or "").strip()
        for endpoint in available
    )


def unit_non_byok_flags(unit: Mapping[str, Any]) -> tuple[set[bool], set[bool]]:
    usage_values: set[bool] = set()
    router_values: set[bool] = set()
    provider_usage = unit.get("provider_usage")
    if not isinstance(provider_usage, Mapping):
        return usage_values, router_values
    if provider_usage.get("is_byok") in {True, False}:
        usage_values.add(bool(provider_usage["is_byok"]))
    router_metadata = provider_usage.get("router_metadata")
    if isinstance(router_metadata, Mapping) and router_metadata.get("is_byok") in {
        True,
        False,
    }:
        router_values.add(bool(router_metadata["is_byok"]))
    evidence = provider_usage.get("stable_receipt_evidence")
    if isinstance(evidence, Mapping):
        usage_values.update(
            value for value in evidence.get("usage_is_byok_values") or [] if value in {True, False}
        )
        router_values.update(
            value for value in evidence.get("router_is_byok_values") or [] if value in {True, False}
        )
    return usage_values, router_values


def unit_exact_non_byok(unit: Mapping[str, Any]) -> bool:
    if str(unit.get("provider") or "").strip().casefold() != "openrouter":
        return False
    provider_usage = unit.get("provider_usage")
    if not isinstance(provider_usage, Mapping):
        return False
    router_metadata = provider_usage.get("router_metadata")
    ids = response_ids(unit)
    routed_model = str(unit.get("requested_model") or unit.get("model") or "").strip()
    successful_models = {upstream_model for _, upstream_model in _successful_router_bindings(unit)}
    if (
        provider_usage.get("is_byok") is not False
        or not isinstance(router_metadata, Mapping)
        or router_metadata.get("is_byok") is not False
        or not router_provider_metadata_complete(router_metadata)
        or not routed_model
        or not successful_models
        or not all(
            _formal_openrouter_models_equivalent(routed_model, upstream_model)
            for upstream_model in successful_models
        )
        or not ids
    ):
        return False
    try:
        billed = required_decimal(unit.get("billed_cost"), label="billed cost")
        reported = required_decimal(
            provider_usage.get("provider_reported_cost"),
            label="provider-reported cost",
        )
    except FinalizationError:
        return False
    return billed.quantize(Decimal("0.000000001")) == reported.quantize(Decimal("0.000000001"))


def unit_cost_is_exact(unit: Mapping[str, Any]) -> bool:
    if str(unit.get("provider") or "").strip().casefold() != "openrouter":
        return False
    provider_usage = unit.get("provider_usage")
    if not isinstance(provider_usage, Mapping) or not response_ids(unit):
        return False
    try:
        billed = required_decimal(unit.get("billed_cost"), label="billed cost")
        reported = required_decimal(
            provider_usage.get("provider_reported_cost"),
            label="provider-reported cost",
        )
    except FinalizationError:
        return False
    return billed.quantize(Decimal("0.000000001")) == reported.quantize(Decimal("0.000000001"))


def ledger_entry_payload(entry: LedgerEntry) -> dict[str, Any]:
    conflicts = stable_receipt_conflicts(entry.units)
    usage_flags: set[bool] = set()
    router_flags: set[bool] = set()
    providers: set[str] = set()
    models: set[str] = set()
    upstream_providers: set[str] = set()
    upstream_models: set[str] = set()
    roles: set[str] = set()
    cost_sources: set[str] = set()
    for unit in entry.units:
        usage, router = unit_non_byok_flags(unit)
        usage_flags.update(usage)
        router_flags.update(router)
        provider = str(unit.get("provider") or "").strip().casefold()
        model = str(unit.get("model") or "").strip()
        if provider:
            providers.add(provider)
        if model:
            models.add(model)
        role = str(unit.get("role") or "").strip()
        if role:
            roles.add(role)
        cost_source = str(unit.get("cost_source") or "").strip()
        if cost_source:
            cost_sources.add(cost_source)
        for upstream_provider, upstream_model in _successful_router_bindings(unit):
            if upstream_provider:
                upstream_providers.add(upstream_provider)
            if upstream_model:
                upstream_models.add(upstream_model)
    combined_flags = usage_flags | router_flags
    if (
        conflicts
        or len(combined_flags) > 1
        or any(provider != "openrouter" for provider in providers)
    ):
        category = "conflict"
    elif True in combined_flags:
        category = "explicit_byok"
    elif any(unit_exact_non_byok(unit) for unit in entry.units):
        category = "exact"
    else:
        category = "unverified"

    exact_costs: set[Decimal] = set()
    costs: list[tuple[Decimal, str]] = []
    for unit in entry.units:
        provider_usage = unit.get("provider_usage")
        declared_unknown = (
            str(unit.get("role") or "").strip().casefold() in MISSING_USAGE_PLACEHOLDER_ROLES
        ) or (isinstance(provider_usage, Mapping) and provider_usage.get("usage_unknown") is True)
        reported = (
            provider_usage.get("provider_reported_cost")
            if isinstance(provider_usage, Mapping)
            else None
        )
        cost_source = str(unit.get("cost_source") or "none").strip().casefold()
        no_recorded_cost_evidence = (
            cost_source in {"none", "unavailable"}
            and reported is None
            and unit.get("estimated_cost_usd") is None
        )
        if unit_cost_is_exact(unit):
            exact_value = required_decimal(
                provider_usage.get("provider_reported_cost"),
                label="exact provider-reported cost",
            ).quantize(Decimal("0.000000001"))
            exact_costs.add(exact_value)
        if declared_unknown or no_recorded_cost_evidence:
            # A placeholder or cost_source=none/unavailable row records no
            # observed spend. Treating its numeric default as a recorded $0
            # would silently erase unknown cost from reconciliation.
            continue
        cost_candidates: list[tuple[Any, str]] = [(reported, "provider_reported")]
        estimated_source = "estimate" in cost_source or cost_source.startswith("opensquilla_")
        if estimated_source:
            # Missing provider dollars retain the normalized billed_cost=0
            # placeholder.  Once token pricing marks the row estimated, that
            # placeholder must not shadow the cache-aware estimate.
            cost_candidates.extend(
                (
                    (unit.get("estimated_cost_usd"), "estimated"),
                    (unit.get("cost_usd"), "estimated"),
                )
            )
        else:
            if cost_source not in {"none", "unavailable"}:
                cost_candidates.append((unit.get("billed_cost"), cost_source or "recorded"))
            cost_candidates.append((unit.get("estimated_cost_usd"), "estimated"))
        for value, source in cost_candidates:
            if value is None or isinstance(value, bool):
                continue
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if parsed.is_finite() and parsed >= 0:
                costs.append((parsed, source))
                break
    recorded_cost: str | None = None
    cost_precision = "unknown"
    if len(exact_costs) > 1:
        conflicts.add("exact_cost")
        category = "conflict"
    if exact_costs:
        recorded_cost = str(next(iter(exact_costs)))
        cost_precision = "exact"
    elif costs:
        provider_costs = [
            value for value, source in costs if source.casefold() == "provider_reported"
        ]
        chosen = provider_costs[0] if provider_costs else costs[0][0]
        recorded_cost = str(chosen)
        sources = {source.casefold() for _, source in costs}
        cost_precision = (
            "estimated"
            if any("estimate" in source or source.startswith("opensquilla_") for source in sources)
            else "recorded"
        )
    input_tokens = max(
        (nonnegative_int(unit.get("input_tokens")) for unit in entry.units),
        default=0,
    )
    output_tokens = max(
        (nonnegative_int(unit.get("output_tokens")) for unit in entry.units),
        default=0,
    )
    generation_references = [
        reference for reference in entry.references if reference.get("phase") == "generation"
    ]
    generation_disposition: str | None = None
    if any(reference.get("selected_generation") is True for reference in generation_references):
        generation_disposition = "selected"
    elif any(reference.get("attempt_outcome") == "failed" for reference in generation_references):
        generation_disposition = "failed"
    elif generation_references:
        generation_disposition = "replaced"
    group_task_pairs = sorted(
        {
            (str(reference.get("group") or ""), str(reference.get("task_id") or ""))
            for reference in entry.references
            if reference.get("group") and reference.get("task_id")
        }
    )
    physical_sources = {
        (
            nonnegative_int(reference.get("physical_source_index")),
            str(reference.get("physical_source_path") or ""),
            nonnegative_int(reference.get("physical_source_line")),
        )
        for reference in entry.references
        if reference.get("physical_source_index") is not None
    }
    if len(physical_sources) != 1:
        raise FinalizationError(
            f"ledger entry is not bound to one physical source shard: {entry.identity}"
        )
    physical_source_index, physical_source_path, physical_source_line = next(iter(physical_sources))
    return {
        "schema": LEDGER_SCHEMA,
        "ledger_id": f"sha256:{canonical_sha256(entry.identity)}",
        "scopes": sorted(entry.scopes),
        "provider": sorted(providers)[0] if len(providers) == 1 else None,
        "model": sorted(models)[0] if len(models) == 1 else None,
        "upstream_providers": sorted(upstream_providers),
        "upstream_models": sorted(upstream_models),
        "roles": sorted(roles),
        "cost_sources": sorted(cost_sources),
        "response_id_sha256": [
            f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
            for value in sorted(entry.response_ids)
        ],
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "recorded_cost_usd": recorded_cost,
        "cost_precision": cost_precision,
        "generation_disposition": generation_disposition,
        "group_task_pairs": [
            {"group": group, "task_id": task_id} for group, task_id in group_task_pairs
        ],
        "non_byok_evidence": category,
        "receipt_conflict_fields": sorted(conflicts),
        "physical_source": {
            "source_index": physical_source_index,
            "path": physical_source_path,
            "line": physical_source_line,
        },
        "receipt_source_indexes": sorted(
            {
                nonnegative_int(reference.get("receipt_source_index"))
                for reference in entry.references
                if reference.get("receipt_source_index") is not None
            }
        ),
        "source_references": sorted(
            {canonical_sha256(reference): reference for reference in entry.references}.values(),
            key=lambda reference: (
                nonnegative_int(reference.get("source_index")),
                nonnegative_int(reference.get("line")),
                str(reference.get("phase") or ""),
            ),
        ),
    }


def validate_runtime_environment(path: Path) -> tuple[dict[str, Any], str]:
    payload = load_json(path)
    if payload.get("schema") != RUNTIME_SCHEMA:
        raise FinalizationError("runtime environment schema differs")
    environment = payload.get("environment")
    fingerprint = str(payload.get("environment_sha256") or "")
    if (
        not isinstance(environment, dict)
        or not HEX64.fullmatch(fingerprint)
        or canonical_sha256(environment) != fingerprint
    ):
        raise FinalizationError("runtime environment fingerprint differs")
    return payload, fingerprint


def validate_lock(
    *,
    lock_file: Path,
    lock_fd: int,
    reconciliation: Mapping[str, Any],
) -> dict[str, Any]:
    path = require_regular_file(lock_file, owner_only=True)
    try:
        descriptor_stat = os.fstat(lock_fd)
    except OSError as exc:
        raise FinalizationError(f"lock fd {lock_fd} is not open") from exc
    path_stat = path.stat()
    if descriptor_stat.st_dev != path_stat.st_dev or descriptor_stat.st_ino != path_stat.st_ino:
        raise FinalizationError("lock fd does not reference --lock-file")
    # A separate open file description cannot acquire a shared lock while the
    # inherited fd owns an exclusive flock.  Merely acquiring the lock here
    # would not prove the before->run->after window was exclusive.
    probe = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(probe, fcntl.LOCK_UN)
            raise FinalizationError("lock file was not already held exclusively by the caller")
    finally:
        os.close(probe)
    expected_path = reconciliation.get("lock_file")
    if expected_path is None or Path(str(expected_path)).resolve() != path:
        raise FinalizationError("reconciliation lock_file does not match")
    expected_inode = reconciliation.get("lock_inode")
    if str(expected_inode) != str(path_stat.st_ino):
        raise FinalizationError("reconciliation lock_inode does not match")
    return {
        "lock_file": str(path),
        "lock_fd": lock_fd,
        "lock_inode": path_stat.st_ino,
        "lock_device": path_stat.st_dev,
        "exclusive_lock_held": True,
        "exclusive_lock_scope": "local_host_filesystem_only",
        "cross_host_exclusivity_proven": False,
    }


def validate_stable_observations(
    reconciliation: Mapping[str, Any],
    *,
    before_usage: Decimal,
    before_byok: Decimal,
    after_usage: Decimal,
    after_byok: Decimal,
) -> dict[str, Any]:
    def exact_int(field: str, expected: int | None = None) -> int:
        value = reconciliation.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise FinalizationError(f"reconciliation {field} must be an integer")
        if expected is not None and value != expected:
            raise FinalizationError(
                f"reconciliation {field} must equal the formal value {expected}"
            )
        return value

    required_stable_count = exact_int(
        "required_stable_poll_count",
        FORMAL_REQUIRED_STABLE_POLL_COUNT,
    )
    poll_interval_seconds = exact_int(
        "poll_interval_seconds",
        FORMAL_POLL_INTERVAL_SECONDS,
    )
    minimum_settlement_seconds = exact_int(
        "minimum_settlement_seconds",
        FORMAL_MINIMUM_SETTLEMENT_SECONDS,
    )
    minimum_stable_tail_seconds = exact_int(
        "minimum_stable_tail_seconds",
        FORMAL_MINIMUM_STABLE_TAIL_SECONDS,
    )
    observations = reconciliation.get("stable_observations")
    if not isinstance(observations, list) or len(observations) < required_stable_count:
        raise FinalizationError("reconciliation lacks the formal account observation count")
    poll_count = exact_int("poll_observation_count")
    if poll_count != len(observations):
        raise FinalizationError("reconciliation poll_observation_count differs from observations")
    stable_count = exact_int("stable_poll_count")
    if stable_count < required_stable_count or stable_count > len(observations):
        raise FinalizationError("reconciliation stable_poll_count is invalid")
    declared_tail_start = exact_int("stable_tail_start_index")
    normalized: list[tuple[Decimal, Decimal, datetime]] = []
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping):
            raise FinalizationError("stable account observation is not an object")
        normalized.append(
            (
                required_decimal(
                    observation.get("usage"),
                    label=f"stable observation {index} usage",
                ),
                required_decimal(
                    observation.get("byok_usage"),
                    label=f"stable observation {index} byok_usage",
                ),
                parse_iso(
                    observation.get("captured_at"),
                    label=f"stable observation {index} captured_at",
                ),
            )
        )
    if normalized[0][0] < before_usage:
        raise FinalizationError("account observation usage predates the before counter")
    if any(
        later[0] < earlier[0] for earlier, later in zip(normalized, normalized[1:], strict=False)
    ):
        raise FinalizationError("stable observation usage is not monotonic")
    # BYOK is a monotonic account counter, just like total usage.  A non-zero
    # delta is retained below and makes the campaign policy proof fail; it is
    # not an execution-integrity error and therefore must not prevent reading
    # the stable tail.
    if normalized[0][1] < before_byok or any(
        later[1] < earlier[1] for earlier, later in zip(normalized, normalized[1:], strict=False)
    ):
        raise FinalizationError("stable observation BYOK usage is not monotonic")
    if normalized[-1][0] != after_usage or normalized[-1][1] != after_byok:
        raise FinalizationError("stable observation tail does not match account after")
    time_gaps = [
        (later[2] - earlier[2]).total_seconds()
        for earlier, later in zip(normalized, normalized[1:], strict=False)
    ]
    if any(gap < poll_interval_seconds for gap in time_gaps):
        raise FinalizationError("stable observation timestamps violate the formal poll interval")
    recomputed_stable_count = 0
    final_counters = normalized[-1][:2]
    for usage, byok, _ in reversed(normalized):
        if (usage, byok) != final_counters:
            break
        recomputed_stable_count += 1
    stable_tail_start = len(normalized) - recomputed_stable_count
    if stable_count != recomputed_stable_count or declared_tail_start != stable_tail_start:
        raise FinalizationError("reconciliation stable tail declaration differs from observations")
    observation_span = (normalized[-1][2] - normalized[0][2]).total_seconds()
    stable_tail_span = (normalized[-1][2] - normalized[stable_tail_start][2]).total_seconds()
    declared_observation_span = required_decimal(
        reconciliation.get("observation_span_seconds"),
        label="reconciliation observation_span_seconds",
    )
    declared_stable_tail_span = required_decimal(
        reconciliation.get("stable_tail_span_seconds"),
        label="reconciliation stable_tail_span_seconds",
    )
    if declared_observation_span != Decimal(str(observation_span)):
        raise FinalizationError(
            "reconciliation observation_span_seconds was not recomputed correctly"
        )
    if declared_stable_tail_span != Decimal(str(stable_tail_span)):
        raise FinalizationError(
            "reconciliation stable_tail_span_seconds was not recomputed correctly"
        )
    if observation_span < minimum_settlement_seconds:
        raise FinalizationError("formal account settlement window is too short")
    if stable_tail_span < minimum_stable_tail_seconds:
        raise FinalizationError("formal stable account tail is too short")
    return {
        "poll_observation_count": poll_count,
        "stable_poll_count": stable_count,
        "required_stable_poll_count": required_stable_count,
        "stable_tail_start_index": stable_tail_start,
        "poll_interval_seconds": poll_interval_seconds,
        "minimum_settlement_seconds": minimum_settlement_seconds,
        "minimum_stable_tail_seconds": minimum_stable_tail_seconds,
        "observation_span_seconds": str(declared_observation_span),
        "stable_tail_span_seconds": str(declared_stable_tail_span),
        "stable_usage_usd": str(after_usage),
        "stable_byok_usage_usd": str(after_byok),
        "first_observation_at": normalized[0][2].isoformat(),
        "last_stable_observation_at": normalized[-1][2].isoformat(),
    }


def selected_time_window(
    records: Sequence[SourceRecord],
) -> tuple[datetime, datetime]:
    starts: list[datetime] = []
    completions: list[datetime] = []
    for record in records:
        started = record.row.get("started_at")
        completed = record.row.get("completed_at")
        if finite_number(started):
            starts.append(datetime.fromtimestamp(float(started), tz=UTC))
        if finite_number(completed):
            completions.append(datetime.fromtimestamp(float(completed), tz=UTC))
    if not starts or not completions:
        raise FinalizationError("source rows lack numeric start/completion timestamps")
    return min(starts), max(completions)


def manifest_source_window_coverage(
    manifest_sources: Sequence[Mapping[str, Any]],
    *,
    source_records: Sequence[SourceRecord],
    campaign_windows: Sequence[Mapping[str, Any]],
) -> tuple[datetime, datetime, list[dict[str, Any]]]:
    """Bind each live shard to exactly one settled campaign account window."""

    if not manifest_sources or not campaign_windows:
        raise FinalizationError("campaign source/account windows are incomplete")
    source_indexes = {record.source_index for record in source_records}
    if source_indexes != set(range(len(manifest_sources))):
        raise FinalizationError("source rows are not bound to every source manifest")

    starts: list[datetime] = []
    completions: list[datetime] = []
    coverage: list[dict[str, Any]] = []
    seen_generation_attempt_ids: set[str] = set()
    for source_index, manifest in enumerate(manifest_sources):
        raw_started = manifest.get("started_at")
        raw_finished = manifest.get("finished_at")
        started = (
            datetime.fromtimestamp(float(raw_started), tz=UTC)
            if finite_number(raw_started)
            else parse_iso(raw_started, label="source manifest started_at")
        )
        finished = (
            datetime.fromtimestamp(float(raw_finished), tz=UTC)
            if finite_number(raw_finished)
            else parse_iso(raw_finished, label="source manifest finished_at")
        )
        if started >= finished:
            raise FinalizationError("source manifest has a non-positive execution window")
        shard_records = sorted(
            (record for record in source_records if record.source_index == source_index),
            key=lambda record: record.line,
        )
        shard_completion_times: list[datetime] = []
        new_generation_attempt_count = 0
        for record in shard_records:
            raw_completed = record.row.get("completed_at")
            if not finite_number(raw_completed):
                raise FinalizationError("source result row lacks a numeric completion timestamp")
            row_completed = datetime.fromtimestamp(float(raw_completed), tz=UTC)
            if not started <= row_completed <= finished:
                raise FinalizationError(
                    "source result row completion is outside its manifest execution window"
                )
            shard_completion_times.append(row_completed)
            execution = record.row.get("execution")
            attempts = (
                execution.get("generation_attempts")
                if isinstance(execution, Mapping)
                and isinstance(execution.get("generation_attempts"), list)
                else []
            )
            for attempt in attempts:
                if not isinstance(attempt, Mapping):
                    continue
                attempt_id = str(attempt.get("attempt_id") or "")
                if attempt_id in seen_generation_attempt_ids:
                    continue
                raw_attempt_started = attempt.get("started_at")
                raw_attempt_completed = attempt.get("completed_at")
                if (
                    HEX32.fullmatch(attempt_id) is None
                    or not finite_number(raw_attempt_started)
                    or not finite_number(raw_attempt_completed)
                ):
                    raise FinalizationError(
                        "physical-first generation attempt lacks immutable timing evidence"
                    )
                attempt_started = datetime.fromtimestamp(float(raw_attempt_started), tz=UTC)
                attempt_completed = datetime.fromtimestamp(float(raw_attempt_completed), tz=UTC)
                if (
                    attempt_started > attempt_completed
                    or attempt_started < started
                    or attempt_completed > finished
                ):
                    raise FinalizationError(
                        "physical-first generation attempt is outside its source manifest"
                    )
                seen_generation_attempt_ids.add(attempt_id)
                new_generation_attempt_count += 1
        matches = [
            window
            for window in campaign_windows
            if parse_iso(
                window.get("account_before_at"),
                label="campaign account before",
            )
            <= started
            and parse_iso(
                window.get("account_after_at"),
                label="campaign account after",
            )
            >= finished
        ]
        if len(matches) != 1:
            raise FinalizationError(
                "source manifest is not covered by exactly one campaign account window"
            )
        starts.append(started)
        completions.append(finished)
        coverage.append(
            {
                "source_index": source_index,
                "manifest_path": manifest.get("path"),
                "result_path": manifest.get("result_path"),
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "source_row_count": len(shard_records),
                "source_row_completed_at_min": min(shard_completion_times).isoformat(),
                "source_row_completed_at_max": max(shard_completion_times).isoformat(),
                "physical_first_generation_attempt_count": (new_generation_attempt_count),
                "account_window_path": matches[0].get("path"),
                "account_window_kind": matches[0].get("kind"),
            }
        )
    return min(starts), max(completions), coverage


def reconcile_ledger_campaign_windows(
    ledger_rows: Sequence[Mapping[str, Any]],
    *,
    source_window_coverage: Sequence[Mapping[str, Any]],
    campaign_windows: Sequence[Mapping[str, Any]],
    tolerance: Decimal,
) -> list[dict[str, Any]]:
    """Reconcile physical-first request origins inside each account window."""

    source_to_window: dict[int, str] = {}
    for coverage in source_window_coverage:
        source_index = nonnegative_int(coverage.get("source_index"))
        window_path = str(coverage.get("account_window_path") or "")
        if source_index in source_to_window or not window_path:
            raise FinalizationError("source manifest account-window binding is ambiguous")
        source_to_window[source_index] = window_path

    rows_by_window: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in ledger_rows:
        physical_source = row.get("physical_source")
        if not isinstance(physical_source, Mapping):
            raise FinalizationError("ledger row lacks physical-first source evidence")
        source_index = nonnegative_int(physical_source.get("source_index"))
        window_path = source_to_window.get(source_index)
        if not window_path:
            raise FinalizationError("ledger physical source is outside campaign manifests")
        rows_by_window[window_path].append(row)

    reconciliations: list[dict[str, Any]] = []
    campaign_paths: set[str] = set()
    for window in campaign_windows:
        window_path = str(window.get("path") or "")
        if not window_path or window_path in campaign_paths:
            raise FinalizationError("campaign account window path is missing or duplicated")
        campaign_paths.add(window_path)
        source_indexes = sorted(
            source_index
            for source_index, bound_path in source_to_window.items()
            if bound_path == window_path
        )
        if not source_indexes:
            raise FinalizationError("campaign account window is not bound to a source manifest")
        rows = rows_by_window.pop(window_path, [])
        delta = required_decimal(
            window.get("usage_delta_usd"),
            label="campaign window usage delta",
        )
        recorded = Decimal(0)
        exact = Decimal(0)
        unknown = 0
        non_exact = 0
        for row in rows:
            raw_cost = row.get("recorded_cost_usd")
            if raw_cost is None:
                unknown += 1
                continue
            cost = required_decimal(raw_cost, label="campaign window ledger cost")
            recorded += cost
            if row.get("cost_precision") == "exact":
                exact += cost
            else:
                non_exact += 1
        gap = delta - recorded
        status = (
            "conflict"
            if exact > delta + tolerance or abs(gap) > tolerance and unknown == 0 and non_exact == 0
            else "exact"
            if abs(gap) <= tolerance and unknown == 0 and non_exact == 0
            else "account_exact_per_request_incomplete"
        )
        reconciliations.append(
            {
                "account_window_path": window_path,
                "account_window_kind": window.get("kind"),
                "source_indexes": source_indexes,
                "physical_request_count": len(rows),
                "ledger_recorded_cost_usd": str(recorded),
                "ledger_exact_cost_usd": str(exact),
                "unknown_cost_request_count": unknown,
                "non_exact_cost_request_count": non_exact,
                "account_usage_delta_usd": str(delta),
                "reconciliation_gap_usd": str(gap),
                "reconciliation_status": status,
                **(
                    {
                        "warnings": [
                            "campaign window exact receipts exceed its account usage delta"
                            if exact > delta + tolerance
                            else (
                                "campaign window ledger does not reconcile to its "
                                "account usage delta"
                            )
                        ]
                    }
                    if status == "conflict"
                    else {}
                ),
            }
        )
    if rows_by_window:
        raise FinalizationError("ledger rows were assigned to a non-campaign account window")
    return reconciliations


def validate_prior_account_window(
    *,
    window_dir: Path,
    expected_key_fingerprint: str,
    current_before_time: datetime,
    current_before_usage: Decimal,
    current_before_byok: Decimal,
    kind: str = "prior_aborted",
    admission_basis: str = "operator_supplied_unallocated_window",
    expected_runtime_fingerprint: str = "",
    expected_lock_file: str = "",
    expected_lock_inode: int = 0,
) -> dict[str, Any]:
    if kind not in {"prior_aborted", "prior_campaign"} or not admission_basis:
        raise FinalizationError("prior account window kind is invalid")
    raw_window_dir = Path(window_dir)
    if raw_window_dir.is_symlink() or not raw_window_dir.is_dir():
        raise FinalizationError("prior account window must be a non-symlink directory")
    if raw_window_dir.stat().st_uid != os.getuid():
        raise FinalizationError("prior account window must be campaign-owned")
    window_dir = raw_window_dir.resolve(strict=True)
    before_path = window_dir / "openrouter-account-before.json"
    after_path = window_dir / "openrouter-account-after.json"
    reconciliation_path = window_dir / "openrouter-account-reconciliation.json"
    runtime_path = window_dir / "runtime-environment.json"
    before_path = require_regular_file(before_path, owner_only=True)
    after_path = require_regular_file(after_path, owner_only=True)
    reconciliation_path = require_regular_file(reconciliation_path, owner_only=True)
    runtime_path = require_regular_file(runtime_path, owner_only=True)
    before = load_json(before_path)
    after = load_json(after_path)
    reconciliation = load_json(reconciliation_path)
    _, runtime_fingerprint = validate_runtime_environment(runtime_path)
    if reconciliation.get("schema") != RECONCILIATION_SCHEMA:
        raise FinalizationError("prior account reconciliation schema differs")
    if reconciliation.get("settlement_status") != "stable":
        raise FinalizationError("prior account reconciliation is not stable")
    fingerprints = {
        normalize_key_fingerprint(before.get("api_key_sha256"), label="prior before key"),
        normalize_key_fingerprint(after.get("api_key_sha256"), label="prior after key"),
        normalize_key_fingerprint(
            reconciliation.get("api_key_sha256"),
            label="prior reconciliation key",
        ),
        expected_key_fingerprint,
    }
    if len(fingerprints) != 1:
        raise FinalizationError("prior and current account windows use different API keys")
    if (
        before.get("benchmark_environment_key_verified") is not True
        or after.get("benchmark_environment_key_verified") is not True
        or before.get("is_free_tier") is not False
        or after.get("is_free_tier") is not False
        or reconciliation.get("is_free_tier") is not False
    ):
        raise FinalizationError("prior account window is not bound to the verified paid key")

    before_usage = required_decimal(before.get("usage"), label="prior before usage")
    after_usage = required_decimal(after.get("usage"), label="prior after usage")
    before_byok = required_decimal(before.get("byok_usage"), label="prior before byok")
    after_byok = required_decimal(after.get("byok_usage"), label="prior after byok")
    if after_usage < before_usage or after_byok < before_byok:
        raise FinalizationError("prior account counters decreased")
    if after_usage > current_before_usage or after_byok > current_before_byok:
        raise FinalizationError("account counters decreased between prior and current windows")
    usage_delta = after_usage - before_usage
    byok_delta = after_byok - before_byok
    expected_values = {
        "usage_before_usd": before_usage,
        "usage_after_usd": after_usage,
        "usage_delta_usd": usage_delta,
        "byok_usage_before_usd": before_byok,
        "byok_usage_after_usd": after_byok,
        "byok_usage_delta_usd": byok_delta,
    }
    for field_name, expected in expected_values.items():
        actual = required_decimal(
            reconciliation.get(field_name),
            label=f"prior reconciliation {field_name}",
        )
        if actual != expected:
            raise FinalizationError(
                f"prior reconciliation {field_name} differs from account snapshots"
            )
    if reconciliation.get("runtime_environment_sha256") != runtime_fingerprint:
        raise FinalizationError("prior reconciliation runtime fingerprint differs")
    if reconciliation.get("runtime_environment_file_sha256") != file_sha256(runtime_path):
        raise FinalizationError("prior reconciliation runtime file hash differs")
    if kind == "prior_campaign" and (
        runtime_fingerprint != expected_runtime_fingerprint
        or not expected_lock_file
        or Path(str(reconciliation.get("lock_file") or "")).resolve(strict=False)
        != Path(expected_lock_file).resolve(strict=False)
        or nonnegative_int(reconciliation.get("lock_inode")) != expected_lock_inode
        or expected_lock_inode <= 0
    ):
        raise FinalizationError(
            "prior campaign account window is not bound to the same runtime/lock"
        )
    stability = validate_stable_observations(
        reconciliation,
        before_usage=before_usage,
        before_byok=before_byok,
        after_usage=after_usage,
        after_byok=after_byok,
    )
    before_time = parse_iso(before.get("captured_at"), label="prior account before captured_at")
    after_time = parse_iso(after.get("captured_at"), label="prior account after captured_at")
    last_stable = parse_iso(
        stability["last_stable_observation_at"],
        label="prior last stable observation",
    )
    first_observation = parse_iso(
        stability["first_observation_at"],
        label="prior first stable observation",
    )
    if before_time > first_observation:
        raise FinalizationError(
            "prior account before snapshot does not precede settlement observations"
        )
    if after_time != last_stable:
        raise FinalizationError("prior account after differs from its stable reconciliation")
    if before_time >= after_time:
        raise FinalizationError("prior account window has a non-positive time span")
    if after_time > current_before_time:
        raise FinalizationError("prior and current account windows overlap")
    source_sha256 = {
        "account_before": file_sha256(before_path),
        "account_after": file_sha256(after_path),
        "account_reconciliation": file_sha256(reconciliation_path),
        "runtime_environment": file_sha256(runtime_path),
    }
    return {
        "kind": kind,
        "admission_basis": admission_basis,
        "path": str(window_dir),
        "usage_before_usd": str(before_usage),
        "usage_after_usd": str(after_usage),
        "usage_delta_usd": str(usage_delta),
        "byok_usage_before_usd": str(before_byok),
        "byok_usage_after_usd": str(after_byok),
        "byok_usage_delta_usd": str(byok_delta),
        "account_before_at": before_time.isoformat(),
        "account_after_at": after_time.isoformat(),
        "runtime_environment_sha256": runtime_fingerprint,
        "source_sha256": source_sha256,
        "sources": [
            {"path": str(before_path), "sha256": source_sha256["account_before"]},
            {"path": str(after_path), "sha256": source_sha256["account_after"]},
            {
                "path": str(reconciliation_path),
                "sha256": source_sha256["account_reconciliation"],
            },
            {"path": str(runtime_path), "sha256": source_sha256["runtime_environment"]},
        ],
        **stability,
    }


def validate_account_proof(
    *,
    before_path: Path,
    after_path: Path,
    reconciliation_path: Path,
    runtime_environment_path: Path,
    lock_file: Path,
    lock_fd: int,
    runtime_key_fingerprint: str,
    source_records: Sequence[SourceRecord],
    manifest_sources: Sequence[Mapping[str, Any]],
    ledger_rows: Sequence[Mapping[str, Any]],
    ledger_summary: Mapping[str, Any],
    prior_account_window_dirs: Sequence[Path] = (),
    prior_campaign_account_window_dirs: Sequence[Path] = (),
) -> dict[str, Any]:
    before_path = require_regular_file(before_path, owner_only=True)
    after_path = require_regular_file(after_path, owner_only=True)
    reconciliation_path = require_regular_file(reconciliation_path, owner_only=True)
    before = load_json(before_path)
    after = load_json(after_path)
    reconciliation = load_json(reconciliation_path)
    _, runtime_fingerprint = validate_runtime_environment(runtime_environment_path)
    policy_warnings: list[str] = []
    reconciliation_warnings: list[str] = []
    if reconciliation.get("schema") != RECONCILIATION_SCHEMA:
        raise FinalizationError("account reconciliation schema differs")
    if reconciliation.get("settlement_status") != "stable":
        raise FinalizationError("account reconciliation is not stable")

    fingerprints = {
        normalize_key_fingerprint(before.get("api_key_sha256"), label="account before key"),
        normalize_key_fingerprint(after.get("api_key_sha256"), label="account after key"),
        normalize_key_fingerprint(
            reconciliation.get("api_key_sha256"),
            label="account reconciliation key",
        ),
        runtime_key_fingerprint,
    }
    if len(fingerprints) != 1:
        raise FinalizationError("runtime/account evidence uses different API keys")
    if (
        before.get("benchmark_environment_key_verified") is not True
        or after.get("benchmark_environment_key_verified") is not True
    ):
        raise FinalizationError("account snapshots do not bind the benchmark key")
    if (
        before.get("is_free_tier") is not False
        or after.get("is_free_tier") is not False
        or reconciliation.get("is_free_tier") is not False
    ):
        raise FinalizationError("formal account proof requires a paid key")

    before_usage = required_decimal(before.get("usage"), label="account before usage")
    after_usage = required_decimal(after.get("usage"), label="account after usage")
    before_byok = required_decimal(before.get("byok_usage"), label="account before byok_usage")
    after_byok = required_decimal(after.get("byok_usage"), label="account after byok_usage")
    if after_usage < before_usage or after_byok < before_byok:
        raise FinalizationError("account counters decreased")
    usage_delta = after_usage - before_usage
    byok_delta = after_byok - before_byok
    expected_values = {
        "usage_before_usd": before_usage,
        "usage_after_usd": after_usage,
        "usage_delta_usd": usage_delta,
        "byok_usage_before_usd": before_byok,
        "byok_usage_after_usd": after_byok,
        "byok_usage_delta_usd": byok_delta,
    }
    for field_name, expected in expected_values.items():
        actual = required_decimal(
            reconciliation.get(field_name), label=f"reconciliation {field_name}"
        )
        if actual != expected:
            raise FinalizationError(f"reconciliation {field_name} differs from account snapshots")
    if byok_delta != Decimal(0):
        policy_warnings.append(f"campaign account BYOK delta is not exactly zero: {byok_delta}")

    reconciliation_runtime = str(reconciliation.get("runtime_environment_sha256") or "")
    if reconciliation_runtime != runtime_fingerprint:
        raise FinalizationError("reconciliation runtime fingerprint differs")
    if reconciliation.get("runtime_environment_file_sha256") != file_sha256(
        require_regular_file(runtime_environment_path, owner_only=True)
    ):
        raise FinalizationError("reconciliation runtime file hash differs")
    lock = validate_lock(
        lock_file=lock_file,
        lock_fd=lock_fd,
        reconciliation=reconciliation,
    )
    stability = validate_stable_observations(
        reconciliation,
        before_usage=before_usage,
        before_byok=before_byok,
        after_usage=after_usage,
        after_byok=after_byok,
    )
    before_time = parse_iso(before.get("captured_at"), label="account before captured_at")
    after_time = parse_iso(after.get("captured_at"), label="account after captured_at")
    last_stable = parse_iso(
        stability["last_stable_observation_at"],
        label="last stable observation",
    )
    first_observation = parse_iso(
        stability["first_observation_at"],
        label="first stable observation",
    )
    if before_time > first_observation:
        raise FinalizationError("account before snapshot does not precede settlement observations")
    if after_time != last_stable:
        raise FinalizationError("account after timestamp differs from the final stable observation")

    prior_campaign_windows = [
        validate_prior_account_window(
            window_dir=Path(window_dir),
            expected_key_fingerprint=next(iter(fingerprints)),
            current_before_time=before_time,
            current_before_usage=before_usage,
            current_before_byok=before_byok,
            kind="prior_campaign",
            admission_basis="operator_supplied_source_bound_campaign_window",
            expected_runtime_fingerprint=runtime_fingerprint,
            expected_lock_file=str(lock.get("lock_file") or ""),
            expected_lock_inode=nonnegative_int(lock.get("lock_inode")),
        )
        for window_dir in prior_campaign_account_window_dirs
    ]
    campaign_usage_delta = usage_delta + sum(
        (
            required_decimal(window["usage_delta_usd"], label="campaign account usage delta")
            for window in prior_campaign_windows
        ),
        Decimal(0),
    )
    campaign_byok_delta = byok_delta + sum(
        (
            required_decimal(window["byok_usage_delta_usd"], label="campaign BYOK delta")
            for window in prior_campaign_windows
        ),
        Decimal(0),
    )
    if campaign_byok_delta != Decimal(0) and byok_delta == Decimal(0):
        policy_warnings.append(
            f"campaign account windows contain a non-zero BYOK delta: {campaign_byok_delta}"
        )

    local_counts = Counter(str(row.get("non_byok_evidence") or "") for row in ledger_rows)
    explicit = local_counts["explicit_byok"]
    conflicts = local_counts["conflict"]
    if explicit or conflicts:
        policy_warnings.append(
            "explicit BYOK or contradictory provider evidence: "
            f"explicit={explicit}, conflict={conflicts}"
        )
    request_count = len(ledger_rows)
    exact = local_counts["exact"]
    unverified = local_counts["unverified"]
    if request_count <= 0 or exact + unverified + explicit + conflicts != request_count:
        reconciliation_warnings.append("campaign request evidence accounting is inconsistent")

    recorded_ledger_cost = required_decimal(
        ledger_summary.get("recorded_cost_usd"), label="ledger recorded cost"
    )
    exact_ledger_cost = required_decimal(
        ledger_summary.get("exact_cost_usd"), label="ledger exact cost"
    )
    tolerance = required_decimal(
        reconciliation.get("cost_reconciliation_tolerance_usd", "0.000001"),
        label="cost reconciliation tolerance",
    )
    if tolerance > Decimal("0.000001"):
        reconciliation_warnings.append("cost reconciliation tolerance exceeds 0.000001 USD")
        tolerance = Decimal("0.000001")
    if exact_ledger_cost > campaign_usage_delta + tolerance:
        reconciliation_warnings.append(
            "exact physical receipt cost exceeds the settled account usage delta"
        )
    gap = campaign_usage_delta - recorded_ledger_cost
    unknown_cost_count = nonnegative_int(ledger_summary.get("unknown_cost_request_count"))
    non_exact_cost_count = nonnegative_int(ledger_summary.get("non_exact_cost_request_count"))
    if gap < -tolerance and unknown_cost_count == 0 and non_exact_cost_count == 0:
        reconciliation_warnings.append(
            "physical receipt ledger exceeds the settled account usage delta"
        )
    if abs(gap) > tolerance and unknown_cost_count == 0 and non_exact_cost_count == 0:
        reconciliation_warnings.append(f"unexplained OpenRouter account/ledger cost delta: {gap}")
    cost_reconciliation_status = (
        "conflict"
        if reconciliation_warnings
        else "exact"
        if abs(gap) <= tolerance and unknown_cost_count == 0 and non_exact_cost_count == 0
        else "account_exact_per_request_incomplete"
    )
    if unknown_cost_count or non_exact_cost_count:
        reconciliation_warnings.append(
            "per-request generation/Judge cost evidence is estimated, recorded-only, or unknown"
        )
    campaign_attributable_exact = (
        cost_reconciliation_status == "exact" and exact == request_count and unverified == 0
    )
    attribution_precision = (
        "campaign-attributable-exact"
        if campaign_attributable_exact
        else "account_window_only_external-use-not-provable"
    )
    prior_aborted_windows = [
        validate_prior_account_window(
            window_dir=Path(window_dir),
            expected_key_fingerprint=next(iter(fingerprints)),
            current_before_time=before_time,
            current_before_usage=before_usage,
            current_before_byok=before_byok,
            kind="prior_aborted",
            admission_basis="operator_supplied_unallocated_window",
        )
        for window_dir in prior_account_window_dirs
    ]
    for window in prior_aborted_windows:
        prior_byok_delta = required_decimal(
            window["byok_usage_delta_usd"],
            label="prior aborted account BYOK delta",
        )
        if prior_byok_delta != Decimal(0):
            policy_warnings.append(
                f"prior aborted account window contains a non-zero BYOK delta: {prior_byok_delta}"
            )
    prior_windows = [*prior_aborted_windows, *prior_campaign_windows]
    ordered_windows = sorted(
        prior_windows,
        key=lambda window: parse_iso(
            window["account_before_at"],
            label="prior account window account_before_at",
        ),
    )
    for earlier, later in zip(ordered_windows, ordered_windows[1:], strict=False):
        earlier_after = parse_iso(
            earlier["account_after_at"], label="prior account window account_after_at"
        )
        later_before = parse_iso(
            later["account_before_at"], label="prior account window account_before_at"
        )
        if earlier_after > later_before:
            raise FinalizationError("prior account windows overlap")
        if required_decimal(
            earlier["usage_after_usd"], label="prior account usage after"
        ) > required_decimal(later["usage_before_usd"], label="prior account usage before"):
            raise FinalizationError("account usage decreased between prior windows")
        if required_decimal(
            earlier["byok_usage_after_usd"], label="prior account BYOK usage after"
        ) > required_decimal(
            later["byok_usage_before_usd"], label="prior account BYOK usage before"
        ):
            raise FinalizationError("account BYOK usage decreased between prior windows")
    current_window = {
        "kind": "current",
        "path": str(before_path.parent.resolve()),
        "usage_before_usd": str(before_usage),
        "usage_after_usd": str(after_usage),
        "usage_delta_usd": str(usage_delta),
        "byok_usage_before_usd": str(before_byok),
        "byok_usage_after_usd": str(after_byok),
        "byok_usage_delta_usd": str(byok_delta),
        "account_before_at": before_time.isoformat(),
        "account_after_at": after_time.isoformat(),
        "runtime_environment_sha256": runtime_fingerprint,
        "source_sha256": {
            "account_before": file_sha256(before_path),
            "account_after": file_sha256(after_path),
            "account_reconciliation": file_sha256(reconciliation_path),
            "runtime_environment": file_sha256(
                require_regular_file(runtime_environment_path, owner_only=True)
            ),
        },
        **stability,
    }
    campaign_windows = [
        *sorted(
            prior_campaign_windows,
            key=lambda window: parse_iso(
                window["account_before_at"],
                label="campaign account before",
            ),
        ),
        current_window,
    ]
    for earlier, later in zip(campaign_windows, campaign_windows[1:], strict=False):
        if required_decimal(
            earlier.get("usage_after_usd"),
            label="earlier campaign account usage after",
        ) != required_decimal(
            later.get("usage_before_usd"),
            label="later campaign account usage before",
        ) or required_decimal(
            earlier.get("byok_usage_after_usd"),
            label="earlier campaign account BYOK after",
        ) != required_decimal(
            later.get("byok_usage_before_usd"),
            label="later campaign account BYOK before",
        ):
            raise FinalizationError("campaign account counters are not continuous between windows")
    earliest_start, latest_completion, source_window_coverage = manifest_source_window_coverage(
        manifest_sources,
        source_records=source_records,
        campaign_windows=campaign_windows,
    )
    ledger_window_reconciliation = reconcile_ledger_campaign_windows(
        ledger_rows,
        source_window_coverage=source_window_coverage,
        campaign_windows=campaign_windows,
        tolerance=tolerance,
    )
    current_window["sources"] = [
        {"path": str(before_path), "sha256": current_window["source_sha256"]["account_before"]},
        {"path": str(after_path), "sha256": current_window["source_sha256"]["account_after"]},
        {
            "path": str(reconciliation_path),
            "sha256": current_window["source_sha256"]["account_reconciliation"],
        },
        {
            "path": str(Path(runtime_environment_path).resolve()),
            "sha256": current_window["source_sha256"]["runtime_environment"],
        },
    ]
    account_windows = [*ordered_windows, current_window]
    aborted_total = sum(
        (
            required_decimal(window["usage_delta_usd"], label="prior account usage delta")
            for window in prior_aborted_windows
        ),
        Decimal(0),
    )
    account_window_total = aborted_total + campaign_usage_delta
    current_window_campaign_attributable_exact = campaign_attributable_exact
    if aborted_total > 0:
        campaign_attributable_exact = False
        attribution_precision = "multi-window-counter-exact-campaign-attribution-unproven"
    source_hashes = {
        "account_before": file_sha256(before_path),
        "account_after": file_sha256(after_path),
        "account_reconciliation": file_sha256(reconciliation_path),
        "runtime_environment": file_sha256(
            require_regular_file(runtime_environment_path, owner_only=True)
        ),
    }
    policy_pass = not policy_warnings
    reconciliation_pass = cost_reconciliation_status == "exact"
    warnings = [*policy_warnings, *reconciliation_warnings]
    proof = {
        "schema": PROOF_SCHEMA,
        "pass": policy_pass,
        "publication_eligible": True,
        "execution_pass": True,
        "policy_pass": policy_pass,
        "reconciliation": {
            "pass": reconciliation_pass,
            "status": cost_reconciliation_status,
            "gap_usd": str(gap),
            "tolerance_usd": str(tolerance),
        },
        "status": (
            "passed"
            if policy_pass and reconciliation_pass
            else "policy_failed"
            if not policy_pass
            else "reconciliation_incomplete"
        ),
        "warnings": warnings,
        "created_at": utc_now(),
        "policy": (
            "explicit BYOK/provider conflicts make this policy proof fail without "
            "erasing an independently valid execution; locally unverified physical "
            "requests are covered only for non-BYOK policy by a paid, same-key "
            "account-window proof with an exact zero Decimal BYOK delta; the local "
            "flock cannot prove that another host did not use the key"
        ),
        "api_key_sha256": next(iter(fingerprints)),
        "runtime_environment_sha256": runtime_fingerprint,
        "account_windows": account_windows,
        "account_window_total_usd": str(account_window_total),
        "unallocated_aborted_window_usd": str(aborted_total),
        "result_row_account_window_scope": (
            "campaign_windows" if prior_campaign_windows else "current_window_only"
        ),
        "account": {
            "usage_before_usd": str(before_usage),
            "usage_after_usd": str(after_usage),
            "usage_delta_usd": str(usage_delta),
            "byok_usage_before_usd": str(before_byok),
            "byok_usage_after_usd": str(after_byok),
            "byok_usage_delta_usd": str(byok_delta),
            "campaign_byok_usage_delta_usd": str(campaign_byok_delta),
            "campaign_usage_delta_usd": str(campaign_usage_delta),
            "campaign_window_count": len(campaign_windows),
            "is_free_tier": False,
        },
        "window": {
            "account_before_at": before_time.isoformat(),
            "earliest_source_started_at": earliest_start.isoformat(),
            "latest_source_completed_at": latest_completion.isoformat(),
            "account_after_at": after_time.isoformat(),
            "source_window_coverage": source_window_coverage,
            "ledger_window_reconciliation": ledger_window_reconciliation,
            **stability,
            **lock,
        },
        "local_physical_request_evidence": {
            "request_count": request_count,
            "exact_non_byok_request_count": exact,
            "unverified_request_count": unverified,
            "explicit_byok_request_count": explicit,
            "conflict_request_count": conflicts,
            "campaign_covered_unverified_request_count": unverified,
            "resolved_request_count": exact + unverified,
        },
        "cost_scope": {
            "ledger_recorded_cost_usd": ledger_summary.get("recorded_cost_usd"),
            "ledger_exact_cost_usd": ledger_summary.get("exact_cost_usd"),
            "account_usage_delta_usd": str(campaign_usage_delta),
            "account_window_delta_usd": str(campaign_usage_delta),
            "current_account_window_delta_usd": str(usage_delta),
            "campaign_bound_account_window_total_usd": str(campaign_usage_delta),
            "unallocated_account_window_total_usd": str(aborted_total),
            "all_account_window_total_usd": str(account_window_total),
            "ledger_window_reconciliation": ledger_window_reconciliation,
            "account_windows": account_windows,
            "account_window_total_usd": str(account_window_total),
            "unallocated_aborted_window_usd": str(aborted_total),
            "reconciliation_gap_usd": str(gap),
            "reconciliation_tolerance_usd": str(tolerance),
            "reconciliation_status": cost_reconciliation_status,
            "unknown_cost_request_count": unknown_cost_count,
            "non_exact_cost_request_count": non_exact_cost_count,
            "attribution_precision": attribution_precision,
            "campaign_attributable_exact": campaign_attributable_exact,
            "current_window_campaign_attributable_exact": (
                current_window_campaign_attributable_exact
            ),
            "campaign_attributable_cost_usd": (
                str(campaign_usage_delta) if campaign_attributable_exact else None
            ),
            "account_total_precision": (
                attribution_precision
                if aborted_total > 0
                else (
                    "campaign-attributable-exact"
                    if campaign_attributable_exact
                    else "window-counter-exact-campaign-attribution-unproven"
                )
            ),
            "per_request_precision": (
                "exact" if cost_reconciliation_status == "exact" else "mixed_or_incomplete"
            ),
            "judge_included": True,
            "brave_external_cost_separate": True,
            "task_allocation_policy": ("account delta is not allocated to individual tasks"),
            "note": (
                "Account proof establishes non-BYOK for locally unverified requests; "
                "it does not convert missing or estimated per-request costs to exact. "
                "When any physical request is non-exact or unknown, the account "
                "delta is only a shared-key window delta because cross-host external "
                "use cannot be proven absent. "
                "Brave spend remains separate from the OpenRouter LLM ledger."
            ),
        },
        "source_sha256": source_hashes,
    }
    proof["proof_sha256"] = canonical_sha256(proof, prefix=True)
    return proof


def failed_account_proof(
    *,
    error: FinalizationError,
    runtime_key_fingerprint: str,
    ledger_rows: Sequence[Mapping[str, Any]],
    ledger_summary: Mapping[str, Any],
) -> dict[str, Any]:
    """Publish a fail-closed audit proof while preserving valid execution output."""

    counts = Counter(str(row.get("non_byok_evidence") or "") for row in ledger_rows)
    warning = f"account proof validation failed: {error}"
    proof: dict[str, Any] = {
        "schema": PROOF_SCHEMA,
        "pass": False,
        "publication_eligible": True,
        "audit_conflict_kind": "account_proof_incomplete",
        "execution_pass": True,
        "policy_pass": False,
        "reconciliation": {
            "pass": False,
            "status": "audit_conflict",
            "gap_usd": None,
            "tolerance_usd": None,
        },
        "status": "audit_conflict",
        "warnings": [warning],
        "created_at": utc_now(),
        "policy": (
            "The account/non-BYOK proof could not be validated. This leaves policy "
            "and reconciliation failed/unknown, while independently validated "
            "execution artifacts remain publishable."
        ),
        "api_key_sha256": runtime_key_fingerprint,
        "runtime_environment_sha256": None,
        "account_windows": [],
        "account_window_total_usd": "0",
        "unallocated_aborted_window_usd": "0",
        "result_row_account_window_scope": "unverified",
        "account": {
            "usage_before_usd": None,
            "usage_after_usd": None,
            "usage_delta_usd": None,
            "byok_usage_before_usd": None,
            "byok_usage_after_usd": None,
            "byok_usage_delta_usd": None,
            "campaign_byok_usage_delta_usd": None,
            "campaign_usage_delta_usd": None,
            "campaign_window_count": 0,
            "is_free_tier": None,
        },
        "window": {
            "source_window_coverage": [],
            "ledger_window_reconciliation": [],
        },
        "local_physical_request_evidence": {
            "request_count": len(ledger_rows),
            "exact_non_byok_request_count": counts["exact"],
            "unverified_request_count": counts["unverified"],
            "explicit_byok_request_count": counts["explicit_byok"],
            "conflict_request_count": counts["conflict"],
            "campaign_covered_unverified_request_count": 0,
            "resolved_request_count": counts["exact"],
        },
        "cost_scope": {
            "ledger_recorded_cost_usd": ledger_summary.get("recorded_cost_usd"),
            "ledger_exact_cost_usd": ledger_summary.get("exact_cost_usd"),
            "account_usage_delta_usd": None,
            "account_window_delta_usd": None,
            "current_account_window_delta_usd": None,
            "campaign_bound_account_window_total_usd": None,
            "unallocated_account_window_total_usd": "0",
            "all_account_window_total_usd": "0",
            "ledger_window_reconciliation": [],
            "account_windows": [],
            "account_window_total_usd": "0",
            "unallocated_aborted_window_usd": "0",
            "reconciliation_gap_usd": None,
            "reconciliation_tolerance_usd": None,
            "reconciliation_status": "audit_conflict",
            "unknown_cost_request_count": ledger_summary.get("unknown_cost_request_count"),
            "non_exact_cost_request_count": ledger_summary.get("non_exact_cost_request_count"),
            "attribution_precision": "unverified",
            "campaign_attributable_exact": False,
            "current_window_campaign_attributable_exact": False,
            "campaign_attributable_cost_usd": None,
            "account_total_precision": "unverified",
            "per_request_precision": "mixed_or_incomplete",
            "judge_included": True,
            "brave_external_cost_separate": True,
            "task_allocation_policy": "account delta is not allocated to individual tasks",
            "note": warning,
        },
        "source_sha256": {},
    }
    proof["proof_sha256"] = canonical_sha256(proof, prefix=True)
    return proof


def trace_row_from_result(row: Mapping[str, Any]) -> dict[str, Any]:
    trace = {
        "trace_schema": RESULT_EVIDENCE_SCHEMA,
        RESULT_EVIDENCE_SHA256_FIELD: row.get(RESULT_EVIDENCE_SHA256_FIELD),
    }
    for field_name in TRACE_FIELDS:
        if field_name in {
            "tool_policy",
            "generation_policy",
            "generation_config",
            "routing_trace",
            "server_tool_use",
            "execution",
            "usage",
            "cost_accounting",
            "openrouter_non_byok_audit",
            "openrouter_non_byok_resolution",
            "run_trace",
            "ensemble_trace",
        }:
            trace[field_name] = row.get(field_name) or {}
        elif field_name == "generation_retry_reasons":
            trace[field_name] = row.get(field_name) or []
        elif field_name != "fusion_delta":
            trace[field_name] = row.get(field_name)
    judge = row.get("judge")
    if isinstance(judge, Mapping):
        trace["judge"] = {
            "mode": judge.get("mode"),
            "score_status": judge.get("score_status"),
            "quality_total": row.get("quality_total"),
            "pass_rate": judge.get("pass_rate"),
            "valid_pass_rate": judge.get("valid_pass_rate"),
            "judge_error_count": judge.get("judge_error_count"),
            "criteria_count": judge.get("criteria_count"),
            "valid_criteria_count": judge.get("valid_criteria_count"),
            "invalid_criteria_count": judge.get("invalid_criteria_count"),
        }
    else:
        trace["judge"] = {}
    trace["candidate_judge_count"] = len(row.get("candidate_judges") or [])
    trace["fusion_delta"] = row.get("fusion_delta")
    return trace


def selected_generation_cost(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a value plus its explicit per-request cost precision."""

    accounting = row.get("cost_accounting")
    generation = (
        accounting.get("selected_generation_attempt") or accounting.get("generation")
        if isinstance(accounting, Mapping)
        else None
    )
    if isinstance(generation, Mapping) and generation.get("recorded_cost_usd") is not None:
        value = required_decimal(
            generation.get("recorded_cost_usd"), label="selected generation cost"
        )
        exact = generation.get("cost_exact") is True
        complete = generation.get("cost_complete") is True
        declared = str(
            generation.get("cost_precision") or generation.get("cost_status") or ""
        ).casefold()
        precision = "exact" if exact else "estimated" if "estimate" in declared else "recorded"
        return {
            "value": value,
            "precision": precision,
            "complete": complete,
            "exact": exact,
        }
    usage = row.get("usage")
    if isinstance(usage, Mapping):
        value = usage.get("billed_cost")
        if value is not None:
            parsed = required_decimal(value, label="selected usage cost")
            units = usage_units(usage)
            exact = bool(units) and all(unit_cost_is_exact(unit) for unit in units)
            return {
                "value": parsed,
                "precision": "exact" if exact else "recorded",
                "complete": exact,
                "exact": exact,
            }
    return {
        "value": None,
        "precision": "unknown",
        "complete": False,
        "exact": False,
    }


def percentile_nearest(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(
        0,
        min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1),
    )
    return ordered[index]


def row_pass_rate(row: Mapping[str, Any]) -> Decimal:
    judge = row.get("judge")
    criteria = judge.get("criterion_judgments") if isinstance(judge, Mapping) else None
    if not isinstance(criteria, list) or not criteria:
        return Decimal(0)
    valid = [item for item in criteria if isinstance(item, Mapping)]
    if not valid:
        return Decimal(0)
    passed = sum(
        (
            item.get("met") is True
            if Decimal(str(item.get("weight") or 0)) >= 0
            else item.get("met") is False
        )
        for item in valid
    )
    return Decimal(passed) / Decimal(len(valid))


def selected_generation_costs_from_ledger(
    rows: Sequence[Mapping[str, Any]],
    ledger_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    expected = {f"{row.get('group')}/{row.get('task_id')}": row for row in rows}
    by_pair: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for ledger_row in ledger_rows:
        if ledger_row.get("generation_disposition") != "selected":
            continue
        pairs = ledger_row.get("group_task_pairs")
        if not isinstance(pairs, list) or len(pairs) != 1:
            raise FinalizationError("selected physical generation request is not owned by one pair")
        pair = pairs[0]
        if not isinstance(pair, Mapping):
            raise FinalizationError("selected generation pair evidence is invalid")
        key = f"{pair.get('group')}/{pair.get('task_id')}"
        if key not in expected:
            raise FinalizationError(f"selected ledger request belongs to an unexpected pair: {key}")
        by_pair[key].append(ledger_row)
    missing = sorted(set(expected) - set(by_pair))
    if missing:
        raise FinalizationError(f"selected generation ledger misses pair(s): {missing[:5]}")
    summaries: dict[str, dict[str, Any]] = {}
    for key, row in expected.items():
        physical = by_pair[key]
        known = [
            required_decimal(
                item.get("recorded_cost_usd"),
                label=f"{key} selected ledger cost",
            )
            for item in physical
            if item.get("recorded_cost_usd") is not None
        ]
        non_estimated_known = [
            required_decimal(
                item.get("recorded_cost_usd"),
                label=f"{key} selected non-estimated ledger cost",
            )
            for item in physical
            if item.get("recorded_cost_usd") is not None
            and item.get("cost_precision") != "estimated"
        ]
        exact_known = [
            required_decimal(
                item.get("recorded_cost_usd"),
                label=f"{key} selected exact ledger cost",
            )
            for item in physical
            if item.get("recorded_cost_usd") is not None and item.get("cost_precision") == "exact"
        ]
        estimated_known = [
            required_decimal(
                item.get("recorded_cost_usd"),
                label=f"{key} selected estimated ledger cost",
            )
            for item in physical
            if item.get("recorded_cost_usd") is not None
            and item.get("cost_precision") == "estimated"
        ]
        precision_counts = Counter(
            str(item.get("cost_precision") or "unknown") for item in physical
        )
        recorded_request_count = len(known)
        exact_request_count = len(exact_known)
        estimated_request_count = len(estimated_known)
        recorded_non_exact_request_count = (
            recorded_request_count - exact_request_count - estimated_request_count
        )
        ignored_request_count = len(physical) - recorded_request_count
        complete = recorded_request_count == len(physical)
        exact = complete and precision_counts["exact"] == len(physical)
        lower_bound = sum(non_estimated_known, Decimal(0))
        # Reporting deliberately separates execution cost from evidence
        # completeness. Exact/recorded dollars are preferred; a frozen-price
        # estimate materialized by the runner is next (cache-aware when frozen
        # cache rates exist, cache-blind otherwise). Requests that remain
        # unpriced are excluded, never coerced to $0. A pair with no priced
        # request has no subtotal.
        value = sum(known, Decimal(0)) if known else None
        exact_value = sum(exact_known, Decimal(0))
        estimated_value = sum(estimated_known, Decimal(0))
        cost_warnings: list[str] = []
        summary = {
            "value": value,
            # Preserve the legacy meaning: this is a complete pair total.
            # counted_cost_usd is the report subtotal after excluding unknowns.
            "recorded_cost_usd": str(value) if complete and value is not None else None,
            "counted_cost_usd": str(value) if value is not None else None,
            "recorded_cost_usd_lower_bound": str(lower_bound),
            "exact_cost_usd": str(exact_value),
            "estimated_cost_usd": str(estimated_value),
            "request_count": len(physical),
            "known_cost_request_count": recorded_request_count,
            "exact_cost_request_count": exact_request_count,
            "non_estimated_known_cost_request_count": len(non_estimated_known),
            "estimated_cost_request_count": estimated_request_count,
            "recorded_non_exact_cost_request_count": recorded_non_exact_request_count,
            "unknown_cost_request_count": ignored_request_count,
            "ignored_cost_request_count": ignored_request_count,
            "ignored_cost_requests_are_zero": False,
            "precision_counts": dict(sorted(precision_counts.items())),
            "precision": (
                "exact"
                if exact
                else "estimated_or_recorded"
                if complete
                else "partial_excluding_unknown"
            ),
            "complete": complete,
            "exact": exact,
            "warnings": cost_warnings,
        }
        declared = selected_generation_cost(row)
        if (
            isinstance(value, Decimal)
            and isinstance(declared.get("value"), Decimal)
            and declared["value"].quantize(Decimal("0.000000001"))
            != value.quantize(Decimal("0.000000001"))
        ):
            cost_warnings.append(f"{key} selected generation row cost conflicts with ledger")
        if declared.get("exact") is True and not exact:
            cost_warnings.append(f"{key} selected generation row falsely declares exact cost")
        summaries[key] = summary
    counted_values = [
        summary["value"]
        for summary in summaries.values()
        if isinstance(summary.get("value"), Decimal)
    ]
    counted_cost = sum(counted_values, Decimal(0)) if counted_values else None
    return summaries, {
        "pair_count": len(summaries),
        "complete_pair_count": sum(value["complete"] is True for value in summaries.values()),
        "exact_pair_count": sum(value["exact"] is True for value in summaries.values()),
        "counted_pair_count": len(counted_values),
        "counted_cost_usd": str(counted_cost) if counted_cost is not None else None,
        "request_count": sum(value["request_count"] for value in summaries.values()),
        "known_cost_request_count": sum(
            value["known_cost_request_count"] for value in summaries.values()
        ),
        "exact_cost_request_count": sum(
            value["exact_cost_request_count"] for value in summaries.values()
        ),
        "estimated_cost_request_count": sum(
            value["estimated_cost_request_count"] for value in summaries.values()
        ),
        "recorded_non_exact_cost_request_count": sum(
            value["recorded_non_exact_cost_request_count"] for value in summaries.values()
        ),
        "ignored_cost_request_count": sum(
            value["ignored_cost_request_count"] for value in summaries.values()
        ),
        "unknown_is_zero": False,
        "reporting_policy": {
            "priority": [
                "recorded_dollar_cost",
                "frozen_cache_aware_token_estimate",
                "exclude_unpriced_request",
            ],
            "ignored_cost_requests_are_zero": False,
        },
        "warnings": [
            warning for summary in summaries.values() for warning in summary.get("warnings") or []
        ],
        "pairs": {
            key: {field: value for field, value in summary.items() if field != "value"}
            for key, summary in sorted(summaries.items())
        },
    }


def group_metrics(
    rows: Sequence[Mapping[str, Any]],
    *,
    selected_costs_by_pair: Mapping[str, Mapping[str, Any]] | None = None,
    groups: Sequence[str] = GROUPS,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("group") or "")].append(row)
    metrics: list[dict[str, Any]] = []
    for group in groups:
        values = grouped[group]
        qualities = [Decimal(str(row["quality_total"])) for row in values]
        pass_rates = [row_pass_rate(row) for row in values]
        selected_costs = [
            dict(
                (selected_costs_by_pair or {}).get(
                    f"{row.get('group')}/{row.get('task_id')}",
                    selected_generation_cost(row),
                )
            )
            for row in values
        ]
        counted_costs = [
            item["value"] for item in selected_costs if isinstance(item.get("value"), Decimal)
        ]
        covered_count = len(counted_costs)
        all_counted = covered_count == len(values)
        exact_count = sum(item["exact"] is True for item in selected_costs)
        complete_count = sum(item["complete"] is True for item in selected_costs)
        all_exact = exact_count == len(values)
        ignored_request_count = sum(
            nonnegative_int(
                item.get(
                    "ignored_cost_request_count",
                    item.get("unknown_cost_request_count", 1 if item.get("value") is None else 0),
                )
            )
            for item in selected_costs
        )
        request_count = sum(
            nonnegative_int(item.get("request_count", 1)) for item in selected_costs
        )
        known_request_count = sum(
            nonnegative_int(
                item.get("known_cost_request_count", 0 if item.get("value") is None else 1)
            )
            for item in selected_costs
        )
        exact_request_count = sum(
            nonnegative_int(
                item.get("exact_cost_request_count", 1 if item.get("exact") is True else 0)
            )
            for item in selected_costs
        )
        estimated_request_count = sum(
            nonnegative_int(
                item.get(
                    "estimated_cost_request_count",
                    1 if item.get("precision") == "estimated" else 0,
                )
            )
            for item in selected_costs
        )
        recorded_non_exact_request_count = max(
            0,
            known_request_count - exact_request_count - estimated_request_count,
        )
        cost_precision = (
            "exact"
            if all_exact
            else "partial_excluding_unknown"
            if ignored_request_count
            else "mixed_or_estimated"
        )
        counted_total = sum(counted_costs, Decimal(0))
        exact_cost_total = sum(
            (
                required_decimal(
                    item.get("exact_cost_usd", "0"),
                    label="selected exact generation cost",
                )
                for item in selected_costs
            ),
            Decimal(0),
        )
        estimated_cost_total = sum(
            (
                required_decimal(
                    item.get("estimated_cost_usd", "0"),
                    label="selected estimated generation cost",
                )
                for item in selected_costs
            ),
            Decimal(0),
        )
        lower_bound_total = sum(
            (
                required_decimal(
                    item.get("recorded_cost_usd_lower_bound"),
                    label="selected generation cost lower bound",
                )
                if item.get("recorded_cost_usd_lower_bound") is not None
                else item["value"]
                if isinstance(item.get("value"), Decimal)
                else Decimal(0)
            )
            for item in selected_costs
        )
        token_totals: Counter[str] = Counter()
        visible_exact = True
        tool_counts: list[int] = []
        step_counts: list[int] = []
        request_counts: list[int] = []
        latencies_ms: list[float] = []
        iteration_counts: list[int] = []
        judge_errors = 0
        for row in values:
            units = usage_units(row.get("usage"))
            for unit in units:
                input_tokens = nonnegative_int(unit.get("input_tokens"))
                output_tokens = nonnegative_int(unit.get("output_tokens"))
                reasoning_tokens = nonnegative_int(unit.get("reasoning_tokens"))
                cached_tokens = max(
                    nonnegative_int(unit.get("cached_tokens")),
                    nonnegative_int(unit.get("cache_read_tokens")),
                )
                token_totals["input"] += input_tokens
                token_totals["output"] += output_tokens
                token_totals["reasoning"] += reasoning_tokens
                token_totals["cached"] += cached_tokens
                if reasoning_tokens > output_tokens:
                    visible_exact = False
                else:
                    token_totals["visible"] += output_tokens - reasoning_tokens
            tool_counts.append(
                max(
                    nonnegative_int(row.get("total_tool_call_count")),
                    nonnegative_int(row.get("stream_tool_call_count"))
                    + nonnegative_int(row.get("server_tool_call_count")),
                )
            )
            step_counts.append(nonnegative_int(row.get("trajectory_steps")))
            request_counts.append(nonnegative_int(row.get("llm_request_count")))
            started = row.get("started_at")
            completed = row.get("completed_at")
            if finite_number(started) and finite_number(completed):
                latencies_ms.append(max(0.0, (float(completed) - float(started)) * 1000.0))
            trace = row.get("ensemble_trace")
            iteration_counts.append(
                nonnegative_int(
                    trace.get("agent_llm_call_count") or trace.get("agent_iterations")
                    if isinstance(trace, Mapping)
                    else 0
                )
            )
            judge = row.get("judge")
            if isinstance(judge, Mapping):
                judge_errors += nonnegative_int(judge.get("judge_error_count"))
        denominator = Decimal(len(values))
        metrics.append(
            {
                "group": group,
                "task_count": len(values),
                "done_count": len(values),
                "avg_quality_total": str(sum(qualities, Decimal(0)) / denominator),
                "avg_pass_rate": str(sum(pass_rates, Decimal(0)) / denominator),
                "judge_error_count": judge_errors,
                "avg_selected_generation_cost_usd": (
                    str(counted_total / Decimal(len(values))) if all_counted else None
                ),
                "covered_avg_selected_generation_cost_usd": (
                    str(counted_total / Decimal(covered_count)) if covered_count else None
                ),
                "selected_generation_cost_usd": (str(counted_total) if all_counted else None),
                "selected_generation_cost_counted_usd": (
                    str(counted_total) if covered_count else None
                ),
                "selected_generation_cost_exact_usd": str(exact_cost_total),
                "selected_generation_cost_estimated_usd": str(estimated_cost_total),
                "selected_generation_cost_usd_lower_bound": str(lower_bound_total),
                "selected_generation_cost_request_count": request_count,
                "selected_generation_cost_known_request_count": known_request_count,
                "selected_generation_cost_exact_request_count": exact_request_count,
                "selected_generation_cost_estimated_request_count": estimated_request_count,
                "selected_generation_cost_recorded_non_exact_request_count": (
                    recorded_non_exact_request_count
                ),
                "selected_generation_cost_ignored_request_count": ignored_request_count,
                "selected_generation_cost_ignored_requests_are_zero": False,
                "selected_generation_cost_reported_task_count": covered_count,
                "selected_generation_cost_covered_task_count": covered_count,
                "selected_generation_cost_exact_task_count": exact_count,
                "selected_generation_cost_complete_task_count": complete_count,
                "selected_generation_cost_complete": complete_count == len(values),
                "selected_generation_cost_precision": cost_precision,
                "avg_llm_requests": str(Decimal(sum(request_counts)) / denominator),
                "avg_input_tokens": str(Decimal(token_totals["input"]) / denominator),
                "avg_output_tokens": str(Decimal(token_totals["output"]) / denominator),
                "avg_reasoning_tokens": str(Decimal(token_totals["reasoning"]) / denominator),
                "avg_cached_tokens": str(Decimal(token_totals["cached"]) / denominator),
                "avg_visible_tokens": (
                    str(Decimal(token_totals["visible"]) / denominator) if visible_exact else None
                ),
                "visible_tokens_exact": visible_exact,
                "avg_total_tokens": str(
                    Decimal(token_totals["input"] + token_totals["output"]) / denominator
                ),
                "avg_tool_calls": str(Decimal(sum(tool_counts)) / denominator),
                "tool_task_rate": str(
                    Decimal(sum(count > 0 for count in tool_counts)) / denominator
                ),
                "avg_trajectory_steps": str(Decimal(sum(step_counts)) / denominator),
                "avg_selected_agent_iterations": str(Decimal(sum(iteration_counts)) / denominator),
                "latency_p50_ms": str(percentile_nearest(latencies_ms, 0.50)),
                "latency_p95_ms": str(percentile_nearest(latencies_ms, 0.95)),
            }
        )
    return metrics


def finalize_rows(
    selected: Sequence[SourceRecord],
    *,
    tasks: Sequence[Mapping[str, Any]],
    proof: Mapping[str, Any],
    pair_audit: Mapping[str, Any],
    groups: Sequence[str] = GROUPS,
) -> list[dict[str, Any]]:
    selected_by_key = {record.key: record for record in selected}
    proof_sha = str(proof.get("proof_sha256") or "")
    final_rows: list[dict[str, Any]] = []
    row_index = 0
    for task in tasks:
        task_id = str(task["id"])
        for group in groups:
            row_index += 1
            record = selected_by_key[(group, task_id)]
            original = record.row
            row = copy.deepcopy(original)
            generation_contract_before = {
                "final_text": original.get("final_text"),
                "usage": original.get("usage"),
                "execution": original.get("execution"),
                "generation_attempt_count": original.get("generation_attempt_count"),
                "generation_attempt_budget_used": original.get("generation_attempt_budget_used"),
                "generation_attempt_total_billed_cost": original.get(
                    "generation_attempt_total_billed_cost"
                ),
                "cost_accounting": original.get("cost_accounting"),
            }
            local_audit = original.get("openrouter_non_byok_audit")
            local_status = (
                str(local_audit.get("status") or "")
                if isinstance(local_audit, Mapping)
                else "unverified"
            )
            selection = pair_audit[f"{group}/{task_id}"]
            selection_warning_rows = selection.get("warnings")
            selection_warnings = [
                str(reason)
                for warning_row in (
                    selection_warning_rows if isinstance(selection_warning_rows, list) else []
                )
                if isinstance(warning_row, Mapping)
                for reason in warning_row.get("reasons") or []
            ]
            proof_warnings = [str(value) for value in proof.get("warnings") or []]
            policy_pass = proof.get("policy_pass") is True
            row["row_index"] = row_index
            row["openrouter_non_byok_resolution"] = {
                "schema": RESOLUTION_SCHEMA,
                "status": (
                    "local_exact"
                    if local_status == "exact" and policy_pass
                    else "resolved_by_campaign_account_proof"
                    if policy_pass
                    else "policy_failed_or_unverified"
                ),
                "local_audit_status": local_status,
                "campaign_proof_path": "openrouter-non-byok-campaign-proof.json",
                "campaign_proof_sha256": proof_sha,
                "campaign_proof_pass": proof.get("pass") is True,
                "policy_pass": policy_pass,
                "reconciliation": copy.deepcopy(proof.get("reconciliation") or {}),
                "warnings": proof_warnings,
                "cost_precision_unchanged": True,
            }
            cost_accounting = row.get("cost_accounting")
            llm_complete = (
                bool(cost_accounting.get("actual_llm_cost_complete"))
                if isinstance(cost_accounting, Mapping)
                else False
            )
            completion_warnings = list(
                dict.fromkeys(
                    [
                        *selection_warnings,
                        *proof_warnings,
                        *([] if llm_complete else ["cost_metadata_incomplete"]),
                    ]
                )
            )
            row["completion_status"] = {
                "generation_accepted": True,
                "judge_complete": True,
                "cost_metadata_complete": llm_complete,
                "cost_metadata_scope": "actual_llm_spend",
                "openrouter_non_byok_resolved": policy_pass,
                "execution_pass": True,
                "policy_pass": policy_pass,
                "reconciliation": copy.deepcopy(proof.get("reconciliation") or {}),
                "status": "complete" if not completion_warnings else "complete_with_warnings",
                "warnings": completion_warnings,
                "incomplete_reasons": completion_warnings,
            }
            row["campaign_finalization"] = {
                "schema": MANIFEST_SCHEMA,
                "selected_source": record.reference,
                "selection": selection,
                "execution_pass": True,
                "policy_pass": policy_pass,
                "reconciliation": copy.deepcopy(proof.get("reconciliation") or {}),
                "status": "complete" if not completion_warnings else "complete_with_warnings",
                "warnings": completion_warnings,
                "finalizer_version": FINALIZER_VERSION,
            }
            generation_contract_after = {
                "final_text": row.get("final_text"),
                "usage": row.get("usage"),
                "execution": row.get("execution"),
                "generation_attempt_count": row.get("generation_attempt_count"),
                "generation_attempt_budget_used": row.get("generation_attempt_budget_used"),
                "generation_attempt_total_billed_cost": row.get(
                    "generation_attempt_total_billed_cost"
                ),
                "cost_accounting": row.get("cost_accounting"),
            }
            if generation_contract_after != generation_contract_before:
                raise FinalizationError("finalization mutated generation/cost contracts")
            final_rows.append(seal_result_row(row))
    return final_rows


def artifact_record(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": path.name,
        "sha256": file_sha256(path),
        "size_bytes": stat.st_size,
        "mode": oct(stat.st_mode & 0o777),
    }


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def paired_quality_comparisons(
    rows: Sequence[Mapping[str, Any]],
    *,
    bootstrap_samples: int = 20_000,
    groups: Sequence[str] = GROUPS,
) -> list[dict[str, Any]]:
    by_group: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for row in rows:
        by_group[str(row.get("group") or "")][str(row.get("task_id") or "")] = Decimal(
            str(row.get("quality_total"))
        )
    comparisons: list[dict[str, Any]] = []
    for group in groups:
        for baseline in (baseline for baseline in ("B0", "B1") if baseline in groups):
            common = sorted(set(by_group[group]) & set(by_group[baseline]))
            differences = [
                by_group[group][task_id] - by_group[baseline][task_id] for task_id in common
            ]
            if not differences:
                continue
            mean = sum(differences, Decimal(0)) / Decimal(len(differences))
            rng = random.Random(f"draco:{group}:{baseline}")
            bootstrap = sorted(
                sum(
                    (differences[rng.randrange(len(differences))] for _ in differences),
                    Decimal(0),
                )
                / Decimal(len(differences))
                for _ in range(bootstrap_samples)
            )
            low = bootstrap[int(0.025 * (bootstrap_samples - 1))]
            high = bootstrap[int(0.975 * (bootstrap_samples - 1))]
            comparisons.append(
                {
                    "group": group,
                    "baseline": baseline,
                    "pair_count": len(common),
                    "delta_quality": str(mean),
                    "ci95_low": str(low),
                    "ci95_high": str(high),
                    "wins": sum(value > 0 for value in differences),
                    "ties": sum(value == 0 for value in differences),
                    "losses": sum(value < 0 for value in differences),
                    "bootstrap_samples": bootstrap_samples,
                    "seed": f"draco:{group}:{baseline}",
                }
            )
    return comparisons


def rubric_section_metrics(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[tuple[Decimal, Decimal, int]]] = defaultdict(list)
    for row in rows:
        group = str(row.get("group") or "")
        judge = row.get("judge")
        judgments = judge.get("criterion_judgments") if isinstance(judge, Mapping) else None
        per_section: dict[tuple[str, str], dict[str, Decimal | int]] = defaultdict(
            lambda: {
                "raw_score": Decimal(0),
                "positive_weight_total": Decimal(0),
                "passed_count": 0,
                "criterion_count": 0,
            }
        )
        for judgment in judgments or []:
            if not isinstance(judgment, Mapping):
                continue
            section_id = str(judgment.get("section_id") or "rubric")
            title = str(judgment.get("section_title") or section_id)
            bucket = per_section[(section_id, title)]
            weight = Decimal(str(judgment.get("weight") or 0))
            met = judgment.get("met")
            bucket["criterion_count"] += 1
            bucket["positive_weight_total"] += max(Decimal(0), weight)
            if met is True:
                bucket["raw_score"] += weight
            if (weight >= 0 and met is True) or (weight < 0 and met is False):
                bucket["passed_count"] += 1
        for (section_id, title), bucket in per_section.items():
            positive_total = Decimal(bucket["positive_weight_total"])
            count = int(bucket["criterion_count"])
            raw_score = Decimal(bucket["raw_score"])
            normalized = (
                max(
                    Decimal(0),
                    min(
                        Decimal(100),
                        raw_score / positive_total * Decimal(100),
                    ),
                )
                if positive_total > 0
                else Decimal(0)
            )
            pass_rate = (
                Decimal(int(bucket["passed_count"])) / Decimal(count) if count else Decimal(0)
            )
            grouped[(group, section_id, title)].append((normalized, pass_rate, count))
    metrics: list[dict[str, Any]] = []
    for (group, section_id, title), row_values in sorted(grouped.items()):
        denominator = Decimal(len(row_values))
        metrics.append(
            {
                "group": group,
                "section_id": section_id,
                "section_title": title,
                "task_count": len(row_values),
                "criterion_repeat_count": sum(count for _, _, count in row_values),
                "avg_normalized_score": str(
                    sum(
                        (value for value, _, _ in row_values),
                        Decimal(0),
                    )
                    / denominator
                ),
                "pass_rate": str(
                    sum(
                        (value for _, value, _ in row_values),
                        Decimal(0),
                    )
                    / denominator
                ),
            }
        )
    return metrics


def repair_action_details(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    descriptions = {
        "judge_only": "复用已接受 generation，仅补跑并完成 Judge。",
        "metadata_only": "复用 generation/Judge，仅回填并核验 provider、费用或回执元数据。",
    }
    for row in rows:
        execution = row.get("execution")
        completion = row.get("resume_completion")
        action = str(
            (completion.get("action") if isinstance(completion, Mapping) else None)
            or (execution.get("resume_action") if isinstance(execution, Mapping) else None)
            or ""
        )
        if action not in descriptions:
            continue
        details.append(
            {
                "group": str(row.get("group") or ""),
                "task_id": str(row.get("task_id") or ""),
                "action": action,
                "generation_reused": bool(
                    isinstance(execution, Mapping) and execution.get("generation_reused") is True
                ),
                "judge_reran": bool(
                    isinstance(execution, Mapping) and execution.get("judge_reran") is True
                ),
                "metadata_repaired": bool(
                    isinstance(execution, Mapping) and execution.get("metadata_repaired") is True
                ),
                "detail": descriptions[action],
            }
        )
    return details


def experiment_policy_report_values(
    policy: FinalizerExperimentPolicy | None,
    *,
    task_concurrency: int,
    judge_concurrency: int,
) -> dict[str, Any]:
    """Render the exact policy values printed in ``EXPERIMENT_RESULTS.md``."""

    def display_number(value: Any) -> str:
        rendered = format(value, "f") if isinstance(value, Decimal) else str(value)
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered

    runner_mode = policy.runner["mode"] if policy is not None else "agent_loop"
    web_search = (
        policy.tools["web_search"]
        if policy is not None
        else {"provider": "brave", "max_results": 5}
    )
    web_fetch = policy.tools["web_fetch"] if policy is not None else {"max_content_tokens": 50_000}
    return {
        "runner_label": "Agent Loop" if runner_mode == "agent_loop" else runner_mode,
        "task_timeout": display_number(
            policy.timeouts["task_seconds"] if policy is not None else FORMAL_TASK_TIMEOUT_SECONDS
        ),
        "max_iterations": (
            policy.runner["agent_max_iterations"]
            if policy is not None
            else FORMAL_AGENT_MAX_ITERATIONS
        ),
        "task_concurrency": task_concurrency,
        "generation_temperature": display_number(
            policy.generation["temperature"] if policy is not None else Decimal("0")
        ),
        "generation_max_tokens": (
            policy.generation["max_tokens"] if policy is not None else FORMAL_GENERATION_MAX_TOKENS
        ),
        "generation_max_attempts": (
            policy.generation_max_attempts if policy is not None else FORMAL_GENERATION_MAX_ATTEMPTS
        ),
        "judge_model": policy.judge_model if policy is not None else JUDGE_MODEL,
        "judge_repeats": policy.judge_repeats if policy is not None else JUDGE_REPEATS,
        "judge_concurrency": judge_concurrency,
        "judge_max_attempts": (
            policy.judge_max_attempts if policy is not None else FORMAL_JUDGE_MAX_ATTEMPTS
        ),
        "web_search_provider": web_search["provider"],
        "web_search_max_results": web_search["max_results"],
        "web_fetch_max_content_tokens": web_fetch["max_content_tokens"],
        "proposer_recovery": (
            policy.proposer_recovery if policy is not None else FORMAL_PROPOSER_RECOVERY_POLICY
        ),
        "aggregator_recovery": (
            policy.aggregator_recovery if policy is not None else FORMAL_AGGREGATOR_RECOVERY_POLICY
        ),
    }


def experiment_results_markdown(
    *,
    task_count: int,
    groups: Sequence[str] = GROUPS,
    final_rows: Sequence[Mapping[str, Any]],
    metrics: Sequence[Mapping[str, Any]],
    ledger_summary: Mapping[str, Any],
    external_tool_cost: Mapping[str, Any],
    proof: Mapping[str, Any],
    comparisons: Sequence[Mapping[str, Any]],
    model_metrics: Sequence[Mapping[str, Any]],
    rubric_metrics: Sequence[Mapping[str, Any]],
    repair_details: Sequence[Mapping[str, Any]],
    experiment_policy: FinalizerExperimentPolicy | None = None,
    expected_task_concurrency: int = FORMAL_TASK_CONCURRENCY,
    expected_judge_concurrency: int = FORMAL_JUDGE_CONCURRENCY,
) -> str:
    account = proof["account"]
    evidence = proof["local_physical_request_evidence"]
    cost_scope = proof["cost_scope"]
    policy_pass = proof.get("policy_pass") is True
    reconciliation_status = str((proof.get("reconciliation") or {}).get("status") or "unknown")
    result_window_scope = str(proof.get("result_row_account_window_scope") or "")
    account_windows = list(cost_scope["account_windows"])
    campaign_windows = [
        window for window in account_windows if window.get("kind") in {"prior_campaign", "current"}
    ]
    aborted_windows = [
        window for window in account_windows if window.get("kind") == "prior_aborted"
    ]
    campaign_bound_total = cost_scope.get(
        "campaign_bound_account_window_total_usd",
        cost_scope["account_window_delta_usd"],
    )
    all_window_total = cost_scope.get(
        "all_account_window_total_usd",
        cost_scope["account_window_total_usd"],
    )
    if cost_scope["campaign_attributable_exact"]:
        attribution_note = (
            "- 每个物理请求均有 exact receipt 且账本与 campaign-bound 账户窗口"
            "增量一致，因此该增量可作为 campaign attributable exact cost。"
        )
    elif result_window_scope == "campaign_windows":
        attribution_note = (
            "- Campaign-bound 账本总额与账户 counter 精确对账；但仍有 "
            f"{evidence['campaign_covered_unverified_request_count']} 个物理请求缺少完整的"
            "逐请求费用/provider 元数据，且本机锁不能证明跨主机 key 独占。因此"
            f"${campaign_bound_total} 是 counter-exact campaign-bound 总额，"
            "但 `campaign_attributable_exact=false`，也不分摊到任务或实验组。"
        )
    elif (
        cost_scope["current_window_campaign_attributable_exact"]
        and Decimal(cost_scope["unallocated_aborted_window_usd"]) > 0
    ):
        attribution_note = (
            "- 当前正式窗口的物理回执与账户 counter 已精确对账；但 prior "
            "aborted window 成本未分配到请求或任务，因此多窗口合计不能称为 "
            "campaign attributable exact cost。"
        )
    else:
        attribution_note = (
            "- 当前正式窗口存在 non-exact/unknown 物理请求；该数值仅是"
            "共享 key 的账户窗口增量，跨主机外部使用无法证明不存在，不能称为 "
            "campaign total，也不会分摊到任务或实验组。"
        )
    physical_request_count = nonnegative_int(ledger_summary.get("physical_request_count"))
    unknown_request_count = nonnegative_int(ledger_summary.get("unknown_cost_request_count"))
    non_exact_request_count = nonnegative_int(ledger_summary.get("non_exact_cost_request_count"))
    exact_request_count = physical_request_count - unknown_request_count - non_exact_request_count
    window_reconciliations = cost_scope.get("ledger_window_reconciliation")
    window_detail = "；".join(
        f"{item.get('account_window_kind')}="
        f"{item.get('physical_request_count')} requests/"
        f"${item.get('account_usage_delta_usd')}/"
        f"{item.get('reconciliation_status')}"
        for item in window_reconciliations or []
        if isinstance(item, Mapping)
    )
    scope_costs = ledger_summary["scope_recorded_cost_usd"]
    judge_scope_cost = sum(
        (Decimal(value) for key, value in scope_costs.items() if "judge" in key),
        Decimal(0),
    )
    disposition_costs = ledger_summary["generation_disposition_recorded_cost_usd"]
    selected_cost_request_count = sum(
        nonnegative_int(metric.get("selected_generation_cost_request_count")) for metric in metrics
    )
    selected_cost_exact_request_count = sum(
        nonnegative_int(metric.get("selected_generation_cost_exact_request_count"))
        for metric in metrics
    )
    selected_cost_estimated_request_count = sum(
        nonnegative_int(metric.get("selected_generation_cost_estimated_request_count"))
        for metric in metrics
    )
    selected_cost_recorded_non_exact_request_count = sum(
        nonnegative_int(metric.get("selected_generation_cost_recorded_non_exact_request_count"))
        for metric in metrics
    )
    selected_cost_ignored_request_count = sum(
        nonnegative_int(metric.get("selected_generation_cost_ignored_request_count"))
        for metric in metrics
    )
    retrospective_recoveries: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for row in final_rows:
        finalization = row.get("campaign_finalization")
        selection = finalization.get("selection") if isinstance(finalization, Mapping) else None
        recovery = (
            selection.get("retrospective_reclassification_recovery")
            if isinstance(selection, Mapping)
            else None
        )
        if isinstance(recovery, Mapping):
            retrospective_recoveries.append((row, recovery))
    active_groups = tuple(groups)
    group_config_rows = {
        "B0": f"| B0 | single | `{B0_MODEL}` |",
        "B1": "| B1 | router_single | frozen `c0/c1/c2/c3` tiers |",
        "B2": "| B2 | selection_mode | `static_openrouter_b5` |",
        "B4": f"| B4 | single | `{B4_MODEL}` |",
        "G1": "| G1 | selection_mode | frozen-registry `router_dynamic` |",
    }
    ensemble_groups = [group for group in active_groups if group in {"B2", "G1"}]
    rendered_policy = experiment_policy_report_values(
        experiment_policy,
        task_concurrency=expected_task_concurrency,
        judge_concurrency=expected_judge_concurrency,
    )
    proposer_recovery = rendered_policy["proposer_recovery"]
    aggregator_recovery = rendered_policy["aggregator_recovery"]
    lines = [
        f"# DRACO Mini {'/'.join(active_groups)} 实验结果",
        "",
        "## 实验结论",
        "",
        f"- 严格完成 {len(final_rows)}/{task_count * len(active_groups)} 个 "
        "group×task；无缺失、无重复。",
        f"- Policy pass：{str(policy_pass).lower()}；成本/账户 reconciliation："
        f"`{reconciliation_status}`。审计警告不覆盖或删除执行结果。",
        "- 质量、成本、Token、工具、Agent Loop、路由、Judge 与修复证据"
        "均由最终 JSONL 和全 campaign 物理请求账本离线重建。",
        "",
        "## 实验配置",
        "",
        f"- 任务集：DRACO Mini（{task_count} 题）",
        f"- 实验组：{'、'.join(active_groups)}",
        f"- 执行：{rendered_policy['runner_label']}，单任务超时 "
        f"{rendered_policy['task_timeout']} 秒，最多 "
        f"{rendered_policy['max_iterations']} 轮；任务并发 "
        f"{rendered_policy['task_concurrency']}",
        f"- 生成：temperature={rendered_policy['generation_temperature']}，max_tokens="
        f"{rendered_policy['generation_max_tokens']}；每个 `(group, task)` 最多 "
        f"{rendered_policy['generation_max_attempts']} 次 generation attempt",
        f"- Judge：`{rendered_policy['judge_model']}`，"
        f"{rendered_policy['judge_repeats']} repeats，Judge 并发 "
        f"{rendered_policy['judge_concurrency']}，单 criterion/repeat 最多 "
        f"{rendered_policy['judge_max_attempts']} 次 attempt",
        f"- Web：本地 `web_search`/`web_fetch`，provider="
        f"`{rendered_policy['web_search_provider']}`，search max_results="
        f"{rendered_policy['web_search_max_results']}，fetch max_content_tokens="
        f"{rendered_policy['web_fetch_max_content_tokens']}；严格屏蔽 "
        "`hf.co`、`huggingface.co`、`datasets-server.huggingface.co`、"
        "`github.com`、`raw.githubusercontent.com`、`openrouter.ai`、"
        "`perplexity.ai`、`research.perplexity.ai`",
        "- OpenRouter：strict provider routing、只允许 non-BYOK、逐请求 metadata "
        "必需、response cache 关闭、campaign key 独占",
        "- 结果选择：最后一个严格有效 generation，并采用该 generation 的最新兼容修复行",
        "- 完成标准：Judge 必须 `score_status=complete`、无 Judge error、存在 `quality_total`",
        *(
            ["- B2：至少 2 个 usable/degraded proposer，最终答案必须由 aggregator 请求绑定"]
            if "B2" in ensemble_groups
            else []
        ),
        *(
            [
                "- G1：正式 provider-native recovery quorum 固定为 2，"
                f"backup={proposer_recovery['configured_backup_count']}，"
                f"最多追加 {proposer_recovery['max_additional_physical_requests']} 个物理请求；"
                f"aggregator recovery={aggregator_recovery['aggregator_recovery_mode']}/"
                f"top-{aggregator_recovery['aggregator_recovery_top_k']}；"
                "最终答案必须由 aggregator 请求绑定"
            ]
            if "G1" in ensemble_groups
            else []
        ),
        "",
        "| Group | Kind | Declared model / selection mode |",
        "|---|---|---|",
        *(group_config_rows[group] for group in active_groups),
        "",
        "## 覆盖与完整性",
        "",
        f"- 最终结果：{len(final_rows)}/{task_count * len(active_groups)}",
        "- 每组任务数："
        + "、".join(
            f"{group}={sum(1 for row in final_rows if row.get('group') == group)}"
            for group in active_groups
        ),
        "- 最终 JSONL 无缺失 pair、无重复 pair；每行已重新 seal，trace 由最终行确定性重建。",
        "",
        "## 分组指标",
        "",
        "| Group | Rows | Done | AvgQ | AvgPass | JudgeErr | Avg Gen$† | "
        "Total Gen$† | Gen exact | Avg Input | Avg Output | Avg Reason | Avg Cache | "
        "Avg Visible | Avg Tokens | Avg Tools | Tool% | Avg Steps | Avg LLMReq | "
        "p50 ms | p95 ms |",
        "|---|" + "---:|" * 20,
    ]
    for metric in metrics:
        raw_cost = metric["avg_selected_generation_cost_usd"]
        rendered_cost = (
            str(Decimal(str(raw_cost)).quantize(Decimal("0.000001")))
            if raw_cost is not None
            else "N/A"
        )
        total_cost = metric["selected_generation_cost_usd"]
        rendered_total = (
            str(Decimal(str(total_cost)).quantize(Decimal("0.000001")))
            if total_cost is not None
            else "N/A"
        )
        visible = metric["avg_visible_tokens"] or "N/A"
        lines.append(
            "| {group} | {task_count} | {done} | {quality} | {pass_rate}% | "
            "{judge_error} | {cost} | {total_cost} | {exact}/{tasks} | "
            "{prompt} | {completion} | {reason} | {cache} | {visible} | {tokens} | "
            "{tools} | {tool_rate}% | {steps} | {requests} | {p50} | {p95} |".format(
                group=metric["group"],
                task_count=metric["task_count"],
                done=metric["done_count"],
                quality=Decimal(str(metric["avg_quality_total"])).quantize(Decimal("0.0001")),
                pass_rate=(Decimal(str(metric["avg_pass_rate"])) * Decimal(100)).quantize(
                    Decimal("0.01")
                ),
                judge_error=metric["judge_error_count"],
                cost=rendered_cost,
                total_cost=rendered_total,
                exact=metric["selected_generation_cost_exact_task_count"],
                tasks=metric["task_count"],
                prompt=Decimal(str(metric["avg_input_tokens"])).quantize(Decimal("0.1")),
                completion=Decimal(str(metric["avg_output_tokens"])).quantize(Decimal("0.1")),
                reason=Decimal(str(metric["avg_reasoning_tokens"])).quantize(Decimal("0.1")),
                cache=Decimal(str(metric["avg_cached_tokens"])).quantize(Decimal("0.1")),
                visible=(
                    Decimal(str(visible)).quantize(Decimal("0.1")) if visible != "N/A" else visible
                ),
                tokens=Decimal(str(metric["avg_total_tokens"])).quantize(Decimal("0.1")),
                tools=Decimal(str(metric["avg_tool_calls"])).quantize(Decimal("0.1")),
                tool_rate=(Decimal(str(metric["tool_task_rate"])) * Decimal(100)).quantize(
                    Decimal("0.01")
                ),
                steps=Decimal(str(metric["avg_trajectory_steps"])).quantize(Decimal("0.1")),
                requests=Decimal(str(metric["avg_llm_requests"])).quantize(Decimal("0.01")),
                p50=Decimal(str(metric["latency_p50_ms"])).quantize(Decimal("1")),
                p95=Decimal(str(metric["latency_p95_ms"])).quantize(Decimal("1")),
            )
        )
    section_titles = sorted(
        {
            str(metric.get("section_title") or metric.get("section_id") or "")
            for metric in rubric_metrics
        }
    )
    lines.extend(
        [
            "",
            "## Rubric 分项平均",
            "",
            "| Group | " + " | ".join(section_titles) + " |",
            "|---|" + "---:|" * len(section_titles),
        ]
    )
    rubric_by_key = {
        (
            str(metric.get("group") or ""),
            str(metric.get("section_title") or metric.get("section_id") or ""),
        ): metric
        for metric in rubric_metrics
    }
    for group in active_groups:
        values = [
            Decimal(str(rubric_by_key[(group, title)]["avg_normalized_score"])).quantize(
                Decimal("0.01")
            )
            if (group, title) in rubric_by_key
            else "N/A"
            for title in section_titles
        ]
        lines.append(f"| {group} | " + " | ".join(str(value) for value in values) + " |")
    lines.extend(
        [
            "",
            "## 修复动作明细",
            "",
            "| Group | Task | Action | Generation reused | Judge reran | "
            "Metadata repaired | 说明 |",
            "|---|---|---|---:|---:|---:|---|",
        ]
    )
    if repair_details:
        for detail in repair_details:
            lines.append(
                "| {group} | `{task}` | `{action}` | {generation} | {judge} | "
                "{metadata} | {description} |".format(
                    group=detail["group"],
                    task=str(detail["task_id"])[:12],
                    action=detail["action"],
                    generation="是" if detail["generation_reused"] else "否",
                    judge="是" if detail["judge_reran"] else "否",
                    metadata="是" if detail["metadata_repaired"] else "否",
                    description=detail["detail"],
                )
            )
    else:
        lines.append("| — | — | `direct` | 否 | 否 | 否 | 无后处理修复。 |")
    lines.extend(["", "### 协议偏差与恢复", ""])
    if retrospective_recoveries:
        for recovery_row, recovery in retrospective_recoveries:
            spend = recovery.get("invalid_post_accept_spend")
            if not isinstance(spend, Mapping):
                raise FinalizationError("retrospective recovery lacks spend disclosure")
            invalid_ids = ", ".join(
                f"`{str(attempt_id)[:12]}…`"
                for attempt_id in recovery.get("invalid_post_accept_attempt_ids", [])
            )
            lines.append(
                "- {group}/`{task}`：旧分类器在 attempt {selected} 已有效后，"
                "仍误发了后续无效 attempt（{invalid}）。最终结果继续绑定 "
                "attempt {selected}；后续 attempt 的 {requests} 个物理请求均保留在 "
                "`actual-spend-ledger.jsonl`，其中 exact={exact}、non-exact="
                "{non_exact}、unknown={unknown}，已记录成本 ${cost}（unknown 不按 "
                "$0 冒充完整成本），且不计入 selected-generation 指标。".format(
                    group=str(recovery_row.get("group") or ""),
                    task=str(recovery_row.get("task_id") or "")[:12],
                    selected=recovery.get("selected_attempt"),
                    invalid=invalid_ids or "未知",
                    requests=spend.get("physical_request_count"),
                    exact=spend.get("exact_request_count"),
                    non_exact=spend.get("non_exact_request_count"),
                    unknown=spend.get("unknown_cost_request_count"),
                    cost=spend.get("recorded_cost_usd"),
                )
            )
    else:
        lines.append("- 无“有效 generation 后仍启动新 attempt”的历史协议偏差。")
    lines.extend(["", "### 同题 Domain 矩阵", ""])
    lines.append("| Domain | Task | " + " | ".join(active_groups) + " |")
    lines.append("|---|---|" + "---:|" * len(active_groups))
    by_task_group = {
        (str(row.get("task_id") or ""), str(row.get("group") or "")): row for row in final_rows
    }
    for task_id in sorted({key[0] for key in by_task_group}):
        exemplar = next(
            by_task_group[(task_id, group)]
            for group in active_groups
            if (task_id, group) in by_task_group
        )
        domain = str(exemplar.get("domain") or "Unknown")
        values = [
            Decimal(str(by_task_group[(task_id, group)]["quality_total"])).quantize(Decimal("0.01"))
            for group in active_groups
        ]
        lines.append(
            f"| {domain} | `{task_id[:8]}…` | " + " | ".join(str(value) for value in values) + " |"
        )
    lines.extend(
        [
            "",
            "## 同题配对比较",
            "",
            "| Group | Baseline | Pairs | ΔQ (95% CI) | W/T/L |",
            "|---|---|---:|---|---|",
        ]
    )
    if comparisons:
        for comparison in comparisons:
            lines.append(
                "| {group} | {baseline} | {pairs} | {delta:+.2f} "
                "[{low:+.2f}, {high:+.2f}] | {wins}/{ties}/{losses} |".format(
                    group=comparison["group"],
                    baseline=comparison["baseline"],
                    pairs=comparison["pair_count"],
                    delta=float(comparison["delta_quality"]),
                    low=float(comparison["ci95_low"]),
                    high=float(comparison["ci95_high"]),
                    wins=comparison["wins"],
                    ties=comparison["ties"],
                    losses=comparison["losses"],
                )
            )
    else:
        lines.append("| — | — | 0 | N/A | — |")
        lines.extend(
            [
                "",
                "- 本次活动组不包含 B0/B1 基线，无法进行同题配对比较。",
            ]
        )
    lines.extend(
        [
            "",
            "- CI 使用固定种子、20,000 次 paired bootstrap percentile 95% CI；"
            "W/T/L 为同题逐题比较。",
            "",
            "## Agent Loop 执行证据",
            "",
            "| Group | Avg selected iterations | Avg physical LLM req | Avg tools | Avg steps |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for metric in metrics:
        lines.append(
            "| {group} | {iterations:.2f} | {requests:.2f} | {tools:.2f} | {steps:.2f} |".format(
                group=metric["group"],
                iterations=float(metric["avg_selected_agent_iterations"]),
                requests=float(metric["avg_llm_requests"]),
                tools=float(metric["avg_tool_calls"]),
                steps=float(metric["avg_trajectory_steps"]),
            )
        )
    lines.extend(
        [
            "",
            "## 生成与 Judge 按模型统计",
            "",
            "| Phase | Model | Upstream provider/revision | Calls | Input | "
            "Output | Cost | Exact/Non-exact/Unknown | Roles |",
            "|---|---|---|---:|---:|---:|---:|---|---|",
        ]
    )
    for metric in model_metrics:
        upstream = (
            ", ".join(
                [
                    *metric["upstream_providers"],
                    *metric["upstream_models"],
                ]
            )
            or "—"
        )
        precision = (
            f"{metric['exact_request_count']}/"
            f"{metric['estimated_or_recorded_request_count']}/"
            f"{metric['unknown_request_count']}"
        )
        lines.append(
            "| {phase} | `{model}` | {upstream} | {calls} | {input} | "
            "{output} | ${cost} | {precision} | {roles} |".format(
                phase=metric["phase"],
                model=metric["model"],
                upstream=upstream,
                calls=metric["calls"],
                input=metric["input_tokens"],
                output=metric["output_tokens"],
                cost=metric["recorded_cost_usd"],
                precision=precision,
                roles=", ".join(metric["roles"]) or "—",
            )
        )
    lines.extend(
        [
            "",
            "## 成本口径",
            "",
            f"- Actual ledger generation："
            f"${scope_costs.get('generation', '0')}；"
            f"Judge：${judge_scope_cost}。",
            f"- Generation disposition 成本：selected="
            f"${disposition_costs.get('selected', '0')}，"
            f"replaced=${disposition_costs.get('replaced', '0')}，"
            f"failed=${disposition_costs.get('failed', '0')}。",
            "- † 上表成本只统计最终选中的成功 generation；不包含 Judge，也不包含"
            "失败或被替换的 generation attempt。多次重试只统计最终被采用的 attempt。",
            "- Selected generation 费用按固定优先级统计：先使用请求已有的美元费用；"
            "若没有美元费用但有 token usage，则使用仓库冻结价格按 input/output/"
            "cache-read/cache-write 分桶补算，并标记 estimated。冻结价格包含 cache "
            "rate 时采用 cache-aware 结果；缺少 cache rate 时保留 pricing engine 的 "
            "cache-blind 上界及其 basis。若补算后仍没有美元费用，则从金额小计中忽略。",
            f"- Selected generation 物理请求：{selected_cost_request_count}；"
            f"exact={selected_cost_exact_request_count}、estimated="
            f"{selected_cost_estimated_request_count}、recorded non-exact="
            f"{selected_cost_recorded_non_exact_request_count}、ignored="
            f"{selected_cost_ignored_request_count}。ignored 只表示未纳入小计，不是 "
            "$0；相应 task/group 仍保留 `complete=false` 与 "
            "`partial_excluding_unknown` 精度。",
            "- `actual-spend-ledger.jsonl` 从所有 wave 的 generation attempts "
            "与 Judge attempts 重建，"
            "失败或被替换 attempt 仍计入真实花费，复制到 repair row 的请求按物理回执去重。",
            f"- Campaign 物理 LLM 请求：{physical_request_count}；"
            f"exact={exact_request_count}、non-exact={non_exact_request_count}、"
            f"unknown={unknown_request_count}；"
            f"账本已记录成本：${ledger_summary['recorded_cost_usd']}；"
            f"exact 请求金额合计：${ledger_summary['exact_cost_usd']}。金额相同不表示"
            "所有请求都具备完整逐请求成本证据。",
            f"- Campaign-bound 分窗：{window_detail}。",
            f"- Campaign-bound OpenRouter account delta：${campaign_bound_total}；"
            f"归因精度：`{cost_scope['attribution_precision']}`。",
            f"- 纳入审计的账户窗口：{len(account_windows)}；其中 campaign-bound "
            f"{len(campaign_windows)} 个，counter 精确增量合计 ${campaign_bound_total}；"
            f"unallocated aborted {len(aborted_windows)} 个，增量合计 "
            f"${cost_scope['unallocated_aborted_window_usd']}；全部窗口合计 "
            f"${all_window_total}。",
            (
                "- Unallocated aborted 窗口没有正式 ledger 行与之绑定；其实际调用数未知，"
                "不能把“0 条正式 ledger 行”解释为“0 次调用”。"
                if aborted_windows
                else "- 没有 unallocated aborted 账户窗口。"
            ),
            (
                "- `results.jsonl` 的每个 source manifest 均绑定到且只绑定到一个 "
                "campaign account window；中止窗口不参与结果或 ledger 归因，"
                "窗口间 gap 不计费。"
                if result_window_scope == "campaign_windows"
                else "- `results.jsonl` 的任务行只绑定当前正式窗口；中止窗口成本仅在 "
                "campaign 级 proof/audit/manifest/report 中归档，窗口间 gap 不计费。"
            ),
            attribution_note,
            "",
            "## Non-BYOK 与 Web 成本说明",
            "",
            f"- OpenRouter BYOK 增量（Decimal 精确值）："
            f"{account.get('campaign_byok_usage_delta_usd', account['byok_usage_delta_usd'])}；"
            f"policy proof={'通过' if policy_pass else '未通过'}；"
            "本机锁不证明跨主机独占，BYOK/冲突证据会保留并使 proof.pass=false。",
            f"- 本地 exact non-BYOK 请求：{evidence['exact_non_byok_request_count']}；"
            f"由 campaign 账户证明覆盖的元数据不完整请求："
            f"{evidence['campaign_covered_unverified_request_count']}；明确 BYOK="
            f"{evidence['explicit_byok_request_count']}、冲突="
            f"{evidence['conflict_request_count']}。",
            f"- 任务内 Web/Brave 调用数："
            f"{external_tool_cost['task_generation_tool_call_count']}；"
            f"live wave preflight 额外调用数："
            f"{external_tool_cost['live_preflight_tool_call_count']}；"
            f"总调用数：{external_tool_cost['tool_call_count']}；"
            f"成本状态：{external_tool_cost['cost_status']}；"
            f"已知成本下界：${external_tool_cost['recorded_cost_usd_lower_bound']}；"
            f"可能未定价调用数上界："
            f"{external_tool_cost['potentially_unpriced_tool_call_count_upper_bound']}。",
            "- 任务内 Web/Brave 成本按 generation attempt ID 跨 wave 去重；"
            "每次 main/resume 的 live preflight 作为 campaign overhead 按 manifest "
            "单列。两者均与 OpenRouter LLM 账户增量严格分离，unknown 不会被报告"
            "为真实 $0。",
            "",
            "机器可审计详情见 `audit.json`、`manifest.json`、"
            "`openrouter-non-byok-campaign-proof.json` 与 `actual-spend-ledger.jsonl`。",
            "",
        ]
    )
    return "\n".join(lines)


def write_markdown(path: Path, value: str) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def publish_atomically(
    *,
    output_dir: Path,
    final_rows: Sequence[Mapping[str, Any]],
    traces: Sequence[Mapping[str, Any]],
    ledger_rows: Sequence[Mapping[str, Any]],
    ledger_summary: Mapping[str, Any],
    proof: Mapping[str, Any],
    audit: dict[str, Any],
    manifest_base: dict[str, Any],
    report_markdown: str,
) -> dict[str, Any]:
    output = output_dir.resolve(strict=False)
    if output.exists():
        raise FinalizationError(f"refusing to overwrite final output: {output}")
    parent = output.parent
    if not parent.is_dir():
        raise FinalizationError(f"final output parent does not exist: {parent}")
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=parent))
    staging.chmod(0o700)
    try:
        results_path = staging / "results.jsonl"
        trace_path = staging / "trace.jsonl"
        ledger_path = staging / "actual-spend-ledger.jsonl"
        proof_path = staging / "openrouter-non-byok-campaign-proof.json"
        audit_path = staging / "audit.json"
        report_path = staging / "EXPERIMENT_RESULTS.md"
        manifest_path = staging / "manifest.json"
        write_jsonl(results_path, final_rows)
        write_jsonl(trace_path, traces)
        write_jsonl(ledger_path, ledger_rows)
        write_json(proof_path, proof)
        audit["artifacts"] = {
            "results": artifact_record(results_path),
            "trace": artifact_record(trace_path),
            "actual_spend_ledger": artifact_record(ledger_path),
            "openrouter_non_byok_campaign_proof": artifact_record(proof_path),
        }
        audit["audit_sha256"] = canonical_sha256(audit, prefix=True)
        write_json(audit_path, audit)
        write_markdown(report_path, report_markdown)
        artifacts = {
            path.name: artifact_record(path)
            for path in (
                results_path,
                trace_path,
                ledger_path,
                proof_path,
                audit_path,
                report_path,
            )
        }
        manifest = {
            **manifest_base,
            "artifacts": artifacts,
            "ledger_summary": ledger_summary,
            "audit_sha256": audit["audit_sha256"],
        }
        manifest["manifest_sha256"] = canonical_sha256(manifest, prefix=True)
        write_json(manifest_path, manifest)
        # Sync directory entries before the one atomic publication rename.
        directory_fd = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.replace(staging, output)
        parent_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def final_audit(
    *,
    tasks: Sequence[Mapping[str, Any]],
    groups: Sequence[str] = GROUPS,
    rows: Sequence[Mapping[str, Any]],
    traces: Sequence[Mapping[str, Any]],
    ledger_summary: Mapping[str, Any],
    external_tool_cost: Mapping[str, Any],
    metrics: Sequence[Mapping[str, Any]],
    comparisons: Sequence[Mapping[str, Any]],
    model_metrics: Sequence[Mapping[str, Any]],
    rubric_metrics: Sequence[Mapping[str, Any]],
    repair_details: Sequence[Mapping[str, Any]],
    proof: Mapping[str, Any],
    pair_audit: Mapping[str, Any],
    selected_attempt_bindings: Mapping[str, str],
    selected_cost_reconciliation: Mapping[str, Any],
    judge_attempt_evidence_audit: Mapping[str, Any],
    max_attempts: int,
    experiment_policy: FinalizerExperimentPolicy | None = None,
    warnings: Sequence[Any] = (),
) -> dict[str, Any]:
    expected = {(group, str(task["id"])) for task in tasks for group in groups}
    observed = [(str(row.get("group") or ""), str(row.get("task_id") or "")) for row in rows]
    duplicate_count = len(observed) - len(set(observed))
    seal_failures = sum(not verify_result_row_evidence(row) for row in rows)
    trace_failures = sum(
        trace != trace_row_from_result(row) for row, trace in zip(rows, traces, strict=True)
    )
    attempt_violations = [
        f"{row.get('group')}/{row.get('task_id')}"
        for row in rows
        if nonnegative_int(row.get("generation_attempt_budget_used")) > max_attempts
    ]
    tasks_by_id = {str(task["id"]): task for task in tasks}
    strict_judge_failures = [
        f"{row.get('group')}/{row.get('task_id')}"
        for row in rows
        if judge_reasons(
            row,
            task=tasks_by_id.get(str(row.get("task_id") or "")),
            judge_model=(
                experiment_policy.judge_model if experiment_policy is not None else JUDGE_MODEL
            ),
            judge_repeats=(
                experiment_policy.judge_repeats if experiment_policy is not None else JUDGE_REPEATS
            ),
            judge_max_attempts=(
                experiment_policy.judge_max_attempts
                if experiment_policy is not None
                else JUDGE_ATTEMPT_BUDGET_LIMIT
            ),
            judge_provider_pin=(
                experiment_policy.judge_provider_pin if experiment_policy is not None else None
            ),
        )
    ]
    execution_pass = (
        set(observed) == expected
        and len(rows) == len(expected)
        and duplicate_count == 0
        and seal_failures == 0
        and trace_failures == 0
        and not attempt_violations
        and nonnegative_int(ledger_summary.get("physical_request_count")) > 0
        and ledger_summary.get("selected_generation_pair_count") == len(expected)
        and len(selected_attempt_bindings) == len(expected)
        and not strict_judge_failures
    )
    policy_pass = proof.get("policy_pass") is True
    reconciliation = dict(proof.get("reconciliation") or {})
    reconciliation_pass = reconciliation.get("pass") is True
    passed = (
        execution_pass
        and policy_pass
        and reconciliation_pass
        and selected_cost_reconciliation.get("pair_count") == len(expected)
        and not strict_judge_failures
    )
    published_warnings: list[str] = []
    for warning in warnings:
        if isinstance(warning, Mapping):
            published_warnings.append(json.dumps(warning, ensure_ascii=False, sort_keys=True))
        else:
            published_warnings.append(str(warning))
    published_warnings.extend(str(value) for value in proof.get("warnings") or [])
    published_warnings.extend(
        str(value) for value in selected_cost_reconciliation.get("warnings") or []
    )
    for pair, selection in pair_audit.items():
        warning_rows = selection.get("warnings") if isinstance(selection, Mapping) else None
        for warning_row in warning_rows if isinstance(warning_rows, list) else []:
            if not isinstance(warning_row, Mapping):
                continue
            published_warnings.extend(
                f"{pair}: {reason}" for reason in warning_row.get("reasons") or []
            )
    if not policy_pass:
        published_warnings.append("OpenRouter non-BYOK policy proof did not pass")
    if not reconciliation_pass:
        published_warnings.append(
            f"cost/account reconciliation is not exact: {reconciliation.get('status') or 'unknown'}"
        )
    if strict_judge_failures:
        published_warnings.append(f"Judge contract failures: {strict_judge_failures}")
    published_warnings = list(dict.fromkeys(published_warnings))
    audit = {
        "schema": AUDIT_SCHEMA,
        "pass": passed,
        "execution_pass": execution_pass,
        "policy_pass": policy_pass,
        "reconciliation": reconciliation,
        "status": (
            "passed"
            if passed
            else "complete_with_warnings"
            if execution_pass
            else "execution_failed"
        ),
        "warnings": published_warnings,
        "created_at": utc_now(),
        "groups": list(groups),
        "task_count": len(tasks),
        "expected_result_count": len(expected),
        "result_count": len(rows),
        "unique_pair_count": len(set(observed)),
        "missing_pairs": sorted([list(key) for key in expected - set(observed)]),
        "unexpected_pairs": sorted([list(key) for key in set(observed) - expected]),
        "duplicate_pair_count": duplicate_count,
        "sealed_result_failure_count": seal_failures,
        "trace_projection_failure_count": trace_failures,
        "generation_attempt_limit": max_attempts,
        "generation_attempt_limit_violations": attempt_violations,
        "judge_complete_count": len(rows) - len(strict_judge_failures),
        "judge_contract_failure_pairs": strict_judge_failures,
        "judge_attempt_evidence": dict(judge_attempt_evidence_audit),
        "selected_generation_attempt_bindings": dict(sorted(selected_attempt_bindings.items())),
        "selected_generation_cost_reconciliation": dict(selected_cost_reconciliation),
        "openrouter_non_byok_campaign_proof_sha256": proof.get("proof_sha256"),
        "account_windows": proof.get("account_windows"),
        "account_window_total_usd": proof.get("account_window_total_usd"),
        "result_row_account_window_scope": proof.get("result_row_account_window_scope"),
        "campaign_bound_account_window_total_usd": proof.get("cost_scope", {}).get(
            "campaign_bound_account_window_total_usd"
        ),
        "all_account_window_total_usd": proof.get("cost_scope", {}).get(
            "all_account_window_total_usd"
        ),
        "ledger_window_reconciliation": proof.get("cost_scope", {}).get(
            "ledger_window_reconciliation"
        ),
        "unallocated_aborted_window_usd": proof.get("unallocated_aborted_window_usd"),
        "physical_request_count": ledger_summary.get("physical_request_count"),
        "external_tool_cost": dict(external_tool_cost),
        "selected_generation_cost": {
            "all_groups_complete": all(
                metric.get("selected_generation_cost_complete") is True for metric in metrics
            ),
            "groups": [dict(metric) for metric in metrics],
            "unknown_costs_are_zero": False,
            "account_delta_allocated_to_tasks": False,
            "account_window_delta_usd": proof.get("cost_scope", {}).get("account_window_delta_usd"),
            "campaign_bound_account_window_total_usd": proof.get("cost_scope", {}).get(
                "campaign_bound_account_window_total_usd"
            ),
            "unallocated_account_window_total_usd": proof.get("cost_scope", {}).get(
                "unallocated_account_window_total_usd"
            ),
            "all_account_window_total_usd": proof.get("cost_scope", {}).get(
                "all_account_window_total_usd"
            ),
            "account_windows": proof.get("cost_scope", {}).get("account_windows"),
            "account_window_total_usd": proof.get("cost_scope", {}).get("account_window_total_usd"),
            "unallocated_aborted_window_usd": proof.get("cost_scope", {}).get(
                "unallocated_aborted_window_usd"
            ),
            "attribution_precision": proof.get("cost_scope", {}).get("attribution_precision"),
            "campaign_attributable_exact": proof.get("cost_scope", {}).get(
                "campaign_attributable_exact"
            ),
        },
        "paired_quality_comparisons": [dict(comparison) for comparison in comparisons],
        "model_provider_metrics": [dict(metric) for metric in model_metrics],
        "rubric_section_metrics": [dict(metric) for metric in rubric_metrics],
        "repair_action_details": [dict(detail) for detail in repair_details],
        "pair_selection": pair_audit,
    }
    if not execution_pass:
        raise FinalizationError(f"final campaign audit failed: {audit}")
    return audit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--result",
        type=Path,
        action="append",
        required=True,
        help="Sealed result JSONL, repeated in chronological wave order.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        action="append",
        required=True,
        help="Terminal source manifest; all manifests must bind the same contracts.",
    )
    parser.add_argument("--account-before", type=Path, required=True)
    parser.add_argument("--account-after", type=Path, required=True)
    parser.add_argument("--account-reconciliation", type=Path, required=True)
    parser.add_argument(
        "--prior-account-window-dir",
        type=Path,
        action="append",
        default=[],
        help=(
            "Archived prior aborted account window directory containing before, "
            "stable-after, reconciliation, and runtime-environment evidence."
        ),
    )
    parser.add_argument(
        "--campaign-account-window-dir",
        type=Path,
        action="append",
        default=[],
        help=(
            "Archived prior source-bound campaign account window directory "
            "containing before, stable-after, reconciliation, and "
            "runtime-environment evidence."
        ),
    )
    parser.add_argument("--runtime-environment", type=Path, required=True)
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--lock-fd", type=int, default=9)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--groups", default=",".join(GROUPS))
    parser.add_argument(
        "--max-generation-attempts",
        type=int,
        default=None,
        help=(
            "Optional assertion; when supplied it must equal the generation "
            "attempt limit authenticated by the run contracts."
        ),
    )
    parser.add_argument(
        "--expected-task-concurrency",
        type=int,
        default=FORMAL_TASK_CONCURRENCY,
        help="Expected generation task concurrency recorded by every source manifest.",
    )
    parser.add_argument(
        "--expected-judge-concurrency",
        type=int,
        default=FORMAL_JUDGE_CONCURRENCY,
        help="Expected Judge concurrency recorded by every source manifest.",
    )
    return parser


def run_finalization(args: argparse.Namespace) -> dict[str, Any]:
    groups = normalize_groups(args.groups)
    expected_task_concurrency = getattr(
        args,
        "expected_task_concurrency",
        FORMAL_TASK_CONCURRENCY,
    )
    if (
        isinstance(expected_task_concurrency, bool)
        or not isinstance(expected_task_concurrency, int)
        or expected_task_concurrency < 1
    ):
        raise FinalizationError("expected task concurrency must be a positive integer")
    expected_judge_concurrency = getattr(
        args,
        "expected_judge_concurrency",
        FORMAL_JUDGE_CONCURRENCY,
    )
    if (
        isinstance(expected_judge_concurrency, bool)
        or not isinstance(expected_judge_concurrency, int)
        or expected_judge_concurrency < 1
    ):
        raise FinalizationError("expected judge concurrency must be a positive integer")
    input_path = require_regular_file(args.input, owner_only=False)
    tasks = read_tasks(input_path)
    frozen_input_sha256 = validate_frozen_draco_input(input_path, tasks)
    source_records, source_snapshots = read_source_rows(args.result)
    unexpected_source_groups = sorted(
        {record.key[0] for record in source_records if record.key[0] not in set(groups)}
    )
    if unexpected_source_groups:
        raise FinalizationError(
            "result sources contain groups outside the active finalization scope: "
            f"{unexpected_source_groups}"
        )
    source_policy_findings = validate_source_policy_history(source_records)
    finalization_warnings: list[Any] = [
        {"kind": "source_policy_finding", **finding} for finding in source_policy_findings
    ]
    critical_source_snapshots = dict(source_snapshots)
    for raw_path in (
        input_path,
        *args.manifest,
        args.account_before,
        args.account_after,
        args.account_reconciliation,
        args.runtime_environment,
    ):
        source_path = require_regular_file(Path(raw_path), owner_only=False)
        critical_source_snapshots[str(source_path)] = file_sha256(source_path)
    prior_account_window_dirs = list(getattr(args, "prior_account_window_dir", ()) or ())
    prior_campaign_account_window_dirs = list(
        getattr(args, "campaign_account_window_dir", ()) or ()
    )
    for prior_dir in (*prior_account_window_dirs, *prior_campaign_account_window_dirs):
        for name in (
            "openrouter-account-before.json",
            "openrouter-account-after.json",
            "openrouter-account-reconciliation.json",
            "runtime-environment.json",
        ):
            source_path = require_regular_file(Path(prior_dir) / name, owner_only=True)
            critical_source_snapshots[str(source_path)] = file_sha256(source_path)
    fingerprints, contracts, runtime_key, manifest_sources = load_manifest_contracts(
        args.manifest,
        result_paths=args.result,
        groups=groups,
        expected_task_concurrency=expected_task_concurrency,
        expected_judge_concurrency=expected_judge_concurrency,
    )
    finalization_warnings.extend(
        {
            "kind": "source_manifest_audit_warning",
            "path": source.get("path"),
            "warning": warning,
        }
        for source in manifest_sources
        for warning in source.get("audit_warnings") or []
    )
    experiment_policy = validate_formal_campaign_contracts(
        contracts,
        groups=groups,
    )
    max_generation_attempts = authenticated_generation_attempt_limit(
        getattr(args, "max_generation_attempts", None),
        experiment_policy,
    )
    attempt_evidence_audit = validate_generation_attempt_evidence(
        source_records,
        max_attempts=max_generation_attempts,
    )
    if "G1" in groups:
        validate_g1_paid_attempt_plan_history(
            source_records,
            contracts=contracts,
        )
    finalization_warnings.extend(
        {
            "kind": "physical_generation_audit_warning",
            **warning,
        }
        for warning in validate_physical_generation_routes(
            source_records,
            contracts=contracts,
        )
    )
    try:
        judge_attempt_evidence_audit = validate_judge_attempt_evidence(
            source_records,
            judge_model=experiment_policy.judge_model,
            judge_max_attempts=experiment_policy.judge_max_attempts,
            judge_provider_pin=experiment_policy.judge_provider_pin,
        )
    except FinalizationError as exc:
        if not judge_evidence_error_is_audit_only(exc):
            raise
        judge_attempt_evidence_audit = {
            "status": "audit_conflict",
            "pass": False,
            "warning": str(exc),
        }
        finalization_warnings.append({"kind": "judge_attempt_audit_conflict", "warning": str(exc)})
    selected, pair_audit = select_results(
        source_records,
        tasks=tasks,
        groups=groups,
        fingerprints=fingerprints,
        contracts=contracts,
        max_attempts=max_generation_attempts,
        experiment_policy=experiment_policy,
        manifest_sources=manifest_sources,
    )
    selected_attempt_bindings = bind_selected_generation_attempts(
        source_records,
        selected,
    )
    for pair, attempt_id in selected_attempt_bindings.items():
        pair_audit[pair]["selected_generation_attempt_id"] = attempt_id
    ledger_rows, ledger_summary = build_actual_spend_ledger(
        source_records,
        selected=selected,
        selected_attempt_bindings=selected_attempt_bindings,
        judge_model=experiment_policy.judge_model,
    )
    attach_retrospective_recovery_spend(pair_audit, ledger_rows)
    model_metrics = ledger_model_metrics(ledger_rows)
    external_tool_cost = build_external_tool_cost_summary(
        source_records,
        manifest_sources=manifest_sources,
    )
    try:
        proof = validate_account_proof(
            before_path=args.account_before,
            after_path=args.account_after,
            reconciliation_path=args.account_reconciliation,
            runtime_environment_path=args.runtime_environment,
            lock_file=args.lock_file,
            lock_fd=args.lock_fd,
            runtime_key_fingerprint=runtime_key,
            source_records=source_records,
            manifest_sources=manifest_sources,
            ledger_rows=ledger_rows,
            ledger_summary=ledger_summary,
            prior_account_window_dirs=prior_account_window_dirs,
            prior_campaign_account_window_dirs=prior_campaign_account_window_dirs,
        )
    except FinalizationError as exc:
        if not account_proof_error_is_audit_only(exc):
            raise
        proof = failed_account_proof(
            error=exc,
            runtime_key_fingerprint=runtime_key,
            ledger_rows=ledger_rows,
            ledger_summary=ledger_summary,
        )
        finalization_warnings.append({"kind": "account_proof_audit_conflict", "warning": str(exc)})
    inherited_policy_warnings = [
        *(
            "source history policy finding: "
            + json.dumps(finding, ensure_ascii=False, sort_keys=True)
            for finding in source_policy_findings
        ),
        *(
            f"source manifest policy finding: {warning}"
            for source in manifest_sources
            for warning in source.get("audit_warnings") or []
            if "byok" in str(warning).casefold()
            or "non_byok_policy_violation" in str(warning).casefold()
        ),
    ]
    if inherited_policy_warnings:
        proof = copy.deepcopy(proof)
        proof.pop("proof_sha256", None)
        proof["pass"] = False
        proof["policy_pass"] = False
        proof["status"] = "policy_failed"
        proof["warnings"] = list(
            dict.fromkeys(
                [
                    *(str(value) for value in proof.get("warnings") or []),
                    *inherited_policy_warnings,
                ]
            )
        )
        proof["proof_sha256"] = canonical_sha256(proof, prefix=True)
    inherited_reconciliation_warnings = [
        f"source manifest reconciliation finding: {warning}"
        for source in manifest_sources
        for warning in source.get("audit_warnings") or []
        if "cost" in str(warning).casefold()
    ]
    if inherited_reconciliation_warnings:
        proof = copy.deepcopy(proof)
        proof.pop("proof_sha256", None)
        proof["reconciliation"] = {
            **dict(proof.get("reconciliation") or {}),
            "pass": False,
            "status": "audit_conflict",
        }
        if proof.get("policy_pass") is True:
            proof["status"] = "reconciliation_incomplete"
        proof["warnings"] = list(
            dict.fromkeys(
                [
                    *(str(value) for value in proof.get("warnings") or []),
                    *inherited_reconciliation_warnings,
                ]
            )
        )
        proof["proof_sha256"] = canonical_sha256(proof, prefix=True)
    final_rows = finalize_rows(
        selected,
        tasks=tasks,
        proof=proof,
        pair_audit=pair_audit,
        groups=groups,
    )
    traces = [trace_row_from_result(row) for row in final_rows]
    selected_costs, selected_cost_reconciliation = selected_generation_costs_from_ledger(
        final_rows, ledger_rows
    )
    metrics = group_metrics(
        final_rows,
        selected_costs_by_pair=selected_costs,
        groups=groups,
    )
    comparisons = paired_quality_comparisons(final_rows, groups=groups)
    rubric_metrics = rubric_section_metrics(final_rows)
    repair_details = repair_action_details(final_rows)
    audit = final_audit(
        tasks=tasks,
        groups=groups,
        rows=final_rows,
        traces=traces,
        ledger_summary=ledger_summary,
        external_tool_cost=external_tool_cost,
        metrics=metrics,
        comparisons=comparisons,
        model_metrics=model_metrics,
        rubric_metrics=rubric_metrics,
        repair_details=repair_details,
        proof=proof,
        pair_audit=pair_audit,
        selected_attempt_bindings=selected_attempt_bindings,
        selected_cost_reconciliation=selected_cost_reconciliation,
        judge_attempt_evidence_audit=judge_attempt_evidence_audit,
        max_attempts=max_generation_attempts,
        experiment_policy=experiment_policy,
        warnings=finalization_warnings,
    )
    audit["generation_attempt_evidence_schema"] = GENERATION_ATTEMPT_EVIDENCE_SCHEMA
    audit["generation_attempt_evidence"] = attempt_evidence_audit
    audit["judge_attempt_evidence_schema"] = JUDGE_ATTEMPT_EVIDENCE_SCHEMA
    audit["frozen_draco_mini_input"] = {
        "sha256": frozen_input_sha256,
        "task_count": len(tasks),
        "task_ids": [str(task["id"]) for task in tasks],
    }
    verify_source_snapshots(critical_source_snapshots)
    report = experiment_results_markdown(
        task_count=len(tasks),
        groups=groups,
        final_rows=final_rows,
        metrics=metrics,
        ledger_summary=ledger_summary,
        external_tool_cost=external_tool_cost,
        proof=proof,
        comparisons=comparisons,
        model_metrics=model_metrics,
        rubric_metrics=rubric_metrics,
        repair_details=repair_details,
        experiment_policy=experiment_policy,
        expected_task_concurrency=expected_task_concurrency,
        expected_judge_concurrency=expected_judge_concurrency,
    )
    manifest_base = {
        "schema": MANIFEST_SCHEMA,
        "status": "complete" if audit["execution_pass"] else "execution_failed",
        "execution_pass": audit["execution_pass"],
        "policy_pass": audit["policy_pass"],
        "reconciliation": audit["reconciliation"],
        "audit_pass": audit["pass"],
        "audit_status": audit["status"],
        "warnings": audit["warnings"],
        "created_at": utc_now(),
        "finalizer_version": FINALIZER_VERSION,
        "groups": list(groups),
        "task_ids": [str(task["id"]) for task in tasks],
        "task_count": len(tasks),
        "result_count": len(final_rows),
        "input": {
            "path": str(input_path),
            "sha256": frozen_input_sha256,
            "frozen_expected_sha256": FROZEN_DRACO_MINI_SHA256,
            "frozen_expected_task_count": FROZEN_DRACO_MINI_TASK_COUNT,
        },
        "source_results": [
            {
                "path": path,
                "sha256": digest,
            }
            for path, digest in source_snapshots.items()
        ],
        "source_manifests": manifest_sources,
        "run_compatibility_fingerprints": fingerprints,
        "max_generation_attempts": max_generation_attempts,
        "experiment_policy": {
            "global_experiment_profile": experiment_policy.profile,
            "aggregator_recovery": experiment_policy.aggregator_recovery,
            "proposer_recovery": experiment_policy.proposer_recovery,
            "judge_provider_pin": experiment_policy.judge_provider_pin,
            "task_concurrency": expected_task_concurrency,
            "judge_concurrency": expected_judge_concurrency,
        },
        "generation_attempt_evidence_schema": GENERATION_ATTEMPT_EVIDENCE_SCHEMA,
        "judge_attempt_evidence_schema": JUDGE_ATTEMPT_EVIDENCE_SCHEMA,
        "judge_attempt_evidence": judge_attempt_evidence_audit,
        "selected_generation_attempt_bindings": dict(sorted(selected_attempt_bindings.items())),
        "selected_generation_cost_reconciliation": (selected_cost_reconciliation),
        "group_metrics": metrics,
        "rubric_section_metrics": rubric_metrics,
        "repair_action_details": repair_details,
        "paired_quality_comparisons": comparisons,
        "model_provider_metrics": model_metrics,
        "external_tool_cost": external_tool_cost,
        "openrouter_non_byok_campaign_proof_sha256": proof["proof_sha256"],
        "account_windows": proof["account_windows"],
        "result_row_account_window_scope": proof["result_row_account_window_scope"],
        "account_window_total_usd": proof["account_window_total_usd"],
        "unallocated_aborted_window_usd": proof["unallocated_aborted_window_usd"],
        "cost_attribution": {
            "account_window_delta_usd": proof["cost_scope"]["account_window_delta_usd"],
            "campaign_bound_account_window_total_usd": proof["cost_scope"][
                "campaign_bound_account_window_total_usd"
            ],
            "unallocated_account_window_total_usd": proof["cost_scope"][
                "unallocated_account_window_total_usd"
            ],
            "all_account_window_total_usd": proof["cost_scope"]["all_account_window_total_usd"],
            "account_windows": proof["cost_scope"]["account_windows"],
            "account_window_total_usd": proof["cost_scope"]["account_window_total_usd"],
            "unallocated_aborted_window_usd": proof["cost_scope"]["unallocated_aborted_window_usd"],
            "attribution_precision": proof["cost_scope"]["attribution_precision"],
            "ledger_window_reconciliation": proof["cost_scope"]["ledger_window_reconciliation"],
            "campaign_attributable_exact": proof["cost_scope"]["campaign_attributable_exact"],
            "campaign_attributable_cost_usd": proof["cost_scope"]["campaign_attributable_cost_usd"],
            "account_delta_allocated_to_tasks": False,
        },
    }
    return publish_atomically(
        output_dir=args.output_dir,
        final_rows=final_rows,
        traces=traces,
        ledger_rows=ledger_rows,
        ledger_summary=ledger_summary,
        proof=proof,
        audit=audit,
        manifest_base=manifest_base,
        report_markdown=report,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manifest = run_finalization(args)
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "output_dir": str(args.output_dir.resolve()),
                "result_count": manifest["result_count"],
                "manifest_sha256": manifest["manifest_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
