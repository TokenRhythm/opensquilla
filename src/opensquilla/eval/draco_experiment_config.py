"""Strict, composable configuration for reproducible DRACO experiments."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FORMAL_DRACO_OPENROUTER_PROVIDER = "openrouter"
FORMAL_DRACO_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
FORMAL_DRACO_OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
FROZEN_TASK_ANALYSIS_SCHEMA = "opensquilla.draco.frozen-task-analysis/v1"
FROZEN_TASK_ANALYSIS_SCHEMA_V2 = "opensquilla.draco.frozen-task-analysis/v2"
FROZEN_TASK_ANALYSIS_SCHEMA_V3 = "opensquilla.draco.frozen-task-analysis/v3"
FROZEN_TASK_ANALYSIS_MODE = "frozen_replay"
FORMAL_DRACO_WEB_SEARCH_API_KEY_ENVS = {
    "brave": "BRAVE_SEARCH_API_KEY",
    "duckduckgo": "",
}


class _StrictConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class DracoReferenceConfig(_StrictConfig):
    repository: str = Field(min_length=1)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    run_directory: str = Field(min_length=1)
    group: str = Field(min_length=1)
    profile: str = Field(min_length=1)


class DracoBenchmarkInputConfig(_StrictConfig):
    name: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_count: int = Field(gt=0)
    task_ids: list[str] = Field(min_length=1)
    enforce_reference_input: bool = True

    @model_validator(mode="after")
    def _validate_tasks(self) -> DracoBenchmarkInputConfig:
        if len(self.task_ids) != self.task_count:
            raise ValueError("benchmark_input.task_ids must match task_count")
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("benchmark_input.task_ids must be unique")
        return self


class DracoRoutingConfig(_StrictConfig):
    selection_mode: Literal["static_openrouter_b5"]
    skip_single_model_router: bool


def _canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class DracoFrozenTaskAnalyzerTraceConfig(_StrictConfig):
    """Public Analyzer provenance retained by a zero-request profile replay."""

    source: Literal["frozen_replay"]
    schema_valid: Literal[True]
    confidence: float = Field(ge=0.0, le=1.0)
    analyzer_version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    fallback_reason: Literal[""]
    usage: dict[str, Any]
    normalization_warnings: list[str]

    @model_validator(mode="after")
    def _validate_zero_request_trace(self) -> DracoFrozenTaskAnalyzerTraceConfig:
        if self.usage:
            raise ValueError("frozen task analyzer replay usage must be empty")
        if any(not value.strip() for value in self.normalization_warnings):
            raise ValueError("frozen task analyzer warnings must be non-empty strings")
        if len(set(self.normalization_warnings)) != len(self.normalization_warnings):
            raise ValueError("frozen task analyzer warnings must be unique")
        return self


class DracoFrozenTaskAnalysisEntryConfig(_StrictConfig):
    task_input_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_profile_pre_escalation: dict[str, Any]
    task_profile_pre_escalation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_analyzer: DracoFrozenTaskAnalyzerTraceConfig

    @model_validator(mode="after")
    def _validate_profile_hash(self) -> DracoFrozenTaskAnalysisEntryConfig:
        if not self.task_profile_pre_escalation:
            raise ValueError("frozen pre-escalation task profile must be non-empty")
        if (
            _canonical_json_sha256(self.task_profile_pre_escalation)
            != self.task_profile_pre_escalation_sha256
        ):
            raise ValueError(
                "frozen task_profile_pre_escalation_sha256 does not match "
                "task_profile_pre_escalation"
            )
        return self


class DracoFrozenTaskAnalysisExecutionConfig(_StrictConfig):
    """Ten inline E0 Analyzer outputs reused without another physical request."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        serialize_by_alias=True,
    )

    schema_id: Literal["opensquilla.draco.frozen-task-analysis/v1"] = Field(alias="schema")
    mode: Literal["frozen_replay"]
    source_experiment: str = Field(min_length=1)
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_results_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_task_analyzer_config: dict[str, Any]
    source_task_analyzer_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: dict[str, DracoFrozenTaskAnalysisEntryConfig]
    entries_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_bundle_hashes(self) -> DracoFrozenTaskAnalysisExecutionConfig:
        if len(self.entries) != 10 or any(
            not task_id or task_id != task_id.strip() for task_id in self.entries
        ):
            raise ValueError("frozen task analysis must contain exactly 10 canonical task ids")
        if not self.source_experiment.strip():
            raise ValueError("source_experiment must be a non-empty canonical id")
        if not self.source_task_analyzer_config:
            raise ValueError("source_task_analyzer_config must be non-empty")
        if (
            _canonical_json_sha256(self.source_task_analyzer_config)
            != self.source_task_analyzer_config_sha256
        ):
            raise ValueError("source_task_analyzer_config_sha256 does not match")
        entries = {
            task_id: entry.model_dump(mode="json") for task_id, entry in self.entries.items()
        }
        if _canonical_json_sha256(entries) != self.entries_sha256:
            raise ValueError("frozen task analysis entries_sha256 does not match entries")
        return self


class DracoFrozenTaskAnalyzerTraceV2Config(_StrictConfig):
    """Replay provenance that preserves the authenticated live-source outcome."""

    source: Literal["frozen_replay"]
    schema_valid: bool
    confidence: float = Field(ge=0.0, le=1.0)
    analyzer_version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    fallback_reason: str
    usage: dict[str, Any]
    normalization_warnings: list[str]

    @model_validator(mode="after")
    def _validate_zero_request_trace(self) -> DracoFrozenTaskAnalyzerTraceV2Config:
        if self.usage:
            raise ValueError("frozen task analyzer replay usage must be empty")
        if any(not value.strip() for value in self.normalization_warnings):
            raise ValueError("frozen task analyzer warnings must be non-empty strings")
        if len(set(self.normalization_warnings)) != len(self.normalization_warnings):
            raise ValueError("frozen task analyzer warnings must be unique")
        return self


class DracoFrozenTaskAnalysisEntryV2Config(_StrictConfig):
    task_input_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_profile_pre_escalation: dict[str, Any]
    task_profile_pre_escalation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    origin_outcome: Literal["live_success", "deterministic_router_fallback"]
    task_analyzer: DracoFrozenTaskAnalyzerTraceV2Config

    @model_validator(mode="after")
    def _validate_profile_and_outcome(self) -> DracoFrozenTaskAnalysisEntryV2Config:
        if not self.task_profile_pre_escalation:
            raise ValueError("frozen pre-escalation task profile must be non-empty")
        if (
            _canonical_json_sha256(self.task_profile_pre_escalation)
            != self.task_profile_pre_escalation_sha256
        ):
            raise ValueError(
                "frozen task_profile_pre_escalation_sha256 does not match "
                "task_profile_pre_escalation"
            )
        if self.origin_outcome == "live_success":
            if self.task_analyzer.schema_valid is not True or self.task_analyzer.fallback_reason:
                raise ValueError("live-success replay provenance is contradictory")
        elif (
            self.task_analyzer.schema_valid is not False
            or not self.task_analyzer.fallback_reason.strip()
        ):
            raise ValueError("router-fallback replay provenance is contradictory")
        return self


class DracoFrozenTaskAnalysisExecutionV2Config(_StrictConfig):
    """V2 replay bundle preserving deterministic router-fallback provenance."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        serialize_by_alias=True,
    )

    schema_id: Literal["opensquilla.draco.frozen-task-analysis/v2"] = Field(alias="schema")
    mode: Literal["frozen_replay"]
    source_experiment: str = Field(min_length=1)
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_results_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_task_analyzer_config: dict[str, Any]
    source_task_analyzer_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: dict[str, DracoFrozenTaskAnalysisEntryV2Config]
    entries_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_bundle_hashes(self) -> DracoFrozenTaskAnalysisExecutionV2Config:
        if len(self.entries) != 10 or any(
            not task_id or task_id != task_id.strip() for task_id in self.entries
        ):
            raise ValueError("frozen task analysis must contain exactly 10 canonical task ids")
        if not self.source_experiment.strip():
            raise ValueError("source_experiment must be a non-empty canonical id")
        if not self.source_task_analyzer_config:
            raise ValueError("source_task_analyzer_config must be non-empty")
        if (
            _canonical_json_sha256(self.source_task_analyzer_config)
            != self.source_task_analyzer_config_sha256
        ):
            raise ValueError("source_task_analyzer_config_sha256 does not match")
        entries = {
            task_id: entry.model_dump(mode="json") for task_id, entry in self.entries.items()
        }
        if _canonical_json_sha256(entries) != self.entries_sha256:
            raise ValueError("frozen task analysis entries_sha256 does not match entries")
        return self


class DracoFrozenTaskAnalysisExecutionV3Config(
    DracoFrozenTaskAnalysisExecutionV2Config
):
    """V3 additionally authenticates the live Analyzer execution chain."""

    schema_id: Literal["opensquilla.draco.frozen-task-analysis/v3"] = Field(
        alias="schema"
    )
    source_task_analyzer_execution_contract: dict[str, Any]
    source_task_analyzer_execution_contract_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_execution_contract(
        self,
    ) -> DracoFrozenTaskAnalysisExecutionV3Config:
        from opensquilla.eval.draco_task_analyzer_execution import (
            task_analyzer_execution_contract_matches_source,
            validated_task_analyzer_execution_contract,
        )

        if (
            validated_task_analyzer_execution_contract(
                self.source_task_analyzer_execution_contract
            )
            is None
            or not task_analyzer_execution_contract_matches_source(
                self.source_task_analyzer_execution_contract,
                self.source_task_analyzer_config,
            )
            or _canonical_json_sha256(
                self.source_task_analyzer_execution_contract
            )
            != self.source_task_analyzer_execution_contract_sha256
        ):
            raise ValueError(
                "source_task_analyzer_execution_contract is invalid"
            )
        return self


class DracoTaskAnalyzerRouteConfig(_StrictConfig):
    """One explicitly pinned OpenRouter route in the live Analyzer chain."""

    provider: Literal["openrouter"] = "openrouter"
    model: str = Field(
        min_length=3,
        pattern=r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:/-]*$",
    )
    upstream_provider: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    max_attempts: Literal[1] = 1


class DracoAnalyzerFailureFallbackConfig(_StrictConfig):
    """Fixed ensemble used only after every live Analyzer route fails."""

    proposers: list[DracoTaskAnalyzerRouteConfig] = Field(
        min_length=4,
        max_length=4,
    )
    aggregator: DracoTaskAnalyzerRouteConfig
    min_successful_proposers: Literal[1] = 1
    complete_proposers_only: Literal[True] = True
    aggregator_max_recovery_actions: Literal[1] = 1

    @model_validator(mode="after")
    def _validate_unique_proposers(self) -> DracoAnalyzerFailureFallbackConfig:
        identities = [(route.provider, route.model) for route in self.proposers]
        if len(set(identities)) != len(identities):
            raise ValueError(
                "analyzer_failure_fallback_ensemble.proposers must not contain duplicate routes"
            )
        return self


class DracoG1RoutingConfig(_StrictConfig):
    """Versioned, fail-closed candidate contract for the formal G1 router."""

    profile_id: str = Field(min_length=1)
    selection_mode: Literal["router_dynamic"]
    user_profile_enabled: Literal[False]
    source_registry_snapshot_version: str = Field(min_length=1)
    expected_source_registry_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_ranking_config_schema_version: str = Field(min_length=1)
    expected_ranking_config_version: str = Field(min_length=1)
    expected_ranking_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_proposer_count_max: int = Field(gt=0)
    expected_candidate_count: int | None = Field(default=None, gt=0)
    expected_routes: dict[str, str] | None = None
    expected_routes_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    task_analysis_execution: (
        DracoFrozenTaskAnalysisExecutionConfig
        | DracoFrozenTaskAnalysisExecutionV2Config
        | DracoFrozenTaskAnalysisExecutionV3Config
        | None
    ) = None
    live_task_analyzer_chain: list[DracoTaskAnalyzerRouteConfig] | None = Field(
        default=None,
        min_length=1,
    )
    analyzer_failure_fallback_ensemble: DracoAnalyzerFailureFallbackConfig | None = None

    @model_validator(mode="after")
    def _validate_live_task_analyzer_chain(self) -> DracoG1RoutingConfig:
        has_live_chain = self.live_task_analyzer_chain is not None
        has_failure_fallback = self.analyzer_failure_fallback_ensemble is not None
        if has_live_chain is not has_failure_fallback:
            raise ValueError(
                "g1_routing live_task_analyzer_chain and "
                "analyzer_failure_fallback_ensemble must be configured together"
            )
        if not has_live_chain:
            return self
        assert self.live_task_analyzer_chain is not None
        identities = [(route.provider, route.model) for route in self.live_task_analyzer_chain]
        if len(set(identities)) != len(identities):
            raise ValueError(
                "g1_routing.live_task_analyzer_chain must not contain duplicate routes"
            )
        return self

    @model_validator(mode="after")
    def _validate_expected_routes(self) -> DracoG1RoutingConfig:
        exact_fields = (
            self.expected_candidate_count,
            self.expected_routes,
            self.expected_routes_sha256,
        )
        if all(value is None for value in exact_fields):
            return self
        if any(value is None for value in exact_fields):
            raise ValueError(
                "g1_routing expected_candidate_count, expected_routes, and "
                "expected_routes_sha256 must be specified together"
            )
        assert self.expected_candidate_count is not None
        assert self.expected_routes is not None
        assert self.expected_routes_sha256 is not None
        if self.expected_proposer_count_max > self.expected_candidate_count:
            raise ValueError(
                "g1_routing.expected_proposer_count_max exceeds expected_candidate_count"
            )
        if len(self.expected_routes) != self.expected_candidate_count:
            raise ValueError("g1_routing.expected_routes must match expected_candidate_count")
        for model, upstream_provider in self.expected_routes.items():
            if model != model.strip().lower() or "/" not in model:
                raise ValueError(
                    "g1_routing.expected_routes model ids must be lowercase, trimmed OpenRouter ids"
                )
            if upstream_provider != upstream_provider.strip().lower():
                raise ValueError(
                    "g1_routing.expected_routes provider slugs must be lowercase and trimmed"
                )
        actual_hash = _canonical_json_sha256(self.expected_routes)
        if actual_hash != self.expected_routes_sha256:
            raise ValueError("g1_routing.expected_routes_sha256 does not match expected_routes")
        return self

    @property
    def candidate_scope(self) -> Literal["registry_all", "exact_routes"]:
        """Use every packaged registry model unless an exact route set is supplied."""

        return "exact_routes" if self.expected_routes is not None else "registry_all"


ThinkingSetting = Literal[
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "adaptive",
]


class DracoEnsembleMemberConfig(_StrictConfig):
    label: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key_env: str = Field(
        default="",
        pattern=r"^(?:[A-Za-z_][A-Za-z0-9_]*)?$",
    )
    base_url: str = ""
    temperature: float | None
    max_tokens: int = Field(gt=0)
    thinking: ThinkingSetting
    k: int = Field(default=1, ge=1)


class DracoEnsembleConfig(_StrictConfig):
    profile_name: str = Field(min_length=1)
    proposers: list[DracoEnsembleMemberConfig] = Field(min_length=1)
    aggregator: DracoEnsembleMemberConfig
    min_successful_proposers: int = Field(ge=1)
    all_failed_policy: Literal["fallback_single", "error"]
    candidate_max_chars: int = Field(ge=0)
    shuffle_candidates: bool
    record_candidates: bool
    proposer_tools: bool
    aggregator_tools: bool
    aggregator_recovery_mode: Literal["off", "serving", "experiment"] = "experiment"
    aggregator_recovery_top_k: int = Field(default=3, ge=1, le=3)
    aggregator_max_tokens_cap: int = Field(default=65_536, ge=2)
    aggregator_visible_answer_reserve_tokens: int = Field(default=8_192, ge=1)
    # Read-only compatibility for archived experiment overlays. The effective
    # proposer backup roster now comes exclusively from ranking config and this
    # legacy input must not reappear in frozen experiment artifacts.
    proposer_backup_count: int = Field(default=0, ge=0, le=2, exclude=True)
    proposer_recovery_max_additional_calls: int = Field(default=0, ge=0, le=3)
    proposer_max_tokens_cap: int = Field(default=65_536, ge=2)
    proposer_visible_answer_reserve_tokens: int = Field(default=4_096, ge=1)
    wait_for_all_proposers: bool
    quorum_grace_seconds: float = Field(ge=0.0)
    candidate_order_seed: int | None = Field(
        default=None,
        ge=0,
        le=(1 << 64) - 1,
    )

    @field_validator("candidate_order_seed", mode="before")
    @classmethod
    def _reject_boolean_candidate_order_seed(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("candidate_order_seed must be an unsigned 64-bit integer")
        return value

    @model_validator(mode="after")
    def _validate_wait_policy(self) -> DracoEnsembleConfig:
        if self.aggregator.k != 1:
            raise ValueError("ensemble.aggregator.k must be 1; aggregation runs once")
        if self.min_successful_proposers > sum(member.k for member in self.proposers):
            raise ValueError("ensemble.min_successful_proposers exceeds proposer sample count")
        if self.wait_for_all_proposers and self.quorum_grace_seconds != 0:
            raise ValueError(
                "ensemble.quorum_grace_seconds must be 0 when wait_for_all_proposers=true"
            )
        if not self.wait_for_all_proposers and self.quorum_grace_seconds <= 0:
            raise ValueError(
                "ensemble.quorum_grace_seconds must be positive when wait_for_all_proposers=false"
            )
        if self.aggregator_visible_answer_reserve_tokens >= self.aggregator_max_tokens_cap:
            raise ValueError(
                "ensemble.aggregator_visible_answer_reserve_tokens must be smaller than "
                "ensemble.aggregator_max_tokens_cap"
            )
        if self.proposer_visible_answer_reserve_tokens >= self.proposer_max_tokens_cap:
            raise ValueError(
                "ensemble.proposer_visible_answer_reserve_tokens must be smaller than "
                "ensemble.proposer_max_tokens_cap"
            )
        return self


class DracoTimeoutConfig(_StrictConfig):
    task_seconds: float = Field(gt=0.0)
    proposer_seconds: float = Field(gt=0.0)
    aggregator_seconds: float = Field(gt=0.0)
    task_margin_seconds: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _validate_budget(self) -> DracoTimeoutConfig:
        sequential_budget = (
            self.proposer_seconds + self.aggregator_seconds + self.task_margin_seconds
        )
        if sequential_budget > self.task_seconds + 1e-9:
            raise ValueError("timeouts proposer + aggregator + margin must not exceed task_seconds")
        return self


class DracoRunnerConfig(_StrictConfig):
    mode: Literal["agent_loop", "provider"]
    agent_max_iterations: int = Field(ge=0)
    concurrency: int = Field(ge=1)
    deadline_wrapup_margin_seconds: int = Field(default=0, ge=0)
    deadline_wrapup_disable_tools: bool = False
    deadline_thinking_off_margin_seconds: int = Field(default=0, ge=0)
    max_iterations_includes_finalization: bool = False
    retrieval_loop_finalization_threshold: int = Field(default=0, ge=0)
    finalization_aggregator_only: bool = False
    finalization_disable_thinking: bool = False


class DracoGenerationConfig(_StrictConfig):
    thinking_enabled: bool
    thinking_budget_tokens: int = Field(gt=0)
    default_thinking_level: ThinkingSetting
    model_thinking_levels: dict[str, ThinkingSetting]
    require_highest_thinking: bool
    temperature: float | None
    max_tokens: int = Field(gt=0)
    max_attempts: int = Field(ge=1, le=3)
    retry_backoff_seconds: float = Field(ge=0.0)


class DracoWebSearchConfig(_StrictConfig):
    provider: Literal["brave", "duckduckgo"]
    api_key_env: str = Field(
        default="",
        pattern=r"^(?:[A-Za-z_][A-Za-z0-9_]*)?$",
    )
    max_results: int = Field(gt=0)


class DracoWebFetchConfig(_StrictConfig):
    max_content_tokens: int = Field(gt=0)


class DracoToolsConfig(_StrictConfig):
    mode: Literal["local_web_tools", "provider_only", "openrouter_server_tools"]
    sandbox_enabled: Literal[False]
    contamination_blocked_domains: list[str]
    web_search: DracoWebSearchConfig
    web_fetch: DracoWebFetchConfig


class DracoJudgeConfig(_StrictConfig):
    model: str = Field(min_length=1)
    repeats: int = Field(ge=1)
    concurrency: int = Field(ge=1)
    max_attempts: int = Field(ge=1, le=3)
    judge_candidates: bool


class DracoExperimentConfig(_StrictConfig):
    schema_version: Literal[1]
    profile_id: str = Field(min_length=1)
    group: Literal["B2"]
    reference: DracoReferenceConfig
    benchmark_input: DracoBenchmarkInputConfig
    routing: DracoRoutingConfig
    g1_routing: DracoG1RoutingConfig | None = None
    router_dynamic_ranking_override: dict[str, Any] = Field(default_factory=dict)
    ensemble: DracoEnsembleConfig
    timeouts: DracoTimeoutConfig
    runner: DracoRunnerConfig
    generation: DracoGenerationConfig
    tools: DracoToolsConfig
    judge: DracoJudgeConfig

    @model_validator(mode="after")
    def _validate_ranking_config_override(self) -> DracoExperimentConfig:
        """Keep only a detached override that passed the full ranking schema."""

        if not self.router_dynamic_ranking_override:
            return self
        from opensquilla.provider.ranking_router import ranking_config_resolution

        resolution = ranking_config_resolution(
            override=self.router_dynamic_ranking_override,
        )
        normalized = resolution.get("override")
        if not isinstance(normalized, dict) or not normalized:
            raise ValueError(
                "router_dynamic_ranking_override did not resolve to a non-empty object"
            )
        object.__setattr__(
            self,
            "router_dynamic_ranking_override",
            copy.deepcopy(normalized),
        )
        return self

    @model_validator(mode="after")
    def _validate_frozen_task_analysis(self) -> DracoExperimentConfig:
        """Bind all ten inline profiles to the benchmark and effective Analyzer."""

        replay = self.g1_routing.task_analysis_execution if self.g1_routing is not None else None
        if replay is None:
            return self
        expected_task_ids = self.benchmark_input.task_ids
        if self.benchmark_input.task_count != 10 or set(replay.entries) != set(expected_task_ids):
            raise ValueError(
                "g1_routing.task_analysis_execution entries must exactly match "
                "the 10 benchmark task ids"
            )
        from opensquilla.provider.ranking_router import (
            TASK_ANALYZER_VERSION,
            ranking_config_resolution,
            task_analyzer_policy,
        )

        resolution = ranking_config_resolution(
            override=(self.router_dynamic_ranking_override or None),
        )
        effective = resolution.get("effective_config")
        effective_analyzer = effective.get("task_analyzer") if isinstance(effective, dict) else None
        if not isinstance(effective_analyzer, dict) or effective_analyzer != (
            replay.source_task_analyzer_config
        ):
            raise ValueError(
                "frozen task analysis source Analyzer config differs from the "
                "effective ranking config"
            )
        policy = task_analyzer_policy(effective)
        authorized_identities = {
            (str(policy["provider"]), str(policy["model"]))
        }
        if isinstance(replay, DracoFrozenTaskAnalysisExecutionV3Config):
            authorized_identities = {
                (str(route["provider"]), str(route["model"]))
                for route in replay.source_task_analyzer_execution_contract[
                    "routes"
                ]
            }
        for task_id, entry in replay.entries.items():
            analyzer = entry.task_analyzer
            if (
                (analyzer.provider, analyzer.model)
                not in authorized_identities
                or analyzer.analyzer_version != TASK_ANALYZER_VERSION
            ):
                raise ValueError(
                    f"frozen task analysis entry {task_id!r} differs from the "
                    "effective Analyzer identity"
                )
        return self

    @model_validator(mode="after")
    def _validate_legacy_proposer_backup_count(self) -> DracoExperimentConfig:
        """Reject a legacy backup value that disagrees with ranking policy."""

        if "proposer_backup_count" not in self.ensemble.model_fields_set:
            return self
        from opensquilla.provider.ranking_router import ranking_config_resolution

        resolution = ranking_config_resolution(
            override=(self.router_dynamic_ranking_override or None),
        )
        effective = resolution.get("effective_config")
        proposer_count = effective.get("proposer_count") if isinstance(effective, dict) else None
        ranking_backup_count = (
            proposer_count.get("backup_count") if isinstance(proposer_count, dict) else None
        )
        if self.ensemble.proposer_backup_count != ranking_backup_count:
            raise ValueError(
                "ensemble.proposer_backup_count is a legacy compatibility input and "
                "must match router_dynamic_ranking_override.proposer_count.backup_count"
            )
        return self

    @model_validator(mode="after")
    def _validate_thinking_policy(self) -> DracoExperimentConfig:
        if not self.generation.require_highest_thinking:
            return self
        for member in (*self.ensemble.proposers, self.ensemble.aggregator):
            expected = self.generation.model_thinking_levels.get(member.model)
            if expected is None:
                raise ValueError(
                    f"generation.model_thinking_levels has no highest setting for {member.model!r}"
                )
            if member.thinking != expected:
                raise ValueError(
                    f"ensemble member {member.model!r} uses {member.thinking!r}; "
                    f"highest configured setting is {expected!r}"
                )
        return self


def validate_formal_draco_ensemble_member_binding(
    member: DracoEnsembleMemberConfig,
    *,
    field_path: str = "ensemble member",
) -> None:
    """Reject provider settings that could redirect a formal DRACO credential."""

    if member.provider.strip().casefold() != FORMAL_DRACO_OPENROUTER_PROVIDER:
        raise ValueError(f"formal DRACO requires {field_path}.provider=openrouter")
    allowed_base_urls = {
        "",
        FORMAL_DRACO_OPENROUTER_BASE_URL,
        f"{FORMAL_DRACO_OPENROUTER_BASE_URL}/",
    }
    if member.base_url not in allowed_base_urls:
        raise ValueError(
            f"formal DRACO requires {field_path}.base_url to be empty or the official "
            "OpenRouter endpoint"
        )
    if member.api_key_env not in {"", FORMAL_DRACO_OPENROUTER_API_KEY_ENV}:
        raise ValueError(
            f"formal DRACO requires {field_path}.api_key_env to be empty or "
            f"{FORMAL_DRACO_OPENROUTER_API_KEY_ENV}"
        )


def validate_formal_draco_credential_bindings(config: DracoExperimentConfig) -> None:
    """Fail closed before formal DRACO resolves any member or search credential."""

    for index, member in enumerate(config.ensemble.proposers):
        validate_formal_draco_ensemble_member_binding(
            member,
            field_path=f"ensemble.proposers.{index}",
        )
    validate_formal_draco_ensemble_member_binding(
        config.ensemble.aggregator,
        field_path="ensemble.aggregator",
    )

    search = config.tools.web_search
    expected_env = FORMAL_DRACO_WEB_SEARCH_API_KEY_ENVS[search.provider]
    if search.api_key_env != expected_env:
        requirement = expected_env or "an empty api_key_env"
        raise ValueError("formal DRACO requires tools.web_search.api_key_env=" + requirement)


def validate_formal_draco_gateway_credential_binding(
    *,
    provider: str,
    base_url: str,
    api_key_env: str,
) -> None:
    """Validate the root provider before its configured credential env is read."""

    if provider.strip().casefold() != FORMAL_DRACO_OPENROUTER_PROVIDER:
        raise ValueError("formal DRACO requires config.llm.provider=openrouter")
    if base_url not in {
        FORMAL_DRACO_OPENROUTER_BASE_URL,
        f"{FORMAL_DRACO_OPENROUTER_BASE_URL}/",
    }:
        raise ValueError(
            "formal DRACO requires config.llm.base_url to be the official OpenRouter endpoint"
        )
    if api_key_env not in {"", FORMAL_DRACO_OPENROUTER_API_KEY_ENV}:
        raise ValueError(
            "formal DRACO requires config.llm.api_key_env to be empty or "
            f"{FORMAL_DRACO_OPENROUTER_API_KEY_ENV}"
        )


@dataclass(frozen=True)
class DracoExperimentConfigBundle:
    config: DracoExperimentConfig
    base_path: Path
    base_sha256: str
    base_document: dict[str, Any]
    override_documents: tuple[tuple[Path, dict[str, Any]], ...]
    override_sha256s: tuple[str, ...]
    inline_overrides: tuple[dict[str, Any], ...]
    inline_overlay_document: dict[str, Any] | None
    inline_overlay_sha256: str | None
    merged_document: dict[str, Any]

    def provenance(self) -> dict[str, Any]:
        return {
            "precedence": [
                "base_json",
                "override_json_in_cli_order",
                "inline_json_object",
                "inline_path_overrides_in_cli_order",
            ],
            "base": {
                "path": str(self.base_path),
                "sha256": self.base_sha256,
            },
            "overrides": [
                {
                    "path": str(path),
                    "sha256": self.override_sha256s[index],
                }
                for index, (path, _) in enumerate(self.override_documents)
            ],
            "effective_config_sha256": _canonical_json_sha256(self.config.model_dump(mode="json")),
            "inline_overlay": {
                "present": self.inline_overlay_document is not None,
                "field_paths": _document_field_paths(self.inline_overlay_document),
            },
            "inline_overrides": {
                "count": len(self.inline_overrides),
                "paths": [str(item["path"]) for item in self.inline_overrides],
            },
        }


def _document_field_paths(value: Any, *, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        if not value:
            return [prefix] if prefix else []
        paths: list[str] = []
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(_document_field_paths(value[key], prefix=child))
        return paths
    if isinstance(value, list):
        if not value:
            return [prefix] if prefix else []
        paths = []
        for index, item in enumerate(value):
            child = f"{prefix}.{index}" if prefix else str(index)
            paths.extend(_document_field_paths(item, prefix=child))
        return paths
    return [prefix] if prefix else []


def _load_json_object_snapshot(path: Path) -> tuple[dict[str, Any], Path, str]:
    resolved = path.expanduser().resolve()
    fd: int | None = None
    try:
        fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            fd = None
            raw = handle.read()
    except FileNotFoundError as exc:
        raise ValueError(f"experiment config does not exist: {resolved}") from exc
    finally:
        if fd is not None:
            os.close(fd)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"experiment config is not valid UTF-8: {resolved}") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid experiment config JSON {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"experiment config must contain a JSON object: {resolved}")
    return value, resolved, hashlib.sha256(raw).hexdigest()


def load_json_object(path: Path) -> dict[str, Any]:
    value, _, _ = _load_json_object_snapshot(path)
    return value


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _inline_overlay_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("--experiment-config-override-json must contain valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("--experiment-config-override-json must contain a JSON object")
    return value


def _inline_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _apply_path_override(document: dict[str, Any], path: str, value: Any) -> None:
    parts = [part.strip() for part in path.split(".")]
    if not parts or any(not part for part in parts):
        raise ValueError(f"invalid experiment config override path: {path!r}")
    current: Any = document
    for part in parts[:-1]:
        if isinstance(current, list):
            try:
                index = int(part)
                current = current[index]
            except (ValueError, IndexError) as exc:
                raise ValueError(f"invalid list index {part!r} in override path {path!r}") from exc
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise ValueError(f"unknown experiment config override path: {path!r}")
    leaf = parts[-1]
    if isinstance(current, list):
        try:
            current[int(leaf)] = value
        except (ValueError, IndexError) as exc:
            raise ValueError(f"invalid list index {leaf!r} in override path {path!r}") from exc
    elif isinstance(current, dict) and leaf in current:
        current[leaf] = value
    else:
        raise ValueError(f"unknown experiment config override path: {path!r}")


def load_draco_experiment_config(
    base_path: Path,
    *,
    override_paths: list[Path] | None = None,
    inline_overlay_json: str | None = None,
    inline_sets: list[str] | None = None,
) -> DracoExperimentConfigBundle:
    base_document, resolved_base_path, base_sha256 = _load_json_object_snapshot(base_path)
    merged = copy.deepcopy(base_document)
    override_documents: list[tuple[Path, dict[str, Any]]] = []
    override_sha256s: list[str] = []
    for override_path in override_paths or []:
        document, resolved_override_path, override_sha256 = _load_json_object_snapshot(
            override_path
        )
        merged = _deep_merge(merged, document)
        override_documents.append((resolved_override_path, document))
        override_sha256s.append(override_sha256)

    inline_overlay_document: dict[str, Any] | None = None
    inline_overlay_sha256: str | None = None
    if inline_overlay_json is not None:
        inline_overlay_document = _inline_overlay_object(inline_overlay_json)
        inline_overlay_sha256 = _canonical_json_sha256(inline_overlay_document)
        merged = _deep_merge(merged, inline_overlay_document)

    inline_overrides: list[dict[str, Any]] = []
    for raw in inline_sets or []:
        if "=" not in raw:
            raise ValueError("--experiment-config-set must use dotted.path=JSON_VALUE syntax")
        dotted_path, raw_value = raw.split("=", 1)
        value = _inline_value(raw_value)
        _apply_path_override(merged, dotted_path, value)
        inline_overrides.append({"path": dotted_path, "value": value})

    config = DracoExperimentConfig.model_validate(merged)
    return DracoExperimentConfigBundle(
        config=config,
        base_path=resolved_base_path,
        base_sha256=base_sha256,
        base_document=base_document,
        override_documents=tuple(override_documents),
        override_sha256s=tuple(override_sha256s),
        inline_overrides=tuple(inline_overrides),
        inline_overlay_document=inline_overlay_document,
        inline_overlay_sha256=inline_overlay_sha256,
        merged_document=merged,
    )


def validate_reference_input(
    path: Path,
    *,
    task_ids: list[str],
    config: DracoBenchmarkInputConfig,
) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    actual_sha256 = hashlib.sha256(resolved.read_bytes()).hexdigest()
    actual_ids = [str(task_id) for task_id in task_ids]
    trace = {
        "name": config.name,
        "path": str(resolved),
        "enforced": config.enforce_reference_input,
        "expected_sha256": config.sha256,
        "actual_sha256": actual_sha256,
        "expected_task_count": config.task_count,
        "actual_task_count": len(actual_ids),
        "task_ids_match": actual_ids == config.task_ids,
    }
    if not config.enforce_reference_input:
        trace["status"] = "not_enforced"
        return trace
    mismatches: list[str] = []
    if actual_sha256 != config.sha256:
        mismatches.append("sha256")
    if len(actual_ids) != config.task_count:
        mismatches.append("task_count")
    if actual_ids != config.task_ids:
        mismatches.append("task_ids_or_order")
    if mismatches:
        raise ValueError(
            "DRACO input does not match the G12 reference dataset: " + ", ".join(mismatches)
        )
    trace["status"] = "matched"
    return trace
