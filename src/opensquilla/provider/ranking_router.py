"""Profile-driven model ranking for the ``router_dynamic`` ensemble mode.

The module implements the Step2 ranking contract as a deterministic, replayable
pipeline.  Task analysis, user profiles, and the model registry are deliberately
small adapters for now; the ranking core does not depend on how those inputs are
produced and can be replaced by trained services later.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import math
import os
import re
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from functools import cache, wraps
from importlib import resources
from types import MappingProxyType
from typing import Any

import structlog

from .admission import (
    ProviderAdmissionController,
    ProviderAdmissionError,
    ProviderAdmissionLease,
    ProviderAdmissionLeaseGuard,
    ProviderAdmissionTimeoutError,
)
from .cache_affinity import (
    CacheAffinityEvidenceInput,
    CacheAffinityScoreAdjustment,
    CacheRole,
    CacheTopology,
    cache_affinity_score_adjustment,
)
from .failures import ProviderFailureKind, classify_provider_error
from .protocol import LLMProvider
from .thinking_execution import THINKING_PHYSICAL_EVIDENCE_SCHEMA
from .types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelCapabilities,
    TextDeltaEvent,
)

log = structlog.get_logger(__name__)


class CacheAffinityUnavailableReason(StrEnum):
    """Safe, replayable reasons why cache evidence could not affect ranking."""

    EXACT_CACHE_PRICE_QUOTE_UNAVAILABLE = "exact_cache_price_quote_unavailable"


RANKING_VERSION = "step2-ranking-v4"
SINGLE_MODEL_RANKING_VERSION = "router-single-ranking-v1"
CALIBRATED_SINGLE_MODEL_RANKING_VERSION = "router-single-ranking-v2"
LEGACY_THINKING_RANKING_VERSION = "step2-ranking-v3"
LEGACY_RANKING_VERSION = "step2-ranking-v2"
RANKING_CONFIG_SCHEMA_VERSION = "step2-ranking-config-v4"
LEGACY_RANKING_CONFIG_SCHEMA_VERSION = "step2-ranking-config-v3"
MODEL_REGISTRY_SCHEMA_VERSION = "step2-model-registry-v2"
LEGACY_MODEL_REGISTRY_SCHEMA_VERSION = "step2-model-registry-v1"
_PACKAGED_RANKING_CONFIG_VERSION = "step2-ranking-2026-08-25.1"
_LEGACY_PACKAGED_RANKING_CONFIG_VERSION = "step2-ranking-2026-08-25.1"
_PRE_C1_BASELINE_RANKING_CONFIG_VERSION = "step2-ranking-2026-08-21.2"
_PRE_TOGETHER_TASK_ANALYZER_RANKING_CONFIG_VERSION = "step2-ranking-2026-08-21.1"
_PRE_DEEPSEEK_TASK_ANALYZER_RANKING_CONFIG_VERSION = "step2-ranking-2026-08-18.4"
_PRE_SCHEMA_REPAIR_RANKING_CONFIG_VERSION = "step2-ranking-2026-08-18.3"
_PRE_RESOURCE_AWARE_RERANK_CONFIG_VERSION = "step2-ranking-2026-08-18.2"
_PREVIOUS_PACKAGED_RANKING_CONFIG_VERSION = "step2-ranking-2026-08-18.1"
_PREVIOUS_COST_BALANCE_RANKING_CONFIG_VERSION = "step2-ranking-2026-08-11.2"
_PREVIOUS_ZERO_FAILURE_PRIOR_RANKING_CONFIG_VERSION = "step2-ranking-2026-08-11.1"
_PRE_ANALYZER_CHAIN_RANKING_CONFIG_VERSION = "step2-ranking-2026-08-10.1"
_PREVIOUS_RELIABILITY_RANKING_CONFIG_VERSION = "step2-ranking-2026-08-05.1"
_PRE_ROSTER_PACKAGED_RANKING_CONFIG_VERSION = "step2-ranking-2026-07-27.1"
_PRE_ROSTER_LEGACY_RANKING_CONFIG_VERSION = "step2-ranking-2026-07-22.1"
_PRE_RELIABILITY_RANKING_CONFIG_VERSIONS = frozenset(
    {
        "step2-ranking-2026-08-02.2",
        "step2-ranking-2026-08-02.1",
        _PRE_ROSTER_PACKAGED_RANKING_CONFIG_VERSION,
        _PRE_ROSTER_LEGACY_RANKING_CONFIG_VERSION,
    }
)
_HISTORICAL_RANKING_CONFIG_BASE_VERSIONS = frozenset(
    {
        _PRE_C1_BASELINE_RANKING_CONFIG_VERSION,
        _PRE_TOGETHER_TASK_ANALYZER_RANKING_CONFIG_VERSION,
        _PRE_DEEPSEEK_TASK_ANALYZER_RANKING_CONFIG_VERSION,
        _PRE_SCHEMA_REPAIR_RANKING_CONFIG_VERSION,
        _PRE_RESOURCE_AWARE_RERANK_CONFIG_VERSION,
        _PREVIOUS_PACKAGED_RANKING_CONFIG_VERSION,
        _PREVIOUS_COST_BALANCE_RANKING_CONFIG_VERSION,
        _PREVIOUS_ZERO_FAILURE_PRIOR_RANKING_CONFIG_VERSION,
        _PRE_ANALYZER_CHAIN_RANKING_CONFIG_VERSION,
        "step2-ranking-2026-08-02.2",
        _PREVIOUS_RELIABILITY_RANKING_CONFIG_VERSION,
    }
)
_CURRENT_REGISTRY_BASE_VERSION = "curated-openrouter-step2-2026-08-20.1"
_PRE_QWEN35_9B_REGISTRY_BASE_VERSION = "curated-openrouter-step2-2026-08-19.1"
_PRE_QWEN35_9B_REGISTRY_SNAPSHOT_VERSION = (
    "curated-openrouter-step2-2026-08-19.1-reliability-20260818T121632Z-01580c6982b4"
)
_PRE_QWEN35_9B_REGISTRY_RESOURCE = "router_dynamic_model_profiles_20260819_01580c6982b4.json"
_PRE_STATUS_DISABLE_REGISTRY_BASE_VERSION = "curated-openrouter-step2-2026-08-18.2"
_PRE_STATUS_DISABLE_REGISTRY_SNAPSHOT_VERSION = (
    "curated-openrouter-step2-2026-08-18.2-reliability-20260818T121632Z-a04f8e2167bf"
)
_PRE_STATUS_DISABLE_REGISTRY_RESOURCE = "router_dynamic_model_profiles_20260818_a04f8e2167bf.json"
_PREVIOUS_REGISTRY_BASE_VERSION = "curated-openrouter-step2-2026-08-18.1"
_LEGACY_REGISTRY_BASE_VERSION = "curated-openrouter-step2-2026-07-31.1"
_HISTORICAL_REGISTRY_BASE_VERSIONS = frozenset(
    {
        _CURRENT_REGISTRY_BASE_VERSION,
        _PRE_QWEN35_9B_REGISTRY_BASE_VERSION,
        _PRE_STATUS_DISABLE_REGISTRY_BASE_VERSION,
        _PREVIOUS_REGISTRY_BASE_VERSION,
        _LEGACY_REGISTRY_BASE_VERSION,
    }
)
_POST_BASE_REGISTRY_FACT_OVERRIDES = {
    "deepseek/deepseek-v4-pro": {
        "price": {
            "input_per_million": 0.435,
            "output_per_million": 0.87,
        }
    },
    "moonshotai/kimi-k2.6": {
        "price": {
            "input_per_million": 0.684,
            "output_per_million": 3.42,
        }
    },
    "moonshotai/kimi-k3": {"modalities": ["text", "image"]},
    "nvidia/nemotron-3-ultra-550b-a55b": {
        "price": {
            "input_per_million": 0.5,
            "output_per_million": 2.2,
        }
    },
    "z-ai/glm-5.2": {
        "price": {
            "input_per_million": 0.8246,
            "output_per_million": 2.5916,
        }
    },
}
_POST_BASE_STATIC_PROFILE_OVERRIDES = {
    "anthropic/claude-sonnet-5": {
        "role_fit_prior": {"proposer": 0.96},
    },
    "moonshotai/kimi-k3": {"tier_dist_prior": {"4": 0.79}},
    "openai/gpt-5.3-codex": {
        "capability_dist_prior": {
            "summarization": 0.93,
            "writing": 0.93,
        },
        "domain_dist_prior": {
            "business_analysis": 0.95,
            "creative_writing": 0.94,
            "education": 0.95,
            "customer_support": 0.93,
            "general": 0.97,
        },
        "tier_dist_prior": {"4": 0.94},
        "role_fit_prior": {"proposer": 0.99},
    },
}
_POST_PREVIOUS_BASE_STATIC_PROFILE_OVERRIDES = {
    model_id: copy.deepcopy(overrides)
    for model_id, overrides in _POST_BASE_STATIC_PROFILE_OVERRIDES.items()
    if model_id in {"anthropic/claude-sonnet-5", "openai/gpt-5.3-codex"}
}
_POST_BASE_DISABLED_MODEL_IDS = frozenset(
    {
        "anthropic/claude-fable-5",
        "arcee-ai/trinity-large-thinking",
        "deepseek/deepseek-v3.1-terminus",
        "google/gemini-3.5-flash",
        "google/gemma-3-27b-it",
        "google/gemma-4-26b-a4b-it",
        "inclusionai/ling-2.6-1t",
        "inclusionai/ring-2.6-1t",
        "kwaipilot/kat-coder-air-v2.5",
        "kwaipilot/kat-coder-pro-v2.5",
        "meituan/longcat-2.0",
        "meta-llama/llama-3.3-70b-instruct",
        "moonshotai/kimi-k3",
        "nex-agi/nex-n2-pro",
        "poolside/laguna-s-2.1",
        "poolside/laguna-xs-2.1",
        "qwen/qwen3.6-27b",
        "tencent/hy3",
        "tencent/hy3-preview",
        "z-ai/glm-5.1",
    }
)
_POST_CURRENT_BASE_DISABLED_MODEL_IDS = frozenset(
    {
        "mistralai/mistral-large-2512",
        "mistralai/mistral-medium-3-5",
        "mistralai/mistral-small-2603",
        "mistralai/ministral-14b-2512",
        "mistralai/voxtral-small-24b-2507",
    }
)
_ROLE_RELIABILITY_SNAPSHOT_SCHEMA_VERSIONS = frozenset(
    {"role-reliability-snapshot-v1", "role-reliability-snapshot-v2"}
)
_TRUSTED_PACKAGED_RELIABILITY_POLICIES = frozenset(
    {"aef-physical-model-calls-v5", "legacy-statistics-invalidated-v1"}
)
_PACKAGED_REGISTRY_SNAPSHOT_VERSION = "curated-openrouter-step2-2026-07-27.1"
_LEGACY_PACKAGED_REGISTRY_SNAPSHOT_VERSION = "curated-openrouter-step2-2026-07-24.3"
TASK_ANALYZER_PROVIDER_ID = "openrouter"
TASK_ANALYZER_MODEL_ID = "deepseek/deepseek-v4-pro"
TASK_ANALYZER_UPSTREAM_PROVIDER = "together"
TASK_ANALYZER_VERSION = "opus-4.8-json-v3"
TASK_ANALYZER_FALLBACK_CHAIN_PROTOCOL = "opensquilla.task-analyzer-fallback-chain/v1"
FROZEN_TASK_ANALYSIS_SCHEMA = "opensquilla.draco.frozen-task-analysis/v1"
FROZEN_TASK_ANALYSIS_SCHEMA_V2 = "opensquilla.draco.frozen-task-analysis/v2"
FROZEN_TASK_ANALYSIS_SCHEMA_V3 = "opensquilla.draco.frozen-task-analysis/v3"
FROZEN_TASK_ANALYSIS_SCHEMAS = frozenset(
    {
        FROZEN_TASK_ANALYSIS_SCHEMA,
        FROZEN_TASK_ANALYSIS_SCHEMA_V2,
        FROZEN_TASK_ANALYSIS_SCHEMA_V3,
    }
)
FROZEN_TASK_ANALYSIS_MODE = "frozen_replay"
FROZEN_TASK_ANALYZER_SOURCE = "frozen_replay"
FROZEN_TASK_ANALYSIS_LIVE_SUCCESS = "live_success"
FROZEN_TASK_ANALYSIS_ROUTER_FALLBACK = "deterministic_router_fallback"
TASK_PROFILE_SCHEMA_VERSION = "step2-task-profile-v1"
THINKING_POLICY_VERSION = "thinking-policy-v1"
GENERATION_POLICY_FILTER_REASON_PREFIX = "generation_policy_"
TASK_ANALYZER_STREAM_CLOSE_TIMEOUT_SECONDS = 1.0
_TASK_ANALYZER_UPSTREAM_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_PHYSICAL_ATTEMPT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_NATIVE_IMAGE_ATTACHMENT_MIMES = frozenset(
    {
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)
_ATTACHMENT_MIME_ALIASES = {"image/jpg": "image/jpeg"}

CAPABILITIES = (
    "reasoning",
    "code_generation",
    "code_review",
    "tool_use",
    "planning",
    "retrieval",
    "summarization",
    "writing",
    "math",
    "data_analysis",
    "visual_understanding",
    "audio_understanding",
    "long_context",
    "format_following",
    "safety_judgment",
)
DOMAINS = (
    "software_engineering",
    "data_science",
    "document_processing",
    "business_analysis",
    "creative_writing",
    "education",
    "research",
    "customer_support",
    "legal",
    "finance",
    "medical",
    "technical_writing",
    "general",
)
TIERS = ("1", "2", "3", "4")
MODALITIES = ("text", "image", "audio", "video", "file")
THINKING_LEVELS = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "adaptive",
)
UNIFIED_THINKING_LEVELS = (
    "low",
    "medium",
    "high",
    "highest",
)
FORMATS = (
    "plain_text",
    "structured_text",
    "json",
    "table",
    "patch",
    "patch_and_explanation",
    "report",
    "slides",
    "code_only",
)

_CONSTRAINT_VALUES = {
    "cost": {"low", "medium", "high", "hard_limit"},
    "latency": {"interactive", "normal", "batch", "hard_timeout"},
    "risk": {"low", "medium", "high"},
}
_SESSION_INTENTS = {"new_task", "continue", "redo"}
_DEFAULT_SESSION_INTENT = "new_task"
_CONTEXT_BUCKET_ORDER = ("short", "medium", "long", "extra_long")
_CONTEXT_BUCKETS = set(_CONTEXT_BUCKET_ORDER)
_ROUTER_TIERS = {"c0", "c1", "c2", "c3"}
_USER_COST_SENSITIVITIES = {"low", "medium", "high", "hard_limit"}
_USER_TRADEOFFS = {"balanced", "latency_first", "quality_first"}
_MODEL_ROLES = {"proposer", "aggregator"}
ROLE_RELIABILITY_WINDOW_SIZE = 50


class DynamicRankingError(ValueError):
    """Raised when no feasible Step2 ``(P, A)`` decision can be built."""

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class TaskAnalyzerStreamCleanupError(RuntimeError):
    """Raised when analyzer stream cleanup cannot be proven within its bound."""

    def __init__(
        self,
        message: str,
        *,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = copy.deepcopy(dict(usage or {}))


class TaskAnalyzerPhysicalEvidenceError(TaskAnalyzerStreamCleanupError):
    """Raised when analyzer physical-request evidence is contradictory."""


_TASK_ANALYZER_ABSOLUTE_DEADLINE_REASON = "TaskAnalyzerAbsoluteDeadlineError"


class TaskAnalyzerSchemaError(ValueError):
    """A safe, structured Analyzer response error eligible for one repair."""

    def __init__(self, code: str, feedback: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.feedback = feedback or code


class TaskAnalyzerProviderError(RuntimeError):
    """A provider failure with a stable, public chain-trace classification."""

    def __init__(self, public_reason: str) -> None:
        super().__init__(public_reason)
        self.public_reason = public_reason


class TaskAnalyzerDeadlineError(TimeoutError):
    """The shared Analyzer-chain deadline elapsed before another request."""


def _immutable_ranking_config_types() -> tuple[Any, ...]:
    """Build the private immutable ranking-config representation.

    The construction token deliberately lives only in this closure. Ordinary
    container mutation, subclassing, or marker-like attributes therefore cannot
    make an external mapping trusted. This is an integrity boundary against
    accidental reuse, not a security boundary against hostile module introspection.
    """

    factory_token = object()
    immutable_values: dict[int, tuple[Any, Any]] = {}
    authentic_roots: set[int] = set()

    def reject_mutation(*_args: Any, **_kwargs: Any) -> Any:
        raise TypeError("prepared router_dynamic ranking config is immutable")

    def register(instance: Any, values: Any) -> None:
        object_id = id(instance)

        def release(reference: Any, *, registered_id: int = object_id) -> None:
            current = immutable_values.get(registered_id)
            if current is not None and current[0] is reference:
                immutable_values.pop(registered_id, None)
                authentic_roots.discard(registered_id)

        immutable_values[object_id] = (weakref.ref(instance, release), values)

    def registered_values(instance: Any) -> Any:
        current = immutable_values.get(id(instance))
        if current is None or current[0]() is not instance:
            raise TypeError("unregistered prepared ranking config container")
        return current[1]

    class ImmutableRankingDict(Mapping[str, Any]):
        __slots__ = ("__weakref__",)

        def __init__(
            self,
            values: Mapping[str, Any],
            *,
            _factory_token: object,
        ) -> None:
            if _factory_token is not factory_token:
                raise TypeError("prepared ranking configs are factory-created")
            register(self, MappingProxyType(dict(values)))

        def __getitem__(self, key: str) -> Any:
            return registered_values(self)[key]

        def __iter__(self) -> Any:
            return iter(registered_values(self))

        def __len__(self) -> int:
            return len(registered_values(self))

        def __repr__(self) -> str:
            return repr(dict(self))

        def __copy__(self) -> ImmutableRankingDict:
            return self

        def __deepcopy__(self, memo: dict[int, Any]) -> ImmutableRankingDict:
            memo[id(self)] = self
            return self

        __setattr__ = reject_mutation
        __delattr__ = reject_mutation
        __setitem__ = reject_mutation
        __delitem__ = reject_mutation
        __ior__ = reject_mutation
        clear = reject_mutation
        pop = reject_mutation
        popitem = reject_mutation
        setdefault = reject_mutation
        update = reject_mutation

    class ImmutableRankingList(Sequence[Any]):
        __slots__ = ("__weakref__",)

        def __init__(self, values: list[Any], *, _factory_token: object) -> None:
            if _factory_token is not factory_token:
                raise TypeError("prepared ranking configs are factory-created")
            register(self, tuple(values))

        def __getitem__(self, index: Any) -> Any:
            return registered_values(self)[index]

        def __len__(self) -> int:
            return len(registered_values(self))

        def __iter__(self) -> Any:
            return iter(registered_values(self))

        def __eq__(self, other: object) -> bool:
            return (
                isinstance(other, Sequence)
                and not isinstance(other, (str, bytes, bytearray))
                and list(self) == list(other)
            )

        def __repr__(self) -> str:
            return repr(list(self))

        def __copy__(self) -> ImmutableRankingList:
            return self

        def __deepcopy__(self, memo: dict[int, Any]) -> ImmutableRankingList:
            memo[id(self)] = self
            return self

        __setattr__ = reject_mutation
        __delattr__ = reject_mutation
        __setitem__ = reject_mutation
        __delitem__ = reject_mutation
        __iadd__ = reject_mutation
        __imul__ = reject_mutation
        append = reject_mutation
        clear = reject_mutation
        extend = reject_mutation
        insert = reject_mutation
        pop = reject_mutation
        remove = reject_mutation
        reverse = reject_mutation
        sort = reject_mutation

    class ValidatedRankingConfig(ImmutableRankingDict):
        """Factory-authenticated, recursively immutable validated config."""

        __slots__ = ()

    def freeze_value(value: Any) -> Any:
        if isinstance(value, Mapping):
            return ImmutableRankingDict(
                {key: freeze_value(child) for key, child in value.items()},
                _factory_token=factory_token,
            )
        if isinstance(value, list):
            return ImmutableRankingList(
                [freeze_value(child) for child in value],
                _factory_token=factory_token,
            )
        return value

    def freeze_config(value: Mapping[str, Any]) -> Mapping[str, Any]:
        frozen = ValidatedRankingConfig(
            {key: freeze_value(child) for key, child in value.items()},
            _factory_token=factory_token,
        )
        authentic_roots.add(id(frozen))
        return frozen

    def registered_plain_value(value: Any) -> tuple[bool, Any]:
        """Detach only containers created by this factory."""

        if type(value) in {ImmutableRankingDict, ValidatedRankingConfig}:
            values = registered_values(value)
            return True, {
                copy.deepcopy(key): registered_plain_value(child)[1]
                for key, child in values.items()
            }
        if type(value) is ImmutableRankingList:
            values = registered_values(value)
            return True, [registered_plain_value(child)[1] for child in values]
        return False, value

    def registered_json_default(value: Any) -> Any:
        """Teach json.dumps about only our private immutable containers."""

        registered, plain = registered_plain_value(value)
        if registered:
            return plain
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

    def wrap_validated_builder(builder: Callable[..., Mapping[str, Any]]) -> Any:
        """Authenticate only the checked plain mapping returned by ``builder``."""

        @wraps(builder)
        def checked_and_frozen(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
            return freeze_config(builder(*args, **kwargs))

        return checked_and_frozen

    def derive_analyzer_candidate(
        base: Mapping[str, Any],
        *,
        provider_id: str,
        model_id: str,
        upstream_provider: str,
        schema_repair_max_retries: int,
    ) -> Mapping[str, Any]:
        """Freeze the one bounded mutation allowed for an Analyzer chain hop."""

        if not is_authentic(base):
            raise DynamicRankingError(
                "router_dynamic analyzer candidate config requires a prepared base"
            )
        if provider_id != TASK_ANALYZER_PROVIDER_ID:
            raise DynamicRankingError(
                "router_dynamic task_analyzer.provider currently must be openrouter"
            )
        if (
            not model_id
            or model_id != model_id.strip().casefold()
            or any(character.isspace() for character in model_id)
            or "/" not in model_id
            or any(not segment for segment in model_id.split("/"))
        ):
            raise DynamicRankingError(
                "router_dynamic task_analyzer.model must be lowercase, trimmed, "
                "contain '/', and contain no whitespace"
            )
        if (
            not upstream_provider
            or upstream_provider != upstream_provider.strip().casefold()
            or _TASK_ANALYZER_UPSTREAM_PROVIDER_RE.fullmatch(upstream_provider) is None
        ):
            raise DynamicRankingError(
                "router_dynamic task_analyzer.upstream_provider must be a lowercase "
                "provider slug or auto"
            )
        if (
            isinstance(schema_repair_max_retries, bool)
            or not isinstance(schema_repair_max_retries, int)
            or not 0 <= schema_repair_max_retries <= 1
        ):
            raise ValueError("task analyzer schema repair retries must be 0 or 1")
        registered, temporary = registered_plain_value(base)
        if not registered or not isinstance(temporary, dict):
            raise DynamicRankingError(
                "router_dynamic analyzer candidate config requires a prepared base"
            )
        analyzer = temporary.get("task_analyzer")
        if not isinstance(analyzer, dict):
            raise DynamicRankingError("router_dynamic task_analyzer must be a JSON object")
        if not {
            "provider",
            "model",
            "upstream_provider",
            "stream_close_timeout_seconds",
        }.issubset(analyzer):
            raise DynamicRankingError(
                "router_dynamic analyzer candidate requires the validated identity policy"
            )
        analyzer.update(
            {
                "provider": provider_id,
                "model": model_id,
                "upstream_provider": upstream_provider,
                "max_retries": schema_repair_max_retries,
            }
        )
        if "fallback_chain" in analyzer:
            analyzer["fallback_chain"] = []
        return freeze_config(temporary)

    def is_authentic(value: Any) -> bool:
        current = immutable_values.get(id(value))
        return type(value) is ValidatedRankingConfig and (
            id(value) in authentic_roots and current is not None and current[0]() is value
        )

    def registry_size() -> int:
        return len(immutable_values)

    return (
        ValidatedRankingConfig,
        wrap_validated_builder,
        derive_analyzer_candidate,
        is_authentic,
        registry_size,
        registered_plain_value,
        registered_json_default,
    )


(
    _ValidatedRankingConfig,
    _wrap_validated_ranking_config_builder,
    _derive_validated_analyzer_candidate,
    _is_validated_ranking_config,
    _ranking_config_registry_size,
    _registered_ranking_config_plain_value,
    _registered_ranking_config_json_default,
) = _immutable_ranking_config_types()


def _detached_ranking_config_value(value: Any) -> Any:
    """Return mutable plain JSON containers without sharing prepared children."""

    registered, plain = _registered_ranking_config_plain_value(value)
    return plain if registered else copy.deepcopy(value)


def _detached_ranking_config(config: Mapping[str, Any]) -> dict[str, Any]:
    registered, plain = _registered_ranking_config_plain_value(config)
    if registered:
        if not isinstance(plain, dict):
            raise TypeError("prepared ranking config root must be an object")
        return plain
    return copy.deepcopy(dict(config))


@dataclass(frozen=True)
class TaskAnalyzerCandidate:
    """One strict, caller-resolved candidate in an Analyzer fallback chain."""

    provider_id: str
    model_id: str
    upstream_provider: str
    provider: LLMProvider | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        provider_id = self.provider_id.strip()
        model_id = self.model_id.strip()
        upstream_provider = self.upstream_provider.strip()
        if (
            not provider_id
            or provider_id != self.provider_id
            or provider_id != provider_id.casefold()
        ):
            raise ValueError(
                "task analyzer candidate provider_id must be non-empty, lowercase, and trimmed"
            )
        if (
            not model_id
            or model_id != self.model_id
            or model_id != model_id.casefold()
            or any(character.isspace() for character in model_id)
            or "/" not in model_id
            or any(not segment for segment in model_id.split("/"))
        ):
            raise ValueError(
                "task analyzer candidate model_id must be lowercase, trimmed, "
                "contain '/', and contain no whitespace"
            )
        if (
            not upstream_provider
            or upstream_provider != self.upstream_provider
            or upstream_provider != upstream_provider.casefold()
            or _TASK_ANALYZER_UPSTREAM_PROVIDER_RE.fullmatch(upstream_provider) is None
        ):
            raise ValueError(
                "task analyzer candidate upstream_provider must be non-empty, "
                "lowercase, and trimmed"
            )
        if self.provider is not None and not callable(getattr(self.provider, "chat", None)):
            raise ValueError(
                "task analyzer candidate provider must implement the LLMProvider interface"
            )


def _normalize_attachment_mime(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.split(";", 1)[0].strip().lower()
    if not normalized:
        return None
    return _ATTACHMENT_MIME_ALIASES.get(normalized, normalized)


@dataclass(frozen=True)
class TaskAnalysisResult:
    """Validated task profile plus provenance for the route trace."""

    profile: dict[str, Any]
    source: str
    schema_valid: bool
    confidence: float
    analyzer_version: str = TASK_ANALYZER_VERSION
    fallback_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    provider_id: str = ""
    model_id: str = ""
    normalization_warnings: tuple[str, ...] = ()
    replay: dict[str, Any] = field(default_factory=dict)
    chain_trace: dict[str, Any] = field(default_factory=dict)

    def trace(self, ranking_config: Mapping[str, Any] | None = None) -> dict[str, Any]:
        decimal_places = _ranking_int(
            _resolve_ranking_config(ranking_config),
            "trace",
            "profile_decimal_places",
        )
        return {
            "source": self.source,
            "schema_valid": self.schema_valid,
            "confidence": round(self.confidence, decimal_places),
            "analyzer_version": self.analyzer_version,
            "provider": self.provider_id,
            "model": self.model_id,
            "fallback_reason": self.fallback_reason,
            "usage": copy.deepcopy(self.usage),
            "normalization_warnings": list(self.normalization_warnings),
            **({"replay": copy.deepcopy(self.replay)} if self.replay else {}),
            **({"chain": copy.deepcopy(self.chain_trace)} if self.chain_trace else {}),
        }


@dataclass(frozen=True)
class RankedModel:
    """Normalized model-registry row used by the ranking core."""

    provider: str
    model_id: str
    version: str
    source: str
    registry_facts: dict[str, Any]
    static_profile: dict[str, Any]
    online_profile: dict[str, Any]
    thinking: str | None = "xhigh"
    requested_thinking_level: str | None = None
    effective_thinking_level: str | None = None
    thinking_fallback_reason: str = ""
    thinking_policy_version: str = ""
    thinking_fallbacks: tuple[dict[str, str], ...] = ()

    @property
    def identity(self) -> str:
        return f"{self.provider}:{self.model_id}"

    @property
    def family(self) -> str:
        return str(self.registry_facts.get("family") or self.model_id).lower()

    @property
    def vendor(self) -> str:
        return str(self.registry_facts.get("vendor") or self.provider).lower()

    def trace(self, *, include_thinking_contract: bool = True) -> dict[str, Any]:
        facts = self.registry_facts
        hash_facts = facts
        if not include_thinking_contract:
            hash_facts = {
                key: value
                for key, value in facts.items()
                if key not in {"thinking_levels", "thinking_level_mapping"}
            }
        payload = {
            "identity": self.identity,
            "provider": self.provider,
            "model": self.model_id,
            "version": self.version,
            "source": self.source,
            "vendor": self.vendor,
            "family": self.family,
            "is_open_source": facts.get("is_open_source"),
            "is_chinese_model": facts.get("is_chinese_model"),
            "supports_reasoning": facts.get("supports_reasoning"),
            "supports_tools": facts.get("supports_tools"),
            "supported_thinking_levels": list(facts.get("supported_thinking_levels") or []),
            "status": str(facts.get("status") or ""),
            "roles": list(facts.get("roles") or []),
            "context_window": _as_int(facts.get("context_window"), 0),
            "modalities": list(facts.get("modalities") or []),
            "health": facts.get("health"),
            "credential_available": bool(facts.get("credential_available", True)),
            "profile_hash": _canonical_hash(
                {
                    "registry_facts": hash_facts,
                    "static_profile": self.static_profile,
                    "online_profile": self.online_profile,
                    "thinking": self.thinking,
                }
            ),
        }
        if include_thinking_contract and "thinking_levels" in facts:
            payload["thinking_levels"] = list(facts.get("thinking_levels") or [])
        if include_thinking_contract and "thinking_level_mapping" in facts:
            mapping = facts.get("thinking_level_mapping")
            payload["thinking_level_mapping"] = (
                copy.deepcopy(dict(mapping)) if isinstance(mapping, Mapping) else {}
            )
        if self.requested_thinking_level is not None:
            payload.update(
                {
                    "requested_thinking_level": self.requested_thinking_level,
                    "effective_thinking_level": self.effective_thinking_level,
                    "provider_thinking_level": self.thinking,
                    "thinking_fallback_reason": self.thinking_fallback_reason,
                    "thinking_policy_version": self.thinking_policy_version,
                    "thinking_fallbacks": [
                        copy.deepcopy(dict(fallback)) for fallback in self.thinking_fallbacks
                    ],
                }
            )
        return payload


@dataclass(frozen=True)
class RankingDecision:
    """Selected proposer set, aggregator, and replayable ranking trace."""

    proposers: tuple[RankedModel, ...]
    aggregator: RankedModel
    effective_tier: int
    trace: dict[str, Any]
    thinking_assignment: dict[str, Any] = field(default_factory=dict)
    thinking_assignment_details: dict[str, Any] = field(default_factory=dict)
    # Item zero is always the selected primary. The remaining items are
    # frozen recovery candidates from the same hard-filtered ranking.
    aggregator_candidates: tuple[RankedModel, ...] = field(default_factory=tuple)
    # Ordered, unselected proposer replacements from the same hard-filtered
    # and scored registry snapshot.
    backup_proposers: tuple[RankedModel, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SingleModelRankingDecision:
    """One direct generation model and its replay evidence."""

    model: RankedModel
    effective_tier: int
    trace: dict[str, Any]
    thinking_assignment: dict[str, Any] = field(default_factory=dict)
    thinking_assignment_details: dict[str, Any] = field(default_factory=dict)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _json_number(value: Any) -> float | None:
    """Return a finite JSON number without accepting booleans or numeric strings."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize public routing evidence with one deterministic UTF-8 contract."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_registered_ranking_config_json_default,
    ).encode("utf-8")


def canonical_json_sha256(value: Any) -> str:
    """Hash public routing evidence using :func:`canonical_json_bytes`."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


# Private compatibility alias for the existing ranking implementation.  New
# cross-component consumers should import the public helper above so runtime
# and offline verification cannot silently choose different JSON encodings.
_canonical_hash = canonical_json_sha256


def _legacy_ranking_config_projection(config: Mapping[str, Any]) -> dict[str, Any]:
    """Project the additive v4 policy config onto the exact pre-feature shape."""

    projected = _detached_ranking_config(config)
    if projected.get("schema_version") != RANKING_CONFIG_SCHEMA_VERSION:
        return projected
    projected["schema_version"] = LEGACY_RANKING_CONFIG_SCHEMA_VERSION
    if projected.get("config_version") == _PACKAGED_RANKING_CONFIG_VERSION:
        projected["config_version"] = _LEGACY_PACKAGED_RANKING_CONFIG_VERSION
    elif projected.get("config_version") == _PRE_ROSTER_PACKAGED_RANKING_CONFIG_VERSION:
        projected["config_version"] = _PRE_ROSTER_LEGACY_RANKING_CONFIG_VERSION
    projected.pop("thinking_assignment", None)
    return projected


def _is_pre_roster_ranking_config_version(value: Any) -> bool:
    """Return whether ``value`` identifies the archived pre-roster policy."""

    version = str(value or "").strip()
    return any(
        version == base or version.startswith(f"{base}+override.")
        for base in (
            _PRE_ROSTER_PACKAGED_RANKING_CONFIG_VERSION,
            _PRE_ROSTER_LEGACY_RANKING_CONFIG_VERSION,
        )
    )


def _is_pre_task_analyzer_policy_config_version(value: Any) -> bool:
    """Return whether an archived config predates public analyzer identity fields."""

    version = str(value or "").strip().split("+override.", 1)[0]
    return version in {
        "step2-ranking-2026-08-02.1",
        _PRE_ROSTER_PACKAGED_RANKING_CONFIG_VERSION,
        _PRE_ROSTER_LEGACY_RANKING_CONFIG_VERSION,
    }


def _is_pre_role_reliability_config_version(value: Any) -> bool:
    """Return whether an archived config predates stability penalties."""

    version = str(value or "").strip().split("+override.", 1)[0]
    return version in _PRE_RELIABILITY_RANKING_CONFIG_VERSIONS


def _is_pre_resource_aware_rerank_config_version(value: Any) -> bool:
    """Return whether a frozen policy predates resource-aware proposer reranking."""

    version = str(value or "").strip().split("+override.", 1)[0]
    return version in {
        _PRE_RESOURCE_AWARE_RERANK_CONFIG_VERSION,
        _PREVIOUS_PACKAGED_RANKING_CONFIG_VERSION,
        _PREVIOUS_COST_BALANCE_RANKING_CONFIG_VERSION,
        _PREVIOUS_ZERO_FAILURE_PRIOR_RANKING_CONFIG_VERSION,
        _PRE_ANALYZER_CHAIN_RANKING_CONFIG_VERSION,
        _PREVIOUS_RELIABILITY_RANKING_CONFIG_VERSION,
        "step2-ranking-2026-08-02.2",
        "step2-ranking-2026-08-02.1",
        _PRE_ROSTER_PACKAGED_RANKING_CONFIG_VERSION,
        _PRE_ROSTER_LEGACY_RANKING_CONFIG_VERSION,
    }


def _legacy_registry_snapshot_projection(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Project the additive v2 thinking facts onto the exact v1 snapshot."""

    projected = copy.deepcopy(dict(snapshot))
    if projected.get("schema_version") != MODEL_REGISTRY_SCHEMA_VERSION:
        return projected
    projected["schema_version"] = LEGACY_MODEL_REGISTRY_SCHEMA_VERSION
    if projected.get("snapshot_version") == _PACKAGED_REGISTRY_SNAPSHOT_VERSION:
        projected["snapshot_version"] = _LEGACY_PACKAGED_REGISTRY_SNAPSHOT_VERSION
    rows = projected.get("models")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            facts = row.get("registry_facts")
            if isinstance(facts, dict):
                facts.pop("thinking_levels", None)
                facts.pop("thinking_level_mapping", None)
    return projected


_TRACE_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
    "auth_token",
    "secret",
    "password",
    "passwd",
    "credential",
    "proxy_auth",
    "proxy_password",
)
_TRACE_SECRET_VALUE_RE = re.compile(
    r"(?:^|[^a-z0-9])sk-[a-z0-9_-]{8,}",
    flags=re.IGNORECASE,
)


def _assert_public_ranking_trace_payload(
    value: Any,
    *,
    label: str,
    path: tuple[str, ...] = (),
) -> None:
    """Reject credential-like fields before replay evidence enters a trace."""

    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().casefold().replace("-", "_")
            child_path = (*path, str(raw_key))
            public_availability_fact = key == "credential_available" and isinstance(child, bool)
            if not public_availability_fact and (
                any(fragment in key for fragment in _TRACE_SECRET_KEY_FRAGMENTS)
                or key in {"token", "bearer", "proxy"}
                or key.endswith("_token")
            ):
                raise DynamicRankingError(
                    f"{label} contains secret-like field {'.'.join(child_path)}"
                )
            _assert_public_ranking_trace_payload(
                child,
                label=label,
                path=child_path,
            )
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _assert_public_ranking_trace_payload(
                child,
                label=label,
                path=(*path, str(index)),
            )
        return
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if (
            _TRACE_SECRET_VALUE_RE.search(normalized) is not None
            or normalized.startswith("bearer ")
            or ("://" in normalized and "@" in normalized.partition("://")[2].partition("/")[0])
        ):
            raise DynamicRankingError(f"{label} contains secret-like value at {'.'.join(path)}")


def _request_context_hash(request_context: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(request_context))
    payload.pop("snapshot_hash", None)
    return _canonical_hash(payload)


ROUTER_DYNAMIC_RETRY_ROUTING_SCHEMA = "opensquilla.router-dynamic-retry-routing/v2"
ROUTER_DYNAMIC_TASK_ANALYSIS_REUSE_SCHEMA = "opensquilla.router-dynamic-task-analysis-reuse/v1"
ROUTER_DYNAMIC_TASK_ANALYSIS_REUSE_FIELDS = (
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


def router_dynamic_task_analysis_reuse_projection(
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the canonical public inputs that a G1 rerank must reuse."""

    return {
        field: copy.deepcopy(plan.get(field)) for field in ROUTER_DYNAMIC_TASK_ANALYSIS_REUSE_FIELDS
    }


def _router_dynamic_task_analysis_projection_reasons(
    projection: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    if set(projection) != set(ROUTER_DYNAMIC_TASK_ANALYSIS_REUSE_FIELDS):
        reasons.append("g1_retry_task_analysis_projection_fields_mismatch")
        return reasons
    task_analyzer = projection.get("task_analyzer")
    task_profile = projection.get("task_profile")
    request_context = projection.get("request_context")
    if not isinstance(task_analyzer, Mapping) or not task_analyzer:
        reasons.append("invalid_g1_retry_task_analyzer")
    if not isinstance(task_profile, Mapping) or not task_profile:
        reasons.append("invalid_g1_retry_task_profile")
    elif str(projection.get("task_profile_hash") or "") != _canonical_hash(task_profile):
        reasons.append("invalid_g1_retry_task_profile_hash")
    if not isinstance(request_context, Mapping) or not request_context:
        reasons.append("invalid_g1_retry_request_context")
    else:
        expected_context_hash = _request_context_hash(request_context)
        if (
            str(request_context.get("snapshot_hash") or "") != expected_context_hash
            or str(projection.get("request_context_hash") or "") != expected_context_hash
        ):
            reasons.append("invalid_g1_retry_request_context_hash")
    if not str(projection.get("routed_tier") or "").strip():
        reasons.append("invalid_g1_retry_routed_tier")
    routing_confidence = projection.get("routing_confidence")
    if (
        isinstance(routing_confidence, bool)
        or not isinstance(routing_confidence, int | float)
        or not math.isfinite(float(routing_confidence))
        or not 0.0 <= float(routing_confidence) <= 1.0
    ):
        reasons.append("invalid_g1_retry_routing_confidence")
    if not isinstance(projection.get("user_profile_enabled"), bool):
        reasons.append("invalid_g1_retry_user_profile_enabled")
    if not isinstance(projection.get("user_profile_version"), str):
        reasons.append("invalid_g1_retry_user_profile_version")
    if not isinstance(projection.get("user_profile_source"), str):
        reasons.append("invalid_g1_retry_user_profile_source")
    return reasons


def build_router_dynamic_task_analysis_reuse_binding(
    source_plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one retry to the exact task-analysis inputs of its source decision."""

    source_decision_id = str(source_plan.get("decision_id") or "").strip()
    projection = router_dynamic_task_analysis_reuse_projection(source_plan)
    reasons = _router_dynamic_task_analysis_projection_reasons(projection)
    if not source_decision_id:
        reasons.append("missing_g1_retry_task_analysis_source_decision")
    if reasons:
        raise DynamicRankingError(
            "router_dynamic retry task-analysis source is invalid: " + ",".join(reasons)
        )
    return {
        "schema": ROUTER_DYNAMIC_TASK_ANALYSIS_REUSE_SCHEMA,
        "source_decision_id": source_decision_id,
        "projection": projection,
        "projection_sha256": _canonical_hash(projection),
    }


def router_dynamic_task_analysis_reuse_reasons(
    source_plan: Mapping[str, Any],
    retry_plan: Mapping[str, Any],
) -> list[str]:
    """Validate a retry's task-analysis binding against source and reranked plans."""

    reasons: list[str] = []
    source_projection = router_dynamic_task_analysis_reuse_projection(source_plan)
    retry_projection = router_dynamic_task_analysis_reuse_projection(retry_plan)
    reasons.extend(_router_dynamic_task_analysis_projection_reasons(source_projection))
    reasons.extend(_router_dynamic_task_analysis_projection_reasons(retry_projection))
    binding = retry_plan.get("task_analysis_reuse")
    if not isinstance(binding, Mapping):
        reasons.append("missing_g1_retry_task_analysis_binding")
        return list(dict.fromkeys(reasons))
    if binding.get("schema") != ROUTER_DYNAMIC_TASK_ANALYSIS_REUSE_SCHEMA:
        reasons.append("wrong_g1_retry_task_analysis_binding_schema")
    source_decision_id = str(source_plan.get("decision_id") or "").strip()
    if not source_decision_id or binding.get("source_decision_id") != source_decision_id:
        reasons.append("wrong_g1_retry_task_analysis_source_decision")
    bound_projection = binding.get("projection")
    if not isinstance(bound_projection, Mapping):
        reasons.append("invalid_g1_retry_task_analysis_projection")
    else:
        reasons.extend(_router_dynamic_task_analysis_projection_reasons(bound_projection))
        if dict(bound_projection) != source_projection:
            reasons.append("g1_retry_task_analysis_source_projection_mismatch")
        if dict(bound_projection) != retry_projection:
            reasons.append("g1_retry_task_analysis_reuse_projection_mismatch")
        if str(binding.get("projection_sha256") or "") != _canonical_hash(bound_projection):
            reasons.append("g1_retry_task_analysis_projection_hash_mismatch")
    return list(dict.fromkeys(reasons))


def _ranking_value(config: Mapping[str, Any], *path: str) -> Any:
    value: Any = config
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            dotted = ".".join(path)
            raise DynamicRankingError(f"router_dynamic ranking config lacks {dotted}")
        value = value[key]
    return value


def _ranking_mapping(config: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    value = _ranking_value(config, *path)
    if not isinstance(value, Mapping):
        dotted = ".".join(path)
        raise DynamicRankingError(f"router_dynamic ranking config {dotted} must be an object")
    return value


def _ranking_number(config: Mapping[str, Any], *path: str) -> float:
    value = _ranking_value(config, *path)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        dotted = ".".join(path)
        raise DynamicRankingError(f"router_dynamic ranking config {dotted} must be numeric")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        dotted = ".".join(path)
        raise DynamicRankingError(f"router_dynamic ranking config {dotted} must be finite") from exc
    if not math.isfinite(number):
        dotted = ".".join(path)
        raise DynamicRankingError(f"router_dynamic ranking config {dotted} must be finite")
    return number


def _ranking_int(config: Mapping[str, Any], *path: str) -> int:
    number = _ranking_number(config, *path)
    integer = int(number)
    if number != integer:
        dotted = ".".join(path)
        raise DynamicRankingError(f"router_dynamic ranking config {dotted} must be an integer")
    return integer


def _ranking_string_set(config: Mapping[str, Any], *path: str) -> set[str]:
    return set(_ranking_string_list(config, *path))


def _ranking_string_list(config: Mapping[str, Any], *path: str) -> list[str]:
    value = _ranking_value(config, *path)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        dotted = ".".join(path)
        raise DynamicRankingError(f"router_dynamic ranking config {dotted} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            dotted = ".".join(path)
            raise DynamicRankingError(
                f"router_dynamic ranking config {dotted} must contain non-empty strings"
            )
        result.append(item.strip())
    if len(set(result)) != len(result):
        dotted = ".".join(path)
        raise DynamicRankingError(
            f"router_dynamic ranking config {dotted} cannot contain duplicates"
        )
    return result


def _ranking_string(config: Mapping[str, Any], *path: str) -> str:
    value = _ranking_value(config, *path)
    if not isinstance(value, str) or not value.strip():
        dotted = ".".join(path)
        raise DynamicRankingError(
            f"router_dynamic ranking config {dotted} must be a non-empty string"
        )
    return value.strip()


def _ranking_bool(config: Mapping[str, Any], *path: str) -> bool:
    value = _ranking_value(config, *path)
    if not isinstance(value, bool):
        dotted = ".".join(path)
        raise DynamicRankingError(f"router_dynamic ranking config {dotted} must be boolean")
    return value


def _context_bucket_min_tokens(config: Mapping[str, Any]) -> dict[str, int]:
    values = _ranking_mapping(config, "context", "bucket_min_tokens")
    return {
        str(bucket): _ranking_int(config, "context", "bucket_min_tokens", str(bucket))
        for bucket in values
    }


def _router_tier_mapping(config: Mapping[str, Any]) -> dict[str, int]:
    values = _ranking_mapping(config, "routing_tiers", "mapping")
    return {
        str(router_tier): _ranking_int(config, "routing_tiers", "mapping", str(router_tier))
        for router_tier in values
    }


def _require_exact_config_keys(
    config: Mapping[str, Any],
    path: tuple[str, ...],
    expected: set[str],
) -> None:
    values = _ranking_mapping(config, *path)
    actual = set(values)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    raise DynamicRankingError(
        "router_dynamic ranking config "
        f"{'.'.join(path)} has unknown or missing keys "
        f"(missing={missing}, unknown={unknown})"
    )


_SINGLE_ROUTE_CALIBRATION_KEYS = {
    "enabled",
    "activation_model_identities",
    "model_prior_weight",
    "model_quality_priors",
    "residual_weight",
    "residual_clip",
    "model_residual_coefficients",
    "quality_guard_enabled",
    "max_quality_drop",
    "predicted_total_cost_enabled",
    "predicted_cost_reference_usd",
    "input_tokens_by_tier",
    "output_tokens_by_tier",
}
_SINGLE_ROUTE_RESIDUAL_ROW_KEYS = {
    "intercept",
    "centers",
    "coefficients",
}
_SINGLE_ROUTE_RESIDUAL_FEATURE_KEYS = {
    "capability",
    "domain",
    "tier",
}


def _validate_single_route_calibration_config(
    config: Mapping[str, Any],
) -> None:
    """Validate the optional, model-bound direct-routing calibration policy."""

    if "single_route_calibration" not in config:
        return
    policy = _ranking_mapping(config, "single_route_calibration")
    actual_policy_keys = set(policy)
    required_policy_keys = _SINGLE_ROUTE_CALIBRATION_KEYS - {
        "activation_model_identities"
    }
    missing_policy_keys = sorted(required_policy_keys - actual_policy_keys)
    unknown_policy_keys = sorted(actual_policy_keys - _SINGLE_ROUTE_CALIBRATION_KEYS)
    if missing_policy_keys or unknown_policy_keys:
        raise DynamicRankingError(
            "router_dynamic ranking config single_route_calibration has unknown "
            f"or missing keys (missing={missing_policy_keys}, "
            f"unknown={unknown_policy_keys})"
        )
    enabled = _ranking_bool(config, "single_route_calibration", "enabled")
    raw_activation_identities = policy.get("activation_model_identities")
    if raw_activation_identities is None:
        raw_activation_identities = []
    if not isinstance(raw_activation_identities, list):
        raise DynamicRankingError(
            "router_dynamic ranking config single_route_calibration."
            "activation_model_identities must be a list"
        )
    activation_identities: list[str] = []
    for raw_identity in raw_activation_identities:
        if (
            not isinstance(raw_identity, str)
            or raw_identity != raw_identity.strip()
            or not raw_identity
            or ":" not in raw_identity
        ):
            raise DynamicRankingError(
                "router_dynamic single-route calibration activation identity "
                "must be a trimmed provider:model string"
            )
        activation_identities.append(raw_identity)
    if len(activation_identities) > 256 or len(set(activation_identities)) != len(
        activation_identities
    ):
        raise DynamicRankingError(
            "router_dynamic single-route calibration activation identities "
            "must be unique and contain at most 256 models"
        )
    prior_weight = _ranking_number(
        config,
        "single_route_calibration",
        "model_prior_weight",
    )
    residual_weight = _ranking_number(
        config,
        "single_route_calibration",
        "residual_weight",
    )
    residual_clip = _ranking_number(
        config,
        "single_route_calibration",
        "residual_clip",
    )
    _ranking_bool(
        config,
        "single_route_calibration",
        "quality_guard_enabled",
    )
    max_quality_drop = _ranking_number(
        config,
        "single_route_calibration",
        "max_quality_drop",
    )
    predicted_total_cost_enabled = _ranking_bool(
        config,
        "single_route_calibration",
        "predicted_total_cost_enabled",
    )
    predicted_cost_reference = _ranking_number(
        config,
        "single_route_calibration",
        "predicted_cost_reference_usd",
    )
    for label, value in (
        ("model_prior_weight", prior_weight),
        ("residual_weight", residual_weight),
        ("residual_clip", residual_clip),
        ("max_quality_drop", max_quality_drop),
    ):
        if not 0.0 <= value <= 1.0:
            raise DynamicRankingError(
                "router_dynamic ranking config single_route_calibration."
                f"{label} must be between 0 and 1"
            )
    if predicted_cost_reference <= 0.0:
        raise DynamicRankingError(
            "router_dynamic ranking config single_route_calibration."
            "predicted_cost_reference_usd must be positive"
        )

    priors = _ranking_mapping(
        config,
        "single_route_calibration",
        "model_quality_priors",
    )
    if len(priors) > 256:
        raise DynamicRankingError(
            "router_dynamic single-route calibration has too many model priors"
        )
    for identity in priors:
        if (
            not isinstance(identity, str)
            or identity != identity.strip()
            or not identity
            or ":" not in identity
        ):
            raise DynamicRankingError(
                "router_dynamic single-route calibration model prior identity "
                "must be a trimmed provider:model string"
            )
        value = _ranking_number(
            config,
            "single_route_calibration",
            "model_quality_priors",
            identity,
        )
        if not 0.0 <= value <= 1.0:
            raise DynamicRankingError(
                "router_dynamic single-route calibration model priors must be "
                "between 0 and 1"
            )
    if enabled and prior_weight > 0.0 and not priors:
        raise DynamicRankingError(
            "router_dynamic enabled single-route prior calibration requires "
            "model_quality_priors"
        )
    if enabled and prior_weight > 0.0:
        missing_activation_priors = sorted(set(activation_identities) - set(priors))
        if missing_activation_priors:
            raise DynamicRankingError(
                "router_dynamic enabled single-route prior calibration lacks "
                f"activation identities {missing_activation_priors}"
            )

    residual_rows = _ranking_mapping(
        config,
        "single_route_calibration",
        "model_residual_coefficients",
    )
    if len(residual_rows) > 256:
        raise DynamicRankingError(
            "router_dynamic single-route calibration has too many residual rows"
        )
    for identity, raw_row in residual_rows.items():
        if (
            not isinstance(identity, str)
            or identity != identity.strip()
            or not identity
            or ":" not in identity
            or not isinstance(raw_row, Mapping)
        ):
            raise DynamicRankingError(
                "router_dynamic single-route residual identity or row is invalid"
            )
        _require_exact_config_keys(
            config,
            (
                "single_route_calibration",
                "model_residual_coefficients",
                identity,
            ),
            _SINGLE_ROUTE_RESIDUAL_ROW_KEYS,
        )
        intercept = _ranking_number(
            config,
            "single_route_calibration",
            "model_residual_coefficients",
            identity,
            "intercept",
        )
        if not -1.0 <= intercept <= 1.0:
            raise DynamicRankingError(
                "router_dynamic single-route residual intercept must be between "
                "-1 and 1"
            )
        for section in ("centers", "coefficients"):
            _require_exact_config_keys(
                config,
                (
                    "single_route_calibration",
                    "model_residual_coefficients",
                    identity,
                    section,
                ),
                _SINGLE_ROUTE_RESIDUAL_FEATURE_KEYS,
            )
            for feature in sorted(_SINGLE_ROUTE_RESIDUAL_FEATURE_KEYS):
                value = _ranking_number(
                    config,
                    "single_route_calibration",
                    "model_residual_coefficients",
                    identity,
                    section,
                    feature,
                )
                if section == "centers" and not 0.0 <= value <= 1.0:
                    raise DynamicRankingError(
                        "router_dynamic single-route residual centers must be "
                        "between 0 and 1"
                    )
                if section == "coefficients" and not -10.0 <= value <= 10.0:
                    raise DynamicRankingError(
                        "router_dynamic single-route residual coefficients must be "
                        "between -10 and 10"
                    )
    if enabled and residual_weight > 0.0 and not residual_rows:
        raise DynamicRankingError(
            "router_dynamic enabled single-route residual calibration requires "
            "model_residual_coefficients"
        )
    if enabled and residual_weight > 0.0:
        missing_activation_residuals = sorted(
            set(activation_identities) - set(residual_rows)
        )
        if missing_activation_residuals:
            raise DynamicRankingError(
                "router_dynamic enabled single-route residual calibration lacks "
                f"activation identities {missing_activation_residuals}"
            )

    expected_tiers = {str(tier) for tier in TIERS}
    for token_map_name in ("input_tokens_by_tier", "output_tokens_by_tier"):
        values = _ranking_mapping(
            config,
            "single_route_calibration",
            token_map_name,
        )
        if values and set(values) != expected_tiers:
            raise DynamicRankingError(
                "router_dynamic single-route predicted-cost token maps must be "
                f"empty or contain tiers {sorted(expected_tiers)}"
            )
        for tier in values:
            tokens = _ranking_int(
                config,
                "single_route_calibration",
                token_map_name,
                tier,
            )
            if tokens <= 0:
                raise DynamicRankingError(
                    "router_dynamic single-route predicted-cost token counts "
                    "must be positive"
                )
    if predicted_total_cost_enabled and (
        not policy["input_tokens_by_tier"]
        or not policy["output_tokens_by_tier"]
    ):
        raise DynamicRankingError(
            "router_dynamic enabled single-route predicted cost requires complete "
            "input and output token maps"
        )


def _validate_cache_affinity_config(config: Mapping[str, Any]) -> None:
    session = _ranking_mapping(config, "session")
    if "kv_cache_affinity" not in session:
        return
    _ranking_mapping(config, "session", "kv_cache_affinity")
    strategy = _ranking_string(config, "session", "kv_cache_affinity", "strategy")
    common_keys = {"strategy", "topologies", "ttl_seconds", "age_decay"}
    if strategy == "bonus":
        strategy_key = "bonus_by_evidence"
    elif strategy == "expected_cost":
        strategy_key = "hit_probability_by_evidence"
    else:
        raise DynamicRankingError(
            "router_dynamic ranking config session.kv_cache_affinity.strategy "
            "must be bonus or expected_cost"
        )
    _require_exact_config_keys(
        config,
        ("session", "kv_cache_affinity"),
        {*common_keys, strategy_key},
    )
    topologies = _ranking_string_list(
        config,
        "session",
        "kv_cache_affinity",
        "topologies",
    )
    if not topologies or not set(topologies).issubset({"single", "multiple"}):
        raise DynamicRankingError(
            "router_dynamic ranking config session.kv_cache_affinity.topologies "
            "must be a non-empty unique list of single or multiple"
        )
    if (
        _ranking_number(
            config,
            "session",
            "kv_cache_affinity",
            "ttl_seconds",
        )
        <= 0.0
    ):
        raise DynamicRankingError(
            "router_dynamic ranking config session.kv_cache_affinity.ttl_seconds must be positive"
        )
    age_decay = _ranking_string(
        config,
        "session",
        "kv_cache_affinity",
        "age_decay",
    )
    if age_decay not in {"none", "linear"}:
        raise DynamicRankingError(
            "router_dynamic ranking config session.kv_cache_affinity.age_decay "
            "must be none or linear"
        )
    evidence_values = _ranking_mapping(
        config,
        "session",
        "kv_cache_affinity",
        strategy_key,
    )
    if set(evidence_values) != {"read_hit", "write_only"}:
        raise DynamicRankingError(
            "router_dynamic ranking config session.kv_cache_affinity."
            f"{strategy_key} must contain read_hit and write_only"
        )
    maximum_bonus = _ranking_number(config, "session", "score_delta")
    for evidence_kind in ("read_hit", "write_only"):
        value = _ranking_number(
            config,
            "session",
            "kv_cache_affinity",
            strategy_key,
            evidence_kind,
        )
        if value < 0.0 or (strategy == "bonus" and value > maximum_bonus):
            suffix = (
                "must be non-negative and no greater than session.score_delta"
                if strategy == "bonus"
                else "must be between 0 and 1"
            )
            raise DynamicRankingError(
                "router_dynamic ranking config session.kv_cache_affinity."
                f"{strategy_key}.{evidence_kind} {suffix}"
            )
        if strategy == "expected_cost" and value > 1.0:
            raise DynamicRankingError(
                "router_dynamic ranking config session.kv_cache_affinity."
                f"{strategy_key}.{evidence_kind} must be between 0 and 1"
            )


def _thinking_assignment_policy(
    config: Mapping[str, Any],
    *,
    allow_legacy_external_switch: bool = False,
) -> dict[str, Any]:
    """Return the strictly validated, versioned unified-thinking policy."""

    thinking_assignment = _ranking_mapping(config, "thinking_assignment")
    legacy_external_switch = allow_legacy_external_switch and "enabled" not in thinking_assignment

    _require_exact_config_keys(
        config,
        ("thinking_assignment",),
        {
            *(set() if legacy_external_switch else {"enabled"}),
            "policy_version",
            "level_order",
            "tier_mapping",
            "aggregator_level_step",
            "risk_floor",
            "resource_constraints",
        },
    )

    enabled = (
        True if legacy_external_switch else _ranking_bool(config, "thinking_assignment", "enabled")
    )
    _require_exact_config_keys(
        config,
        ("thinking_assignment", "tier_mapping"),
        set(TIERS),
    )
    _require_exact_config_keys(
        config,
        ("thinking_assignment", "risk_floor"),
        {"high"},
    )
    _require_exact_config_keys(
        config,
        ("thinking_assignment", "resource_constraints"),
        {
            "cost_values",
            "latency_values",
            "downshift_levels",
        },
    )

    policy_version = _ranking_string(config, "thinking_assignment", "policy_version")
    if policy_version != THINKING_POLICY_VERSION:
        raise DynamicRankingError(
            "router_dynamic ranking config thinking_assignment.policy_version "
            f"must be {THINKING_POLICY_VERSION}"
        )

    level_order = _ranking_string_list(
        config,
        "thinking_assignment",
        "level_order",
    )
    if tuple(level_order) != UNIFIED_THINKING_LEVELS:
        raise DynamicRankingError(
            "router_dynamic ranking config thinking_assignment.level_order "
            f"must be {list(UNIFIED_THINKING_LEVELS)}"
        )

    tier_mapping_raw = _ranking_mapping(
        config,
        "thinking_assignment",
        "tier_mapping",
    )
    tier_mapping = {
        tier: _ranking_string(
            config,
            "thinking_assignment",
            "tier_mapping",
            tier,
        )
        for tier in TIERS
    }
    expected_tier_mapping = dict(zip(TIERS, UNIFIED_THINKING_LEVELS, strict=True))
    if tier_mapping != expected_tier_mapping or set(tier_mapping_raw) != set(TIERS):
        raise DynamicRankingError(
            "router_dynamic ranking config thinking_assignment.tier_mapping "
            f"must be {expected_tier_mapping}"
        )

    aggregator_level_step = _ranking_int(
        config,
        "thinking_assignment",
        "aggregator_level_step",
    )
    if aggregator_level_step != 1:
        raise DynamicRankingError(
            "router_dynamic ranking config thinking_assignment.aggregator_level_step must be 1"
        )

    risk_floor = {
        "high": _ranking_string(
            config,
            "thinking_assignment",
            "risk_floor",
            "high",
        )
    }
    if risk_floor != {"high": "high"}:
        raise DynamicRankingError(
            "router_dynamic ranking config thinking_assignment.risk_floor must be {'high': 'high'}"
        )

    cost_values = _ranking_string_list(
        config,
        "thinking_assignment",
        "resource_constraints",
        "cost_values",
    )
    latency_values = _ranking_string_list(
        config,
        "thinking_assignment",
        "resource_constraints",
        "latency_values",
    )
    if set(cost_values) != {"low", "hard_limit"}:
        raise DynamicRankingError(
            "router_dynamic ranking config thinking_assignment.resource_constraints."
            "cost_values must contain low and hard_limit"
        )
    if set(latency_values) != {"interactive", "hard_timeout"}:
        raise DynamicRankingError(
            "router_dynamic ranking config thinking_assignment.resource_constraints."
            "latency_values must contain interactive and hard_timeout"
        )
    downshift_levels = _ranking_int(
        config,
        "thinking_assignment",
        "resource_constraints",
        "downshift_levels",
    )
    if downshift_levels != 1:
        raise DynamicRankingError(
            "router_dynamic ranking config thinking_assignment.resource_constraints."
            "downshift_levels must be 1"
        )

    return {
        "enabled": enabled,
        "policy_version": policy_version,
        "level_order": tuple(level_order),
        "tier_mapping": tier_mapping,
        "aggregator_level_step": aggregator_level_step,
        "risk_floor": risk_floor,
        "resource_constraints": {
            "cost_values": frozenset(cost_values),
            "latency_values": frozenset(latency_values),
            "downshift_levels": downshift_levels,
        },
    }


@_wrap_validated_ranking_config_builder
def _validate_ranking_config(
    raw: Any,
    *,
    allow_legacy_external_thinking_switch: bool = False,
) -> _ValidatedRankingConfig:
    if not isinstance(raw, Mapping):
        raise DynamicRankingError("router_dynamic ranking config must be an object")
    config = _detached_ranking_config(raw)
    schema_version = _ranking_string(config, "schema_version")
    if schema_version not in {
        RANKING_CONFIG_SCHEMA_VERSION,
        LEGACY_RANKING_CONFIG_SCHEMA_VERSION,
    }:
        raise DynamicRankingError(
            "router_dynamic ranking config schema_version must be "
            f"{LEGACY_RANKING_CONFIG_SCHEMA_VERSION} or "
            f"{RANKING_CONFIG_SCHEMA_VERSION}"
        )
    has_thinking_policy = "thinking_assignment" in config
    legacy_external_thinking_switch = False
    if has_thinking_policy and schema_version == RANKING_CONFIG_SCHEMA_VERSION:
        thinking_assignment = _ranking_mapping(config, "thinking_assignment")
        legacy_external_thinking_switch = (
            allow_legacy_external_thinking_switch and "enabled" not in thinking_assignment
        )
    if schema_version == RANKING_CONFIG_SCHEMA_VERSION and not has_thinking_policy:
        raise DynamicRankingError("router_dynamic ranking config v4 requires thinking_assignment")
    if schema_version == LEGACY_RANKING_CONFIG_SCHEMA_VERSION and has_thinking_policy:
        raise DynamicRankingError(
            "router_dynamic legacy ranking config cannot declare thinking_assignment"
        )
    config_version = _ranking_string(config, "config_version")
    penalties_config = _ranking_mapping(config, "penalties")
    has_latency_penalty_policy = "latency_penalty_enabled" in penalties_config
    predates_resource_aware_rerank = _is_pre_resource_aware_rerank_config_version(config_version)
    if has_latency_penalty_policy and predates_resource_aware_rerank:
        raise DynamicRankingError(
            "router_dynamic historical ranking config cannot declare "
            "penalties.latency_penalty_enabled"
        )
    if not has_latency_penalty_policy and not predates_resource_aware_rerank:
        raise DynamicRankingError(
            "router_dynamic ranking config lacks the versioned latency penalty policy"
        )
    has_role_reliability = "role_reliability" in config
    if not has_role_reliability and not _is_pre_role_reliability_config_version(config_version):
        raise DynamicRankingError(
            "router_dynamic ranking config lacks the versioned role_reliability policy"
        )
    has_single_route_calibration = "single_route_calibration" in config
    base_required_sections = (
        "validation",
        "trace",
        "routing_tiers",
        "context",
        "task_profile_schema",
        "task_analyzer",
        "fallback_task_profile",
        "mock_user_profile",
        "synthetic_model",
        "hard_filter",
        "exploration",
        "normalization",
        "task_match",
        "user_score",
        "quality",
        *(("role_reliability",) if has_role_reliability else ()),
        "penalties",
        "session",
        "proposer_count",
        "rerank",
        "aggregator",
        *(
            ("single_route_calibration",)
            if has_single_route_calibration
            else ()
        ),
    )
    required_sections = (
        (*base_required_sections, "thinking_assignment")
        if has_thinking_policy
        else base_required_sections
    )
    for key in required_sections:
        _ranking_mapping(config, key)
    if set(config) != {"schema_version", "config_version", *required_sections}:
        raise DynamicRankingError(
            "router_dynamic ranking config has unknown or missing top-level keys"
        )
    proposer_count_config = _ranking_mapping(config, "proposer_count")
    aggregator_config = _ranking_mapping(config, "aggregator")
    session_config = _ranking_mapping(config, "session")
    has_cache_affinity = "kv_cache_affinity" in session_config
    has_backup_count = "backup_count" in proposer_count_config
    has_aggregator_candidate_count = "candidate_count" in aggregator_config
    if has_backup_count != has_aggregator_candidate_count:
        raise DynamicRankingError(
            "router_dynamic ranking config must declare proposer_count.backup_count "
            "and aggregator.candidate_count together"
        )
    has_roster_policy = has_backup_count and has_aggregator_candidate_count
    if not has_roster_policy and not _is_pre_roster_ranking_config_version(config_version):
        raise DynamicRankingError(
            "router_dynamic ranking config lacks the versioned selection roster policy"
        )
    task_analyzer_config = _ranking_mapping(config, "task_analyzer")
    analyzer_policy_keys = {
        "provider",
        "model",
        "upstream_provider",
        "stream_close_timeout_seconds",
    }
    present_analyzer_policy_keys = set(task_analyzer_config) & analyzer_policy_keys
    if present_analyzer_policy_keys and present_analyzer_policy_keys != analyzer_policy_keys:
        raise DynamicRankingError(
            "router_dynamic task_analyzer identity policy must declare provider, model, "
            "upstream_provider, and stream_close_timeout_seconds together"
        )
    has_task_analyzer_policy = present_analyzer_policy_keys == analyzer_policy_keys
    if not has_task_analyzer_policy and not _is_pre_task_analyzer_policy_config_version(
        config_version
    ):
        raise DynamicRankingError(
            "router_dynamic ranking config lacks the versioned task_analyzer identity policy"
        )
    analyzer_chain_policy_keys = {
        "fallback_chain",
        "total_timeout_seconds",
        "payload_max_chars",
        "payload_max_bytes",
        "payload_max_estimated_tokens",
        "schema_repair_max_retries",
    }
    present_analyzer_chain_policy_keys = set(task_analyzer_config) & analyzer_chain_policy_keys
    if present_analyzer_chain_policy_keys and (
        present_analyzer_chain_policy_keys != analyzer_chain_policy_keys
    ):
        raise DynamicRankingError(
            "router_dynamic task_analyzer chain and payload policy must be declared together"
        )
    has_analyzer_chain_policy = present_analyzer_chain_policy_keys == analyzer_chain_policy_keys
    fixed_object_keys = {
        ("validation",): {
            "weight_sum_tolerance",
            "task_profile_sum_tolerance",
        },
        ("trace",): {
            "profile_decimal_places",
            "score_decimal_places",
            "session_nonzero_epsilon",
        },
        ("routing_tiers",): {"default_router_tier", "mapping"},
        ("thinking_assignment",): {
            *(set() if legacy_external_thinking_switch else {"enabled"}),
            "policy_version",
            "level_order",
            "tier_mapping",
            "aggregator_level_step",
            "risk_floor",
            "resource_constraints",
        },
        ("thinking_assignment", "tier_mapping"): set(TIERS),
        ("thinking_assignment", "risk_floor"): {"high"},
        ("thinking_assignment", "resource_constraints"): {
            "cost_values",
            "latency_values",
            "downshift_levels",
        },
        ("context",): {
            "bucket_min_tokens",
            "default_bucket",
            "request_limits",
            "token_estimation",
            "output_budget",
        },
        ("context", "request_limits"): {
            "role_max_chars",
            "max_recent_turns",
            "fallback_history_max_turns",
            "turn_max_chars",
            "summary_max_chars",
            "state_max_items",
            "item_max_chars",
            "tool_summary_max_chars",
            "test_results_max_chars",
            "intermediate_max_items",
            "intermediate_max_chars",
            "attachment_max_items",
            "last_route_max_models",
            "max_scanned_items_multiplier",
        },
        ("context", "token_estimation"): {
            "utf8_bytes_per_token",
            "dense_chars_per_token",
            "candidate_chars_per_token",
        },
        ("context", "output_budget"): {"default_tokens", "minimum_tokens"},
        ("task_profile_schema",): {
            "constraint_values",
            "session_intents",
            "default_session_intent",
        },
        ("task_analyzer",): {
            *(analyzer_policy_keys if has_task_analyzer_policy else set()),
            *(analyzer_chain_policy_keys if has_analyzer_chain_policy else set()),
            "timeout_seconds",
            "input_max_chars",
            "response_max_chars",
            "max_output_tokens",
            "temperature",
            "thinking",
            "default_confidence",
            "max_retries",
            "truncation_head_fraction",
        },
        ("fallback_task_profile",): {
            "capability_dist",
            "domain_dist",
            "constraints",
            "risk_by_tier",
            "session_intent",
        },
        ("fallback_task_profile", "constraints"): {"cost", "latency"},
        ("fallback_task_profile", "risk_by_tier"): set(TIERS),
        ("fallback_task_profile", "session_intent"): {"type", "confidence"},
        ("mock_user_profile",): {
            "profile_version",
            "profile_source",
            "permission",
            "preference",
            "history",
        },
        ("mock_user_profile", "permission"): {
            "allow_models",
            "deny_models",
            "risk_allowlist",
        },
        ("mock_user_profile", "preference"): {
            "quality_latency_tradeoff",
            "cost_sensitivity",
        },
        ("mock_user_profile", "history"): {
            "positive_model_ids",
            "negative_model_ids",
            "feedback_count",
            "last_updated_at",
        },
        ("synthetic_model",): {
            "family_name_parts",
            "thinking",
            "version",
            "status",
            "context_window",
            "effective_context_bucket",
            "price_input_per_million",
            "price_output_per_million",
            "latency_p50_ms",
            "latency_p95_ms",
            "quota",
            "rate_limit",
            "health",
            "base_strength_by_tier",
            "tier_strength_penalty_per_level",
            "aggregator_role_fit_minimum",
            "aggregator_role_fit_penalty",
        },
        ("hard_filter",): {
            "eligible_statuses",
            "unavailable_health_states",
            "unavailable_quota_states",
            "unavailable_rate_limit_states",
            "default_health",
            "default_quota",
            "default_rate_limit",
            "default_required_modalities",
        },
        ("exploration",): {"enabled", "decision_propensity"},
        ("normalization",): {
            "price_reference_usd_per_million",
            "latency_reference_ms",
            "price_input_weight",
            "price_output_weight",
        },
        ("task_match",): {
            "capability_weight",
            "domain_weight",
            "tier_weight",
            "proposer_task_weight",
            "proposer_role_fit_weight",
            "context_underqualified_multiplier",
            "format_base_multiplier",
            "format_strength_multiplier",
            "missing_strength_default",
            "missing_role_fit_default",
        },
        ("user_score",): {
            "neutral_score",
            "history_signal_weight",
            "feedback_saturation_count",
        },
        ("quality",): {"task_match_weight", "user_score_weight"},
        **(
            {
                ("role_reliability",): {
                    "penalty_weight",
                    "prior_success",
                    "prior_failure",
                }
            }
            if has_role_reliability
            else {}
        ),
        ("penalties",): {
            *({"latency_penalty_enabled"} if has_latency_penalty_policy else set()),
            "task_cost_weights",
            "task_latency_weights",
            "user_cost_sensitivity_weights",
            "default_cost_weight",
            "default_latency_weight",
            "latency_first_adjustment",
            "quality_first_latency_reduction",
            "quality_first_cost_reduction",
            "quality_first_minimum_weight",
        },
        ("session",): {
            "intent_confidence_threshold",
            "score_delta",
            "max_escalation_level",
            "default_quality_feedback",
            "route_cache_max_entries",
            *(("kv_cache_affinity",) if has_cache_affinity else ()),
        },
        ("proposer_count",): {
            "effective_tier_rounding_offset",
            *(("backup_count",) if has_roster_policy else ()),
            "by_tier",
            "high_risk",
            "constrained_max",
            "constrained_cost_values",
            "constrained_latency_values",
            "constrained_user_cost_values",
            "constrained_user_tradeoffs",
        },
        ("proposer_count", "high_risk"): {"min", "max"},
        ("rerank",): {
            "top_l_min",
            "top_l_multiplier",
            "quality_floor_margin_by_risk",
            "default_quality_floor_margin",
            "quality_weight",
            "coverage_gain_weight",
            "error_complementarity_weight",
            "similarity_penalty_weight",
            "stop_threshold",
            "trace_top_candidates",
            "similarity",
            "error_dimensions",
        },
        ("rerank", "similarity"): {
            "capability_weight",
            "lineage_weight",
            "same_family_score",
            "same_vendor_score",
            "unrelated_score",
        },
        ("aggregator",): {
            *(("candidate_count",) if has_roster_policy else ()),
            *(("prompt_version",) if "prompt_version" in aggregator_config else ()),
            "task_match_weight",
            "role_fit_weight",
            "same_model_penalty",
            "same_family_or_vendor_penalty",
        },
    }
    if not has_thinking_policy:
        fixed_object_keys = {
            object_path: expected_keys
            for object_path, expected_keys in fixed_object_keys.items()
            if object_path[0] != "thinking_assignment"
        }
    for object_path, expected_keys in fixed_object_keys.items():
        _require_exact_config_keys(config, object_path, expected_keys)
    if has_latency_penalty_policy:
        _ranking_bool(config, "penalties", "latency_penalty_enabled")
    _validate_cache_affinity_config(config)
    _validate_single_route_calibration_config(config)
    if "prompt_version" in aggregator_config:
        from .aggregator_prompt import AGGREGATOR_PROMPT_VERSIONS

        prompt_version = _ranking_string(config, "aggregator", "prompt_version")
        if prompt_version not in AGGREGATOR_PROMPT_VERSIONS:
            supported = ", ".join(sorted(AGGREGATOR_PROMPT_VERSIONS))
            raise DynamicRankingError(
                "router_dynamic ranking config aggregator.prompt_version must be one "
                f"of {supported}"
            )
    if has_thinking_policy:
        _thinking_assignment_policy(
            config,
            allow_legacy_external_switch=legacy_external_thinking_switch,
        )

    weight_sum_tolerance = _ranking_number(config, "validation", "weight_sum_tolerance")
    if not 0.0 < weight_sum_tolerance < 1.0:
        raise DynamicRankingError(
            "router_dynamic validation.weight_sum_tolerance must be between 0 and 1"
        )
    task_profile_sum_tolerance = _ranking_number(config, "validation", "task_profile_sum_tolerance")
    if not 0.0 < task_profile_sum_tolerance < 1.0:
        raise DynamicRankingError(
            "router_dynamic validation.task_profile_sum_tolerance must be between 0 and 1"
        )

    weight_groups = (
        (
            ("normalization", "price_input_weight"),
            ("normalization", "price_output_weight"),
        ),
        (
            ("task_match", "capability_weight"),
            ("task_match", "domain_weight"),
            ("task_match", "tier_weight"),
        ),
        (
            ("task_match", "proposer_task_weight"),
            ("task_match", "proposer_role_fit_weight"),
        ),
        (
            ("task_match", "format_base_multiplier"),
            ("task_match", "format_strength_multiplier"),
        ),
        (
            ("quality", "task_match_weight"),
            ("quality", "user_score_weight"),
        ),
        (
            ("rerank", "similarity", "capability_weight"),
            ("rerank", "similarity", "lineage_weight"),
        ),
        (
            ("aggregator", "task_match_weight"),
            ("aggregator", "role_fit_weight"),
        ),
    )
    for group in weight_groups:
        weight_values = [_ranking_number(config, *weight_path) for weight_path in group]
        if any(value < 0.0 for value in weight_values) or not math.isclose(
            sum(weight_values), 1.0, abs_tol=weight_sum_tolerance
        ):
            dotted = ", ".join(".".join(weight_path) for weight_path in group)
            raise DynamicRankingError(
                f"router_dynamic ranking weights must be non-negative and sum to 1: {dotted}"
            )

    for path in (
        ("trace", "profile_decimal_places"),
        ("trace", "score_decimal_places"),
    ):
        if _ranking_int(config, *path) < 0:
            raise DynamicRankingError(
                f"router_dynamic ranking config {'.'.join(path)} cannot be negative"
            )
    session_nonzero_epsilon = _ranking_number(config, "trace", "session_nonzero_epsilon")
    if not 0.0 < session_nonzero_epsilon <= 1.0:
        raise DynamicRankingError(
            "router_dynamic trace.session_nonzero_epsilon must be between 0 and 1"
        )

    router_tier_mapping = _router_tier_mapping(config)
    if (
        set(router_tier_mapping) != _ROUTER_TIERS
        or len(set(router_tier_mapping.values())) != len(router_tier_mapping)
        or set(str(value) for value in router_tier_mapping.values()) != set(TIERS)
    ):
        raise DynamicRankingError(
            "router_dynamic routing_tiers.mapping must map c0-c3 one-to-one to task tiers"
        )
    default_router_tier = _ranking_string(config, "routing_tiers", "default_router_tier")
    if default_router_tier not in router_tier_mapping:
        raise DynamicRankingError("router_dynamic routing_tiers.default_router_tier is invalid")

    bucket_min_tokens = _context_bucket_min_tokens(config)
    if (
        set(bucket_min_tokens) != _CONTEXT_BUCKETS
        or any(value < 0 for value in bucket_min_tokens.values())
        or len(set(bucket_min_tokens.values())) != len(bucket_min_tokens)
        or [bucket_min_tokens[name] for name in _CONTEXT_BUCKET_ORDER]
        != sorted(bucket_min_tokens.values())
    ):
        raise DynamicRankingError(
            "router_dynamic context.bucket_min_tokens must define unique short-to-extra-long "
            "thresholds"
        )
    default_bucket = _ranking_string(config, "context", "default_bucket")
    if default_bucket not in bucket_min_tokens:
        raise DynamicRankingError(
            "router_dynamic context.default_bucket must exist in bucket_min_tokens"
        )
    if bucket_min_tokens[default_bucket] != min(bucket_min_tokens.values()):
        raise DynamicRankingError(
            "router_dynamic context.default_bucket must have the lowest token threshold"
        )
    for key in (
        "role_max_chars",
        "max_recent_turns",
        "fallback_history_max_turns",
        "turn_max_chars",
        "summary_max_chars",
        "state_max_items",
        "item_max_chars",
        "tool_summary_max_chars",
        "test_results_max_chars",
        "intermediate_max_items",
        "intermediate_max_chars",
        "attachment_max_items",
        "last_route_max_models",
        "max_scanned_items_multiplier",
    ):
        if _ranking_int(config, "context", "request_limits", key) <= 0:
            raise DynamicRankingError(
                f"router_dynamic context.request_limits.{key} must be positive"
            )
    for key in (
        "utf8_bytes_per_token",
        "dense_chars_per_token",
        "candidate_chars_per_token",
    ):
        if _ranking_number(config, "context", "token_estimation", key) <= 0.0:
            raise DynamicRankingError(
                f"router_dynamic context.token_estimation.{key} must be positive"
            )
    for key in ("default_tokens", "minimum_tokens"):
        if _ranking_int(config, "context", "output_budget", key) <= 0:
            raise DynamicRankingError(
                f"router_dynamic context.output_budget.{key} must be positive"
            )
    if _ranking_int(config, "context", "output_budget", "default_tokens") < _ranking_int(
        config, "context", "output_budget", "minimum_tokens"
    ):
        raise DynamicRankingError(
            "router_dynamic context.output_budget.default_tokens cannot be below minimum_tokens"
        )

    constraint_values = _ranking_mapping(config, "task_profile_schema", "constraint_values")
    for key, expected_values in _CONSTRAINT_VALUES.items():
        configured_values = _ranking_string_set(
            config, "task_profile_schema", "constraint_values", key
        )
        if configured_values != expected_values:
            raise DynamicRankingError(
                "router_dynamic task_profile_schema.constraint_values."
                f"{key} must match the supported protocol values"
            )
    if set(constraint_values) != set(_CONSTRAINT_VALUES):
        raise DynamicRankingError(
            "router_dynamic task_profile_schema.constraint_values has invalid keys"
        )
    session_intents = _ranking_string_set(config, "task_profile_schema", "session_intents")
    default_intent = _ranking_string(config, "task_profile_schema", "default_session_intent")
    if session_intents != _SESSION_INTENTS or default_intent != _DEFAULT_SESSION_INTENT:
        raise DynamicRankingError(
            "router_dynamic task_profile_schema session intents must match the supported "
            "protocol values"
        )

    for key in (
        "input_max_chars",
        "response_max_chars",
        "max_output_tokens",
    ):
        if _ranking_int(config, "task_analyzer", key) <= 0:
            raise DynamicRankingError(f"router_dynamic task_analyzer.{key} must be positive")
    if _ranking_number(config, "task_analyzer", "timeout_seconds") <= 0.0:
        raise DynamicRankingError("router_dynamic task_analyzer.timeout_seconds must be positive")
    if has_analyzer_chain_policy:
        total_timeout = _ranking_number(
            config,
            "task_analyzer",
            "total_timeout_seconds",
        )
        if total_timeout <= 0.0:
            raise DynamicRankingError(
                "router_dynamic task_analyzer.total_timeout_seconds must be positive"
            )
        for key in (
            "payload_max_chars",
            "payload_max_bytes",
            "payload_max_estimated_tokens",
        ):
            if _ranking_int(config, "task_analyzer", key) <= 0:
                raise DynamicRankingError(f"router_dynamic task_analyzer.{key} must be positive")
        schema_repair_max_retries = _ranking_int(
            config,
            "task_analyzer",
            "schema_repair_max_retries",
        )
        if not 0 <= schema_repair_max_retries <= 1:
            raise DynamicRankingError(
                "router_dynamic task_analyzer.schema_repair_max_retries must be 0 or 1"
            )
        fallback_chain = task_analyzer_config.get("fallback_chain")
        if not isinstance(fallback_chain, list):
            raise DynamicRankingError(
                "router_dynamic task_analyzer.fallback_chain must be a JSON array"
            )
        normalized_routes: list[tuple[str, str, str]] = []
        for index, route in enumerate(fallback_chain):
            if not isinstance(route, Mapping) or set(route) != {
                "provider",
                "model",
                "upstream_provider",
            }:
                raise DynamicRankingError(
                    "router_dynamic task_analyzer fallback route at index "
                    f"{index} must contain exactly provider, model, and upstream_provider"
                )
            try:
                candidate = TaskAnalyzerCandidate(
                    provider_id=str(route["provider"]),
                    model_id=str(route["model"]),
                    upstream_provider=str(route["upstream_provider"]),
                )
            except ValueError as exc:
                raise DynamicRankingError(
                    f"router_dynamic task_analyzer fallback route at index {index} is invalid"
                ) from exc
            if candidate.provider_id != TASK_ANALYZER_PROVIDER_ID:
                raise DynamicRankingError(
                    "router_dynamic task_analyzer fallback providers currently must be openrouter"
                )
            normalized_routes.append(
                (
                    candidate.provider_id,
                    candidate.model_id,
                    candidate.upstream_provider,
                )
            )
        primary_route = (
            str(task_analyzer_config.get("provider") or ""),
            str(task_analyzer_config.get("model") or ""),
            str(task_analyzer_config.get("upstream_provider") or ""),
        )
        if len({primary_route, *normalized_routes}) != len(normalized_routes) + 1:
            raise DynamicRankingError(
                "router_dynamic task_analyzer chain cannot contain duplicate routes"
            )
    if has_task_analyzer_policy:
        analyzer_provider = _ranking_string(config, "task_analyzer", "provider")
        if (
            analyzer_provider != analyzer_provider.casefold()
            or analyzer_provider != str(task_analyzer_config.get("provider"))
            or analyzer_provider != TASK_ANALYZER_PROVIDER_ID
        ):
            raise DynamicRankingError(
                "router_dynamic task_analyzer.provider currently must be openrouter"
            )
        analyzer_model = _ranking_string(config, "task_analyzer", "model")
        if (
            analyzer_model != analyzer_model.casefold()
            or analyzer_model != str(task_analyzer_config.get("model"))
            or any(character.isspace() for character in analyzer_model)
            or "/" not in analyzer_model
            or any(not segment for segment in analyzer_model.split("/"))
        ):
            raise DynamicRankingError(
                "router_dynamic task_analyzer.model must be lowercase, trimmed, "
                "contain '/', and contain no whitespace"
            )
        upstream_provider = _ranking_string(
            config,
            "task_analyzer",
            "upstream_provider",
        )
        if (
            upstream_provider != upstream_provider.casefold()
            or upstream_provider != str(task_analyzer_config.get("upstream_provider"))
            or _TASK_ANALYZER_UPSTREAM_PROVIDER_RE.fullmatch(upstream_provider) is None
        ):
            raise DynamicRankingError(
                "router_dynamic task_analyzer.upstream_provider must be a lowercase "
                "provider slug or auto"
            )
        stream_close_timeout = _ranking_number(
            config,
            "task_analyzer",
            "stream_close_timeout_seconds",
        )
        analyzer_timeout = _ranking_number(config, "task_analyzer", "timeout_seconds")
        if not 0.0 < stream_close_timeout <= min(analyzer_timeout, 60.0):
            raise DynamicRankingError(
                "router_dynamic task_analyzer.stream_close_timeout_seconds must be "
                "positive, at most 60 seconds, and no greater than timeout_seconds"
            )
    analyzer_temperature = _ranking_number(config, "task_analyzer", "temperature")
    if analyzer_temperature < 0.0:
        raise DynamicRankingError("router_dynamic task_analyzer.temperature cannot be negative")
    _ranking_bool(config, "task_analyzer", "thinking")
    analyzer_max_retries = _ranking_int(config, "task_analyzer", "max_retries")
    if not 0 <= analyzer_max_retries <= 10:
        raise DynamicRankingError(
            "router_dynamic task_analyzer.max_retries must be between 0 and 10"
        )

    def validate_distribution(path: tuple[str, ...], allowed: set[str]) -> None:
        distribution = _ranking_mapping(config, *path)
        if not distribution or not set(distribution).issubset(allowed):
            raise DynamicRankingError(
                f"router_dynamic ranking config {'.'.join(path)} has invalid dimensions"
            )
        total = 0.0
        for key in distribution:
            value = _ranking_number(config, *path, str(key))
            if value < 0.0:
                raise DynamicRankingError(
                    f"router_dynamic ranking config {'.'.join(path)}.{key} cannot be negative"
                )
            total += value
        if not math.isclose(total, 1.0, abs_tol=weight_sum_tolerance):
            raise DynamicRankingError(
                f"router_dynamic ranking config {'.'.join(path)} must sum to 1"
            )

    validate_distribution(("fallback_task_profile", "capability_dist"), set(CAPABILITIES))
    validate_distribution(("fallback_task_profile", "domain_dist"), set(DOMAINS))
    fallback_constraints = _ranking_mapping(config, "fallback_task_profile", "constraints")
    for key in ("cost", "latency"):
        if _ranking_string(config, "fallback_task_profile", "constraints", key) not in {
            str(value)
            for value in _ranking_value(config, "task_profile_schema", "constraint_values", key)
        }:
            raise DynamicRankingError(
                f"router_dynamic fallback_task_profile.constraints.{key} is invalid"
            )
    if set(fallback_constraints) != {"cost", "latency"}:
        raise DynamicRankingError(
            "router_dynamic fallback_task_profile.constraints has invalid keys"
        )
    risk_values = _ranking_string_set(config, "task_profile_schema", "constraint_values", "risk")
    for tier in TIERS:
        if (
            _ranking_string(config, "fallback_task_profile", "risk_by_tier", tier)
            not in risk_values
        ):
            raise DynamicRankingError(
                f"router_dynamic fallback_task_profile.risk_by_tier.{tier} is invalid"
            )
    if (
        _ranking_string(config, "fallback_task_profile", "session_intent", "type")
        not in session_intents
    ):
        raise DynamicRankingError(
            "router_dynamic fallback_task_profile.session_intent.type is invalid"
        )

    for key in ("allow_models", "deny_models"):
        _ranking_string_list(config, "mock_user_profile", "permission", key)
    if not _ranking_string_set(
        config, "mock_user_profile", "permission", "risk_allowlist"
    ).issubset(risk_values):
        raise DynamicRankingError(
            "router_dynamic mock_user_profile.permission.risk_allowlist is invalid"
        )

    if (
        _ranking_string(config, "mock_user_profile", "preference", "quality_latency_tradeoff")
        not in _USER_TRADEOFFS
    ):
        raise DynamicRankingError(
            "router_dynamic mock_user_profile.preference.quality_latency_tradeoff is invalid"
        )
    cost_sensitivity = _ranking_string(
        config, "mock_user_profile", "preference", "cost_sensitivity"
    )
    if cost_sensitivity not in _ranking_mapping(
        config, "penalties", "user_cost_sensitivity_weights"
    ):
        raise DynamicRankingError(
            "router_dynamic mock_user_profile.preference.cost_sensitivity is invalid"
        )
    _ranking_string_list(config, "mock_user_profile", "history", "positive_model_ids")
    _ranking_string_list(config, "mock_user_profile", "history", "negative_model_ids")
    if _ranking_int(config, "mock_user_profile", "history", "feedback_count") < 0:
        raise DynamicRankingError(
            "router_dynamic mock_user_profile.history.feedback_count cannot be negative"
        )
    _ranking_string(config, "mock_user_profile", "history", "last_updated_at")
    _ranking_string(config, "mock_user_profile", "profile_version")
    _ranking_string(config, "mock_user_profile", "profile_source")

    for key in (
        "family_name_parts",
        "context_window",
        "latency_p50_ms",
        "latency_p95_ms",
    ):
        if _ranking_int(config, "synthetic_model", key) <= 0:
            raise DynamicRankingError(f"router_dynamic synthetic_model.{key} must be positive")
    for key in (
        "price_input_per_million",
        "price_output_per_million",
        "tier_strength_penalty_per_level",
        "aggregator_role_fit_minimum",
        "aggregator_role_fit_penalty",
    ):
        if _ranking_number(config, "synthetic_model", key) < 0.0:
            raise DynamicRankingError(f"router_dynamic synthetic_model.{key} cannot be negative")
    for key in (
        "thinking",
        "version",
        "status",
        "effective_context_bucket",
        "quota",
        "rate_limit",
        "health",
    ):
        _ranking_string(config, "synthetic_model", key)
    for tier in TIERS:
        strength = _ranking_number(config, "synthetic_model", "base_strength_by_tier", tier)
        if not 0.0 <= strength <= 1.0:
            raise DynamicRankingError(
                f"router_dynamic synthetic_model.base_strength_by_tier.{tier} is invalid"
            )
    if set(_ranking_mapping(config, "synthetic_model", "base_strength_by_tier")) != set(TIERS):
        raise DynamicRankingError(
            "router_dynamic synthetic_model.base_strength_by_tier has invalid keys"
        )

    for key in (
        "eligible_statuses",
        "unavailable_health_states",
        "unavailable_quota_states",
        "unavailable_rate_limit_states",
        "default_required_modalities",
    ):
        if not _ranking_string_set(config, "hard_filter", key):
            raise DynamicRankingError(f"router_dynamic hard_filter.{key} cannot be empty")
    default_modalities = _ranking_string_set(config, "hard_filter", "default_required_modalities")
    if not default_modalities.issubset(set(MODALITIES)):
        raise DynamicRankingError(
            "router_dynamic hard_filter.default_required_modalities is invalid"
        )
    for key in ("default_health", "default_quota", "default_rate_limit"):
        _ranking_string(config, "hard_filter", key)
    exploration_enabled = _ranking_bool(config, "exploration", "enabled")
    if (
        _ranking_string(config, "synthetic_model", "effective_context_bucket")
        not in bucket_min_tokens
    ):
        raise DynamicRankingError(
            "router_dynamic synthetic_model.effective_context_bucket is invalid"
        )
    synthetic_bucket = _ranking_string(config, "synthetic_model", "effective_context_bucket")
    if (
        _ranking_int(config, "synthetic_model", "context_window")
        < bucket_min_tokens[synthetic_bucket]
    ):
        raise DynamicRankingError(
            "router_dynamic synthetic_model.context_window is smaller than its context bucket"
        )
    if _ranking_int(config, "synthetic_model", "latency_p50_ms") > _ranking_int(
        config, "synthetic_model", "latency_p95_ms"
    ):
        raise DynamicRankingError("router_dynamic synthetic_model latency p50 cannot exceed p95")

    unit_interval_paths = (
        ("task_analyzer", "default_confidence"),
        ("task_analyzer", "truncation_head_fraction"),
        ("fallback_task_profile", "session_intent", "confidence"),
        ("task_match", "context_underqualified_multiplier"),
        ("task_match", "format_base_multiplier"),
        ("task_match", "format_strength_multiplier"),
        ("task_match", "missing_strength_default"),
        ("task_match", "missing_role_fit_default"),
        ("user_score", "neutral_score"),
        ("user_score", "history_signal_weight"),
        *((("role_reliability", "penalty_weight"),) if has_role_reliability else ()),
        ("session", "intent_confidence_threshold"),
        ("session", "default_quality_feedback"),
        ("session", "score_delta"),
        ("proposer_count", "effective_tier_rounding_offset"),
        ("rerank", "default_quality_floor_margin"),
        ("rerank", "similarity", "same_family_score"),
        ("rerank", "similarity", "same_vendor_score"),
        ("rerank", "similarity", "unrelated_score"),
        ("aggregator", "same_model_penalty"),
        ("aggregator", "same_family_or_vendor_penalty"),
        ("synthetic_model", "tier_strength_penalty_per_level"),
        ("synthetic_model", "aggregator_role_fit_minimum"),
        ("synthetic_model", "aggregator_role_fit_penalty"),
        ("exploration", "decision_propensity"),
    )
    for unit_path in unit_interval_paths:
        unit_value = _ranking_number(config, *unit_path)
        if not 0.0 <= unit_value <= 1.0:
            raise DynamicRankingError(
                f"router_dynamic ranking config {'.'.join(unit_path)} must be between 0 and 1"
            )
    decision_propensity = _ranking_number(config, "exploration", "decision_propensity")
    if exploration_enabled or decision_propensity != 1.0:
        raise DynamicRankingError(
            "router_dynamic exploration is reserved and must remain disabled with propensity 1"
        )

    if has_role_reliability:
        prior_success = _ranking_int(config, "role_reliability", "prior_success")
        prior_failure = _ranking_int(config, "role_reliability", "prior_failure")
        if prior_success < 0 or prior_failure < 0:
            raise DynamicRankingError(
                "router_dynamic role_reliability priors must be non-negative integers"
            )
        if prior_success + prior_failure <= 0:
            raise DynamicRankingError(
                "router_dynamic role_reliability priors must have a positive total"
            )

    nonnegative_paths = (
        ("penalties", "default_cost_weight"),
        ("penalties", "default_latency_weight"),
        ("penalties", "latency_first_adjustment"),
        ("penalties", "quality_first_latency_reduction"),
        ("penalties", "quality_first_cost_reduction"),
        ("penalties", "quality_first_minimum_weight"),
        ("rerank", "quality_weight"),
        ("rerank", "coverage_gain_weight"),
        ("rerank", "error_complementarity_weight"),
        ("rerank", "similarity_penalty_weight"),
    )
    for nonnegative_path in nonnegative_paths:
        if _ranking_number(config, *nonnegative_path) < 0.0:
            raise DynamicRankingError(
                f"router_dynamic ranking config {'.'.join(nonnegative_path)} cannot be negative"
            )

    numeric_mapping_keys = {
        ("penalties", "task_cost_weights"): _CONSTRAINT_VALUES["cost"],
        ("penalties", "task_latency_weights"): _CONSTRAINT_VALUES["latency"],
        ("penalties", "user_cost_sensitivity_weights"): _USER_COST_SENSITIVITIES,
        ("rerank", "quality_floor_margin_by_risk"): _CONSTRAINT_VALUES["risk"],
    }
    for mapping_path, expected_keys in numeric_mapping_keys.items():
        mapping_values = _ranking_mapping(config, *mapping_path)
        if set(mapping_values) != expected_keys:
            raise DynamicRankingError(
                "router_dynamic ranking config "
                f"{'.'.join(mapping_path)} must define the supported protocol values"
            )
        for mapping_key in mapping_values:
            numeric_value = _ranking_number(config, *mapping_path, str(mapping_key))
            if numeric_value < 0.0:
                raise DynamicRankingError(
                    "router_dynamic ranking config "
                    f"{'.'.join(mapping_path)}.{mapping_key} must be a non-negative number"
                )
            if mapping_path == ("rerank", "quality_floor_margin_by_risk") and numeric_value > 1.0:
                raise DynamicRankingError(
                    "router_dynamic rerank quality-floor margins cannot exceed 1"
                )
    for tier in TIERS:
        tier_bounds = _ranking_mapping(config, "proposer_count", "by_tier", tier)
        if set(tier_bounds) != {"min", "max"}:
            raise DynamicRankingError(
                f"router_dynamic proposer_count.by_tier.{tier} has invalid keys"
            )
        minimum = _ranking_int(config, "proposer_count", "by_tier", tier, "min")
        maximum = _ranking_int(config, "proposer_count", "by_tier", tier, "max")
        if minimum < 1 or maximum < minimum:
            raise DynamicRankingError(
                f"router_dynamic proposer_count.by_tier.{tier} has invalid bounds"
            )
    if set(_ranking_mapping(config, "proposer_count", "by_tier")) != set(TIERS):
        raise DynamicRankingError("router_dynamic proposer_count.by_tier has invalid keys")
    high_risk_minimum = _ranking_int(config, "proposer_count", "high_risk", "min")
    high_risk_maximum = _ranking_int(config, "proposer_count", "high_risk", "max")
    if high_risk_minimum < 1 or high_risk_maximum < high_risk_minimum:
        raise DynamicRankingError("router_dynamic proposer_count.high_risk has invalid bounds")
    if _ranking_int(config, "proposer_count", "constrained_max") < 1:
        raise DynamicRankingError("router_dynamic proposer_count.constrained_max must be positive")
    if has_roster_policy:
        backup_count = _ranking_int(config, "proposer_count", "backup_count")
        if not 0 <= backup_count <= 2:
            raise DynamicRankingError(
                "router_dynamic proposer_count.backup_count must be between 0 and 2"
            )
        aggregator_candidate_count = _ranking_int(
            config,
            "aggregator",
            "candidate_count",
        )
        if not 1 <= aggregator_candidate_count <= 3:
            raise DynamicRankingError(
                "router_dynamic aggregator.candidate_count must be between 1 and 3"
            )
    if _ranking_int(config, "session", "max_escalation_level") < 0:
        raise DynamicRankingError("router_dynamic session.max_escalation_level cannot be negative")
    if _ranking_int(config, "session", "route_cache_max_entries") <= 0:
        raise DynamicRankingError("router_dynamic session.route_cache_max_entries must be positive")
    for path in (
        ("normalization", "price_reference_usd_per_million"),
        ("normalization", "latency_reference_ms"),
    ):
        if _ranking_number(config, *path) <= 0.0:
            raise DynamicRankingError(
                f"router_dynamic ranking config {'.'.join(path)} must be positive"
            )
    for path in (
        ("user_score", "feedback_saturation_count"),
        ("rerank", "top_l_min"),
        ("rerank", "top_l_multiplier"),
        ("rerank", "trace_top_candidates"),
    ):
        if _ranking_int(config, *path) <= 0:
            raise DynamicRankingError(
                f"router_dynamic ranking config {'.'.join(path)} must be a positive integer"
            )
    _ranking_number(config, "rerank", "stop_threshold")
    constrained_cost_values = _ranking_string_set(
        config, "proposer_count", "constrained_cost_values"
    )
    constrained_latency_values = _ranking_string_set(
        config, "proposer_count", "constrained_latency_values"
    )
    constrained_user_cost_values = _ranking_string_set(
        config, "proposer_count", "constrained_user_cost_values"
    )
    constrained_user_tradeoffs = _ranking_string_set(
        config, "proposer_count", "constrained_user_tradeoffs"
    )
    if not constrained_cost_values.issubset(_CONSTRAINT_VALUES["cost"]):
        raise DynamicRankingError(
            "router_dynamic proposer_count.constrained_cost_values is invalid"
        )
    if not constrained_latency_values.issubset(_CONSTRAINT_VALUES["latency"]):
        raise DynamicRankingError(
            "router_dynamic proposer_count.constrained_latency_values is invalid"
        )
    if not constrained_user_cost_values.issubset(
        set(_ranking_mapping(config, "penalties", "user_cost_sensitivity_weights"))
    ):
        raise DynamicRankingError(
            "router_dynamic proposer_count.constrained_user_cost_values is invalid"
        )
    if not constrained_user_tradeoffs.issubset(_USER_TRADEOFFS):
        raise DynamicRankingError(
            "router_dynamic proposer_count.constrained_user_tradeoffs is invalid"
        )
    error_dimensions = _ranking_string_list(config, "rerank", "error_dimensions")
    if not error_dimensions:
        raise DynamicRankingError("router_dynamic rerank.error_dimensions cannot be empty")
    return config


# The generic authenticator is intentionally unavailable after the strict
# validator is constructed. Candidate derivation uses the separately bounded
# capability created by the same private factory.
del _wrap_validated_ranking_config_builder


@cache
def _packaged_ranking_config() -> _ValidatedRankingConfig:
    try:
        path = resources.files("opensquilla.provider").joinpath(
            "router_dynamic_ranking_config.json"
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surfaced as a precise routing error
        raise DynamicRankingError("router_dynamic ranking config unavailable") from exc
    return _validate_ranking_config(payload)


def _normalize_ranking_config_override(
    value: Any,
    *,
    path: tuple[str, ...] = (),
) -> Any:
    """Return a detached, strictly JSON-compatible override value."""

    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                location = ".".join(path) or "<root>"
                raise DynamicRankingError(
                    f"router_dynamic ranking config override must use string keys at {location}"
                )
            normalized[raw_key] = _normalize_ranking_config_override(
                child,
                path=(*path, raw_key),
            )
        return normalized
    if isinstance(value, list):
        return [
            _normalize_ranking_config_override(child, path=(*path, str(index)))
            for index, child in enumerate(value)
        ]
    if value is None or isinstance(value, (str, bool, int)):
        return copy.deepcopy(value)
    if isinstance(value, float) and math.isfinite(value):
        return value
    location = ".".join(path) or "<root>"
    raise DynamicRankingError(
        f"router_dynamic ranking config override must contain only JSON values at {location}"
    )


def _assert_no_sensitive_ranking_config_override_keys(
    value: Any,
    *,
    path: tuple[str, ...] = (),
) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = raw_key.strip().casefold().replace("-", "_")
            child_path = (*path, raw_key)
            if (
                any(fragment in key for fragment in _TRACE_SECRET_KEY_FRAGMENTS)
                or key in {"token", "bearer", "proxy"}
                or key.endswith("_token")
            ):
                raise DynamicRankingError(
                    "router_dynamic ranking config override contains secret-like field "
                    f"{'.'.join(child_path)}"
                )
            _assert_no_sensitive_ranking_config_override_keys(child, path=child_path)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_sensitive_ranking_config_override_keys(
                child,
                path=(*path, str(index)),
            )


def _deep_merge_ranking_config_override(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    merged = _detached_ranking_config(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge_ranking_config_override(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _ranking_config_for_base_version(
    base_version: str | None,
) -> _ValidatedRankingConfig:
    """Select the packaged policy or one exact allowlisted historical base."""

    packaged = _packaged_ranking_config()
    if base_version is None:
        return packaged
    if not isinstance(base_version, str) or not base_version.strip():
        raise DynamicRankingError(
            "router_dynamic ranking config base_version must be a non-empty string"
        )
    requested = base_version.strip()
    packaged_version = _ranking_string(packaged, "config_version")
    if requested == packaged_version:
        return packaged
    if requested not in _HISTORICAL_RANKING_CONFIG_BASE_VERSIONS:
        raise DynamicRankingError(
            f"router_dynamic ranking config base_version {requested!r} is not available"
        )
    historical = _detached_ranking_config(packaged)
    historical["config_version"] = requested
    historical.pop("single_route_calibration", None)
    historical["normalization"]["price_reference_usd_per_million"] = 20.0
    historical["penalties"]["task_cost_weights"]["low"] = 0.20
    historical["task_match"]["capability_weight"] = 0.45
    historical["task_match"]["domain_weight"] = 0.25
    historical["task_match"]["tier_weight"] = 0.30
    if requested == _PRE_C1_BASELINE_RANKING_CONFIG_VERSION:
        return _validate_ranking_config(historical)
    if requested == _PRE_TOGETHER_TASK_ANALYZER_RANKING_CONFIG_VERSION:
        historical["task_analyzer"]["upstream_provider"] = "deepseek"
        return _validate_ranking_config(historical)
    historical["task_analyzer"]["model"] = "anthropic/claude-opus-4.8"
    historical["task_analyzer"]["upstream_provider"] = "anthropic"
    if requested == _PRE_DEEPSEEK_TASK_ANALYZER_RANKING_CONFIG_VERSION:
        return _validate_ranking_config(historical)
    historical["task_analyzer"]["schema_repair_max_retries"] = 1
    if requested == _PRE_SCHEMA_REPAIR_RANKING_CONFIG_VERSION:
        return _validate_ranking_config(historical)
    historical["normalization"]["price_reference_usd_per_million"] = 40.0
    historical["penalties"].pop("latency_penalty_enabled", None)
    if requested == _PRE_RESOURCE_AWARE_RERANK_CONFIG_VERSION:
        return _validate_ranking_config(historical)
    historical["task_analyzer"]["max_retries"] = 3
    historical["penalties"]["task_cost_weights"] = {
        "low": 0.20,
        "medium": 0.10,
        "high": 0.04,
        "hard_limit": 0.28,
    }
    historical["penalties"]["user_cost_sensitivity_weights"] = {
        "low": 0.04,
        "medium": 0.10,
        "high": 0.20,
        "hard_limit": 0.28,
    }
    historical["penalties"]["default_cost_weight"] = 0.10
    historical["rerank"]["top_l_min"] = 8
    if requested == _PREVIOUS_PACKAGED_RANKING_CONFIG_VERSION:
        historical["penalties"]["task_cost_weights"] = {
            "low": 0.28,
            "medium": 0.20,
            "high": 0.10,
            "hard_limit": 0.40,
        }
        historical["penalties"]["user_cost_sensitivity_weights"] = {
            "low": 0.04,
            "medium": 0.20,
            "high": 0.28,
            "hard_limit": 0.40,
        }
        historical["penalties"]["default_cost_weight"] = 0.20
        historical["rerank"]["top_l_min"] = 15
    if requested not in {
        _PREVIOUS_PACKAGED_RANKING_CONFIG_VERSION,
        _PREVIOUS_COST_BALANCE_RANKING_CONFIG_VERSION,
        _PREVIOUS_ZERO_FAILURE_PRIOR_RANKING_CONFIG_VERSION,
    }:
        analyzer = historical.get("task_analyzer")
        if isinstance(analyzer, dict):
            for key in (
                "fallback_chain",
                "total_timeout_seconds",
                "payload_max_chars",
                "payload_max_bytes",
                "payload_max_estimated_tokens",
                "schema_repair_max_retries",
            ):
                analyzer.pop(key, None)
    if requested in {
        _PREVIOUS_ZERO_FAILURE_PRIOR_RANKING_CONFIG_VERSION,
        _PRE_ANALYZER_CHAIN_RANKING_CONFIG_VERSION,
    }:
        historical["role_reliability"] = {
            "penalty_weight": 0.40,
            "prior_success": 10,
            "prior_failure": 0,
        }
    elif requested == _PREVIOUS_RELIABILITY_RANKING_CONFIG_VERSION:
        historical["role_reliability"] = {
            "penalty_weight": 0.40,
            "prior_success": 9,
            "prior_failure": 1,
        }
    elif requested not in {
        _PREVIOUS_PACKAGED_RANKING_CONFIG_VERSION,
        _PREVIOUS_COST_BALANCE_RANKING_CONFIG_VERSION,
    }:
        historical.pop("role_reliability", None)
    return _validate_ranking_config(historical)


def load_ranking_config(*, base_version: str | None = None) -> dict[str, Any]:
    """Return an isolated copy of the selected Step2 ranking parameters."""

    return _detached_ranking_config(_ranking_config_for_base_version(base_version))


@cache
def _packaged_legacy_ranking_config() -> _ValidatedRankingConfig:
    return _validate_ranking_config(_legacy_ranking_config_projection(_packaged_ranking_config()))


@cache
def _packaged_enabled_ranking_config() -> _ValidatedRankingConfig:
    """Return the packaged v4 policy with its compatibility switch enabled."""

    enabled = _detached_ranking_config(_packaged_ranking_config())
    enabled["thinking_assignment"]["enabled"] = True
    return _validate_ranking_config(enabled)


def ranking_config_resolution(
    *,
    thinking_assignment_enabled: bool | None = None,
    override: Mapping[str, Any] | None = None,
    base_version: str | None = None,
) -> dict[str, Any]:
    """Resolve and fingerprint one immutable runtime ranking configuration.

    The packaged policy is the authoritative base.  A supplied override is a
    sparse JSON object layered on top of that base before the existing strict
    full-config validator runs.  Identity fields are derived, never caller
    controlled, so traces can bind an override to one deterministic version.
    """

    if thinking_assignment_enabled is not None and not isinstance(
        thinking_assignment_enabled,
        bool,
    ):
        raise DynamicRankingError(
            "router_dynamic legacy thinking assignment switch must be a boolean"
        )
    packaged_base = _ranking_config_for_base_version(base_version)
    packaged_default_enabled = _ranking_bool(
        packaged_base,
        "thinking_assignment",
        "enabled",
    )
    compatibility_enabled = (
        thinking_assignment_enabled
        if thinking_assignment_enabled is not None
        else packaged_default_enabled
    )
    full_base = _detached_ranking_config(packaged_base)
    full_base["thinking_assignment"]["enabled"] = compatibility_enabled
    base = _validate_ranking_config(
        full_base if compatibility_enabled else _legacy_ranking_config_projection(full_base)
    )
    base_config = _detached_ranking_config(base)
    base_sha256 = _canonical_hash(base_config)
    if override is None:
        return {
            "base_config": copy.deepcopy(base_config),
            "override": None,
            "effective_config": copy.deepcopy(base_config),
            "base_sha256": base_sha256,
            "override_sha256": None,
            "effective_sha256": base_sha256,
            "thinking_assignment_enabled": compatibility_enabled,
        }
    if not isinstance(override, Mapping):
        raise DynamicRankingError("router_dynamic ranking config override must be a JSON object")
    normalized_override = _normalize_ranking_config_override(override)
    if not normalized_override:
        return {
            "base_config": copy.deepcopy(base_config),
            "override": None,
            "effective_config": copy.deepcopy(base_config),
            "base_sha256": base_sha256,
            "override_sha256": None,
            "effective_sha256": base_sha256,
            "thinking_assignment_enabled": compatibility_enabled,
        }
    if "schema_version" in normalized_override or "config_version" in normalized_override:
        raise DynamicRankingError(
            "router_dynamic ranking config override cannot override "
            "schema_version or config_version"
        )
    _assert_no_sensitive_ranking_config_override_keys(normalized_override)
    _assert_public_ranking_trace_payload(
        normalized_override,
        label="router_dynamic ranking config override",
    )
    thinking_override = normalized_override.get("thinking_assignment")
    explicit_override_enabled: bool | None = None
    if isinstance(thinking_override, Mapping) and "enabled" in thinking_override:
        raw_override_enabled = thinking_override["enabled"]
        if not isinstance(raw_override_enabled, bool):
            raise DynamicRankingError(
                "router_dynamic ranking config thinking_assignment.enabled must be boolean"
            )
        explicit_override_enabled = raw_override_enabled
    if (
        thinking_assignment_enabled is not None
        and explicit_override_enabled is not None
        and explicit_override_enabled is not thinking_assignment_enabled
    ):
        raise DynamicRankingError(
            "router_dynamic ranking config override thinking_assignment.enabled "
            "conflicts with the legacy ranking_thinking_assignment_enabled switch"
        )
    override_sha256 = _canonical_hash(normalized_override)
    effective_full = _deep_merge_ranking_config_override(
        full_base,
        normalized_override,
    )
    analyzer_override = normalized_override.get("task_analyzer")
    if (
        isinstance(analyzer_override, Mapping)
        and "fallback_chain" not in analyzer_override
        and any(key in analyzer_override for key in ("provider", "model", "upstream_provider"))
    ):
        # Sparse overrides historically changed only the single Analyzer route.
        # With the packaged chain, retain the remaining backups but never replay
        # the newly selected primary as its own fallback. An explicitly supplied
        # fallback_chain remains strict and is validated without normalization.
        analyzer_config = effective_full.get("task_analyzer")
        if isinstance(analyzer_config, dict):
            primary_route = (
                str(analyzer_config.get("provider") or ""),
                str(analyzer_config.get("model") or ""),
                str(analyzer_config.get("upstream_provider") or ""),
            )
            fallback_chain = analyzer_config.get("fallback_chain")
            if isinstance(fallback_chain, list):
                analyzer_config["fallback_chain"] = [
                    route
                    for route in fallback_chain
                    if not isinstance(route, Mapping)
                    or (
                        str(route.get("provider") or ""),
                        str(route.get("model") or ""),
                        str(route.get("upstream_provider") or ""),
                    )
                    != primary_route
                ]
    effective_full["config_version"] = (
        f"{base_config['config_version']}+override.{override_sha256[:12]}"
    )
    validated_full = _validate_ranking_config(effective_full)
    effective_enabled = _ranking_bool(
        validated_full,
        "thinking_assignment",
        "enabled",
    )
    effective = (
        validated_full if effective_enabled else _legacy_ranking_config_projection(validated_full)
    )
    validated_effective = _validate_ranking_config(effective)
    effective_config = _detached_ranking_config(validated_effective)
    return {
        "base_config": copy.deepcopy(base_config),
        "override": copy.deepcopy(normalized_override),
        "effective_config": effective_config,
        "base_sha256": base_sha256,
        "override_sha256": override_sha256,
        "effective_sha256": _canonical_hash(effective_config),
        "thinking_assignment_enabled": effective_enabled,
    }


def ranking_config_snapshot(
    *,
    thinking_assignment_enabled: bool | None = None,
    override: Mapping[str, Any] | None = None,
    base_version: str | None = None,
) -> Mapping[str, Any]:
    """Return the validated effective packaged policy plus any sparse override.

    ``None`` means "use the packaged default", not "force the legacy policy
    off".  Keeping this helper on the same resolution path as startup freezing
    prevents a future packaged default change from producing two different
    router policies depending on which public helper a caller used.
    """

    resolution = ranking_config_resolution(
        thinking_assignment_enabled=thinking_assignment_enabled,
        override=override,
        base_version=base_version,
    )
    return _detached_ranking_config(resolution["effective_config"])


def _prepare_ranking_config(
    ranking_config: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Validate and recursively freeze one external config exactly once."""

    if _is_validated_ranking_config(ranking_config):
        return ranking_config
    return (
        _validate_ranking_config(ranking_config)
        if ranking_config is not None
        else _packaged_ranking_config()
    )


def _prepare_effective_ranking_config(
    ranking_config: Mapping[str, Any] | None,
    *,
    thinking_assignment_enabled: bool,
) -> Mapping[str, Any]:
    """Prepare the exact thinking/legacy policy consumed by one turn."""

    if not isinstance(thinking_assignment_enabled, bool):
        raise DynamicRankingError(
            "router_dynamic ranking_thinking_assignment_enabled must be a boolean"
        )
    if _is_validated_ranking_config(ranking_config):
        schema_version = ranking_config.get("schema_version")
        matches_mode = (
            schema_version == RANKING_CONFIG_SCHEMA_VERSION
            and _ranking_bool(ranking_config, "thinking_assignment", "enabled")
            if thinking_assignment_enabled
            else schema_version == LEGACY_RANKING_CONFIG_SCHEMA_VERSION
        )
        if matches_mode:
            return ranking_config
    if thinking_assignment_enabled:
        if ranking_config is None:
            return _packaged_enabled_ranking_config()
        source_thinking = ranking_config.get("thinking_assignment")
        if (
            ranking_config.get("schema_version") == RANKING_CONFIG_SCHEMA_VERSION
            and isinstance(source_thinking, Mapping)
            and source_thinking.get("enabled") is True
        ):
            return _validate_ranking_config(ranking_config)
        source_config = _detached_ranking_config(ranking_config)
        thinking_section = source_config.get("thinking_assignment")
        if isinstance(thinking_section, dict) and thinking_section.get("enabled") is False:
            # Compatibility adapter for callers that still pass the old
            # out-of-band switch together with the packaged v4 template.
            thinking_section["enabled"] = True
        return _validate_ranking_config(
            source_config,
            allow_legacy_external_thinking_switch=True,
        )
    source_config = ranking_config if ranking_config is not None else _packaged_ranking_config()
    if source_config.get("schema_version") == LEGACY_RANKING_CONFIG_SCHEMA_VERSION:
        return _validate_ranking_config(source_config)
    return _validate_ranking_config(_legacy_ranking_config_projection(source_config))


def _resolve_ranking_config(
    ranking_config: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    return _prepare_ranking_config(ranking_config)


def router_dynamic_cache_affinity_policy(
    ranking_config: Mapping[str, Any] | None = None,
    *,
    topology: CacheTopology,
) -> dict[str, Any] | None:
    """Return a detached policy only when cache affinity is enabled here."""

    if topology not in {"single", "multiple"}:
        raise DynamicRankingError(
            "router_dynamic cache affinity topology must be single or multiple"
        )
    policy = _cache_affinity_policy_for_topology(
        _resolve_ranking_config(ranking_config),
        topology,
    )
    return copy.deepcopy(dict(policy)) if policy is not None else None


def task_analyzer_policy(
    ranking_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the validated, public task-analyzer execution policy.

    Archived ranking configs predate these identity fields.  They replay with
    the exact historical Opus/OpenRouter defaults without mutating their
    authenticated config payload or hash.
    """

    effective = _resolve_ranking_config(ranking_config)
    analyzer = _ranking_mapping(effective, "task_analyzer")
    has_public_policy = all(
        key in analyzer
        for key in (
            "provider",
            "model",
            "upstream_provider",
            "stream_close_timeout_seconds",
        )
    )
    return {
        "protocol_version": TASK_ANALYZER_VERSION,
        "provider": (
            _ranking_string(effective, "task_analyzer", "provider")
            if has_public_policy
            else TASK_ANALYZER_PROVIDER_ID
        ),
        "model": (
            _ranking_string(effective, "task_analyzer", "model")
            if has_public_policy
            else TASK_ANALYZER_MODEL_ID
        ),
        "upstream_provider": (
            _ranking_string(effective, "task_analyzer", "upstream_provider")
            if has_public_policy
            else TASK_ANALYZER_UPSTREAM_PROVIDER
        ),
        "stream_close_timeout_seconds": (
            _ranking_number(
                effective,
                "task_analyzer",
                "stream_close_timeout_seconds",
            )
            if has_public_policy
            else TASK_ANALYZER_STREAM_CLOSE_TIMEOUT_SECONDS
        ),
        "timeout_seconds": _ranking_number(
            effective,
            "task_analyzer",
            "timeout_seconds",
        ),
        "max_retries": _ranking_int(
            effective,
            "task_analyzer",
            "max_retries",
        ),
    }


def task_analyzer_chain_policy(
    ranking_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the optional ordered Analyzer chain and its shared budget.

    Historical configs have no chain policy. Their single route and retry
    settings remain intact; callers can distinguish that path through
    ``configured`` rather than silently changing replay semantics.
    """

    effective = _resolve_ranking_config(ranking_config)
    analyzer = _ranking_mapping(effective, "task_analyzer")
    primary = task_analyzer_policy(effective)
    configured = all(
        key in analyzer
        for key in (
            "fallback_chain",
            "total_timeout_seconds",
            "payload_max_chars",
            "payload_max_bytes",
            "payload_max_estimated_tokens",
            "schema_repair_max_retries",
        )
    )
    fallback_routes = analyzer.get("fallback_chain") if configured else []
    if not isinstance(fallback_routes, Sequence) or isinstance(
        fallback_routes, (str, bytes, bytearray)
    ):
        raise DynamicRankingError(
            "router_dynamic task_analyzer.fallback_chain must be a JSON array"
        )
    detached_fallback_routes = _detached_ranking_config_value(fallback_routes)
    return {
        "configured": configured,
        "routes": [
            {
                "provider": str(primary["provider"]),
                "model": str(primary["model"]),
                "upstream_provider": str(primary["upstream_provider"]),
            },
            *detached_fallback_routes,
        ],
        "total_timeout_seconds": (
            _ranking_number(effective, "task_analyzer", "total_timeout_seconds")
            if configured
            else _ranking_number(effective, "task_analyzer", "timeout_seconds")
        ),
        "schema_repair_max_retries": (
            _ranking_int(effective, "task_analyzer", "schema_repair_max_retries")
            if configured
            else 0
        ),
    }


def default_session_quality_feedback() -> float:
    """Return the configured neutral feedback stored for a completed route."""

    return _clamp(
        _ranking_number(_packaged_ranking_config(), "session", "default_quality_feedback")
    )


def router_dynamic_route_cache_max_entries() -> int:
    """Return the configured per-process dynamic-route cache bound."""

    return _ranking_int(_packaged_ranking_config(), "session", "route_cache_max_entries")


def _normalize_distribution(
    raw: Any,
    allowed: Sequence[str],
    fallback: Mapping[str, float],
    *,
    sum_tolerance: float,
) -> tuple[dict[str, float], bool]:
    if not isinstance(raw, Mapping):
        return dict(fallback), False
    values: dict[str, float] = {}
    valid = True
    for name, value in raw.items():
        key = str(name)
        if key not in allowed:
            valid = False
            continue
        number = _json_number(value)
        if number is None or number < 0.0:
            valid = False
            continue
        if number > 0.0:
            values[key] = number
    total = sum(values.values())
    if total <= 0.0:
        return dict(fallback), False
    valid = valid and math.isclose(
        total,
        1.0,
        rel_tol=0.0,
        abs_tol=sum_tolerance,
    )
    return {key: value / total for key, value in values.items()}, valid


def _canonical_domain_key(value: Any) -> str:
    raw = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return "_".join(part for part in raw.split("_") if part)


def _normalize_domain_distribution(
    raw: Any,
    fallback: Mapping[str, float],
    *,
    sum_tolerance: float,
) -> tuple[dict[str, float], bool, bool]:
    """Return a valid domain distribution whenever usable domain mass exists.

    The analyzer is constrained with JSON Schema, but this final local boundary
    deliberately repairs harmless model drift: case/separator differences,
    numeric strings, unknown extra keys, and totals that need renormalizing.
    A payload with no positive mass on any supported domain remains invalid so
    the caller can retry rather than silently inventing a task domain.
    """

    if not isinstance(raw, Mapping):
        return dict(fallback), False, False
    values: dict[str, float] = {}
    repaired = False
    for name, value in raw.items():
        raw_key = str(name)
        key = _canonical_domain_key(raw_key)
        if key != raw_key:
            repaired = True
        if key not in DOMAINS:
            repaired = True
            continue
        number = _json_number(value)
        if number is None and isinstance(value, str):
            try:
                parsed = float(value.strip())
            except ValueError:
                parsed = math.nan
            if math.isfinite(parsed):
                number = parsed
                repaired = True
        if number is None or number < 0.0:
            repaired = True
            continue
        if number > 0.0:
            if key in values:
                repaired = True
            values[key] = values.get(key, 0.0) + number
    total = sum(values.values())
    if total <= 0.0:
        return dict(fallback), False, repaired
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=sum_tolerance):
        repaired = True
    return {key: value / total for key, value in values.items()}, True, repaired


@cache
def _task_analyzer_output_schema() -> dict[str, Any]:
    def distribution(keys: Sequence[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                key: {
                    "type": "number",
                    "description": "Non-negative weight; distribution must sum to 1.",
                }
                for key in keys
            },
            "required": list(keys),
        }

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "capability_dist": distribution(CAPABILITIES),
            "domain_dist": distribution(DOMAINS),
            "tier_dist": distribution(TIERS),
            "constraints": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "cost": {
                        "type": "string",
                        "enum": sorted(_CONSTRAINT_VALUES["cost"]),
                    },
                    "latency": {
                        "type": "string",
                        "enum": sorted(_CONSTRAINT_VALUES["latency"]),
                    },
                    "context": {
                        "type": "string",
                        "enum": list(_CONTEXT_BUCKET_ORDER),
                    },
                    "modality": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(MODALITIES)},
                    },
                    "risk": {
                        "type": "string",
                        "enum": sorted(_CONSTRAINT_VALUES["risk"]),
                    },
                },
                "required": ["cost", "latency", "context", "modality", "risk"],
            },
            "optional_constraints": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "format": {"type": "string", "enum": list(FORMATS)},
                },
                "required": ["format"],
            },
            "session_intent": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": sorted(_SESSION_INTENTS),
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Confidence from 0 through 1.",
                    },
                },
                "required": ["type", "confidence"],
            },
            "analysis_confidence": {
                "type": "number",
                "description": "Confidence from 0 through 1.",
            },
        },
        "required": [
            "capability_dist",
            "domain_dist",
            "tier_dist",
            "constraints",
            "optional_constraints",
            "session_intent",
            "analysis_confidence",
        ],
    }


def _merge_task_analyzer_usage(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> dict[str, Any]:
    previous_map = dict(previous or {})
    current_map = dict(current or {})
    if not previous_map:
        return copy.deepcopy(current_map)
    if not current_map:
        return copy.deepcopy(previous_map)
    merged = copy.deepcopy(previous_map)
    merged.update(copy.deepcopy(current_map))
    for key in (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_write_tokens",
    ):
        merged[key] = _as_int(previous_map.get(key), 0) + _as_int(current_map.get(key), 0)
    merged["billed_cost"] = _as_float(previous_map.get("billed_cost"), 0.0) + _as_float(
        current_map.get("billed_cost"), 0.0
    )
    previous_provider = previous_map.get("provider_usage")
    current_provider = current_map.get("provider_usage")
    if isinstance(previous_provider, Mapping) and isinstance(current_provider, Mapping):
        provider_usage = copy.deepcopy(dict(previous_provider))
        provider_usage.update(copy.deepcopy(dict(current_provider)))
        response_ids = [
            str(item)
            for source in (previous_provider, current_provider)
            for item in source.get("response_ids", [])
            if isinstance(item, str) and item
        ]
        if response_ids:
            provider_usage["response_ids"] = list(dict.fromkeys(response_ids))
        if (
            "provider_reported_cost" in previous_provider
            or "provider_reported_cost" in current_provider
        ):
            provider_usage["provider_reported_cost"] = _as_float(
                previous_provider.get("provider_reported_cost"), 0.0
            ) + _as_float(current_provider.get("provider_reported_cost"), 0.0)
        merged["provider_usage"] = provider_usage
    physical_attempts = [
        copy.deepcopy(dict(item))
        for source in (previous_map, current_map)
        for item in source.get("physical_attempts", [])
        if isinstance(item, Mapping)
    ]
    if physical_attempts:
        merged["physical_attempts"] = physical_attempts
    return merged


def _task_analyzer_physical_attempt_id(
    *,
    decision_id: str,
    request_context: Mapping[str, Any],
    message: str,
    attempt: int,
) -> str:
    """Return a stable identity for one analyzer provider request."""

    logical_scope = (
        decision_id.strip()
        or str(request_context.get("snapshot_hash") or "").strip()
        or hashlib.sha256(message.encode("utf-8")).hexdigest()
    )
    return hashlib.sha256(f"{logical_scope}:task_analyzer:{attempt}".encode()).hexdigest()[:32]


def _task_analyzer_physical_attempt_count(
    usage: Mapping[str, Any] | None,
) -> int:
    raw_attempts = (usage or {}).get("physical_attempts")
    return (
        len([row for row in raw_attempts if isinstance(row, Mapping)])
        if isinstance(raw_attempts, list)
        else 0
    )


def _task_analyzer_zero_request_usage(
    accumulated: Mapping[str, Any] | None,
) -> dict[str, Any]:
    usage = copy.deepcopy(dict(accumulated or {}))
    attempts = usage.get("physical_attempts")
    usage["physical_attempts"] = (
        [copy.deepcopy(dict(row)) for row in attempts if isinstance(row, Mapping)]
        if isinstance(attempts, list)
        else []
    )
    usage["attempt_count"] = len(usage["physical_attempts"])
    return usage


def _task_analyzer_usage_from_done(event: DoneEvent) -> dict[str, Any]:
    usage = {
        "provider": str(event.provider or ""),
        "model": event.model,
        "requested_provider": str(event.requested_provider or ""),
        "requested_model": str(event.requested_model or ""),
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "reasoning_tokens": event.reasoning_tokens,
        "cached_tokens": event.cached_tokens,
        "cache_write_tokens": event.cache_write_tokens,
        "billed_cost": event.billed_cost,
        "cost_source": event.cost_source,
        "provider_usage": copy.deepcopy(dict(event.provider_usage)),
    }
    if event.billing_receipt is not None:
        usage["billing_receipt"] = event.billing_receipt
    return usage


def _task_analyzer_usage_from_receipt_row(
    row: Mapping[str, Any],
) -> dict[str, Any]:
    usage = copy.deepcopy(dict(row))
    usage.pop("role", None)
    usage.pop("label", None)
    usage.pop("request_count", None)
    usage.pop("attempt", None)
    usage["cached_tokens"] = _as_int(
        usage.get("cached_tokens", usage.get("cache_read_tokens")),
        0,
    )
    usage.pop("cache_read_tokens", None)
    provider_usage = usage.get("provider_usage")
    if isinstance(provider_usage, Mapping):
        usage["provider_usage"] = copy.deepcopy(dict(provider_usage))
    else:
        usage["provider_usage"] = {}
    return usage


def _event_reported_physical_attempt_ids(event: object) -> list[str]:
    """Read additive physical-request evidence without requiring event fields."""

    raw_ids = [str(getattr(event, "physical_attempt_id", "") or "").strip()]
    provider_usage = getattr(event, "provider_usage", None)
    if isinstance(provider_usage, Mapping):
        raw_ids.append(str(provider_usage.get("physical_attempt_id") or "").strip())
    return raw_ids


def _task_analyzer_reported_physical_attempt_ids(
    event: ErrorEvent,
    receipt_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[str], bool]:
    """Return reported IDs and whether any source is invalid or contradictory."""

    raw_ids = _event_reported_physical_attempt_ids(event)
    diagnostic_done = event.diagnostic_done
    if isinstance(diagnostic_done, DoneEvent):
        raw_ids.extend(_event_reported_physical_attempt_ids(diagnostic_done))
    row_conflict = False
    for row in receipt_rows:
        direct = str(row.get("physical_attempt_id") or "").strip()
        provider_usage = row.get("provider_usage")
        nested = (
            str(provider_usage.get("physical_attempt_id") or "").strip()
            if isinstance(provider_usage, Mapping)
            else ""
        )
        if direct and nested and direct.casefold() != nested.casefold():
            row_conflict = True
        raw_ids.extend((direct, nested))
    nonempty_ids = [value.casefold() for value in raw_ids if value]
    invalid = any(_PHYSICAL_ATTEMPT_ID_RE.fullmatch(value) is None for value in nonempty_ids)
    unique_ids = list(dict.fromkeys(nonempty_ids))
    return unique_ids, bool(invalid or row_conflict or len(unique_ids) > 1)


def _task_analyzer_attempt_usage(
    usage: Mapping[str, Any] | None,
    *,
    attempt: int,
    physical_attempt_id: str,
    provider_id: str,
    model_id: str,
    unknown_reason: str = "",
) -> dict[str, Any]:
    """Preserve one physical analyzer attempt without inventing a receipt."""

    row = copy.deepcopy(dict(usage or {}))
    row.pop("physical_attempts", None)
    row.pop("attempt_count", None)
    has_usage = bool(row)
    row["attempt"] = attempt
    row["physical_attempt_id"] = physical_attempt_id
    row.setdefault("requested_provider", provider_id)
    row.setdefault("requested_model", model_id)
    if has_usage:
        raw_provider_usage = row.get("provider_usage")
        provider_usage = (
            copy.deepcopy(dict(raw_provider_usage))
            if isinstance(raw_provider_usage, Mapping)
            else {}
        )
        nested_physical_attempt_id = str(provider_usage.get("physical_attempt_id") or "").strip()
        if (
            nested_physical_attempt_id
            and nested_physical_attempt_id.casefold() != physical_attempt_id.casefold()
        ):
            raise TaskAnalyzerPhysicalEvidenceError(
                "task analyzer physical_attempt_id mirror is contradictory"
            )
        provider_usage["physical_attempt_id"] = physical_attempt_id
        row["provider_usage"] = provider_usage
        return row
    return {
        "attempt": attempt,
        "physical_attempt_id": physical_attempt_id,
        "provider": "",
        "model": "",
        "requested_provider": provider_id,
        "requested_model": model_id,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
        "usage_unknown": True,
        "unknown_reason": unknown_reason or "physical_receipt_unavailable",
        "provider_usage": {
            "usage_unknown": True,
            "unknown_reason": unknown_reason or "physical_receipt_unavailable",
            "physical_attempt_id": physical_attempt_id,
        },
    }


def _merge_task_analyzer_attempt(
    accumulated: Mapping[str, Any] | None,
    usage: Mapping[str, Any] | None,
    *,
    attempt: int,
    physical_attempt_id: str,
    provider_id: str,
    model_id: str,
    unknown_reason: str = "",
) -> dict[str, Any]:
    attempt_row = _task_analyzer_attempt_usage(
        usage,
        attempt=attempt,
        physical_attempt_id=physical_attempt_id,
        provider_id=provider_id,
        model_id=model_id,
        unknown_reason=unknown_reason,
    )
    current = copy.deepcopy(dict(usage or {}))
    current["physical_attempts"] = [attempt_row]
    merged = _merge_task_analyzer_usage(accumulated, current)
    merged["attempt_count"] = attempt
    return merged


def _merge_task_analyzer_error_evidence(
    accumulated: Mapping[str, Any] | None,
    receipt_rows: Sequence[Mapping[str, Any]],
    *,
    physical_request_count: int,
    reported_physical_attempt_ids: Sequence[str],
    decision_id: str,
    request_context: Mapping[str, Any],
    message: str,
    provider_id: str,
    model_id: str,
    unknown_reason: str,
) -> dict[str, Any]:
    """Preserve every proven error request before failing closed."""

    merged = _task_analyzer_zero_request_usage(accumulated)
    start_ordinal = _task_analyzer_physical_attempt_count(merged)
    for offset in range(physical_request_count):
        ordinal = start_ordinal + offset + 1
        row = (
            _task_analyzer_usage_from_receipt_row(receipt_rows[offset])
            if offset < len(receipt_rows)
            else {}
        )
        physical_attempt_id = (
            str(reported_physical_attempt_ids[offset])
            if offset < len(reported_physical_attempt_ids)
            and _PHYSICAL_ATTEMPT_ID_RE.fullmatch(str(reported_physical_attempt_ids[offset]))
            is not None
            else _task_analyzer_physical_attempt_id(
                decision_id=decision_id,
                request_context=request_context,
                message=message,
                attempt=ordinal,
            )
        )
        if row:
            provider_usage = row.get("provider_usage")
            nested_id = (
                str(provider_usage.get("physical_attempt_id") or "").strip()
                if isinstance(provider_usage, Mapping)
                else ""
            )
            direct_id = str(row.get("physical_attempt_id") or "").strip()
            conflicting_ids = list(
                dict.fromkeys(
                    value.casefold()
                    for value in (
                        direct_id,
                        nested_id,
                        physical_attempt_id,
                    )
                    if value
                )
            )
            if len(conflicting_ids) > 1:
                normalized_provider_usage = (
                    copy.deepcopy(dict(provider_usage))
                    if isinstance(provider_usage, Mapping)
                    else {}
                )
                normalized_provider_usage["reported_physical_attempt_ids"] = conflicting_ids
                normalized_provider_usage.pop(
                    "physical_attempt_id",
                    None,
                )
                row["provider_usage"] = normalized_provider_usage
                row.pop("physical_attempt_id", None)
        merged = _merge_task_analyzer_attempt(
            merged,
            row,
            attempt=ordinal,
            physical_attempt_id=physical_attempt_id,
            provider_id=provider_id,
            model_id=model_id,
            unknown_reason=unknown_reason,
        )
    return merged


def _router_tier(value: Any, ranking_config: Mapping[str, Any]) -> str:
    mapping = _router_tier_mapping(ranking_config)
    default = _ranking_string(ranking_config, "routing_tiers", "default_router_tier")
    raw = str(value or "").strip().lower()
    if raw in mapping:
        return raw
    if raw.startswith("t") and raw[1:].isdigit():
        tier = int(raw[1:])
        inverse = {value: key for key, value in mapping.items()}
        if tier in inverse:
            return inverse[tier]
    return default


def _context_bucket_for_tokens(tokens: int, ranking_config: Mapping[str, Any]) -> str:
    thresholds = _context_bucket_min_tokens(ranking_config)
    for bucket, minimum in sorted(thresholds.items(), key=lambda item: item[1], reverse=True):
        if tokens >= minimum:
            return bucket
    return _ranking_string(ranking_config, "context", "default_bucket")


def _bounded_recent_turn(value: Any, ranking_config: Mapping[str, Any]) -> Any:
    role_max_chars = _ranking_int(ranking_config, "context", "request_limits", "role_max_chars")
    turn_max_chars = _ranking_int(ranking_config, "context", "request_limits", "turn_max_chars")
    if isinstance(value, Mapping):
        role = str(value.get("role") or "")[:role_max_chars]
        content = value.get("content", value.get("text", ""))
        return {
            "role": role,
            "content": str(content)[:turn_max_chars],
        }
    return str(value)[:turn_max_chars]


def dynamic_output_token_budgets(
    *,
    configured_output_tokens: int,
    candidate_max_chars: int,
    ranking_config: Mapping[str, Any] | None = None,
) -> tuple[int, int]:
    """Return conservative candidate and aggregator output-token budgets."""

    effective_config = _resolve_ranking_config(ranking_config)
    minimum_tokens = _ranking_int(effective_config, "context", "output_budget", "minimum_tokens")
    default_tokens = _ranking_int(effective_config, "context", "output_budget", "default_tokens")
    aggregator_tokens = max(
        minimum_tokens,
        configured_output_tokens if configured_output_tokens > 0 else default_tokens,
    )
    if candidate_max_chars <= 0:
        return aggregator_tokens, aggregator_tokens
    # A character cap is not four ASCII characters per token for CJK and other
    # dense scripts. One token per retained character is a safer routing bound.
    # Ensemble members resolve their own generation caps, so the routed anchor's
    # configured cap must not reduce this candidate-text bound.
    chars_per_token = _ranking_number(
        effective_config, "context", "token_estimation", "candidate_chars_per_token"
    )
    candidate_tokens = math.ceil(candidate_max_chars / chars_per_token)
    return max(minimum_tokens, candidate_tokens), aggregator_tokens


def _bounded_text(value: Any, max_chars: int) -> str:
    if isinstance(value, str):
        return value[:max_chars]
    if value is None:
        return ""
    if isinstance(value, (Mapping, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)[
                :max_chars
            ]
        except (TypeError, ValueError):
            pass
    return str(value)[:max_chars]


def _bounded_string_list(
    value: Any,
    *,
    max_items: int,
    max_chars: int,
    max_scanned_items_multiplier: int,
) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    max_scanned_items = max_items * max_scanned_items_multiplier
    for scanned_items, item in enumerate(value, start=1):
        if scanned_items > max_scanned_items:
            break
        text = _bounded_text(item, max_chars).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= max_items:
            break
    return result


def _sanitize_last_route(value: Any, ranking_config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    last_route_max_models = _ranking_int(
        ranking_config, "context", "request_limits", "last_route_max_models"
    )
    item_max_chars = _ranking_int(ranking_config, "context", "request_limits", "item_max_chars")
    scan_multiplier = _ranking_int(
        ranking_config,
        "context",
        "request_limits",
        "max_scanned_items_multiplier",
    )
    selected_p = _bounded_string_list(
        value.get("selected_P"),
        max_items=last_route_max_models,
        max_chars=item_max_chars,
        max_scanned_items_multiplier=scan_multiplier,
    )
    selected_a = _bounded_text(value.get("selected_A"), item_max_chars).strip()
    if not selected_p and not selected_a:
        return {}
    default_feedback = _ranking_number(ranking_config, "session", "default_quality_feedback")
    max_escalation = _ranking_int(ranking_config, "session", "max_escalation_level")
    route: dict[str, Any] = {
        "selected_P": selected_p,
        "selected_A": selected_a,
        "quality_feedback": _clamp(_as_float(value.get("quality_feedback"), default_feedback)),
        "escalation_level": max(
            0,
            min(max_escalation, _as_int(value.get("escalation_level"), 0)),
        ),
    }
    return route


def _estimated_tokens_from_text(value: str, ranking_config: Mapping[str, Any]) -> int:
    bytes_per_token = _ranking_number(
        ranking_config, "context", "token_estimation", "utf8_bytes_per_token"
    )
    dense_chars_per_token = _ranking_number(
        ranking_config, "context", "token_estimation", "dense_chars_per_token"
    )
    ascii_chars = sum(character.isascii() for character in value)
    dense_chars = len(value) - ascii_chars
    return math.ceil(ascii_chars / bytes_per_token + dense_chars / dense_chars_per_token)


def mock_user_profile(
    ranking_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the replaceable Step2 chapter-4 global default profile."""

    effective_config = _resolve_ranking_config(ranking_config)
    return _detached_ranking_config(_ranking_mapping(effective_config, "mock_user_profile"))


def validate_user_profile(
    profile: Mapping[str, Any],
    ranking_config: Mapping[str, Any] | None = None,
) -> list[str]:
    """Reasons a stored profile cannot be trusted; empty when it is fine.

    ``profile.json`` is hand-editable and is the *only* configuration surface
    for ``deny_models``. A typo there must be rejected rather than silently
    ignored: ``_cost_latency_weights`` falls back to a default on an unknown
    ``cost_sensitivity``, so an invalid edit would route exactly as if it had
    never been made, and say nothing.

    This lives here rather than in ``profile.py`` because the vocabularies are
    defined here, and ``self_learning`` must not import from ``provider``.
    Reading them from the same ranking config as the mock validator
    (``_validate_ranking_config``) is what stops the two from drifting apart —
    a shared source rather than two lists that agree today.

    Only keys the file actually carries are checked; a partial profile is
    normal, and the seam fills the rest from the mock baseline.
    """

    effective = _resolve_ranking_config(ranking_config)
    errors: list[str] = []

    permission = profile.get("permission")
    if isinstance(permission, Mapping):
        risk_values = _ranking_string_set(
            effective, "task_profile_schema", "constraint_values", "risk"
        )
        allowlist = permission.get("risk_allowlist")
        if allowlist is not None:
            if not isinstance(allowlist, list) or not all(isinstance(v, str) for v in allowlist):
                errors.append("permission.risk_allowlist must be a list of strings")
            elif not set(allowlist).issubset(risk_values):
                unknown = sorted(set(allowlist) - risk_values)
                errors.append(f"permission.risk_allowlist has unknown values: {unknown}")
        for key in ("allow_models", "deny_models"):
            value = permission.get(key)
            if value is not None and (
                not isinstance(value, list) or not all(isinstance(v, str) for v in value)
            ):
                errors.append(f"permission.{key} must be a list of strings")

    preference = profile.get("preference")
    if isinstance(preference, Mapping):
        tradeoff = preference.get("quality_latency_tradeoff")
        if tradeoff is not None and tradeoff not in _USER_TRADEOFFS:
            errors.append(
                f"preference.quality_latency_tradeoff {tradeoff!r} is not one of "
                f"{sorted(_USER_TRADEOFFS)}"
            )
        sensitivity = preference.get("cost_sensitivity")
        if sensitivity is not None:
            known = _ranking_mapping(effective, "penalties", "user_cost_sensitivity_weights")
            if sensitivity not in known:
                errors.append(
                    f"preference.cost_sensitivity {sensitivity!r} is not one of {sorted(known)}"
                )

    history = profile.get("history")
    if isinstance(history, Mapping):
        for key in ("positive_model_ids", "negative_model_ids"):
            value = history.get(key)
            if value is not None and (
                not isinstance(value, list) or not all(isinstance(v, str) for v in value)
            ):
                errors.append(f"history.{key} must be a list of strings")
        count = history.get("feedback_count")
        if count is not None and (not isinstance(count, int) or count < 0):
            errors.append("history.feedback_count must be a non-negative integer")

    return errors


def _legacy_fusion_last_route(
    *,
    turn_metadata: Mapping[str, Any] | None,
    effective_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the existing B5 P/A route memory without changing its contract."""

    metadata = dict(turn_metadata or {})
    supplied = metadata.get("router_dynamic_request_context") or metadata.get("request_context")
    supplied_map = supplied if isinstance(supplied, Mapping) else {}
    supplied_last_route = supplied_map.get("last_route")
    if not isinstance(supplied_last_route, Mapping):
        supplied_last_route = metadata.get("router_dynamic_last_route") or metadata.get(
            "last_route"
        )
    return _sanitize_last_route(supplied_last_route, effective_config)


def _build_request_context_base(
    *,
    message: str,
    turn_metadata: Mapping[str, Any] | None,
    attachments: Sequence[Mapping[str, Any]] | None,
    last_route: Mapping[str, Any],
    include_previous_candidates: bool,
    effective_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Build request facts shared by fusion and direct routing.

    Output-role budgets and the final snapshot hash deliberately belong to the
    public mode-specific wrappers below.  Keeping this base role-neutral makes
    it impossible for direct routing to acquire proposer/aggregator budgeting
    as an implementation side effect.
    """

    limits = _ranking_mapping(effective_config, "context", "request_limits")
    max_recent_turns = _as_int(limits["max_recent_turns"])
    fallback_history_max_turns = _as_int(limits["fallback_history_max_turns"])
    turn_max_chars = _as_int(limits["turn_max_chars"])
    summary_max_chars = _as_int(limits["summary_max_chars"])
    state_max_items = _as_int(limits["state_max_items"])
    item_max_chars = _as_int(limits["item_max_chars"])
    tool_summary_max_chars = _as_int(limits["tool_summary_max_chars"])
    test_results_max_chars = _as_int(limits["test_results_max_chars"])
    intermediate_max_items = _as_int(limits["intermediate_max_items"])
    intermediate_max_chars = _as_int(limits["intermediate_max_chars"])
    attachment_max_items = _as_int(limits["attachment_max_items"])
    scan_multiplier = _as_int(limits["max_scanned_items_multiplier"])
    minimum_tokens = _ranking_int(effective_config, "context", "output_budget", "minimum_tokens")
    default_modalities = _ranking_string_list(
        effective_config, "hard_filter", "default_required_modalities"
    )
    metadata = dict(turn_metadata or {})
    supplied = metadata.get("router_dynamic_request_context") or metadata.get("request_context")
    supplied_map = supplied if isinstance(supplied, Mapping) else {}
    supplied_conversation = supplied_map.get("conversation")
    conversation_raw = supplied_conversation if isinstance(supplied_conversation, Mapping) else {}
    conversation: dict[str, Any] = {
        "summary": _bounded_text(conversation_raw.get("summary"), summary_max_chars)
    }
    recent_turns = conversation_raw.get("recent_turns")
    if not isinstance(recent_turns, Sequence) or isinstance(recent_turns, str):
        recent_turns = []
    conversation["recent_turns"] = [
        _bounded_recent_turn(value, effective_config)
        for value in deque(recent_turns, maxlen=max_recent_turns)
    ]
    if not conversation["recent_turns"]:
        history = metadata.get("router_history_user_texts")
        if isinstance(history, Sequence) and not isinstance(history, str):
            conversation["recent_turns"] = [
                f"user: {str(value)[:turn_max_chars]}"
                for value in deque(history, maxlen=fallback_history_max_turns)
            ]
        previous_assistant = str(metadata.get("router_prev_assistant_text") or "").strip()
        if previous_assistant:
            conversation["recent_turns"].append(f"assistant: {previous_assistant[:turn_max_chars]}")

    supplied_tool_state = supplied_map.get("tool_state")
    tool_raw = supplied_tool_state if isinstance(supplied_tool_state, Mapping) else {}
    tool_state = {
        "called_tools": _bounded_string_list(
            tool_raw.get("called_tools"),
            max_items=state_max_items,
            max_chars=item_max_chars,
            max_scanned_items_multiplier=scan_multiplier,
        ),
        "tool_results_summary": _bounded_text(
            tool_raw.get("tool_results_summary"),
            tool_summary_max_chars,
        ),
        "failed_tools": _bounded_string_list(
            tool_raw.get("failed_tools"),
            max_items=state_max_items,
            max_chars=item_max_chars,
            max_scanned_items_multiplier=scan_multiplier,
        ),
    }
    supplied_workspace = supplied_map.get("workspace_state")
    workspace_raw = supplied_workspace if isinstance(supplied_workspace, Mapping) else {}
    workspace_state = {
        "referenced_files": _bounded_string_list(
            workspace_raw.get("referenced_files"),
            max_items=state_max_items,
            max_chars=item_max_chars,
            max_scanned_items_multiplier=scan_multiplier,
        ),
        "changed_files": _bounded_string_list(
            workspace_raw.get("changed_files"),
            max_items=state_max_items,
            max_chars=item_max_chars,
            max_scanned_items_multiplier=scan_multiplier,
        ),
        "test_results": _bounded_text(
            workspace_raw.get("test_results") or "unknown",
            test_results_max_chars,
        ),
    }
    supplied_intermediate = supplied_map.get("intermediate_outputs")
    intermediate_raw = supplied_intermediate if isinstance(supplied_intermediate, Mapping) else {}
    intermediate_outputs: dict[str, Any] = {}
    if include_previous_candidates:
        intermediate_outputs["previous_candidates"] = _bounded_string_list(
            intermediate_raw.get("previous_candidates"),
            max_items=intermediate_max_items,
            max_chars=intermediate_max_chars,
            max_scanned_items_multiplier=scan_multiplier,
        )
    intermediate_outputs["current_errors"] = _bounded_string_list(
        intermediate_raw.get("current_errors"),
        max_items=intermediate_max_items,
        max_chars=intermediate_max_chars,
        max_scanned_items_multiplier=scan_multiplier,
    )
    modalities = list(default_modalities)
    attachment_refs: list[str] = []
    for index, attachment_value in enumerate(attachments or []):
        if index >= attachment_max_items:
            break
        attachment = attachment_value if isinstance(attachment_value, Mapping) else {}
        media_type = ""
        for key in ("type", "mime", "media_type", "mime_type"):
            value = attachment.get(key)
            if isinstance(value, str) and value:
                media_type = value.lower()
                break
        # Match the provider-facing projection in ``engine.runtime``. Supported
        # images remain native image blocks; PDF, Office, email, text, audio,
        # video, and opaque attachments are rendered as text/context markers
        # before any provider call. Treating those as native ``file``/media
        # requirements would incorrectly exclude otherwise capable text models.
        normalized_media_type = _normalize_attachment_mime(media_type)
        modality = "image" if normalized_media_type in _NATIVE_IMAGE_ATTACHMENT_MIMES else "text"
        if modality not in modalities:
            modalities.append(modality)
        attachment_refs.append(
            _bounded_text(
                attachment.get("name") or attachment.get("filename") or f"attachment-{index + 1}",
                item_max_chars,
            )
        )
    attachment_refs = _bounded_string_list(
        attachment_refs,
        max_items=attachment_max_items,
        max_chars=item_max_chars,
        max_scanned_items_multiplier=scan_multiplier,
    )
    if attachment_refs:
        workspace_state["referenced_files"] = _bounded_string_list(
            [*attachment_refs, *workspace_state["referenced_files"]],
            max_items=state_max_items,
            max_chars=item_max_chars,
            max_scanned_items_multiplier=scan_multiplier,
        )

    normalization = metadata.get("input_normalization")
    normalization_map = normalization if isinstance(normalization, Mapping) else {}
    supplied_budget = supplied_map.get("routing_budget")
    supplied_budget_map = supplied_budget if isinstance(supplied_budget, Mapping) else {}
    auxiliary_context = {
        "workspace_state": workspace_state,
        "intermediate_outputs": intermediate_outputs,
        "last_route": last_route,
        "input_modalities": modalities,
        "attachment_refs": attachment_refs,
    }
    estimated_from_content = _estimated_tokens_from_text(
        message
        + json.dumps(
            {"conversation": conversation, **auxiliary_context},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ),
        effective_config,
    )
    estimated_input_tokens = max(
        _as_int(metadata.get("input_tokens"), 0),
        _as_int(metadata.get("material_estimated_tokens"), 0),
        _as_int(normalization_map.get("material_estimated_tokens"), 0),
        _as_int(supplied_budget_map.get("estimated_input_tokens"), 0),
        estimated_from_content,
        minimum_tokens,
    )
    has_tool_state = bool(
        tool_state["called_tools"]
        or tool_state["tool_results_summary"]
        or tool_state["failed_tools"]
    )
    estimated_tool_tokens = (
        _estimated_tokens_from_text(
            json.dumps(
                tool_state,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ),
            effective_config,
        )
        if has_tool_state
        else 0
    )
    routing_budget = {
        "estimated_input_tokens": estimated_input_tokens,
        "tool_log_tokens": max(
            0,
            _as_int(metadata.get("tool_log_tokens"), 0),
            _as_int(supplied_budget_map.get("tool_log_tokens"), 0),
            estimated_tool_tokens,
        ),
    }
    context = {
        "conversation": conversation,
        "tool_state": tool_state,
        "workspace_state": workspace_state,
        "intermediate_outputs": intermediate_outputs,
        "last_route": last_route,
        "routing_budget": routing_budget,
        "input_modalities": modalities,
        "attachment_refs": attachment_refs,
    }
    return context


def build_request_context(
    *,
    message: str,
    turn_metadata: Mapping[str, Any] | None,
    attachments: Sequence[Mapping[str, Any]] | None,
    candidate_output_tokens: int,
    aggregator_output_tokens: int,
    ranking_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the temporary chapter-2 fusion context without logging raw input."""

    effective_config = _resolve_ranking_config(ranking_config)
    context = _build_request_context_base(
        message=message,
        turn_metadata=turn_metadata,
        attachments=attachments,
        last_route=_legacy_fusion_last_route(
            turn_metadata=turn_metadata,
            effective_config=effective_config,
        ),
        include_previous_candidates=True,
        effective_config=effective_config,
    )
    minimum_tokens = _ranking_int(effective_config, "context", "output_budget", "minimum_tokens")
    context["routing_budget"].update(
        {
            "candidate_output_tokens": max(minimum_tokens, candidate_output_tokens),
            "aggregator_output_tokens": max(minimum_tokens, aggregator_output_tokens),
        }
    )
    context["snapshot_hash"] = _request_context_hash(context)
    return context


def build_single_model_request_context(
    *,
    message: str,
    turn_metadata: Mapping[str, Any] | None,
    attachments: Sequence[Mapping[str, Any]] | None,
    output_tokens: int,
    ranking_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build direct-routing context without exposing fusion output budgets."""

    if isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or output_tokens <= 0:
        raise DynamicRankingError(
            "router_dynamic single-model output_tokens must be a positive integer"
        )
    effective_config = _resolve_ranking_config(ranking_config)
    context = _build_request_context_base(
        message=message,
        turn_metadata=turn_metadata,
        attachments=attachments,
        last_route={},
        include_previous_candidates=False,
        effective_config=effective_config,
    )
    minimum_tokens = _ranking_int(effective_config, "context", "output_budget", "minimum_tokens")
    context["routing_budget"]["direct_output_tokens"] = max(minimum_tokens, output_tokens)
    context["snapshot_hash"] = _request_context_hash(context)
    return context


def fallback_task_profile(
    *,
    routed_tier: str,
    request_context: Mapping[str, Any],
    ranking_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a conservative, schema-valid profile when task analysis fails."""

    effective_config = _resolve_ranking_config(ranking_config)
    router_tier_mapping = _router_tier_mapping(effective_config)
    tier = router_tier_mapping[_router_tier(routed_tier, effective_config)]
    budget = request_context.get("routing_budget")
    budget_map = budget if isinstance(budget, Mapping) else {}
    input_tokens = _as_int(budget_map.get("estimated_input_tokens"), 0)
    default_modalities = _ranking_string_list(
        effective_config, "hard_filter", "default_required_modalities"
    )
    modalities = [
        item
        for item in request_context.get("input_modalities", default_modalities)
        if str(item) in MODALITIES
    ]
    if not modalities:
        modalities = list(default_modalities)
    fallback_constraints = _ranking_mapping(
        effective_config, "fallback_task_profile", "constraints"
    )
    risk = _ranking_string(effective_config, "fallback_task_profile", "risk_by_tier", str(tier))
    return {
        "capability_dist": copy.deepcopy(
            dict(_ranking_mapping(effective_config, "fallback_task_profile", "capability_dist"))
        ),
        "domain_dist": copy.deepcopy(
            dict(_ranking_mapping(effective_config, "fallback_task_profile", "domain_dist"))
        ),
        "tier_dist": {str(tier): 1.0},
        "constraints": {
            "cost": str(fallback_constraints["cost"]),
            "latency": str(fallback_constraints["latency"]),
            "context": _context_bucket_for_tokens(input_tokens, effective_config),
            "modality": modalities,
            "risk": risk,
        },
        "optional_constraints": {},
        "session_intent": copy.deepcopy(
            dict(_ranking_mapping(effective_config, "fallback_task_profile", "session_intent"))
        ),
    }


def normalize_task_profile(
    raw_profile: Any,
    *,
    routed_tier: str,
    request_context: Mapping[str, Any],
    ranking_config: Mapping[str, Any] | None = None,
    cache_continuity_available: bool = False,
) -> tuple[dict[str, Any], bool, list[str]]:
    """Validate and normalize a task-analyzer payload into the Step2 schema."""

    if not isinstance(cache_continuity_available, bool):
        raise DynamicRankingError("router_dynamic cache_continuity_available must be a boolean")
    effective_config = _resolve_ranking_config(ranking_config)
    fallback = fallback_task_profile(
        routed_tier=routed_tier,
        request_context=request_context,
        ranking_config=effective_config,
    )
    if not isinstance(raw_profile, Mapping):
        return fallback, False, ["profile_not_object"]

    fatal_errors: list[str] = []
    warnings: list[str] = []
    distribution_tolerance = _ranking_number(
        effective_config, "validation", "task_profile_sum_tolerance"
    )
    capability, valid_capability = _normalize_distribution(
        raw_profile.get("capability_dist"),
        CAPABILITIES,
        fallback["capability_dist"],
        sum_tolerance=distribution_tolerance,
    )
    domain, valid_domain, repaired_domain = _normalize_domain_distribution(
        raw_profile.get("domain_dist"),
        fallback["domain_dist"],
        sum_tolerance=distribution_tolerance,
    )
    if repaired_domain:
        warnings.append("repaired_domain_dist")
    tier, valid_tier = _normalize_distribution(
        raw_profile.get("tier_dist"),
        TIERS,
        fallback["tier_dist"],
        sum_tolerance=distribution_tolerance,
    )
    if not valid_capability:
        fatal_errors.append("invalid_capability_dist")
    if not valid_domain:
        fatal_errors.append("invalid_domain_dist")
    if not valid_tier:
        fatal_errors.append("invalid_tier_dist")

    constraints_raw = raw_profile.get("constraints")
    if not isinstance(constraints_raw, Mapping):
        constraints_raw = {}
        fatal_errors.append("invalid_constraints")
    configured_constraint_values = _ranking_mapping(
        effective_config, "task_profile_schema", "constraint_values"
    )
    allowed_values = {
        key: {str(item) for item in value} for key, value in configured_constraint_values.items()
    }
    allowed_values["context"] = set(_context_bucket_min_tokens(effective_config))
    constraints: dict[str, Any] = {}
    for key, allowed in allowed_values.items():
        value = str(constraints_raw.get(key) or "").strip().lower()
        if value not in allowed:
            value = str(fallback["constraints"][key])
            fatal_errors.append(f"invalid_constraint_{key}")
        constraints[key] = value
    raw_modalities = constraints_raw.get("modality")
    if isinstance(raw_modalities, Sequence) and not isinstance(raw_modalities, str):
        normalized_modalities = [
            item.strip().lower() if isinstance(item, str) else "" for item in raw_modalities
        ]
        modalities = list(
            dict.fromkeys(item for item in normalized_modalities if item in MODALITIES)
        )
        if any(item not in MODALITIES for item in normalized_modalities):
            fatal_errors.append("invalid_constraint_modality")
    else:
        modalities = []
    if not modalities:
        modalities = list(fallback["constraints"]["modality"])
        fatal_errors.append("invalid_constraint_modality")
    context_modalities_raw = request_context.get("input_modalities")
    default_modalities = _ranking_string_list(
        effective_config, "hard_filter", "default_required_modalities"
    )
    context_modalities = (
        [
            str(item).strip().lower()
            for item in context_modalities_raw
            if str(item).strip().lower() in MODALITIES
        ]
        if isinstance(context_modalities_raw, Sequence)
        and not isinstance(context_modalities_raw, str)
        else default_modalities
    )
    missing_context_modalities = [
        modality for modality in context_modalities if modality not in modalities
    ]
    if missing_context_modalities:
        modalities.extend(missing_context_modalities)
        fatal_errors.append("missing_request_modality")
    constraints["modality"] = modalities

    optional: dict[str, Any] = {}
    optional_raw = raw_profile.get("optional_constraints")
    if optional_raw is not None and not isinstance(optional_raw, Mapping):
        warnings.append("invalid_optional_constraints")
    elif isinstance(optional_raw, Mapping) and optional_raw.get("format") is not None:
        output_format = str(optional_raw.get("format") or "").strip().lower()
        if output_format in FORMATS:
            optional["format"] = output_format
        else:
            warnings.append("invalid_optional_format")

    raw_analysis_confidence = raw_profile.get("analysis_confidence")
    analysis_confidence = _json_number(raw_analysis_confidence)
    if raw_analysis_confidence is not None and (
        analysis_confidence is None or not 0.0 <= analysis_confidence <= 1.0
    ):
        warnings.append("invalid_analysis_confidence")

    intent_raw = raw_profile.get("session_intent")
    if not isinstance(intent_raw, Mapping):
        intent_raw = {}
        fatal_errors.append("invalid_session_intent")
    default_intent = _ranking_string(
        effective_config, "task_profile_schema", "default_session_intent"
    )
    allowed_intents = _ranking_string_set(
        effective_config, "task_profile_schema", "session_intents"
    )
    intent_type = str(intent_raw.get("type") or default_intent).strip().lower()
    if intent_type not in allowed_intents:
        intent_type = default_intent
        fatal_errors.append("invalid_session_intent_type")
    raw_intent_confidence = intent_raw.get("confidence")
    parsed_intent_confidence = _json_number(raw_intent_confidence)
    if parsed_intent_confidence is None or not 0.0 <= parsed_intent_confidence <= 1.0:
        intent_confidence = _as_float(fallback["session_intent"].get("confidence"), 0.0)
        fatal_errors.append("invalid_session_intent_confidence")
    else:
        intent_confidence = parsed_intent_confidence
    last_route = request_context.get("last_route")
    cache_can_preserve_continue = (
        cache_continuity_available
        and _cache_affinity_policy_for_topology(effective_config, "single") is not None
        and intent_type == "continue"
        and intent_confidence
        >= _ranking_number(
            effective_config,
            "session",
            "intent_confidence_threshold",
        )
    )
    if (
        intent_type != default_intent
        and (not isinstance(last_route, Mapping) or not last_route)
        and not cache_can_preserve_continue
    ):
        intent_type = default_intent
        intent_confidence = 0.0

    profile = {
        "capability_dist": capability,
        "domain_dist": domain,
        "tier_dist": tier,
        "constraints": constraints,
        "optional_constraints": optional,
        "session_intent": {"type": intent_type, "confidence": intent_confidence},
    }
    required_valid = not fatal_errors
    return profile, required_valid, list(dict.fromkeys([*fatal_errors, *warnings]))


_FROZEN_TASK_ANALYSIS_FIELDS = frozenset(
    {
        "schema",
        "mode",
        "source_experiment",
        "source_manifest_sha256",
        "source_results_sha256",
        "source_task_analyzer_config",
        "source_task_analyzer_config_sha256",
        "entries",
        "entries_sha256",
    }
)
_FROZEN_TASK_ANALYSIS_V3_FIELDS = frozenset(
    {
        *_FROZEN_TASK_ANALYSIS_FIELDS,
        "source_task_analyzer_execution_contract",
        "source_task_analyzer_execution_contract_sha256",
    }
)
_FROZEN_TASK_ANALYSIS_ENTRY_FIELDS = frozenset(
    {
        "task_input_sha256",
        "prompt_sha256",
        "task_profile_pre_escalation",
        "task_profile_pre_escalation_sha256",
        "task_analyzer",
    }
)
_FROZEN_TASK_ANALYSIS_V2_ENTRY_FIELDS = frozenset(
    {*_FROZEN_TASK_ANALYSIS_ENTRY_FIELDS, "origin_outcome"}
)
_FROZEN_TASK_ANALYZER_TRACE_FIELDS = frozenset(
    {
        "source",
        "schema_valid",
        "confidence",
        "analyzer_version",
        "provider",
        "model",
        "fallback_reason",
        "usage",
        "normalization_warnings",
    }
)


def _frozen_task_analyzer_routes(
    source_config: Mapping[str, Any],
) -> list[dict[str, str]] | None:
    """Return the exact ordered Analyzer routes authorized by a frozen config."""

    raw_routes: list[Any] = [
        {
            "provider": source_config.get("provider"),
            "model": source_config.get("model"),
            "upstream_provider": source_config.get("upstream_provider"),
        }
    ]
    if "fallback_chain" in source_config:
        fallback_chain = source_config.get("fallback_chain")
        if not isinstance(fallback_chain, Sequence) or isinstance(
            fallback_chain, (str, bytes, bytearray)
        ):
            return None
        raw_routes.extend(fallback_chain)
    routes: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for raw_route in raw_routes:
        if not isinstance(raw_route, Mapping) or set(raw_route) != {
            "provider",
            "model",
            "upstream_provider",
        }:
            return None
        try:
            route = TaskAnalyzerCandidate(
                provider_id=str(raw_route["provider"]),
                model_id=str(raw_route["model"]),
                upstream_provider=str(raw_route["upstream_provider"]),
            )
        except (KeyError, ValueError):
            return None
        identity = (route.provider_id, route.model_id)
        if identity in identities:
            return None
        identities.add(identity)
        routes.append(
            {
                "provider": route.provider_id,
                "model": route.model_id,
                "upstream_provider": route.upstream_provider,
            }
        )
    return routes


def frozen_task_analysis_contract_reasons(
    value: Any,
    *,
    expected_task_ids: Sequence[str] | None = None,
) -> list[str]:
    """Authenticate an inline ten-task Analyzer replay bundle."""

    reasons: list[str] = []
    if not isinstance(value, Mapping):
        return ["invalid_frozen_task_analysis_contract"]
    schema = value.get("schema")
    expected_contract_fields = (
        _FROZEN_TASK_ANALYSIS_V3_FIELDS
        if schema == FROZEN_TASK_ANALYSIS_SCHEMA_V3
        else _FROZEN_TASK_ANALYSIS_FIELDS
    )
    if set(value) != expected_contract_fields:
        return ["invalid_frozen_task_analysis_contract"]
    if (
        schema not in FROZEN_TASK_ANALYSIS_SCHEMAS
        or value.get("mode") != FROZEN_TASK_ANALYSIS_MODE
        or not str(value.get("source_experiment") or "").strip()
    ):
        reasons.append("invalid_frozen_task_analysis_identity")
    for field_name in (
        "source_manifest_sha256",
        "source_results_sha256",
        "source_task_analyzer_config_sha256",
        "entries_sha256",
    ):
        raw_hash = str(value.get(field_name) or "")
        if len(raw_hash) != 64 or any(char not in "0123456789abcdef" for char in raw_hash):
            reasons.append(f"invalid_frozen_task_analysis_{field_name}")
    source_config = value.get("source_task_analyzer_config")
    authorized_routes: list[dict[str, str]] | None = None
    if (
        not isinstance(source_config, Mapping)
        or not source_config
        or _canonical_hash(source_config)
        != str(value.get("source_task_analyzer_config_sha256") or "")
    ):
        reasons.append("invalid_frozen_task_analysis_source_analyzer_config")
    elif (authorized_routes := _frozen_task_analyzer_routes(source_config)) is None:
        reasons.append("invalid_frozen_task_analysis_source_analyzer_identity")
    if schema == FROZEN_TASK_ANALYSIS_SCHEMA_V3:
        from opensquilla.draco_task_analyzer_execution import (
            task_analyzer_execution_contract_matches_source,
            validated_task_analyzer_execution_contract,
        )

        execution_contract = value.get("source_task_analyzer_execution_contract")
        validated_execution_contract = validated_task_analyzer_execution_contract(
            execution_contract
        )
        if (
            validated_execution_contract is None
            or not task_analyzer_execution_contract_matches_source(
                execution_contract,
                source_config if isinstance(source_config, Mapping) else {},
            )
            or _canonical_hash(execution_contract)
            != str(value.get("source_task_analyzer_execution_contract_sha256") or "")
        ):
            reasons.append("invalid_frozen_task_analyzer_execution_contract")
            authorized_routes = None
        else:
            authorized_routes = [
                {key: str(route[key]) for key in ("provider", "model", "upstream_provider")}
                for route in validated_execution_contract["routes"]
            ]
    entries = value.get("entries")
    if not isinstance(entries, Mapping) or len(entries) != 10:
        reasons.append("invalid_frozen_task_analysis_entries")
        return list(dict.fromkeys(reasons))
    task_ids = [str(task_id) for task_id in entries]
    if any(not task_id or task_id != task_id.strip() for task_id in task_ids):
        reasons.append("invalid_frozen_task_analysis_task_ids")
    if expected_task_ids is not None and (
        len(expected_task_ids) != 10
        or len(set(str(task_id) for task_id in expected_task_ids)) != 10
        or set(task_ids) != set(str(task_id) for task_id in expected_task_ids)
    ):
        reasons.append("wrong_frozen_task_analysis_task_set")
    expected_entry_fields = (
        _FROZEN_TASK_ANALYSIS_V2_ENTRY_FIELDS
        if schema in {FROZEN_TASK_ANALYSIS_SCHEMA_V2, FROZEN_TASK_ANALYSIS_SCHEMA_V3}
        else _FROZEN_TASK_ANALYSIS_ENTRY_FIELDS
    )
    for raw_entry in entries.values():
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != expected_entry_fields:
            reasons.append("invalid_frozen_task_analysis_entry")
            continue
        profile = raw_entry.get("task_profile_pre_escalation")
        profile_hash = str(raw_entry.get("task_profile_pre_escalation_sha256") or "")
        analyzer = raw_entry.get("task_analyzer")
        task_input_hash = str(raw_entry.get("task_input_sha256") or "")
        prompt_hash = str(raw_entry.get("prompt_sha256") or "")
        if (
            not task_input_hash.startswith("sha256:")
            or len(task_input_hash) != 71
            or any(char not in "0123456789abcdef" for char in task_input_hash[7:])
            or len(prompt_hash) != 64
            or any(char not in "0123456789abcdef" for char in prompt_hash)
        ):
            reasons.append("invalid_frozen_task_analysis_input_hash")
        if (
            not isinstance(profile, Mapping)
            or not profile
            or _canonical_hash(profile) != profile_hash
        ):
            reasons.append("invalid_frozen_task_profile_hash")
        if not isinstance(analyzer, Mapping) or set(analyzer) != _FROZEN_TASK_ANALYZER_TRACE_FIELDS:
            reasons.append("invalid_frozen_task_analyzer_trace")
            continue
        confidence = analyzer.get("confidence")
        warnings = analyzer.get("normalization_warnings")
        origin_outcome = (
            raw_entry.get("origin_outcome")
            if schema in {FROZEN_TASK_ANALYSIS_SCHEMA_V2, FROZEN_TASK_ANALYSIS_SCHEMA_V3}
            else FROZEN_TASK_ANALYSIS_LIVE_SUCCESS
        )
        outcome_valid = (
            origin_outcome == FROZEN_TASK_ANALYSIS_LIVE_SUCCESS
            and analyzer.get("schema_valid") is True
            and analyzer.get("fallback_reason") == ""
        ) or (
            schema in {FROZEN_TASK_ANALYSIS_SCHEMA_V2, FROZEN_TASK_ANALYSIS_SCHEMA_V3}
            and origin_outcome == FROZEN_TASK_ANALYSIS_ROUTER_FALLBACK
            and analyzer.get("schema_valid") is False
            and bool(str(analyzer.get("fallback_reason") or "").strip())
        )
        if (
            analyzer.get("source") != FROZEN_TASK_ANALYZER_SOURCE
            or not outcome_valid
            or isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not math.isfinite(float(confidence))
            or not 0.0 <= float(confidence) <= 1.0
            or not str(analyzer.get("analyzer_version") or "").strip()
            or not str(analyzer.get("provider") or "").strip()
            or not str(analyzer.get("model") or "").strip()
            or analyzer.get("usage") != {}
            or not isinstance(warnings, list)
            or any(not isinstance(warning, str) or not warning.strip() for warning in warnings)
            or len(warnings) != len(set(warnings))
        ):
            reasons.append("invalid_frozen_task_analyzer_trace")
        elif (
            authorized_routes is None
            or (
                str(analyzer.get("provider") or ""),
                str(analyzer.get("model") or ""),
            )
            not in {(route["provider"], route["model"]) for route in authorized_routes}
            or str(analyzer.get("analyzer_version") or "") != TASK_ANALYZER_VERSION
        ):
            reasons.append("wrong_frozen_task_analyzer_identity")
    try:
        entries_hash = _canonical_hash(entries)
        _assert_public_ranking_trace_payload(value, label="frozen_task_analysis")
    except (DynamicRankingError, TypeError, ValueError):
        reasons.append("unsafe_frozen_task_analysis_evidence")
    else:
        if entries_hash != str(value.get("entries_sha256") or ""):
            reasons.append("invalid_frozen_task_analysis_entries_sha256")
    return list(dict.fromkeys(reasons))


def frozen_task_analysis_plan_reasons(
    plan: Any,
    contract: Any,
    *,
    expected_task_id: str | None = None,
    expected_task_input_sha256: str | None = None,
    expected_prompt_sha256: str | None = None,
) -> list[str]:
    """Bind one ranker plan to exactly one entry in the replay bundle."""

    reasons = frozen_task_analysis_contract_reasons(contract)
    if reasons:
        return reasons
    if not isinstance(plan, Mapping) or not isinstance(contract, Mapping):
        return ["invalid_frozen_task_analysis_plan"]
    analyzer = plan.get("task_analyzer")
    proof = analyzer.get("replay") if isinstance(analyzer, Mapping) else None
    if not isinstance(proof, Mapping):
        return ["missing_frozen_task_analysis_replay_proof"]
    task_id = str(proof.get("task_id") or "")
    if expected_task_id is not None and task_id != str(expected_task_id):
        reasons.append("wrong_frozen_task_analysis_task_id")
    entries = contract["entries"]
    entry = entries.get(task_id) if isinstance(entries, Mapping) else None
    if not isinstance(entry, Mapping):
        reasons.append("unknown_frozen_task_analysis_task_id")
        return list(dict.fromkeys(reasons))
    if (
        expected_task_input_sha256 is not None
        and str(entry.get("task_input_sha256") or "") != expected_task_input_sha256
    ):
        reasons.append("wrong_frozen_task_analysis_task_input_sha256")
    if (
        expected_prompt_sha256 is not None
        and str(entry.get("prompt_sha256") or "") != expected_prompt_sha256
    ):
        reasons.append("wrong_frozen_task_analysis_prompt_sha256")
    contract_schema = str(contract["schema"])
    expected_proof = {
        "schema": contract_schema,
        "mode": FROZEN_TASK_ANALYSIS_MODE,
        "task_id": task_id,
        "source_experiment": contract["source_experiment"],
        "source_manifest_sha256": contract["source_manifest_sha256"],
        "source_results_sha256": contract["source_results_sha256"],
        "source_task_analyzer_config_sha256": contract["source_task_analyzer_config_sha256"],
        "entries_sha256": contract["entries_sha256"],
        "task_input_sha256": entry["task_input_sha256"],
        "prompt_sha256": entry["prompt_sha256"],
        "task_profile_pre_escalation_sha256": entry["task_profile_pre_escalation_sha256"],
        "physical_request_count": 0,
    }
    if contract_schema in {
        FROZEN_TASK_ANALYSIS_SCHEMA_V2,
        FROZEN_TASK_ANALYSIS_SCHEMA_V3,
    }:
        expected_proof["origin_outcome"] = entry["origin_outcome"]
    if contract_schema == FROZEN_TASK_ANALYSIS_SCHEMA_V3:
        expected_proof["source_task_analyzer_execution_contract_sha256"] = contract[
            "source_task_analyzer_execution_contract_sha256"
        ]
    if dict(proof) != expected_proof:
        reasons.append("wrong_frozen_task_analysis_replay_proof")
    expected_analyzer = copy.deepcopy(dict(entry["task_analyzer"]))
    observed_analyzer = copy.deepcopy(dict(analyzer)) if isinstance(analyzer, Mapping) else {}
    observed_analyzer.pop("replay", None)
    if observed_analyzer != expected_analyzer:
        reasons.append("wrong_frozen_task_analyzer_provenance")
    profile = plan.get("task_profile_pre_escalation")
    if (
        not isinstance(profile, Mapping)
        or dict(profile) != dict(entry["task_profile_pre_escalation"])
        or _canonical_hash(profile) != entry["task_profile_pre_escalation_sha256"]
    ):
        reasons.append("wrong_frozen_task_profile")
    ranking_parameters = plan.get("ranking_parameters")
    analyzer_config = (
        ranking_parameters.get("task_analyzer") if isinstance(ranking_parameters, Mapping) else None
    )
    if not isinstance(analyzer_config, Mapping) or dict(analyzer_config) != dict(
        contract["source_task_analyzer_config"]
    ):
        reasons.append("wrong_frozen_task_analysis_source_analyzer_config")
    return list(dict.fromkeys(reasons))


def frozen_task_analysis_result(
    contract: Mapping[str, Any],
    *,
    task_id: str,
    task_input_sha256: str,
    prompt_sha256: str,
    routed_tier: str,
    request_context: Mapping[str, Any],
    ranking_config: Mapping[str, Any] | None = None,
    cache_continuity_available: bool = False,
) -> TaskAnalysisResult:
    """Materialize a validated frozen profile without starting an LLM request."""

    reasons = frozen_task_analysis_contract_reasons(contract)
    entry = contract.get("entries", {}).get(task_id)
    if reasons or not isinstance(entry, Mapping):
        detail = ",".join(reasons or ["unknown_frozen_task_analysis_task_id"])
        raise DynamicRankingError(f"invalid frozen task analysis replay: {detail}")
    if (
        str(entry.get("task_input_sha256") or "") != task_input_sha256
        or str(entry.get("prompt_sha256") or "") != prompt_sha256
    ):
        raise DynamicRankingError(
            f"frozen task analysis input binding differs for task {task_id!r}"
        )
    profile = entry.get("task_profile_pre_escalation")
    normalized, schema_valid, _ = normalize_task_profile(
        profile,
        routed_tier=routed_tier,
        request_context=request_context,
        ranking_config=ranking_config,
        cache_continuity_available=cache_continuity_available,
    )
    origin_outcome = (
        entry.get("origin_outcome")
        if contract.get("schema")
        in {FROZEN_TASK_ANALYSIS_SCHEMA_V2, FROZEN_TASK_ANALYSIS_SCHEMA_V3}
        else FROZEN_TASK_ANALYSIS_LIVE_SUCCESS
    )
    if (
        not schema_valid
        or not isinstance(profile, Mapping)
        or normalized != dict(profile)
        or _canonical_hash(normalized) != str(entry.get("task_profile_pre_escalation_sha256") or "")
    ):
        raise DynamicRankingError(
            f"frozen task analysis profile {task_id!r} is invalid for this request"
        )
    if origin_outcome == FROZEN_TASK_ANALYSIS_ROUTER_FALLBACK:
        expected_fallback = fallback_task_profile(
            routed_tier=routed_tier,
            request_context=request_context,
            ranking_config=ranking_config,
        )
        if expected_fallback != normalized:
            raise DynamicRankingError(
                f"frozen task analysis fallback profile {task_id!r} is not deterministic"
            )
    analyzer = entry["task_analyzer"]
    proof = {
        "schema": contract["schema"],
        "mode": FROZEN_TASK_ANALYSIS_MODE,
        "task_id": task_id,
        "source_experiment": contract["source_experiment"],
        "source_manifest_sha256": contract["source_manifest_sha256"],
        "source_results_sha256": contract["source_results_sha256"],
        "source_task_analyzer_config_sha256": contract["source_task_analyzer_config_sha256"],
        "entries_sha256": contract["entries_sha256"],
        "task_input_sha256": entry["task_input_sha256"],
        "prompt_sha256": entry["prompt_sha256"],
        "task_profile_pre_escalation_sha256": entry["task_profile_pre_escalation_sha256"],
        "physical_request_count": 0,
    }
    if contract.get("schema") in {
        FROZEN_TASK_ANALYSIS_SCHEMA_V2,
        FROZEN_TASK_ANALYSIS_SCHEMA_V3,
    }:
        proof["origin_outcome"] = origin_outcome
    if contract.get("schema") == FROZEN_TASK_ANALYSIS_SCHEMA_V3:
        proof["source_task_analyzer_execution_contract_sha256"] = contract[
            "source_task_analyzer_execution_contract_sha256"
        ]
    return TaskAnalysisResult(
        profile=copy.deepcopy(normalized),
        source=FROZEN_TASK_ANALYZER_SOURCE,
        schema_valid=analyzer["schema_valid"] is True,
        confidence=float(analyzer["confidence"]),
        analyzer_version=str(analyzer["analyzer_version"]),
        fallback_reason=str(analyzer["fallback_reason"]),
        usage={},
        provider_id=str(analyzer["provider"]),
        model_id=str(analyzer["model"]),
        normalization_warnings=tuple(analyzer["normalization_warnings"]),
        replay=proof,
    )


def _extract_json_object(text: str) -> Any:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return value
    raise ValueError("task analyzer returned no JSON object")


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    with contextlib.suppress(BaseException):
        task.result()


async def _bounded_close_task_analyzer_stream(
    stream: Any,
    *,
    timeout_seconds: float,
    require_aclose: bool,
    pending_cleanup_tracker: (Callable[[asyncio.Future[Any], str], None] | None) = None,
) -> bool:
    """Close an analyzer stream without allowing provider cleanup to block routing."""

    aclose = getattr(stream, "aclose", None)
    if not callable(aclose):
        return not require_aclose
    try:
        close_task = asyncio.ensure_future(aclose())
    except Exception:
        return False
    if pending_cleanup_tracker is not None:
        pending_cleanup_tracker(close_task, "task_analyzer_stream_close")
    try:
        done, _ = await asyncio.wait(
            {close_task},
            timeout=max(0.0, timeout_seconds),
        )
    except BaseException:
        close_task.cancel()
        close_task.add_done_callback(_consume_task_result)
        raise
    if close_task in done:
        try:
            close_task.result()
        except BaseException:
            return False
        return True
    close_task.cancel()
    close_task.add_done_callback(_consume_task_result)
    return False


def _truncate_task_analyzer_text(
    value: str,
    *,
    max_chars: int,
    head_fraction: float,
) -> str:
    if len(value) <= max_chars:
        return value
    marker = "\n[task input truncated for classification]\n"[:max_chars]
    retained_chars = max(0, max_chars - len(marker))
    head_chars = math.floor(retained_chars * head_fraction)
    tail_chars = retained_chars - head_chars
    tail = value[-tail_chars:] if tail_chars else ""
    return value[:head_chars] + marker + tail


def _compact_task_analyzer_request_context(
    request_context: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep routing-critical facts when the full context exceeds the payload cap."""

    routing_budget_raw = request_context.get("routing_budget")
    direct_routing = isinstance(routing_budget_raw, Mapping) and (
        "direct_output_tokens" in routing_budget_raw
    )
    routing_budget_keys = ["estimated_input_tokens", "tool_log_tokens"]
    if direct_routing:
        routing_budget_keys.append("direct_output_tokens")
    else:
        routing_budget_keys.extend(["candidate_output_tokens", "aggregator_output_tokens"])
    routing_budget = (
        {key: max(0, _as_int(routing_budget_raw.get(key), 0)) for key in routing_budget_keys}
        if isinstance(routing_budget_raw, Mapping)
        else {}
    )
    modalities_raw = request_context.get("input_modalities")
    modalities = (
        [str(value) for value in modalities_raw if isinstance(value, str) and value in MODALITIES][
            : len(MODALITIES)
        ]
        if isinstance(modalities_raw, Sequence) and not isinstance(modalities_raw, (str, bytes))
        else []
    )
    snapshot_hash = str(request_context.get("snapshot_hash") or "")[:128]
    compact: dict[str, Any] = {
        "routing_budget": routing_budget,
        "input_modalities": modalities,
        "last_route": (
            {}
            if direct_routing
            else _sanitize_last_route(
                request_context.get("last_route"),
                ranking_config,
            )
        ),
        "payload_context_truncated": True,
    }
    if snapshot_hash:
        compact["snapshot_hash"] = snapshot_hash
    with contextlib.suppress(TypeError, ValueError):
        compact["full_context_sha256"] = canonical_json_sha256(request_context)
    return compact


def _task_analyzer_payload_metrics(
    payload: str,
    ranking_config: Mapping[str, Any],
) -> dict[str, int]:
    return {
        "chars": len(payload),
        "bytes": len(payload.encode("utf-8")),
        "estimated_tokens": _estimated_tokens_from_text(payload, ranking_config),
    }


def _serialize_task_analyzer_input(
    analyzer_input: Mapping[str, Any],
    *,
    message: str,
    request_context: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Serialize one complete Analyzer payload under all configured budgets."""

    analyzer = _ranking_mapping(ranking_config, "task_analyzer")
    budget_keys = (
        "payload_max_chars",
        "payload_max_bytes",
        "payload_max_estimated_tokens",
    )
    if not all(key in analyzer for key in budget_keys):
        payload = json.dumps(analyzer_input, ensure_ascii=True)
        return payload, {
            **_task_analyzer_payload_metrics(payload, ranking_config),
            "context_truncated": False,
            "task_truncated": False,
            "legacy_serialization": True,
        }

    limits = {
        "chars": _ranking_int(ranking_config, "task_analyzer", "payload_max_chars"),
        "bytes": _ranking_int(ranking_config, "task_analyzer", "payload_max_bytes"),
        "estimated_tokens": _ranking_int(
            ranking_config,
            "task_analyzer",
            "payload_max_estimated_tokens",
        ),
    }

    def serialize(candidate: Mapping[str, Any]) -> tuple[str, dict[str, int]]:
        payload = json.dumps(
            candidate,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return payload, _task_analyzer_payload_metrics(payload, ranking_config)

    def fits(metrics: Mapping[str, int]) -> bool:
        return all(metrics[key] <= limits[key] for key in limits)

    full_input = copy.deepcopy(dict(analyzer_input))
    payload, metrics = serialize(full_input)
    if fits(metrics):
        return payload, {
            **metrics,
            "context_truncated": False,
            "task_truncated": False,
            "legacy_serialization": False,
        }

    compact_input = copy.deepcopy(full_input)
    compact_input["request_context"] = _compact_task_analyzer_request_context(
        request_context,
        ranking_config,
    )
    payload, metrics = serialize(compact_input)
    if fits(metrics):
        return payload, {
            **metrics,
            "context_truncated": True,
            "task_truncated": False,
            "legacy_serialization": False,
        }

    head_fraction = _ranking_number(
        ranking_config,
        "task_analyzer",
        "truncation_head_fraction",
    )
    low = 0
    high = len(message)
    best: tuple[str, dict[str, int], str] | None = None
    while low <= high:
        candidate_chars = (low + high) // 2
        candidate_task = _truncate_task_analyzer_text(
            message,
            max_chars=candidate_chars,
            head_fraction=head_fraction,
        )
        compact_input["task"] = candidate_task
        candidate_payload, candidate_metrics = serialize(compact_input)
        if fits(candidate_metrics):
            best = candidate_payload, candidate_metrics, candidate_task
            low = candidate_chars + 1
        else:
            high = candidate_chars - 1
    if best is None:
        raise DynamicRankingError("task analyzer payload budget is smaller than its fixed schema")
    return best[0], {
        **best[1],
        "context_truncated": True,
        "task_truncated": best[2] != message,
        "legacy_serialization": False,
    }


async def analyze_task_with_provider(
    *,
    provider: LLMProvider | None,
    message: str,
    user_profile_enabled: bool,
    request_context: Mapping[str, Any],
    routed_tier: str,
    routing_confidence: float,
    timeout_seconds: float | None = None,
    usage_tracker: Any | None = None,
    session_key: str | None = None,
    analyzer_provider_id: str = "",
    analyzer_model_id: str = "",
    ranking_config: Mapping[str, Any] | None = None,
    decision_id: str = "",
    admission_controller: ProviderAdmissionController | None = None,
    admission_deadline: float | None = None,
    admission_before_release: Callable[[str], None] | None = None,
    cache_continuity_available: bool = False,
    _attempt: int = 1,
    _retry_feedback: str = "",
    _accumulated_usage: Mapping[str, Any] | None = None,
    _allow_provider_stream_fallback: bool = True,
    _absolute_deadline: float | None = None,
    _schema_repair_only: bool = False,
) -> TaskAnalysisResult:
    """Use the caller-supplied dedicated provider as the task analyzer."""

    if not isinstance(cache_continuity_available, bool):
        raise DynamicRankingError("router_dynamic cache_continuity_available must be a boolean")
    effective_config = _resolve_ranking_config(ranking_config)
    configured_policy = task_analyzer_policy(effective_config)
    configured_provider_id = str(configured_policy["provider"])
    configured_model_id = str(configured_policy["model"])
    explicit_provider_id = analyzer_provider_id.strip()
    explicit_model_id = analyzer_model_id.strip()
    if explicit_provider_id and explicit_provider_id != configured_provider_id:
        raise DynamicRankingError(
            "task analyzer caller provider identity conflicts with the frozen "
            "router_dynamic ranking config"
        )
    if explicit_model_id and explicit_model_id != configured_model_id:
        raise DynamicRankingError(
            "task analyzer caller model identity conflicts with the frozen "
            "router_dynamic ranking config"
        )
    analyzer_input_max_chars = _ranking_int(effective_config, "task_analyzer", "input_max_chars")
    analyzer_response_max_chars = _ranking_int(
        effective_config, "task_analyzer", "response_max_chars"
    )
    analyzer_max_output_tokens = _ranking_int(
        effective_config, "task_analyzer", "max_output_tokens"
    )
    analyzer_temperature = _ranking_number(effective_config, "task_analyzer", "temperature")
    analyzer_thinking = _ranking_bool(effective_config, "task_analyzer", "thinking")
    analyzer_max_retries = _ranking_int(effective_config, "task_analyzer", "max_retries")
    configured_timeout = (
        _ranking_number(effective_config, "task_analyzer", "timeout_seconds")
        if timeout_seconds is None
        else timeout_seconds
    )
    deadline_remaining = (
        max(0.0, _absolute_deadline - asyncio.get_running_loop().time())
        if _absolute_deadline is not None
        else configured_timeout
    )
    effective_timeout = min(configured_timeout, deadline_remaining)
    profile_decimal_places = _ranking_int(effective_config, "trace", "profile_decimal_places")
    fallback = fallback_task_profile(
        routed_tier=routed_tier,
        request_context=request_context,
        ranking_config=effective_config,
    )
    provider_id = configured_provider_id
    model_id = configured_model_id
    if provider is None:
        log.warning(
            "llm_ensemble.router_dynamic.task_analyzer_fallback",
            decision_id=decision_id,
            analyzer_version=TASK_ANALYZER_VERSION,
            reason="provider_unavailable",
            provider=provider_id or "unknown",
            model=model_id,
            routed_tier=_router_tier(routed_tier, effective_config),
            user_profile_enabled=user_profile_enabled,
        )
        return TaskAnalysisResult(
            profile=fallback,
            source="router_fallback",
            schema_valid=False,
            confidence=_clamp(routing_confidence),
            fallback_reason="provider_unavailable",
            provider_id=provider_id,
            model_id=model_id,
        )
    if effective_timeout <= 0.0:
        return TaskAnalysisResult(
            profile=fallback,
            source="router_fallback",
            schema_valid=False,
            confidence=_clamp(routing_confidence),
            fallback_reason=TaskAnalyzerDeadlineError.__name__,
            usage=_task_analyzer_zero_request_usage(_accumulated_usage),
            provider_id=provider_id,
            model_id=model_id,
        )

    system_prompt = (
        "You are a task-profile classifier. Return one JSON object only and do "
        "not answer the user's task. Use exactly this object shape: "
        '{"capability_dist":{"<allowed capability>":<number>},'
        '"domain_dist":{"<allowed domain>":<number>},'
        '"tier_dist":{"<allowed tier>":<number>},'
        '"constraints":{"cost":"<allowed value>",'
        '"latency":"<allowed value>","context":"<allowed value>",'
        '"modality":["<allowed modality>"],"risk":"<allowed value>"},'
        '"optional_constraints":{"format":"<allowed format>"},'
        '"session_intent":{"type":"<allowed intent>","confidence":<0..1>},'
        '"analysis_confidence":<0..1>}. '
        "Every displayed top-level key is required except optional_constraints "
        "and analysis_confidence. modality must always be a JSON array, even for "
        "one item. session_intent is required; use new_task with confidence 0 "
        "when there is no prior route. Distributions must use only supplied enum "
        "values, contain non-negative numbers, and each sum to 1. Include every "
        "supplied capability, domain, and tier key; use 0 for irrelevant entries. "
        f"capability_dist keys are exactly {json.dumps(list(CAPABILITIES))}; "
        f"domain_dist keys are exactly {json.dumps(list(DOMAINS))}. Copy these "
        "names literally and never move a domain name into capability_dist. In "
        "particular, research is a domain, not a capability; represent research "
        "work with capabilities such as retrieval, reasoning, summarization, or "
        "data_analysis as appropriate."
    )
    analysis_message = _truncate_task_analyzer_text(
        message,
        max_chars=analyzer_input_max_chars,
        head_fraction=_ranking_number(
            effective_config,
            "task_analyzer",
            "truncation_head_fraction",
        ),
    )
    constraint_values = _ranking_mapping(
        effective_config, "task_profile_schema", "constraint_values"
    )
    analyzer_input: dict[str, Any] = {
        "task": analysis_message,
        "allowed_capabilities": list(CAPABILITIES),
        "allowed_domains": list(DOMAINS),
        "allowed_tiers": list(TIERS),
        "allowed_modalities": list(MODALITIES),
        "allowed_constraints": {
            "cost": list(constraint_values["cost"]),
            "latency": list(constraint_values["latency"]),
            "context": list(_context_bucket_min_tokens(effective_config)),
            "risk": list(constraint_values["risk"]),
        },
        "allowed_formats": list(FORMATS),
        "allowed_session_intents": _ranking_string_list(
            effective_config, "task_profile_schema", "session_intents"
        ),
        "request_context": request_context,
    }
    if _retry_feedback:
        analyzer_input["retry_feedback"] = (
            "The previous attempt failed. Correct this validation error: " + _retry_feedback[:500]
        )
    try:
        analyzer_payload, payload_metrics = _serialize_task_analyzer_input(
            analyzer_input,
            message=analysis_message,
            request_context=request_context,
            ranking_config=effective_config,
        )
    except (TypeError, ValueError, DynamicRankingError) as exc:
        reason = type(exc).__name__
        log.warning(
            "llm_ensemble.router_dynamic.task_analyzer_fallback",
            decision_id=decision_id,
            analyzer_version=TASK_ANALYZER_VERSION,
            reason=reason,
            provider=provider_id or "unknown",
            model=model_id,
            routed_tier=_router_tier(routed_tier, effective_config),
            user_profile_enabled=user_profile_enabled,
            physical_request_count=0,
        )
        return TaskAnalysisResult(
            profile=fallback,
            source="router_fallback",
            schema_valid=False,
            confidence=_clamp(routing_confidence),
            fallback_reason=reason,
            usage=_task_analyzer_zero_request_usage(_accumulated_usage),
            provider_id=provider_id,
            model_id=model_id,
        )
    log.info(
        "llm_ensemble.router_dynamic.task_analyzer_started",
        decision_id=decision_id,
        analyzer_version=TASK_ANALYZER_VERSION,
        provider=provider_id or "unknown",
        model=model_id,
        input_chars=len(message),
        input_truncated=(len(analysis_message) < len(message) or payload_metrics["task_truncated"]),
        request_context_hash=request_context.get("snapshot_hash"),
        user_profile_enabled=user_profile_enabled,
        attempt=_attempt,
        max_attempts=analyzer_max_retries + 1,
        payload_chars=payload_metrics["chars"],
        payload_bytes=payload_metrics["bytes"],
        payload_estimated_tokens=payload_metrics["estimated_tokens"],
        payload_context_truncated=payload_metrics["context_truncated"],
        payload_task_truncated=payload_metrics["task_truncated"],
    )
    physical_attempt_ordinal = _task_analyzer_physical_attempt_count(_accumulated_usage) + 1
    physical_attempt_id = _task_analyzer_physical_attempt_id(
        decision_id=decision_id,
        request_context=request_context,
        message=message,
        attempt=physical_attempt_ordinal,
    )
    usage: dict[str, Any] = {}
    count_current_request = True
    accounting_override: dict[str, Any] | None = None
    normalization_issues: list[str] = []
    admission_lease: ProviderAdmissionLease | None = None
    admission_guard: ProviderAdmissionLeaseGuard | None = None
    attempt_timeout = effective_timeout
    try:
        analyzer_messages = [Message(role="user", content=analyzer_payload)]
        request_timeout = effective_timeout
        if _absolute_deadline is not None:
            cleanup_reserve = min(
                float(configured_policy["stream_close_timeout_seconds"]),
                effective_timeout * 0.5,
                max(0.005, effective_timeout * 0.10),
            )
            request_timeout = max(0.000001, effective_timeout - cleanup_reserve)
        analyzer_config = ChatConfig(
            max_tokens=analyzer_max_output_tokens,
            temperature=analyzer_temperature,
            system=system_prompt,
            thinking=analyzer_thinking,
            model_capabilities=(
                ModelCapabilities(
                    supports_reasoning=True,
                    reasoning_format="openrouter_explicit_off",
                )
                if model_id == TASK_ANALYZER_MODEL_ID
                else None
            ),
            timeout=request_timeout,
            allow_provider_stream_fallback=_allow_provider_stream_fallback,
            output_json_schema=copy.deepcopy(_task_analyzer_output_schema()),
            output_json_schema_strict=True,
        )
        # Task analysis is a physical provider request just like generation and
        # must cross the same fail-closed durable accounting boundary.  The
        # helper is a pass-through when no scope is bound (CLI/unit tests).
        from opensquilla.engine.usage_accounting import (
            account_provider_stream,
            has_known_provider_usage_receipt,
            provider_accounts_physical_usage,
            provider_usage_receipt_rows,
        )

        if admission_controller is not None:
            try:
                admission_lease = await admission_controller.acquire(
                    provider=provider_id,
                    model=model_id,
                    role="analyzer",
                    absolute_deadline=admission_deadline,
                )
                admission_guard = ProviderAdmissionLeaseGuard(
                    admission_lease,
                    before_release=(
                        (lambda: admission_before_release("analyzer"))
                        if admission_before_release is not None
                        else None
                    ),
                )
                log.debug(
                    "llm_ensemble.router_dynamic.task_analyzer_admitted",
                    decision_id=decision_id,
                    wait_ms=admission_lease.wait_ms,
                    weight=admission_lease.weight,
                )
                # A grant at the deadline boundary is still not permission to
                # start a physical request. Release the zero-request lease and
                # fail open to the normal Analyzer fallback without retrying.
                if admission_deadline is not None and time.monotonic() >= admission_deadline:
                    wait_ms = admission_lease.wait_ms
                    admission_guard.finish()
                    admission_guard = None
                    admission_lease = None
                    count_current_request = False
                    raise ProviderAdmissionTimeoutError(
                        role="analyzer",
                        provider=provider_id,
                        model=model_id,
                        wait_ms=wait_ms,
                    )
            except ProviderAdmissionError as exc:
                count_current_request = False
                log.info(
                    "llm_ensemble.router_dynamic.task_analyzer_admission_rejected",
                    decision_id=decision_id,
                    code=exc.code,
                    wait_ms=exc.wait_ms,
                )
                raise
        if admission_deadline is not None:
            remaining_to_deadline = admission_deadline - time.monotonic()
            if remaining_to_deadline <= 0:
                count_current_request = False
                raise ProviderAdmissionTimeoutError(
                    role="analyzer",
                    provider=provider_id,
                    model=model_id,
                    wait_ms=(admission_lease.wait_ms if admission_lease else 0),
                )
            request_timeout = min(request_timeout, remaining_to_deadline)
        if _absolute_deadline is not None:
            final_deadline_remaining = _absolute_deadline - asyncio.get_running_loop().time()
            if final_deadline_remaining <= cleanup_reserve:
                count_current_request = False
                raise TaskAnalyzerDeadlineError(
                    "task analyzer deadline elapsed before provider request"
                )
            request_timeout = min(
                request_timeout,
                max(0.000001, final_deadline_remaining - cleanup_reserve),
            )
        attempt_timeout = request_timeout
        analyzer_config = analyzer_config.model_copy(update={"timeout": request_timeout})
        try:
            stream = (
                provider.chat(analyzer_messages, tools=None, config=analyzer_config)
                if provider_accounts_physical_usage(provider)
                else account_provider_stream(
                    lambda: provider.chat(
                        analyzer_messages,
                        tools=None,
                        config=analyzer_config,
                    ),
                    provider=provider_id,
                    model=model_id,
                )
            )
        except BaseException:
            if admission_guard is not None:
                admission_guard.finish()
                admission_guard = None
            elif admission_lease is not None:
                admission_lease.release()
            if admission_lease is not None:
                admission_lease = None
            raise
        text_parts: list[str] = []
        total_chars = 0
        got_done = False
        terminal_observed = False
        stream_exhausted = False
        try:
            iteration_timeout = request_timeout
            if admission_deadline is not None:
                remaining_to_deadline = admission_deadline - time.monotonic()
                if remaining_to_deadline <= 0:
                    raise TimeoutError
                iteration_timeout = min(
                    iteration_timeout,
                    remaining_to_deadline,
                )
            async with asyncio.timeout(iteration_timeout):
                async for event in stream:
                    if isinstance(event, TextDeltaEvent):
                        total_chars += len(event.text)
                        if total_chars > analyzer_response_max_chars:
                            raise ValueError("task analyzer response exceeded size limit")
                        text_parts.append(event.text)
                    elif isinstance(event, DoneEvent):
                        got_done = True
                        terminal_observed = True
                        usage = _task_analyzer_usage_from_done(event)
                        usage["provider"] = str(usage.get("provider") or configured_provider_id)
                        usage["model"] = str(usage.get("model") or configured_model_id)
                        usage["requested_provider"] = str(
                            event.requested_provider or configured_provider_id
                        )
                        usage["requested_model"] = str(event.requested_model or configured_model_id)
                        done_reported_ids = _event_reported_physical_attempt_ids(event)
                        done_nonempty_ids = [
                            value.casefold() for value in done_reported_ids if value
                        ]
                        done_unique_ids = list(dict.fromkeys(done_nonempty_ids))
                        done_id_conflict = bool(
                            any(
                                _PHYSICAL_ATTEMPT_ID_RE.fullmatch(value) is None
                                for value in done_nonempty_ids
                            )
                            or len(done_unique_ids) > 1
                        )
                        if done_id_conflict:
                            provider_usage = copy.deepcopy(dict(usage["provider_usage"]))
                            provider_usage["reported_physical_attempt_ids"] = done_unique_ids
                            provider_usage.pop(
                                "physical_attempt_id",
                                None,
                            )
                            usage["provider_usage"] = provider_usage
                            accounting_override = _merge_task_analyzer_attempt(
                                _accumulated_usage,
                                usage,
                                attempt=physical_attempt_ordinal,
                                physical_attempt_id=physical_attempt_id,
                                provider_id=provider_id,
                                model_id=model_id,
                                unknown_reason=("TaskAnalyzerPhysicalEvidenceError"),
                            )
                            raise TaskAnalyzerPhysicalEvidenceError(
                                "task analyzer DoneEvent physical-request identity is contradictory"
                            )
                        if done_unique_ids:
                            physical_attempt_id = done_unique_ids[0]
                        if usage_tracker is not None and session_key:
                            try:
                                usage_tracker.add(
                                    session_key,
                                    input_tokens=event.input_tokens,
                                    output_tokens=event.output_tokens,
                                    model_id=event.model,
                                    provider=provider_id,
                                    cache_read_tokens=event.cached_tokens,
                                    cache_write_tokens=event.cache_write_tokens,
                                    billed_cost=(
                                        event.billed_cost
                                        if event.cost_source == "provider_billed"
                                        else 0.0
                                    ),
                                )
                            except Exception:  # noqa: BLE001 - accounting cannot break routing
                                log.warning(
                                    "llm_ensemble.router_dynamic.task_analyzer_usage_failed",
                                    decision_id=decision_id,
                                    provider=provider_id or "unknown",
                                    model=model_id,
                                )
                        break
                    elif isinstance(event, ErrorEvent):
                        terminal_observed = True
                        known_receipt = has_known_provider_usage_receipt(event)
                        receipt_rows = (
                            provider_usage_receipt_rows(
                                event,
                                default_provider=provider_id,
                                default_model=model_id,
                            )
                            if known_receipt
                            else []
                        )
                        (
                            reported_physical_attempt_ids,
                            reported_id_conflict,
                        ) = _task_analyzer_reported_physical_attempt_ids(
                            event,
                            receipt_rows,
                        )
                        explicit_count = (
                            max(0, int(event.physical_request_count))
                            if isinstance(event.physical_request_count, int)
                            and not isinstance(event.physical_request_count, bool)
                            else None
                        )
                        trace = (
                            event.ensemble_trace
                            if isinstance(event.ensemble_trace, Mapping)
                            else {}
                        )
                        missing_count = max(
                            _as_int(event.usage_missing_count, 0),
                            _as_int(trace.get("usage_missing_count"), 0),
                        )
                        physical_count = max(
                            explicit_count or 0,
                            _as_int(trace.get("physical_request_count"), 0),
                            _as_int(trace.get("llm_request_count"), 0),
                            len(receipt_rows) + missing_count,
                            1 if event.request_started is True else 0,
                            1 if reported_physical_attempt_ids else 0,
                        )
                        explicit_zero = bool(event.request_started is False or explicit_count == 0)
                        evidence_conflict = bool(
                            (explicit_zero and physical_count > 0)
                            or physical_count > 1
                            or len(receipt_rows) > 1
                            or reported_id_conflict
                        )
                        if (
                            not reported_id_conflict
                            and len(reported_physical_attempt_ids) == 1
                            and physical_count == 1
                        ):
                            physical_attempt_id = reported_physical_attempt_ids[0]
                        if evidence_conflict:
                            accounting_override = _merge_task_analyzer_error_evidence(
                                _accumulated_usage,
                                receipt_rows,
                                physical_request_count=max(
                                    1,
                                    physical_count,
                                    len(receipt_rows),
                                ),
                                reported_physical_attempt_ids=(reported_physical_attempt_ids),
                                decision_id=decision_id,
                                request_context=request_context,
                                message=message,
                                provider_id=provider_id,
                                model_id=model_id,
                                unknown_reason="TaskAnalyzerPhysicalEvidenceError",
                            )
                            raise TaskAnalyzerPhysicalEvidenceError(
                                "task analyzer physical-request evidence is contradictory"
                            )
                        if explicit_zero:
                            count_current_request = False
                        elif receipt_rows:
                            usage = _task_analyzer_usage_from_receipt_row(receipt_rows[0])
                        if _schema_repair_only:
                            raise TaskAnalyzerProviderError(
                                _task_analyzer_provider_failure_reason(
                                    provider_id=provider_id,
                                    event=event,
                                )
                            )
                        raise RuntimeError(f"provider_error:{event.code or 'unknown'}")
                else:
                    stream_exhausted = True
        finally:
            cleanup_remaining = (
                max(0.0, _absolute_deadline - asyncio.get_running_loop().time())
                if _absolute_deadline is not None
                else effective_timeout
            )
            close_timeout = min(
                float(configured_policy["stream_close_timeout_seconds"]),
                max(0.0, attempt_timeout),
                cleanup_remaining,
            )
            if admission_deadline is not None:
                close_timeout = min(
                    close_timeout,
                    max(0.0, admission_deadline - time.monotonic()),
                )
            try:
                closed = await _bounded_close_task_analyzer_stream(
                    stream,
                    timeout_seconds=close_timeout,
                    require_aclose=not (terminal_observed or stream_exhausted),
                    pending_cleanup_tracker=(
                        admission_guard.track_cleanup if admission_guard is not None else None
                    ),
                )
            finally:
                if admission_guard is not None:
                    admission_guard.finish()
                    admission_guard = None
                elif admission_lease is not None:
                    admission_lease.release()
                if admission_lease is not None:
                    admission_lease = None
            if not closed:
                log.warning(
                    "llm_ensemble.router_dynamic.task_analyzer_stream_close_failed",
                    decision_id=decision_id,
                    analyzer_version=TASK_ANALYZER_VERSION,
                    provider=provider_id or "unknown",
                    model=model_id,
                    timeout_seconds=close_timeout,
                    attempt=_attempt,
                )
                raise TaskAnalyzerStreamCleanupError("task analyzer stream cleanup was not proven")
        if not got_done:
            raise RuntimeError("task analyzer stream ended before DoneEvent")
        try:
            payload = _extract_json_object("".join(text_parts))
        except ValueError as exc:
            raise TaskAnalyzerSchemaError("invalid_json") from exc
        profile, schema_valid, normalization_issues = normalize_task_profile(
            payload,
            routed_tier=routed_tier,
            request_context=request_context,
            ranking_config=effective_config,
            cache_continuity_available=cache_continuity_available,
        )
        if not schema_valid:
            raise TaskAnalyzerSchemaError(
                "invalid_schema",
                ";".join(normalization_issues) or "invalid task profile",
            )
    except TaskAnalyzerStreamCleanupError as exc:
        # A replacement request must not begin while the previous physical
        # provider stream may still be billed in the background.
        exc.usage = (
            accounting_override
            if accounting_override is not None
            else _merge_task_analyzer_attempt(
                _accumulated_usage,
                usage,
                attempt=physical_attempt_ordinal,
                physical_attempt_id=physical_attempt_id,
                provider_id=provider_id,
                model_id=model_id,
                unknown_reason=type(exc).__name__,
            )
        )
        raise
    except Exception as exc:  # noqa: BLE001 - analysis must fail open to a safe profile
        # A failure after admission but before the stream-specific finalizer
        # (for example request-config projection) must not hold capacity while
        # a retry waits on the same controller. Deferred stream cleanup has
        # already transferred ownership and cleared these local references.
        if admission_guard is not None:
            admission_guard.finish()
            admission_guard = None
        elif admission_lease is not None:
            admission_lease.release()
        if admission_lease is not None:
            admission_lease = None
        absolute_deadline_exhausted = bool(
            (admission_deadline is not None and time.monotonic() >= admission_deadline)
            or (
                _absolute_deadline is not None
                and asyncio.get_running_loop().time() >= _absolute_deadline
            )
        )
        schema_error = isinstance(exc, TaskAnalyzerSchemaError)
        reason = (
            _TASK_ANALYZER_ABSOLUTE_DEADLINE_REASON
            if absolute_deadline_exhausted and not isinstance(exc, ProviderAdmissionError)
            else exc.code
            if schema_error and _schema_repair_only
            else exc.public_reason
            if isinstance(exc, TaskAnalyzerProviderError) and _schema_repair_only
            else "transient"
            if isinstance(exc, TimeoutError) and _schema_repair_only
            else "ValueError"
            if schema_error
            else type(exc).__name__
        )
        accumulated_usage = (
            _task_analyzer_zero_request_usage(_accumulated_usage)
            if not count_current_request
            else _merge_task_analyzer_attempt(
                _accumulated_usage,
                usage,
                attempt=physical_attempt_ordinal,
                physical_attempt_id=physical_attempt_id,
                provider_id=provider_id,
                model_id=model_id,
                unknown_reason=reason,
            )
        )
        should_retry = (
            _attempt <= analyzer_max_retries
            and not isinstance(exc, ProviderAdmissionError)
            and not absolute_deadline_exhausted
            and (not _schema_repair_only or schema_error)
        )
        if should_retry:
            log.warning(
                "llm_ensemble.router_dynamic.task_analyzer_retry",
                decision_id=decision_id,
                analyzer_version=TASK_ANALYZER_VERSION,
                reason=reason,
                provider=provider_id or "unknown",
                model=model_id,
                attempt=_attempt,
                next_attempt=_attempt + 1,
                max_attempts=analyzer_max_retries + 1,
                routed_tier=_router_tier(routed_tier, effective_config),
                user_profile_enabled=user_profile_enabled,
            )
            return await analyze_task_with_provider(
                provider=provider,
                message=message,
                user_profile_enabled=user_profile_enabled,
                request_context=request_context,
                routed_tier=routed_tier,
                routing_confidence=routing_confidence,
                timeout_seconds=timeout_seconds,
                usage_tracker=usage_tracker,
                session_key=session_key,
                analyzer_provider_id=configured_provider_id,
                analyzer_model_id=configured_model_id,
                ranking_config=effective_config,
                decision_id=decision_id,
                admission_controller=admission_controller,
                admission_deadline=admission_deadline,
                admission_before_release=admission_before_release,
                cache_continuity_available=cache_continuity_available,
                _attempt=_attempt + 1,
                _retry_feedback=(exc.feedback if schema_error and _schema_repair_only else reason),
                _accumulated_usage=accumulated_usage,
                _allow_provider_stream_fallback=(_allow_provider_stream_fallback),
                _absolute_deadline=_absolute_deadline,
                _schema_repair_only=_schema_repair_only,
            )
        log.warning(
            "llm_ensemble.router_dynamic.task_analyzer_fallback",
            decision_id=decision_id,
            analyzer_version=TASK_ANALYZER_VERSION,
            reason=reason,
            provider=provider_id or "unknown",
            model=model_id,
            routed_tier=_router_tier(routed_tier, effective_config),
            user_profile_enabled=user_profile_enabled,
            attempt=_attempt,
            max_attempts=analyzer_max_retries + 1,
        )
        return TaskAnalysisResult(
            profile=fallback,
            source="router_fallback",
            schema_valid=False,
            confidence=_clamp(routing_confidence),
            fallback_reason=reason,
            usage=accumulated_usage,
            provider_id=provider_id,
            model_id=model_id,
            normalization_warnings=tuple(normalization_issues),
        )
    finally:
        # Cover pre-stream failures (for example config projection) as well as
        # the normal close path. ``finish`` is idempotent and transfers the
        # lease to any still-running close proof registered above.
        if admission_guard is not None:
            admission_guard.finish()
            admission_guard = None
        elif admission_lease is not None:
            admission_lease.release()
            admission_lease = None

    usage = _merge_task_analyzer_attempt(
        _accumulated_usage,
        usage,
        attempt=physical_attempt_ordinal,
        physical_attempt_id=physical_attempt_id,
        provider_id=provider_id,
        model_id=model_id,
    )
    payload_map = payload if isinstance(payload, Mapping) else {}
    default_confidence = _ranking_number(effective_config, "task_analyzer", "default_confidence")
    raw_confidence = payload_map.get("analysis_confidence")
    parsed_confidence = _json_number(raw_confidence)
    confidence = (
        parsed_confidence
        if parsed_confidence is not None and 0.0 <= parsed_confidence <= 1.0
        else default_confidence
    )
    log.info(
        "llm_ensemble.router_dynamic.task_analyzer_completed",
        decision_id=decision_id,
        analyzer_version=TASK_ANALYZER_VERSION,
        provider=provider_id,
        model=model_id,
        schema_valid=True,
        confidence=round(confidence, profile_decimal_places),
        profile_hash=_canonical_hash(profile),
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        billed_cost=usage.get("billed_cost", 0.0),
        user_profile_enabled=user_profile_enabled,
        normalization_warnings=normalization_issues,
    )
    return TaskAnalysisResult(
        profile=profile,
        source="llm_provider",
        schema_valid=True,
        confidence=confidence,
        usage=usage,
        provider_id=provider_id,
        model_id=model_id,
        normalization_warnings=tuple(normalization_issues),
    )


def _normalize_task_analyzer_chain_candidates(
    candidates: Sequence[TaskAnalyzerCandidate | Mapping[str, Any]],
) -> tuple[TaskAnalyzerCandidate, ...]:
    if isinstance(candidates, (str, bytes)):
        raise ValueError("task analyzer fallback candidates must be a sequence")
    normalized: list[TaskAnalyzerCandidate] = []
    expected_mapping_keys = {
        "provider",
        "provider_id",
        "model_id",
        "upstream_provider",
    }
    for index, raw_candidate in enumerate(candidates):
        if isinstance(raw_candidate, TaskAnalyzerCandidate):
            candidate = TaskAnalyzerCandidate(
                provider=raw_candidate.provider,
                provider_id=raw_candidate.provider_id,
                model_id=raw_candidate.model_id,
                upstream_provider=raw_candidate.upstream_provider,
            )
        elif isinstance(raw_candidate, Mapping):
            if set(raw_candidate) != expected_mapping_keys:
                raise ValueError(
                    "task analyzer fallback candidate mapping must contain exactly "
                    "provider, provider_id, model_id, and upstream_provider"
                )
            candidate = TaskAnalyzerCandidate(
                provider=raw_candidate["provider"],
                provider_id=str(raw_candidate["provider_id"]),
                model_id=str(raw_candidate["model_id"]),
                upstream_provider=str(raw_candidate["upstream_provider"]),
            )
        else:
            raise ValueError(
                "task analyzer fallback candidate at index "
                f"{index} must be a TaskAnalyzerCandidate or mapping"
            )
        normalized.append(candidate)
    if not normalized:
        raise ValueError("task analyzer fallback candidates cannot be empty")
    routes = [
        (candidate.provider_id, candidate.model_id, candidate.upstream_provider)
        for candidate in normalized
    ]
    if len(routes) != len(set(routes)):
        raise ValueError("task analyzer fallback candidates cannot contain duplicate routes")
    return tuple(normalized)


def _task_analyzer_candidate_ranking_config(
    ranking_config: Mapping[str, Any],
    candidate: TaskAnalyzerCandidate,
    *,
    schema_repair_max_retries: int,
) -> _ValidatedRankingConfig:
    if not _is_validated_ranking_config(ranking_config):
        raise DynamicRankingError(
            "router_dynamic analyzer candidate config requires a prepared base"
        )
    if not isinstance(candidate, TaskAnalyzerCandidate):
        raise ValueError("task analyzer candidate must be normalized")
    if (
        isinstance(schema_repair_max_retries, bool)
        or not isinstance(schema_repair_max_retries, int)
        or not 0 <= schema_repair_max_retries <= 1
    ):
        raise ValueError("task analyzer schema repair retries must be 0 or 1")
    candidate = TaskAnalyzerCandidate(
        provider=candidate.provider,
        provider_id=candidate.provider_id,
        model_id=candidate.model_id,
        upstream_provider=candidate.upstream_provider,
    )
    return _derive_validated_analyzer_candidate(
        ranking_config,
        provider_id=candidate.provider_id,
        model_id=candidate.model_id,
        upstream_provider=candidate.upstream_provider,
        schema_repair_max_retries=schema_repair_max_retries,
    )


def _public_task_analyzer_chain_failure_reason(value: Any) -> str:
    reason = str(value or "").strip()
    if reason in {
        TaskAnalyzerDeadlineError.__name__,
        _TASK_ANALYZER_ABSOLUTE_DEADLINE_REASON,
    }:
        return "transient"
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", reason):
        return reason
    return "analyzer_failed"


def _task_analyzer_provider_failure_reason(*, provider_id: str, event: ErrorEvent) -> str:
    raw_code = str(event.code or "")
    kind = classify_provider_error(
        provider_id,
        int(raw_code) if raw_code.isdigit() else None,
        raw_code=raw_code,
        message=str(event.message or ""),
    )
    if kind is ProviderFailureKind.AUTH_INVALID:
        return "auth"
    if kind in {
        ProviderFailureKind.UNSUPPORTED_FEATURE,
        ProviderFailureKind.MODEL_NOT_FOUND,
    }:
        return "unsupported"
    if kind in {
        ProviderFailureKind.RATE_LIMITED,
        ProviderFailureKind.PROVIDER_OVERLOADED,
        ProviderFailureKind.TRANSPORT_TRANSIENT,
    }:
        return "transient"
    return kind.value


async def analyze_task_with_fallback_chain(
    *,
    candidates: Sequence[TaskAnalyzerCandidate | Mapping[str, Any]],
    message: str,
    user_profile_enabled: bool,
    request_context: Mapping[str, Any],
    routed_tier: str,
    routing_confidence: float,
    timeout_seconds: float | None = None,
    absolute_deadline: float | None = None,
    schema_repair_max_retries: int | None = None,
    usage_tracker: Any | None = None,
    session_key: str | None = None,
    ranking_config: Mapping[str, Any] | None = None,
    decision_id: str = "",
    admission_controller: ProviderAdmissionController | None = None,
    admission_deadline: float | None = None,
    admission_before_release: Callable[[str], None] | None = None,
    cache_continuity_available: bool = False,
) -> TaskAnalysisResult:
    """Try an ordered Analyzer chain under one absolute deadline.

    The chain may repair one schema error at most once. Other ordinary failures
    advance directly to the next candidate. Stream-cleanup or physical-evidence
    failures remain fail-closed because a replacement request cannot safely
    start while the previous request may still be running or its billing
    identity is contradictory.
    """

    if not isinstance(cache_continuity_available, bool):
        raise DynamicRankingError("router_dynamic cache_continuity_available must be a boolean")
    normalized_candidates = _normalize_task_analyzer_chain_candidates(candidates)
    effective_config = _resolve_ranking_config(ranking_config)
    chain_policy = task_analyzer_chain_policy(effective_config)
    total_timeout = (
        float(timeout_seconds)
        if timeout_seconds is not None
        else (
            float(chain_policy["total_timeout_seconds"])
            if chain_policy["configured"]
            else float(chain_policy["total_timeout_seconds"]) * len(normalized_candidates)
        )
    )
    if not math.isfinite(total_timeout) or total_timeout <= 0.0:
        raise ValueError("task analyzer fallback chain timeout must be positive")
    configured_schema_repairs = (
        int(chain_policy["schema_repair_max_retries"])
        if schema_repair_max_retries is None
        else schema_repair_max_retries
    )
    if (
        isinstance(configured_schema_repairs, bool)
        or not isinstance(configured_schema_repairs, int)
        or not 0 <= configured_schema_repairs <= 1
    ):
        raise ValueError("task analyzer fallback chain schema repair retries must be 0 or 1")
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    chain_absolute_deadline = started_at + total_timeout
    if absolute_deadline is not None:
        if not math.isfinite(float(absolute_deadline)):
            raise ValueError("task analyzer absolute deadline must be finite")
        chain_absolute_deadline = min(
            chain_absolute_deadline,
            float(absolute_deadline),
        )
    if admission_deadline is not None:
        if not math.isfinite(float(admission_deadline)):
            raise ValueError("task analyzer admission deadline must be finite")
        chain_absolute_deadline = min(
            chain_absolute_deadline,
            float(admission_deadline),
        )
    configured_deadline_seconds = round(
        max(0.0, chain_absolute_deadline - started_at),
        6,
    )
    configured_routes = [
        {
            "provider": candidate.provider_id,
            "model": candidate.model_id,
            "upstream_provider": candidate.upstream_provider,
        }
        for candidate in normalized_candidates
    ]
    outcomes: list[dict[str, Any]] = []
    accumulated_usage = _task_analyzer_zero_request_usage(None)
    last_result: TaskAnalysisResult | None = None

    def chain_trace(*, selected_index: int | None, exhausted: bool) -> dict[str, Any]:
        now = loop.time()
        return {
            "protocol": TASK_ANALYZER_FALLBACK_CHAIN_PROTOCOL,
            "configured_routes": copy.deepcopy(configured_routes),
            "attempt_outcomes": copy.deepcopy(outcomes),
            "selected_index": selected_index,
            "exhausted": exhausted,
            "schema_repair_max_retries": configured_schema_repairs,
            "deadline": {
                "configured_seconds": configured_deadline_seconds,
                "elapsed_seconds": round(max(0.0, now - started_at), 6),
                "remaining_seconds": round(
                    max(0.0, chain_absolute_deadline - now),
                    6,
                ),
                "expired": now >= chain_absolute_deadline,
            },
        }

    schema_repairs_remaining = configured_schema_repairs
    chain_cleanup_reserve = float(
        task_analyzer_policy(effective_config)["stream_close_timeout_seconds"]
    )
    for candidate_index, candidate in enumerate(normalized_candidates):
        candidate_config = _task_analyzer_candidate_ranking_config(
            effective_config,
            candidate,
            schema_repair_max_retries=schema_repairs_remaining,
        )
        physical_attempts_before = _task_analyzer_physical_attempt_count(accumulated_usage)
        result = await analyze_task_with_provider(
            provider=candidate.provider,
            message=message,
            user_profile_enabled=user_profile_enabled,
            request_context=request_context,
            routed_tier=routed_tier,
            routing_confidence=routing_confidence,
            timeout_seconds=None,
            usage_tracker=usage_tracker,
            session_key=session_key,
            analyzer_provider_id=candidate.provider_id,
            analyzer_model_id=candidate.model_id,
            ranking_config=candidate_config,
            decision_id=decision_id,
            admission_controller=admission_controller,
            admission_deadline=admission_deadline,
            admission_before_release=admission_before_release,
            cache_continuity_available=cache_continuity_available,
            _accumulated_usage=accumulated_usage,
            _allow_provider_stream_fallback=False,
            _absolute_deadline=chain_absolute_deadline,
            _schema_repair_only=True,
        )
        if result.usage:
            accumulated_usage = copy.deepcopy(result.usage)
        physical_attempts_after = _task_analyzer_physical_attempt_count(accumulated_usage)
        if physical_attempts_after - physical_attempts_before > 1:
            schema_repairs_remaining = 0
        outcome = {
            "candidate_index": candidate_index,
            "provider": candidate.provider_id,
            "model": candidate.model_id,
            "upstream_provider": candidate.upstream_provider,
            "outcome": "success" if result.schema_valid else "failed",
            "reason": (
                ""
                if result.schema_valid
                else _public_task_analyzer_chain_failure_reason(result.fallback_reason)
            ),
            "physical_request_count": max(
                0,
                physical_attempts_after - physical_attempts_before,
            ),
        }
        outcomes.append(outcome)
        last_result = result
        if result.schema_valid:
            return replace(
                result,
                usage=copy.deepcopy(accumulated_usage),
                chain_trace=chain_trace(
                    selected_index=candidate_index,
                    exhausted=False,
                ),
            )
        deadline_remaining = chain_absolute_deadline - loop.time()
        if admission_deadline is not None and 0.0 < deadline_remaining <= chain_cleanup_reserve:
            # The remaining slice is reserved for proving the just-finished
            # stream closed.  Do not spend it on a replacement physical
            # request; wait out the authenticated Analyzer deadline instead
            # of claiming an early prefix exhaustion.
            await asyncio.sleep(deadline_remaining)
        if loop.time() >= chain_absolute_deadline or (
            admission_deadline is not None and time.monotonic() >= admission_deadline
        ):
            break

    assert last_result is not None
    return replace(
        last_result,
        usage=copy.deepcopy(accumulated_usage),
        chain_trace=chain_trace(selected_index=None, exhausted=True),
    )


def _validate_registry_snapshot(
    raw: Any,
    ranking_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise DynamicRankingError("router_dynamic model registry snapshot must be an object")
    snapshot = copy.deepcopy(dict(raw))
    models = snapshot.get("models")
    if not isinstance(models, list):
        raise DynamicRankingError("router_dynamic model registry snapshot is malformed")
    schema_version = str(snapshot.get("schema_version") or "").strip()
    if not schema_version or not str(snapshot.get("snapshot_version") or "").strip():
        raise DynamicRankingError(
            "router_dynamic model registry snapshot requires schema and snapshot versions"
        )
    if schema_version.startswith("step2-model-registry-") and schema_version not in {
        MODEL_REGISTRY_SCHEMA_VERSION,
        LEGACY_MODEL_REGISTRY_SCHEMA_VERSION,
    }:
        raise DynamicRankingError(
            "router_dynamic model registry schema_version must be "
            f"{LEGACY_MODEL_REGISTRY_SCHEMA_VERSION} or "
            f"{MODEL_REGISTRY_SCHEMA_VERSION}"
        )

    identities: set[tuple[str, str]] = set()
    for index, row in enumerate(models):
        if not isinstance(row, Mapping):
            raise DynamicRankingError(
                f"router_dynamic model registry row {index} must be an object"
            )
        facts = row.get("registry_facts")
        static_profile = row.get("static_profile")
        if not isinstance(facts, Mapping) or not isinstance(static_profile, Mapping):
            raise DynamicRankingError(
                f"router_dynamic model registry row {index} lacks facts or static profile"
            )
        provider = str(facts.get("provider") or "").strip().lower()
        model_id = str(facts.get("model_id") or "").strip().lower()
        if not provider or not model_id:
            raise DynamicRankingError(
                f"router_dynamic model registry row {index} lacks provider/model_id"
            )
        identity = (provider, model_id)
        if identity in identities:
            raise DynamicRankingError(
                "router_dynamic model registry snapshot contains duplicate model identities"
            )
        identities.add(identity)
        if schema_version == MODEL_REGISTRY_SCHEMA_VERSION and (
            "thinking_levels" not in facts or "thinking_level_mapping" not in facts
        ):
            raise DynamicRankingError(
                "router_dynamic model registry v2 row "
                f"{provider}:{model_id} requires thinking_levels and "
                "thinking_level_mapping"
            )
        for optional_object in ("runtime", "online_profile"):
            value = row.get(optional_object)
            if value is not None and not isinstance(value, Mapping):
                raise DynamicRankingError(
                    f"router_dynamic model registry row {index} has invalid {optional_object}"
                )
    effective_config = _resolve_ranking_config(ranking_config)
    for row in models:
        normalized = _normalize_model(row, effective_config)
        if schema_version == MODEL_REGISTRY_SCHEMA_VERSION:
            _registry_thinking_contract(
                normalized.registry_facts,
                identity=normalized.identity,
                policy={"level_order": UNIFIED_THINKING_LEVELS},
            )
    return snapshot


@cache
def _packaged_registry_snapshot() -> dict[str, Any]:
    try:
        path = resources.files("opensquilla.provider").joinpath(
            "router_dynamic_model_profiles.json"
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surfaced as a precise startup/build error
        raise DynamicRankingError("router_dynamic model registry snapshot unavailable") from exc
    validated = _validate_registry_snapshot(payload)
    _validate_packaged_role_reliability_provenance(validated)
    return validated


def _validate_packaged_role_reliability_provenance(
    snapshot: Mapping[str, Any],
) -> None:
    """Reject packaged reliability counts produced by obsolete attribution rules."""

    models = snapshot.get("models")
    rows = models if isinstance(models, list) else []
    has_reliability = any(
        isinstance(row, Mapping)
        and isinstance(row.get("online_profile"), Mapping)
        and "role_reliability" in row["online_profile"]
        for row in rows
    )
    provenance = snapshot.get("role_reliability_snapshot")
    if not has_reliability and provenance is None:
        return
    if not isinstance(provenance, Mapping):
        raise DynamicRankingError(
            "router_dynamic packaged reliability profiles lack snapshot provenance"
        )
    schema = str(provenance.get("schema_version") or "").strip()
    if schema not in _ROLE_RELIABILITY_SNAPSHOT_SCHEMA_VERSIONS:
        raise DynamicRankingError(
            "router_dynamic packaged reliability provenance schema is unsupported"
        )
    policy = str(provenance.get("observation_policy") or "").strip()
    if policy not in _TRUSTED_PACKAGED_RELIABILITY_POLICIES:
        raise DynamicRankingError(
            "router_dynamic packaged reliability observation policy is obsolete"
        )
    if policy == "aef-physical-model-calls-v5":
        _validate_role_reliability_snapshot_content_hash(snapshot, provenance)
        return
    if provenance.get("completion_gate") != "legacy_statistics_invalidated":
        raise DynamicRankingError(
            "router_dynamic invalidated reliability snapshot has an invalid gate"
        )
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        online_profile = row.get("online_profile")
        reliability = (
            online_profile.get("role_reliability") if isinstance(online_profile, Mapping) else None
        )
        if not isinstance(reliability, Mapping):
            raise DynamicRankingError(
                "router_dynamic invalidated reliability snapshot lacks zero profiles"
            )
        if reliability.get("source") != "legacy_statistics_invalidated":
            raise DynamicRankingError(
                "router_dynamic invalidated reliability snapshot has an invalid source"
            )
        for role in _MODEL_ROLES:
            counts = reliability.get(role)
            if not isinstance(counts, Mapping) or any(
                counts.get(field) != 0 for field in ("success", "failure")
            ):
                raise DynamicRankingError(
                    "router_dynamic invalidated reliability snapshot has nonzero counts"
                )
    if schema == "role-reliability-snapshot-v2":
        _validate_role_reliability_snapshot_content_hash(snapshot, provenance)


def _role_reliability_snapshot_content_sha256(
    models: Sequence[Any],
    provenance: Mapping[str, Any],
) -> str:
    provenance_without_hash = dict(provenance)
    provenance_without_hash.pop("content_sha256", None)
    return _canonical_hash(
        {
            "models": list(models),
            "role_reliability_snapshot": provenance_without_hash,
        }
    )


def _validate_role_reliability_snapshot_content_hash(
    snapshot: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> None:
    if provenance.get("schema_version") != "role-reliability-snapshot-v2":
        raise DynamicRankingError(
            "router_dynamic trusted reliability statistics require content-bound provenance"
        )
    recorded = str(provenance.get("content_sha256") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", recorded) is None:
        raise DynamicRankingError(
            "router_dynamic reliability provenance lacks a valid content_sha256"
        )
    models = snapshot.get("models")
    if not isinstance(models, list) or (
        _role_reliability_snapshot_content_sha256(models, provenance) != recorded
    ):
        raise DynamicRankingError("router_dynamic reliability provenance content_sha256 differs")


def _archived_pre_status_disable_registry_snapshot() -> dict[str, Any]:
    try:
        path = resources.files("opensquilla.provider").joinpath(
            _PRE_STATUS_DISABLE_REGISTRY_RESOURCE
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surfaced as a precise startup/build error
        raise DynamicRankingError(
            "router_dynamic archived model registry snapshot unavailable"
        ) from exc
    validated = _validate_registry_snapshot(payload)
    _validate_packaged_role_reliability_provenance(validated)
    if (
        str(validated.get("snapshot_version") or "").strip()
        != _PRE_STATUS_DISABLE_REGISTRY_SNAPSHOT_VERSION
    ):
        raise DynamicRankingError(
            "router_dynamic archived model registry has an unexpected version"
        )
    return validated


def _archived_pre_qwen35_9b_registry_snapshot() -> dict[str, Any]:
    """Load the exact 79-model snapshot that predates Qwen3.5-9B."""

    try:
        path = resources.files("opensquilla.provider").joinpath(_PRE_QWEN35_9B_REGISTRY_RESOURCE)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surfaced as a precise startup/build error
        raise DynamicRankingError(
            "router_dynamic archived pre-Qwen3.5-9B registry snapshot unavailable"
        ) from exc
    validated = _validate_registry_snapshot(payload)
    _validate_packaged_role_reliability_provenance(validated)
    if (
        str(validated.get("snapshot_version") or "").strip()
        != _PRE_QWEN35_9B_REGISTRY_SNAPSHOT_VERSION
    ):
        raise DynamicRankingError(
            "router_dynamic archived pre-Qwen3.5-9B registry has an unexpected version"
        )
    return validated


def _registry_snapshot_for_base_version(base_version: str | None) -> dict[str, Any]:
    """Select the packaged registry or reconstruct an allowlisted base snapshot."""

    packaged = _packaged_registry_snapshot()
    if base_version is None:
        return packaged
    if not isinstance(base_version, str) or not base_version.strip():
        raise DynamicRankingError(
            "router_dynamic model registry base_version must be a non-empty string"
        )
    requested = base_version.strip()
    packaged_version = str(packaged.get("snapshot_version") or "").strip()
    if requested == packaged_version:
        return packaged
    if requested == _PRE_QWEN35_9B_REGISTRY_SNAPSHOT_VERSION:
        return _archived_pre_qwen35_9b_registry_snapshot()
    if requested == _PRE_STATUS_DISABLE_REGISTRY_SNAPSHOT_VERSION:
        return _archived_pre_status_disable_registry_snapshot()
    if requested not in _HISTORICAL_REGISTRY_BASE_VERSIONS:
        raise DynamicRankingError(
            f"router_dynamic model registry base_version {requested!r} is not available"
        )
    reconstruction_source = packaged
    if requested != _CURRENT_REGISTRY_BASE_VERSION:
        reconstruction_source = _archived_pre_qwen35_9b_registry_snapshot()
    source_version = str(reconstruction_source.get("snapshot_version") or "").strip()
    provenance = reconstruction_source.get("role_reliability_snapshot")
    if not isinstance(provenance, Mapping):
        raise DynamicRankingError(
            "router_dynamic model registry cannot reconstruct the requested base snapshot"
        )
    provenance_schema = str(provenance.get("schema_version") or "").strip()
    if provenance_schema not in _ROLE_RELIABILITY_SNAPSHOT_SCHEMA_VERSIONS:
        raise DynamicRankingError(
            "router_dynamic model registry reliability provenance schema is unsupported"
        )
    recorded_base = str(provenance.get("base_snapshot_version") or "").strip()
    if recorded_base not in _HISTORICAL_REGISTRY_BASE_VERSIONS:
        raise DynamicRankingError(
            "router_dynamic model registry reliability provenance has a different base"
        )
    if not source_version.startswith(f"{recorded_base}-reliability-"):
        raise DynamicRankingError(
            "router_dynamic model registry cannot reconstruct the requested base snapshot"
        )
    if requested not in {
        recorded_base,
        _PRE_STATUS_DISABLE_REGISTRY_BASE_VERSION,
        _PREVIOUS_REGISTRY_BASE_VERSION,
        _LEGACY_REGISTRY_BASE_VERSION,
    }:
        raise DynamicRankingError(
            "router_dynamic model registry reliability provenance has a different base"
        )
    historical = copy.deepcopy(dict(reconstruction_source))
    historical["snapshot_version"] = requested
    historical.pop("role_reliability_snapshot", None)
    rows = historical.get("models")
    if not isinstance(rows, list):
        raise DynamicRankingError("router_dynamic model registry snapshot is malformed")
    for row in rows:
        if not isinstance(row, dict):
            raise DynamicRankingError("router_dynamic model registry snapshot is malformed")
        online_profile = row.get("online_profile")
        if isinstance(online_profile, dict):
            online_profile.pop("role_reliability", None)
        facts = row.get("registry_facts")
        if not isinstance(facts, dict):
            continue
        model_id = str(facts.get("model_id") or "").strip()
        if (
            requested
            not in {
                _CURRENT_REGISTRY_BASE_VERSION,
                _PRE_QWEN35_9B_REGISTRY_BASE_VERSION,
            }
            and model_id in _POST_CURRENT_BASE_DISABLED_MODEL_IDS
        ):
            facts["status"] = "enabled"
        if requested == _LEGACY_REGISTRY_BASE_VERSION:
            if model_id in _POST_BASE_DISABLED_MODEL_IDS:
                facts["status"] = "enabled"
            for key, value in _POST_BASE_REGISTRY_FACT_OVERRIDES.get(model_id, {}).items():
                facts[key] = copy.deepcopy(value)
        static_profile = row.get("static_profile")
        if not isinstance(static_profile, dict):
            continue
        if requested == _LEGACY_REGISTRY_BASE_VERSION:
            profile_overrides = _POST_BASE_STATIC_PROFILE_OVERRIDES
        elif requested == _PREVIOUS_REGISTRY_BASE_VERSION:
            profile_overrides = _POST_PREVIOUS_BASE_STATIC_PROFILE_OVERRIDES
        else:
            profile_overrides = {}
        for profile_key, values in profile_overrides.get(model_id, {}).items():
            profile = static_profile.get(profile_key)
            if isinstance(profile, dict):
                profile.update(copy.deepcopy(values))
    return _validate_registry_snapshot(historical)


def load_model_registry_snapshot(*, base_version: str | None = None) -> dict[str, Any]:
    """Return an isolated copy of the selected versioned registry snapshot."""

    return copy.deepcopy(_registry_snapshot_for_base_version(base_version))


def _split_model_identity(
    provider: str,
    model_id: str,
    ranking_config: Mapping[str, Any],
) -> tuple[str, str]:
    model_lower = model_id.lower()
    if "/" in model_lower:
        vendor, name = model_lower.split("/", 1)
    else:
        vendor, name = provider.lower(), model_lower
    pieces = name.replace("_", "-").split("-")
    family_name_parts = _ranking_int(ranking_config, "synthetic_model", "family_name_parts")
    family = "-".join(pieces[:family_name_parts]) if len(pieces) >= family_name_parts else name
    return vendor or provider.lower(), family or model_lower


def _synthesized_model(
    *,
    provider: str,
    model_id: str,
    source: str,
    routed_tier: str,
    roles: Sequence[str] = ("proposer", "aggregator"),
    modalities: Sequence[str] = ("text",),
    ranking_config: Mapping[str, Any],
) -> dict[str, Any]:
    router_tier_mapping = _router_tier_mapping(ranking_config)
    tier = router_tier_mapping[_router_tier(routed_tier, ranking_config)]
    vendor, family = _split_model_identity(provider, model_id, ranking_config)
    base_strength = _ranking_number(
        ranking_config, "synthetic_model", "base_strength_by_tier", str(tier)
    )
    tier_penalty = _ranking_number(
        ranking_config, "synthetic_model", "tier_strength_penalty_per_level"
    )
    aggregator_fit_minimum = _ranking_number(
        ranking_config, "synthetic_model", "aggregator_role_fit_minimum"
    )
    aggregator_fit_penalty = _ranking_number(
        ranking_config, "synthetic_model", "aggregator_role_fit_penalty"
    )
    return {
        "source": source,
        "runtime": {"thinking": _ranking_string(ranking_config, "synthetic_model", "thinking")},
        "registry_facts": {
            "model_id": model_id,
            "version": _ranking_string(ranking_config, "synthetic_model", "version"),
            "provider": provider,
            "vendor": vendor,
            "family": family,
            "status": _ranking_string(ranking_config, "synthetic_model", "status"),
            "roles": list(dict.fromkeys(roles)),
            "context_window": _ranking_int(ranking_config, "synthetic_model", "context_window"),
            "effective_context_bucket": _ranking_string(
                ranking_config, "synthetic_model", "effective_context_bucket"
            ),
            "modalities": list(dict.fromkeys(modalities)),
            "tools": [],
            "price": {
                "input_per_million": _ranking_number(
                    ranking_config, "synthetic_model", "price_input_per_million"
                ),
                "output_per_million": _ranking_number(
                    ranking_config, "synthetic_model", "price_output_per_million"
                ),
            },
            "latency_p50_ms": _ranking_int(ranking_config, "synthetic_model", "latency_p50_ms"),
            "latency_p95_ms": _ranking_int(ranking_config, "synthetic_model", "latency_p95_ms"),
            "quota": _ranking_string(ranking_config, "synthetic_model", "quota"),
            "rate_limit": _ranking_string(ranking_config, "synthetic_model", "rate_limit"),
            "health": _ranking_string(ranking_config, "synthetic_model", "health"),
            # Unknown/provider-local deployments must not inherit another
            # provider's native thinking-level contract.
            "thinking_levels": [],
            "thinking_level_mapping": {},
        },
        "static_profile": {
            "capability_dist_prior": {name: base_strength for name in CAPABILITIES},
            "domain_dist_prior": {name: base_strength for name in DOMAINS},
            "tier_dist_prior": {
                tier_name: _clamp(base_strength - tier_penalty * max(0, _as_int(tier_name) - tier))
                for tier_name in TIERS
            },
            "role_fit_prior": {
                "proposer": base_strength,
                "aggregator": max(
                    aggregator_fit_minimum,
                    base_strength - aggregator_fit_penalty,
                ),
            },
        },
        "online_profile": {"error_rates": {}},
    }


def _template_for_model(
    templates: Sequence[Mapping[str, Any]], model_id: str
) -> dict[str, Any] | None:
    target = model_id.strip().lower()
    target_basename = target.rsplit("/", 1)[-1]
    basename_matches: list[Mapping[str, Any]] = []
    for row in templates:
        facts = row.get("registry_facts")
        if not isinstance(facts, Mapping):
            continue
        candidate = str(facts.get("model_id") or "").strip().lower()
        if candidate == target:
            return copy.deepcopy(dict(row))
        if "/" not in target and candidate.rsplit("/", 1)[-1] == target_basename:
            basename_matches.append(row)
    if len(basename_matches) == 1:
        return copy.deepcopy(dict(basename_matches[0]))
    return None


@dataclass(frozen=True)
class _PackagedRegistryTemplateIndex:
    """Read-only lookup tables for the already validated packaged registry."""

    snapshot_version: str
    schema_version: str
    templates: tuple[Mapping[str, Any], ...]
    exact_model_ids: Mapping[str, Mapping[str, Any]]
    unique_basenames: Mapping[str, Mapping[str, Any]]


_PACKAGED_REGISTRY_TEMPLATE_INDEX_LOCK = threading.Lock()
_PACKAGED_REGISTRY_TEMPLATE_INDEX: _PackagedRegistryTemplateIndex | None = None


def _reset_packaged_registry_template_index_lock_after_fork() -> None:
    """Drop a possibly inherited locked mutex without discarding the index."""

    global _PACKAGED_REGISTRY_TEMPLATE_INDEX_LOCK

    _PACKAGED_REGISTRY_TEMPLATE_INDEX_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_packaged_registry_template_index_lock_after_fork)


def _compile_packaged_registry_template_index(
    snapshot: Mapping[str, Any],
) -> _PackagedRegistryTemplateIndex:
    """Compile deterministic lookup tables without changing template semantics."""

    models = snapshot.get("models")
    if not isinstance(models, list):
        raise DynamicRankingError("router_dynamic model registry has no models list")
    templates = tuple(models)
    exact_model_ids: dict[str, Mapping[str, Any]] = {}
    unique_basenames: dict[str, Mapping[str, Any]] = {}
    ambiguous_basenames: set[str] = set()
    for row in templates:
        facts = row.get("registry_facts")
        if not isinstance(facts, Mapping):
            continue
        candidate = str(facts.get("model_id") or "").strip().lower()
        # Keep the first row, exactly as the pre-index linear lookup does.
        exact_model_ids.setdefault(candidate, row)
        basename = candidate.rsplit("/", 1)[-1]
        if basename in ambiguous_basenames:
            continue
        if basename in unique_basenames:
            unique_basenames.pop(basename)
            ambiguous_basenames.add(basename)
            continue
        unique_basenames[basename] = row
    return _PackagedRegistryTemplateIndex(
        snapshot_version=str(snapshot.get("snapshot_version") or "mock-unknown"),
        schema_version=str(snapshot.get("schema_version") or "step2-model-registry-v1"),
        templates=templates,
        exact_model_ids=MappingProxyType(exact_model_ids),
        unique_basenames=MappingProxyType(unique_basenames),
    )


def _packaged_registry_template_index_lookup() -> tuple[_PackagedRegistryTemplateIndex, bool]:
    """Atomically return the packaged index and this lookup's hit status."""

    global _PACKAGED_REGISTRY_TEMPLATE_INDEX

    cached = _PACKAGED_REGISTRY_TEMPLATE_INDEX
    if cached is not None:
        return cached, True
    with _PACKAGED_REGISTRY_TEMPLATE_INDEX_LOCK:
        cached = _PACKAGED_REGISTRY_TEMPLATE_INDEX
        if cached is not None:
            return cached, True
        compiled = _compile_packaged_registry_template_index(_packaged_registry_snapshot())
        _PACKAGED_REGISTRY_TEMPLATE_INDEX = compiled
        return compiled, False


def _packaged_registry_template_index() -> _PackagedRegistryTemplateIndex:
    """Return indexes for the validated, process-local packaged registry only."""

    return _packaged_registry_template_index_lookup()[0]


def _ranking_stage_elapsed_ms(started_ns: int) -> int:
    """Return a bounded monotonic duration without entering ranking state."""

    elapsed_ns = max(0, time.monotonic_ns() - started_ns)
    return min((1 << 63) - 1, elapsed_ns // 1_000_000)


def _template_for_packaged_model(
    template_index: _PackagedRegistryTemplateIndex,
    model_id: str,
) -> dict[str, Any] | None:
    """Look up a packaged template while retaining caller-visible copy isolation."""

    target = model_id.strip().lower()
    row = template_index.exact_model_ids.get(target)
    if row is None and "/" not in target:
        row = template_index.unique_basenames.get(target)
    return copy.deepcopy(dict(row)) if row is not None else None


def build_model_registry_snapshot(
    *,
    inherited_provider: str,
    inherited_model: str,
    routed_tier: str,
    anchor_modalities: Sequence[str] = ("text",),
    operator_candidates: Sequence[Mapping[str, Any]] = (),
    legacy_model_options: Sequence[str] = (),
    router_tiers: Mapping[str, Any] | None = None,
    packaged_snapshot: Mapping[str, Any] | None = None,
    ranking_config: Mapping[str, Any] | None = None,
    _observability_out: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the mock snapshot with runtime and operator-defined deployments."""

    if _observability_out is not None and type(_observability_out) is not dict:
        raise TypeError("router_dynamic snapshot observability output must be a dict")
    if _observability_out is not None:
        _observability_out.pop("packaged_template_cache_hit", None)
    effective_config = _resolve_ranking_config(ranking_config)
    template_index: _PackagedRegistryTemplateIndex | None = None
    if packaged_snapshot is None:
        # The cached source has passed strict snapshot and provenance validation
        # in _packaged_registry_snapshot().  Arbitrary caller snapshots retain
        # the historical validate-and-linear-lookup path below.
        template_index, cache_hit = _packaged_registry_template_index_lookup()
        if _observability_out is not None:
            _observability_out["packaged_template_cache_hit"] = cache_hit
        templates = template_index.templates
        snapshot_version = template_index.snapshot_version
        schema_version = template_index.schema_version
    else:
        base = _validate_registry_snapshot(packaged_snapshot, effective_config)
        templates_raw = base.get("models")
        if not isinstance(templates_raw, list):
            raise DynamicRankingError("router_dynamic model registry has no models list")
        templates = list(templates_raw)
        snapshot_version = str(base.get("snapshot_version") or "mock-unknown")
        schema_version = str(base.get("schema_version") or "step2-model-registry-v1")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    rows_by_identity: dict[tuple[str, str], dict[str, Any]] = {}

    def add(
        *,
        provider: str,
        model_id: str,
        source: str,
        roles: Sequence[str] = ("proposer", "aggregator"),
        modalities: Sequence[str] = ("text",),
        thinking: str | None = "xhigh",
        override_existing_roles: bool = False,
    ) -> None:
        provider_normalized = provider.strip().lower()
        model_normalized = model_id.strip()
        identity = (provider_normalized, model_normalized.lower())
        if not provider_normalized or not model_normalized:
            return
        if identity in seen:
            if override_existing_roles:
                existing_facts = rows_by_identity[identity].get("registry_facts")
                if isinstance(existing_facts, dict):
                    existing_facts["roles"] = list(dict.fromkeys(roles))
            return
        row = (
            _template_for_packaged_model(template_index, model_normalized)
            if template_index is not None
            else _template_for_model(templates, model_normalized)
        ) or _synthesized_model(
            provider=provider_normalized,
            model_id=model_normalized,
            source=source,
            routed_tier=routed_tier,
            roles=roles,
            modalities=modalities,
            ranking_config=effective_config,
        )
        facts = row.setdefault("registry_facts", {})
        template_provider = str(facts.get("provider") or "").strip().lower()
        if template_provider and template_provider != provider_normalized:
            # Preserve legacy scoring/profile reuse while refusing to claim
            # that another provider accepts the same native thinking values.
            facts["thinking_levels"] = []
            facts["thinking_level_mapping"] = {}
            facts.pop("supported_thinking_levels", None)
        facts["provider"] = provider_normalized
        facts["model_id"] = model_normalized
        facts["roles"] = list(dict.fromkeys(roles or facts.get("roles") or []))
        row["source"] = source
        runtime = row.setdefault("runtime", {})
        runtime["thinking"] = thinking
        seen.add(identity)
        rows_by_identity[identity] = row
        rows.append(row)

    add(
        provider=inherited_provider,
        model_id=inherited_model,
        source="router_anchor",
        modalities=anchor_modalities,
        thinking=None,
    )
    for candidate in operator_candidates:
        if candidate.get("enabled", True) is False:
            continue
        role = str(candidate.get("role") or "").strip().lower()
        roles = ("aggregator",) if role == "aggregator" else ("proposer",)
        add(
            provider=str(candidate.get("provider") or inherited_provider),
            model_id=str(candidate.get("model") or ""),
            source=str(candidate.get("source") or "custom"),
            roles=roles,
            override_existing_roles=True,
        )
    for model_id in legacy_model_options:
        model = str(model_id or "").strip()
        add(
            provider="openrouter" if "/" in model else inherited_provider,
            model_id=model,
            source="legacy_model_options",
        )
    for tier_name, tier_config in (router_tiers or {}).items():
        if not isinstance(tier_config, Mapping):
            continue
        add(
            provider=str(tier_config.get("provider") or inherited_provider),
            model_id=str(tier_config.get("model") or ""),
            source=f"router_tier:{tier_name}",
            thinking=str(tier_config.get("thinking_level") or "xhigh"),
        )
    for template in templates:
        facts = template.get("registry_facts")
        if not isinstance(facts, Mapping):
            continue
        add(
            provider=str(facts.get("provider") or "openrouter"),
            model_id=str(facts.get("model_id") or ""),
            source=str(template.get("source") or "mock_registry"),
            roles=[str(item) for item in facts.get("roles") or []],
            modalities=[str(item) for item in facts.get("modalities") or ["text"]],
            thinking=str((template.get("runtime") or {}).get("thinking") or "xhigh"),
        )

    return {
        "snapshot_version": snapshot_version,
        "schema_version": schema_version,
        "models": rows,
    }


def _registry_number(value: Any, *, identity: str, field_name: str) -> float:
    number = _json_number(value)
    if number is None:
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} has invalid {field_name}"
        )
    return number


def _registry_string_list(
    value: Any,
    *,
    identity: str,
    field_name: str,
    allowed: set[str],
) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} has invalid {field_name}"
        )
    normalized = [item.strip().lower() if isinstance(item, str) else "" for item in value]
    if not normalized or any(not item or item not in allowed for item in normalized):
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} has invalid {field_name}"
        )
    return list(dict.fromkeys(normalized))


def _registry_thinking_contract(
    facts: Mapping[str, Any],
    *,
    identity: str,
    policy: Mapping[str, Any],
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Validate one model's unified levels and provider-native mapping.

    Missing or empty unified levels are a valid registry statement: the
    feature-gated hard filter will fail that deployment closed with the
    auditable ``thinking_level_unavailable`` reason. Malformed non-empty
    contracts are registry errors and never degrade silently.
    """

    level_order = tuple(str(level) for level in policy["level_order"])
    allowed_levels = set(level_order)
    raw_levels = facts.get("thinking_levels")
    if raw_levels is None:
        levels: list[str] = []
    else:
        if not isinstance(raw_levels, Sequence) or isinstance(
            raw_levels,
            (str, bytes, bytearray),
        ):
            raise DynamicRankingError(
                f"router_dynamic model registry {identity} has invalid thinking_levels"
            )
        levels = [item.strip().lower() if isinstance(item, str) else "" for item in raw_levels]
        if any(not level or level not in allowed_levels for level in levels):
            raise DynamicRankingError(
                f"router_dynamic model registry {identity} has invalid thinking_levels"
            )
        if len(set(levels)) != len(levels):
            raise DynamicRankingError(
                f"router_dynamic model registry {identity} has duplicate thinking_levels"
            )
    levels = sorted(levels, key=level_order.index)

    raw_mapping = facts.get("thinking_level_mapping")
    if raw_mapping is None:
        mapping: dict[str, str] = {}
    elif not isinstance(raw_mapping, Mapping):
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} has invalid thinking_level_mapping"
        )
    else:
        mapping = {}
        for raw_level, raw_provider_level in raw_mapping.items():
            level = raw_level.strip().lower() if isinstance(raw_level, str) else ""
            provider_level = (
                raw_provider_level.strip().lower() if isinstance(raw_provider_level, str) else ""
            )
            if (
                not level
                or level not in allowed_levels
                or level in mapping
                or not provider_level
                or provider_level not in THINKING_LEVELS
            ):
                raise DynamicRankingError(
                    f"router_dynamic model registry {identity} has invalid thinking_level_mapping"
                )
            mapping[level] = provider_level

    if set(mapping) != set(levels):
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} thinking_level_mapping "
            "must exactly cover thinking_levels"
        )
    allowed_native_by_unified = {
        "low": {"minimal", "low"},
        "medium": {"medium"},
        "high": {"high"},
        "highest": {"xhigh", "max"},
    }
    invalid_semantic_mappings = sorted(
        f"{level}->{provider_level}"
        for level, provider_level in mapping.items()
        if provider_level not in allowed_native_by_unified[level]
    )
    if invalid_semantic_mappings:
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} has semantically invalid "
            "thinking_level_mapping: " + ", ".join(invalid_semantic_mappings)
        )

    supported_native = facts.get("supported_thinking_levels")
    if supported_native is not None:
        supported = {
            str(level).strip().lower()
            for level in supported_native
            if isinstance(level, str) and str(level).strip()
        }
        unsupported_mappings = sorted(
            {
                provider_level
                for provider_level in mapping.values()
                if provider_level not in supported
            }
        )
        if unsupported_mappings:
            raise DynamicRankingError(
                f"router_dynamic model registry {identity} thinking_level_mapping "
                "uses provider levels outside supported_thinking_levels: "
                + ", ".join(unsupported_mappings)
            )
    if levels and facts.get("supports_reasoning") is False:
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} advertises thinking_levels "
            "without reasoning support"
        )
    return tuple(levels), {level: mapping[level] for level in levels}


def _validate_registry_profile(
    profile: Mapping[str, Any],
    *,
    identity: str,
    profile_key: str,
    allowed_dimensions: set[str],
) -> None:
    values = profile.get(profile_key)
    if not isinstance(values, Mapping) or not set(values).issubset(allowed_dimensions):
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} has invalid {profile_key}"
        )
    for dimension, value in values.items():
        number = _registry_number(
            value,
            identity=identity,
            field_name=f"{profile_key}.{dimension}",
        )
        if not 0.0 <= number <= 1.0:
            raise DynamicRankingError(
                "router_dynamic model registry "
                f"{identity} has out-of-range {profile_key}.{dimension}"
            )


def _registry_nonnegative_int(
    value: Any,
    *,
    identity: str,
    field_name: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} has invalid {field_name}"
        )
    return value


def _validate_role_reliability(
    online_profile: Mapping[str, Any],
    *,
    identity: str,
) -> None:
    raw = online_profile.get("role_reliability")
    if raw is None:
        return
    if not isinstance(raw, Mapping) or set(raw) != {
        "window_size",
        "proposer",
        "aggregator",
        "source",
    }:
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} has invalid role_reliability"
        )
    window_size = _registry_nonnegative_int(
        raw.get("window_size"),
        identity=identity,
        field_name="role_reliability.window_size",
    )
    if window_size != ROLE_RELIABILITY_WINDOW_SIZE:
        raise DynamicRankingError(
            "router_dynamic model registry "
            f"{identity} role_reliability.window_size must be "
            f"{ROLE_RELIABILITY_WINDOW_SIZE}"
        )
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} has invalid role_reliability.source"
        )
    for role in sorted(_MODEL_ROLES):
        counts = raw.get(role)
        if not isinstance(counts, Mapping) or set(counts) != {"success", "failure"}:
            raise DynamicRankingError(
                f"router_dynamic model registry {identity} has invalid role_reliability.{role}"
            )
        success = _registry_nonnegative_int(
            counts.get("success"),
            identity=identity,
            field_name=f"role_reliability.{role}.success",
        )
        failure = _registry_nonnegative_int(
            counts.get("failure"),
            identity=identity,
            field_name=f"role_reliability.{role}.failure",
        )
        if success + failure > window_size:
            raise DynamicRankingError(
                "router_dynamic model registry "
                f"{identity} role_reliability.{role} exceeds window_size"
            )


def _validate_registry_model(
    *,
    identity: str,
    facts: Mapping[str, Any],
    static_profile: Mapping[str, Any],
    online_profile: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
) -> None:
    context_window = _registry_number(
        facts.get("context_window"),
        identity=identity,
        field_name="context_window",
    )
    if context_window <= 0.0 or not context_window.is_integer():
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} has invalid context_window"
        )

    _registry_string_list(
        facts.get("roles"),
        identity=identity,
        field_name="roles",
        allowed=_MODEL_ROLES,
    )
    _registry_string_list(
        facts.get("modalities"),
        identity=identity,
        field_name="modalities",
        allowed=set(MODALITIES),
    )
    context_bucket = facts.get("effective_context_bucket")
    if not isinstance(context_bucket, str) or context_bucket not in _context_bucket_min_tokens(
        ranking_config
    ):
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} has invalid effective_context_bucket"
        )
    credential_available = facts.get("credential_available")
    if credential_available is not None and not isinstance(credential_available, bool):
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} has invalid credential_available"
        )
    for boolean_fact in (
        "is_open_source",
        "is_chinese_model",
        "supports_reasoning",
        "supports_tools",
    ):
        value = facts.get(boolean_fact)
        if value is not None and not isinstance(value, bool):
            raise DynamicRankingError(
                f"router_dynamic model registry {identity} has invalid {boolean_fact}"
            )

    supported_thinking_levels = facts.get("supported_thinking_levels")
    if supported_thinking_levels is not None:
        normalized_thinking_levels = _registry_string_list(
            supported_thinking_levels,
            identity=identity,
            field_name="supported_thinking_levels",
            allowed=set(THINKING_LEVELS),
        )
        if len(normalized_thinking_levels) != len(supported_thinking_levels):
            raise DynamicRankingError(
                f"router_dynamic model registry {identity} has duplicate supported_thinking_levels"
            )
        supports_reasoning = facts.get("supports_reasoning")
        has_enabled_level = any(level != "off" for level in normalized_thinking_levels)
        if supports_reasoning is False and has_enabled_level:
            raise DynamicRankingError(
                "router_dynamic model registry "
                f"{identity} advertises thinking levels without reasoning support"
            )
        if supports_reasoning is True and not has_enabled_level:
            raise DynamicRankingError(
                f"router_dynamic model registry {identity} has no enabled supported_thinking_levels"
            )

    price = facts.get("price")
    if isinstance(price, Mapping):
        price_values = {
            "input": price.get("input_per_million", price.get("input", price.get("prompt"))),
            "output": price.get("output_per_million", price.get("output", price.get("completion"))),
        }
    else:
        price_values = {"combined": price}
    for price_name, price_value in price_values.items():
        number = _registry_number(
            price_value,
            identity=identity,
            field_name=f"price.{price_name}",
        )
        if number < 0.0:
            raise DynamicRankingError(
                f"router_dynamic model registry {identity} has negative price.{price_name}"
            )

    latency_p50 = _registry_number(
        facts.get("latency_p50_ms", facts.get("latency_p50")),
        identity=identity,
        field_name="latency_p50_ms",
    )
    latency_p95 = _registry_number(
        facts.get("latency_p95_ms", facts.get("latency_p95")),
        identity=identity,
        field_name="latency_p95_ms",
    )
    if latency_p50 < 0.0 or latency_p95 < latency_p50:
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} has invalid latency bounds"
        )

    for profile_key, allowed_dimensions in (
        ("capability_dist_prior", set(CAPABILITIES)),
        ("domain_dist_prior", set(DOMAINS)),
        ("tier_dist_prior", set(TIERS)),
        ("role_fit_prior", _MODEL_ROLES),
    ):
        _validate_registry_profile(
            static_profile,
            identity=identity,
            profile_key=profile_key,
            allowed_dimensions=allowed_dimensions,
        )

    error_rates = online_profile.get("error_rates", {})
    if not isinstance(error_rates, Mapping):
        raise DynamicRankingError(
            f"router_dynamic model registry {identity} has invalid error_rates"
        )
    for dimension, value in error_rates.items():
        number = _registry_number(
            value,
            identity=identity,
            field_name=f"error_rates.{dimension}",
        )
        if not 0.0 <= number <= 1.0:
            raise DynamicRankingError(
                f"router_dynamic model registry {identity} has out-of-range error_rates.{dimension}"
            )
    _validate_role_reliability(online_profile, identity=identity)


def _normalize_model(
    row: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
    *,
    thinking_policy: Mapping[str, Any] | None = None,
) -> RankedModel:
    facts_raw = row.get("registry_facts")
    profile_raw = row.get("static_profile")
    if not isinstance(facts_raw, Mapping) or not isinstance(profile_raw, Mapping):
        raise DynamicRankingError("model registry row lacks facts or static profile")
    facts = copy.deepcopy(dict(facts_raw))
    model_id = str(facts.get("model_id") or "").strip()
    provider = str(facts.get("provider") or "").strip().lower()
    if not model_id or not provider:
        raise DynamicRankingError("model registry row lacks provider/model_id")
    online = row.get("online_profile")
    runtime = row.get("runtime")
    if online is not None and not isinstance(online, Mapping):
        raise DynamicRankingError(
            f"router_dynamic model registry {provider}:{model_id} has invalid online_profile"
        )
    if runtime is not None and not isinstance(runtime, Mapping):
        raise DynamicRankingError(
            f"router_dynamic model registry {provider}:{model_id} has invalid runtime"
        )
    online_profile = copy.deepcopy(dict(online)) if isinstance(online, Mapping) else {}
    _validate_registry_model(
        identity=f"{provider}:{model_id}",
        facts=facts,
        static_profile=profile_raw,
        online_profile=online_profile,
        ranking_config=ranking_config,
    )
    if thinking_policy is not None:
        thinking_levels, thinking_level_mapping = _registry_thinking_contract(
            facts,
            identity=f"{provider}:{model_id}",
            policy=thinking_policy,
        )
        facts["thinking_levels"] = list(thinking_levels)
        facts["thinking_level_mapping"] = thinking_level_mapping
    default_thinking = _ranking_string(ranking_config, "synthetic_model", "thinking")
    thinking_value = runtime.get("thinking") if isinstance(runtime, Mapping) else default_thinking
    return RankedModel(
        provider=provider,
        model_id=model_id,
        version=str(
            facts.get("version") or _ranking_string(ranking_config, "synthetic_model", "version")
        ),
        source=str(row.get("source") or "registry"),
        registry_facts=facts,
        static_profile=copy.deepcopy(dict(profile_raw)),
        online_profile=online_profile,
        thinking=None if thinking_value is None else str(thinking_value),
    )


def _permission_matches(model: RankedModel, values: Sequence[Any]) -> bool:
    normalized = {str(value).strip().lower() for value in values}
    return model.model_id.lower() in normalized or model.identity.lower() in normalized


def _routing_budget(
    request_context: Mapping[str, Any], ranking_config: Mapping[str, Any]
) -> dict[str, int]:
    raw = request_context.get("routing_budget")
    values = raw if isinstance(raw, Mapping) else {}
    default_output_tokens = _ranking_int(
        ranking_config, "context", "output_budget", "default_tokens"
    )
    minimum_tokens = _ranking_int(ranking_config, "context", "output_budget", "minimum_tokens")
    return {
        "input": max(0, _as_int(values.get("estimated_input_tokens"), 0)),
        "tools": max(0, _as_int(values.get("tool_log_tokens"), 0)),
        "candidate": max(
            minimum_tokens,
            _as_int(values.get("candidate_output_tokens"), default_output_tokens),
        ),
        "aggregator": max(
            minimum_tokens,
            _as_int(values.get("aggregator_output_tokens"), default_output_tokens),
        ),
    }


def _context_need(
    *,
    role: str,
    task_profile: Mapping[str, Any],
    request_context: Mapping[str, Any],
    proposer_count: int,
    ranking_config: Mapping[str, Any],
) -> int:
    constraints = task_profile.get("constraints")
    constraint_map = constraints if isinstance(constraints, Mapping) else {}
    default_bucket = _ranking_string(ranking_config, "context", "default_bucket")
    bucket = str(constraint_map.get("context") or default_bucket)
    budget = _routing_budget(request_context, ranking_config)
    bucket_min_tokens = _context_bucket_min_tokens(ranking_config)
    input_tokens = max(
        budget["input"],
        bucket_min_tokens.get(bucket, bucket_min_tokens[default_bucket]),
    )
    if role == "aggregator":
        return (
            input_tokens
            + budget["tools"]
            + proposer_count * budget["candidate"]
            + budget["aggregator"]
        )
    return input_tokens + budget["tools"] + budget["candidate"]


def _availability_reasons(
    model: RankedModel,
    role: str,
    ranking_config: Mapping[str, Any],
    *,
    thinking_policy_managed: bool = False,
) -> list[str]:
    facts = model.registry_facts
    reasons: list[str] = []
    eligible_statuses = {
        value.lower()
        for value in _ranking_string_set(ranking_config, "hard_filter", "eligible_statuses")
    }
    if str(facts.get("status") or "").lower() not in eligible_statuses:
        reasons.append("status_unavailable")
    if not bool(facts.get("credential_available", True)):
        reasons.append("credential_unavailable")
    default_health = _ranking_string(ranking_config, "hard_filter", "default_health")
    unavailable_health = {
        value.lower()
        for value in _ranking_string_set(ranking_config, "hard_filter", "unavailable_health_states")
    }
    if str(facts.get("health") or default_health).lower() in unavailable_health:
        reasons.append("health_unavailable")
    default_quota = _ranking_string(ranking_config, "hard_filter", "default_quota")
    unavailable_quota = {
        value.lower()
        for value in _ranking_string_set(ranking_config, "hard_filter", "unavailable_quota_states")
    }
    quota = facts.get("quota", default_quota)
    if (isinstance(quota, (int, float)) and quota <= 0) or str(quota).lower() in unavailable_quota:
        reasons.append("quota_exhausted")
    default_rate_limit = _ranking_string(ranking_config, "hard_filter", "default_rate_limit")
    unavailable_rate_limits = {
        value.lower()
        for value in _ranking_string_set(
            ranking_config, "hard_filter", "unavailable_rate_limit_states"
        )
    }
    rate_limit = str(facts.get("rate_limit") or default_rate_limit).lower()
    if rate_limit in unavailable_rate_limits:
        reasons.append("rate_limited")
    if role.lower() not in {str(value).strip().lower() for value in facts.get("roles") or []}:
        reasons.append(f"role_{role}_unsupported")
    runtime_reasons = facts.get("runtime_hard_filter_reasons")
    if isinstance(runtime_reasons, Sequence) and not isinstance(runtime_reasons, (str, bytes)):
        reasons.extend(str(reason).strip() for reason in runtime_reasons if str(reason).strip())
    runtime_reasons_by_role = facts.get("runtime_hard_filter_reasons_by_role")
    if isinstance(runtime_reasons_by_role, Mapping):
        role_reasons = runtime_reasons_by_role.get(role.strip().lower())
        if isinstance(role_reasons, Sequence) and not isinstance(
            role_reasons,
            (str, bytes),
        ):
            reasons.extend(str(reason).strip() for reason in role_reasons if str(reason).strip())
    if role.strip().lower() == "proposer" and facts.get("retry_excluded_proposer") is True:
        reasons.append("prior_attempt_reasoning_only_length")
    return reasons


def _hard_filter_reasons(
    model: RankedModel,
    *,
    role: str,
    task_profile: Mapping[str, Any],
    user_profile: Mapping[str, Any],
    request_context: Mapping[str, Any],
    proposer_count: int,
    ranking_config: Mapping[str, Any],
    thinking_policy: Mapping[str, Any] | None = None,
) -> tuple[list[str], int]:
    reasons = _availability_reasons(
        model,
        role,
        ranking_config,
        thinking_policy_managed=thinking_policy is not None,
    )
    permission = user_profile.get("permission")
    permission_map = permission if isinstance(permission, Mapping) else {}
    allowed = permission_map.get("allow_models")
    denied = permission_map.get("deny_models")
    allowed_values = (
        allowed if isinstance(allowed, Sequence) and not isinstance(allowed, str) else []
    )
    denied_values = denied if isinstance(denied, Sequence) and not isinstance(denied, str) else []
    if allowed_values and not _permission_matches(model, allowed_values):
        reasons.append("no_permission")
    if denied_values and _permission_matches(model, denied_values):
        reasons.append("no_permission")

    constraints = task_profile.get("constraints")
    constraint_map = constraints if isinstance(constraints, Mapping) else {}
    if thinking_policy is not None:
        level_order = tuple(str(level) for level in thinking_policy["level_order"])
        thinking_levels = {
            str(level).strip().lower()
            for level in model.registry_facts.get("thinking_levels") or []
        }
        task_risk = str(constraint_map.get("risk") or "low").strip().lower()
        risk_floor = thinking_policy["risk_floor"].get(task_risk)
        has_eligible_level = bool(thinking_levels)
        if risk_floor:
            floor_index = level_order.index(str(risk_floor))
            has_eligible_level = any(
                level_order.index(level) >= floor_index
                for level in thinking_levels
                if level in level_order
            )
        if not has_eligible_level:
            reasons.append("thinking_level_unavailable")
    risk_allowlist = permission_map.get("risk_allowlist")
    if isinstance(risk_allowlist, Sequence) and not isinstance(risk_allowlist, str):
        allowed_risks = {str(value).strip().lower() for value in risk_allowlist}
        task_risk = str(constraint_map.get("risk") or "low").strip().lower()
        if allowed_risks and task_risk not in allowed_risks:
            reasons.append("risk_not_allowed")
    default_modalities = _ranking_string_list(
        ranking_config, "hard_filter", "default_required_modalities"
    )
    required_modalities = {
        str(value).strip().lower() for value in constraint_map.get("modality") or default_modalities
    }
    supported_modalities = {
        str(value).strip().lower() for value in model.registry_facts.get("modalities") or []
    }
    if not required_modalities.issubset(supported_modalities):
        reasons.append("modality_mismatch")
    raw_required_by_role = (
        request_context.get("required_parameters_by_role") if thinking_policy is not None else None
    )
    required_by_role = (
        raw_required_by_role.get(role) if isinstance(raw_required_by_role, Mapping) else None
    )
    required_parameters = (
        {str(value).strip().lower() for value in required_by_role if str(value).strip()}
        if isinstance(required_by_role, Sequence) and not isinstance(required_by_role, (str, bytes))
        else set()
    )
    if "tools" in required_parameters and not bool(model.registry_facts.get("supports_tools")):
        reasons.append("required_parameter_tools_unsupported")

    context_need = _context_need(
        role=role,
        task_profile=task_profile,
        request_context=request_context,
        proposer_count=proposer_count,
        ranking_config=ranking_config,
    )
    if _as_int(model.registry_facts.get("context_window"), 0) < context_need:
        reasons.append("context_exceeded")
    return list(dict.fromkeys(reasons)), context_need


def _strength(
    model: RankedModel,
    profile_key: str,
    dimension: str,
    ranking_config: Mapping[str, Any],
) -> float:
    raw = model.static_profile.get(profile_key)
    values = raw if isinstance(raw, Mapping) else {}
    default = _ranking_number(ranking_config, "task_match", "missing_strength_default")
    return _clamp(_as_float(values.get(dimension), default))


def _expectation(
    model: RankedModel,
    distribution: Mapping[str, Any],
    profile_key: str,
    ranking_config: Mapping[str, Any],
) -> float:
    return sum(
        _as_float(weight) * _strength(model, profile_key, str(dimension), ranking_config)
        for dimension, weight in distribution.items()
    )


def _role_fit(model: RankedModel, role: str, ranking_config: Mapping[str, Any]) -> float:
    raw = model.static_profile.get("role_fit_prior")
    values = raw if isinstance(raw, Mapping) else {}
    default = _ranking_number(ranking_config, "task_match", "missing_role_fit_default")
    return _clamp(_as_float(values.get(role), default))


def _role_reliability_score(
    model: RankedModel,
    role: str,
    ranking_config: Mapping[str, Any],
) -> dict[str, Any]:
    if "role_reliability" not in ranking_config:
        return {
            "role": role,
            "success": 0,
            "failure": 0,
            "observed": 0,
            "window_size": ROLE_RELIABILITY_WINDOW_SIZE,
            "source": "",
            "failure_rate": 0.0,
            "penalty_weight": 0.0,
            "penalty": 0.0,
        }
    raw = model.online_profile.get("role_reliability")
    profile = raw if isinstance(raw, Mapping) else {}
    raw_counts = profile.get(role)
    counts = raw_counts if isinstance(raw_counts, Mapping) else {}
    success = _as_int(counts.get("success"), 0)
    failure = _as_int(counts.get("failure"), 0)
    observed = success + failure
    prior_success = _ranking_int(ranking_config, "role_reliability", "prior_success")
    prior_failure = _ranking_int(ranking_config, "role_reliability", "prior_failure")
    failure_rate = (failure + prior_failure) / (observed + prior_success + prior_failure)
    penalty_weight = _ranking_number(
        ranking_config,
        "role_reliability",
        "penalty_weight",
    )
    return {
        "role": role,
        "success": success,
        "failure": failure,
        "observed": observed,
        "window_size": _as_int(profile.get("window_size"), ROLE_RELIABILITY_WINDOW_SIZE),
        "source": str(profile.get("source") or ""),
        "failure_rate": failure_rate,
        "penalty_weight": penalty_weight,
        "penalty": penalty_weight * failure_rate,
    }


def _role_reliability_trace(
    value: Any,
    ranking_config: Mapping[str, Any],
) -> dict[str, Any]:
    reliability = value if isinstance(value, Mapping) else {}
    decimal_places = _ranking_int(ranking_config, "trace", "score_decimal_places")
    return {
        "role": str(reliability.get("role") or ""),
        "success": _as_int(reliability.get("success"), 0),
        "failure": _as_int(reliability.get("failure"), 0),
        "observed": _as_int(reliability.get("observed"), 0),
        "failure_rate": round(
            _as_float(reliability.get("failure_rate")),
            decimal_places,
        ),
        "penalty_weight": round(
            _as_float(reliability.get("penalty_weight")),
            decimal_places,
        ),
        "penalty": round(
            _as_float(reliability.get("penalty")),
            decimal_places,
        ),
        "window_size": _as_int(
            reliability.get("window_size"),
            ROLE_RELIABILITY_WINDOW_SIZE,
        ),
        "source": str(reliability.get("source") or ""),
    }


def _task_match(
    model: RankedModel,
    task_profile: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
    *,
    role: str | None,
) -> float:
    capability = task_profile.get("capability_dist")
    domain = task_profile.get("domain_dist")
    tier = task_profile.get("tier_dist")
    capability_map = capability if isinstance(capability, Mapping) else {}
    domain_map = domain if isinstance(domain, Mapping) else {}
    tier_map = tier if isinstance(tier, Mapping) else {}
    parts = [
        (
            _ranking_number(ranking_config, "task_match", "capability_weight"),
            _expectation(model, capability_map, "capability_dist_prior", ranking_config),
        ),
        (
            _ranking_number(ranking_config, "task_match", "domain_weight"),
            _expectation(model, domain_map, "domain_dist_prior", ranking_config),
        ),
        (
            _ranking_number(ranking_config, "task_match", "tier_weight"),
            _expectation(model, tier_map, "tier_dist_prior", ranking_config),
        ),
    ]
    match = sum(weight * value for weight, value in parts)
    if role is not None:
        match = _ranking_number(
            ranking_config, "task_match", "proposer_task_weight"
        ) * match + _ranking_number(
            ranking_config, "task_match", "proposer_role_fit_weight"
        ) * _role_fit(model, role, ranking_config)

    constraints = task_profile.get("constraints")
    constraints_map = constraints if isinstance(constraints, Mapping) else {}
    default_bucket = _ranking_string(ranking_config, "context", "default_bucket")
    requested_bucket = str(constraints_map.get("context") or default_bucket)
    available_bucket = str(model.registry_facts.get("effective_context_bucket") or default_bucket)
    bucket_min_tokens = _context_bucket_min_tokens(ranking_config)
    default_bucket_minimum = bucket_min_tokens[default_bucket]
    if bucket_min_tokens.get(available_bucket, default_bucket_minimum) < bucket_min_tokens.get(
        requested_bucket, default_bucket_minimum
    ):
        match *= _ranking_number(ranking_config, "task_match", "context_underqualified_multiplier")
    optional = task_profile.get("optional_constraints")
    if isinstance(optional, Mapping) and optional.get("format"):
        format_strength = _strength(
            model, "capability_dist_prior", "format_following", ranking_config
        )
        match *= (
            _ranking_number(ranking_config, "task_match", "format_base_multiplier")
            + _ranking_number(ranking_config, "task_match", "format_strength_multiplier")
            * format_strength
        )
    return _clamp(match)


def _user_score(
    model: RankedModel,
    user_profile: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
) -> float:
    history = user_profile.get("history")
    history_map = history if isinstance(history, Mapping) else {}
    positive = history_map.get("positive_model_ids")
    negative = history_map.get("negative_model_ids")
    positive_values = (
        positive if isinstance(positive, Sequence) and not isinstance(positive, str) else []
    )
    negative_values = (
        negative if isinstance(negative, Sequence) and not isinstance(negative, str) else []
    )
    signal = int(_permission_matches(model, positive_values)) - int(
        _permission_matches(model, negative_values)
    )
    saturation = _ranking_number(ranking_config, "user_score", "feedback_saturation_count")
    confidence = min(1.0, max(0, _as_int(history_map.get("feedback_count"), 0)) / saturation)
    score = (
        _ranking_number(ranking_config, "user_score", "neutral_score")
        + _ranking_number(ranking_config, "user_score", "history_signal_weight")
        * signal
        * confidence
    )
    return _clamp(score)


def _model_in_last_route(model: RankedModel, last_route: Mapping[str, Any]) -> bool:
    values: list[Any] = []
    selected_p = last_route.get("selected_P")
    if isinstance(selected_p, Sequence) and not isinstance(selected_p, str):
        values.extend(selected_p)
    selected_a = last_route.get("selected_A")
    if selected_a:
        values.append(selected_a)
    return _permission_matches(model, values)


def _session_score(
    model: RankedModel,
    task_profile: Mapping[str, Any],
    request_context: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
) -> float:
    intent = task_profile.get("session_intent")
    intent_map = intent if isinstance(intent, Mapping) else {}
    if _as_float(intent_map.get("confidence"), 0.0) < _ranking_number(
        ranking_config, "session", "intent_confidence_threshold"
    ):
        return 0.0
    last_route = request_context.get("last_route")
    if not isinstance(last_route, Mapping) or not _model_in_last_route(model, last_route):
        return 0.0
    intent_type = str(intent_map.get("type") or "new_task")
    if intent_type == "continue":
        default_feedback = _ranking_number(ranking_config, "session", "default_quality_feedback")
        feedback = _clamp(_as_float(last_route.get("quality_feedback"), default_feedback))
        return _ranking_number(ranking_config, "session", "score_delta") * feedback
    if intent_type == "redo":
        return -_ranking_number(ranking_config, "session", "score_delta")
    return 0.0


def _model_price(model: RankedModel, ranking_config: Mapping[str, Any]) -> float:
    raw = model.registry_facts.get("price")
    if isinstance(raw, Mapping):
        input_price = _as_float(
            raw.get("input_per_million", raw.get("input", raw.get("prompt"))), 0.0
        )
        output_price = _as_float(
            raw.get("output_per_million", raw.get("output", raw.get("completion"))), 0.0
        )
        return (
            _ranking_number(ranking_config, "normalization", "price_input_weight") * input_price
            + _ranking_number(ranking_config, "normalization", "price_output_weight") * output_price
        )
    return max(0.0, _as_float(raw, 0.0))


def _model_price_components(model: RankedModel) -> tuple[float, float] | None:
    """Return the exact decomposable rates used by ``_model_price``."""

    raw = model.registry_facts.get("price")
    if not isinstance(raw, Mapping):
        return None
    raw_input = raw.get("input_per_million", raw.get("input", raw.get("prompt")))
    raw_output = raw.get(
        "output_per_million",
        raw.get("output", raw.get("completion")),
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0.0
        for value in (raw_input, raw_output)
    ):
        return None
    return float(raw_input), float(raw_output)


def _single_route_calibration_policy(
    ranking_config: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return the enabled direct-route calibration policy, if any."""

    raw_policy = ranking_config.get("single_route_calibration")
    if raw_policy is None:
        return None
    if not isinstance(raw_policy, Mapping):
        raise DynamicRankingError(
            "router_dynamic ranking config single_route_calibration must be an object"
        )
    return raw_policy if raw_policy.get("enabled") is True else None


def _validate_single_route_calibration_coverage(
    policy: Mapping[str, Any] | None,
    models: Sequence[RankedModel],
) -> bool:
    """Return whether a validated calibration applies to this eligible pool.

    An empty activation list preserves the original strict all-model behavior used
    by experiment overrides. A non-empty list scopes a production calibration to
    the measured model family: pools containing any other eligible model keep the
    shared ranking weights but do not mix calibrated and uncalibrated quality
    scores.
    """

    if policy is None:
        return False
    identities = {model.identity for model in models}
    raw_activation_identities = policy.get("activation_model_identities")
    activation_identities = (
        {
            str(identity)
            for identity in raw_activation_identities
            if isinstance(identity, str)
        }
        if isinstance(raw_activation_identities, Sequence)
        and not isinstance(raw_activation_identities, (str, bytes))
        else set()
    )
    if activation_identities and not identities.issubset(activation_identities):
        return False
    required_sources = (
        (
            "model_quality_priors",
            _as_float(policy.get("model_prior_weight")) > 0.0,
        ),
        (
            "model_residual_coefficients",
            _as_float(policy.get("residual_weight")) > 0.0,
        ),
    )
    for source_name, required in required_sources:
        if not required:
            continue
        raw_values = policy.get(source_name)
        values = raw_values if isinstance(raw_values, Mapping) else {}
        missing = sorted(identities - set(values))
        if missing:
            raise DynamicRankingError(
                "router_dynamic enabled single-route calibration "
                f"{source_name} lacks eligible identities {missing}"
            )
    if policy.get("predicted_total_cost_enabled") is True:
        missing_prices = sorted(
            model.identity
            for model in models
            if _model_price_components(model) is None
        )
        if missing_prices:
            raise DynamicRankingError(
                "router_dynamic enabled single-route predicted cost requires "
                f"decomposable prices for eligible identities {missing_prices}"
            )
    return True


def _single_route_match_features(
    model: RankedModel,
    task_profile: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
) -> dict[str, float]:
    """Return raw capability/domain/tier expectations for residual calibration."""

    features: dict[str, float] = {}
    for feature, task_key, profile_key in (
        ("capability", "capability_dist", "capability_dist_prior"),
        ("domain", "domain_dist", "domain_dist_prior"),
        ("tier", "tier_dist", "tier_dist_prior"),
    ):
        raw_distribution = task_profile.get(task_key)
        distribution = (
            raw_distribution if isinstance(raw_distribution, Mapping) else {}
        )
        features[feature] = _expectation(
            model,
            distribution,
            profile_key,
            ranking_config,
        )
    return features


def _single_route_quality_calibration(
    model: RankedModel,
    task_profile: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
    *,
    quality_raw_clean: float,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply model-prior blending and the fitted centered residual."""

    prior_weight = _as_float(policy.get("model_prior_weight"))
    raw_priors = policy.get("model_quality_priors")
    priors = raw_priors if isinstance(raw_priors, Mapping) else {}
    model_quality_prior = (
        _as_float(priors[model.identity])
        if prior_weight > 0.0
        else quality_raw_clean
    )
    quality_prior_blended = (
        (1.0 - prior_weight) * quality_raw_clean
        + prior_weight * model_quality_prior
    )

    match_features = _single_route_match_features(
        model,
        task_profile,
        ranking_config,
    )
    residual_weight = _as_float(policy.get("residual_weight"))
    residual_raw = 0.0
    if residual_weight > 0.0:
        raw_rows = policy.get("model_residual_coefficients")
        rows = raw_rows if isinstance(raw_rows, Mapping) else {}
        raw_row = rows[model.identity]
        if not isinstance(raw_row, Mapping):
            raise DynamicRankingError(
                "router_dynamic single-route residual row must be an object"
            )
        raw_centers = raw_row.get("centers")
        raw_coefficients = raw_row.get("coefficients")
        centers = raw_centers if isinstance(raw_centers, Mapping) else {}
        coefficients = (
            raw_coefficients if isinstance(raw_coefficients, Mapping) else {}
        )
        residual_raw = _as_float(raw_row.get("intercept")) + sum(
            _as_float(coefficients[feature])
            * (match_features[feature] - _as_float(centers[feature]))
            for feature in sorted(_SINGLE_ROUTE_RESIDUAL_FEATURE_KEYS)
        )
    residual_clip = _as_float(policy.get("residual_clip"))
    residual_clipped = max(
        -residual_clip,
        min(residual_clip, residual_raw),
    )
    quality_calibrated_clean = _clamp(
        quality_prior_blended + residual_weight * residual_clipped
    )
    return {
        "quality_raw_clean": quality_raw_clean,
        "model_quality_prior": model_quality_prior,
        "model_prior_weight": prior_weight,
        "quality_prior_blended": quality_prior_blended,
        "match_features": match_features,
        "residual_raw": residual_raw,
        "residual_clipped": residual_clipped,
        "residual_clip": residual_clip,
        "residual_weight": residual_weight,
        "quality_calibrated_clean": quality_calibrated_clean,
    }


def _single_route_predicted_total_cost(
    model: RankedModel,
    task_profile: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Predict input-plus-output spend from the effective task tier."""

    prices = _model_price_components(model)
    if prices is None:
        raise DynamicRankingError(
            "router_dynamic enabled single-route predicted cost requires "
            f"a decomposable price for {model.identity}"
        )
    effective_tier = str(_effective_tier(task_profile, ranking_config))
    raw_input_tokens = policy.get("input_tokens_by_tier")
    raw_output_tokens = policy.get("output_tokens_by_tier")
    input_tokens_by_tier = (
        raw_input_tokens if isinstance(raw_input_tokens, Mapping) else {}
    )
    output_tokens_by_tier = (
        raw_output_tokens if isinstance(raw_output_tokens, Mapping) else {}
    )
    input_tokens = _as_int(input_tokens_by_tier[effective_tier])
    output_tokens = _as_int(output_tokens_by_tier[effective_tier])
    input_price, output_price = prices
    predicted_cost_usd = (
        input_tokens * input_price + output_tokens * output_price
    ) / 1_000_000.0
    reference_usd = _as_float(policy.get("predicted_cost_reference_usd"))
    return {
        "effective_tier": effective_tier,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_price_usd_per_million": input_price,
        "output_price_usd_per_million": output_price,
        "predicted_cost_usd": predicted_cost_usd,
        "reference_usd": reference_usd,
        "normalized": _clamp(predicted_cost_usd / reference_usd),
    }


def _model_price_source(model: RankedModel) -> str | None:
    """Return the frozen registry provenance for decomposable model pricing."""

    raw = model.registry_facts.get("price")
    if not isinstance(raw, Mapping):
        return None
    value = raw.get("price_source")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _cache_affinity_policy_for_topology(
    ranking_config: Mapping[str, Any],
    topology: CacheTopology,
) -> Mapping[str, Any] | None:
    session = _ranking_mapping(ranking_config, "session")
    raw_policy = session.get("kv_cache_affinity")
    if raw_policy is None:
        return None
    if not isinstance(raw_policy, Mapping):
        raise DynamicRankingError(
            "router_dynamic ranking config session.kv_cache_affinity must be an object"
        )
    topologies = raw_policy.get("topologies")
    if not isinstance(topologies, Sequence) or isinstance(
        topologies,
        (str, bytes, bytearray),
    ):
        raise DynamicRankingError(
            "router_dynamic ranking config session.kv_cache_affinity.topologies must be a list"
        )
    return raw_policy if topology in topologies else None


def _cache_affinity_unavailable_reason_trace(
    raw_reasons: Sequence[Mapping[str, Any]] | None,
    *,
    policy: Mapping[str, Any] | None,
    topology: CacheTopology,
    candidate_identities: Sequence[str],
) -> list[dict[str, str]]:
    """Validate safe diagnostics supplied by the deployment-aware caller."""

    if raw_reasons is None:
        return []
    if policy is None or str(policy.get("strategy") or "") != "expected_cost":
        raise DynamicRankingError(
            "router_dynamic cache affinity unavailable reasons require an "
            "active expected_cost policy"
        )
    if not isinstance(raw_reasons, Sequence) or isinstance(
        raw_reasons,
        (str, bytes, bytearray),
    ):
        raise DynamicRankingError(
            "router_dynamic cache affinity unavailable reasons must be a list"
        )
    allowed_roles = {"single"} if topology == "single" else {"proposer", "aggregator"}
    allowed_identities = set(candidate_identities)
    allowed_reason = CacheAffinityUnavailableReason.EXACT_CACHE_PRICE_QUOTE_UNAVAILABLE.value
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in raw_reasons:
        if not isinstance(raw, Mapping) or set(raw) != {
            "role",
            "identity",
            "reason",
        }:
            raise DynamicRankingError(
                "router_dynamic cache affinity unavailable reason is malformed"
            )
        role = str(raw.get("role") or "")
        identity = str(raw.get("identity") or "")
        reason = str(raw.get("reason") or "")
        key = (role, identity, reason)
        if (
            role not in allowed_roles
            or identity not in allowed_identities
            or reason != allowed_reason
            or key in seen
        ):
            raise DynamicRankingError("router_dynamic cache affinity unavailable reason is invalid")
        seen.add(key)
        normalized.append({"role": role, "identity": identity, "reason": reason})
    normalized.sort(key=lambda row: (row["role"], row["identity"], row["reason"]))
    return normalized


def _cache_affinity_evidence(
    cache_affinity_inputs: Mapping[
        str,
        Mapping[str, CacheAffinityEvidenceInput],
    ]
    | None,
    *,
    role: CacheRole,
    identity: str,
) -> CacheAffinityEvidenceInput | None:
    if cache_affinity_inputs is None:
        return None
    if not isinstance(cache_affinity_inputs, Mapping):
        raise DynamicRankingError("router_dynamic cache_affinity_inputs must be a role mapping")
    role_inputs = cache_affinity_inputs.get(role)
    if role_inputs is None:
        return None
    if not isinstance(role_inputs, Mapping):
        raise DynamicRankingError(
            f"router_dynamic cache_affinity_inputs.{role} must be an identity mapping"
        )
    evidence = role_inputs.get(identity)
    if evidence is None:
        return None
    if (
        not isinstance(evidence, CacheAffinityEvidenceInput)
        or evidence.identity != identity
        or evidence.role != role
    ):
        raise DynamicRankingError(
            "router_dynamic cache affinity evidence identity or role is invalid"
        )
    return evidence


def _cache_affinity_adjustment_for_model(
    *,
    policy: Mapping[str, Any],
    cache_affinity_inputs: Mapping[
        str,
        Mapping[str, CacheAffinityEvidenceInput],
    ]
    | None,
    role: CacheRole,
    topology: CacheTopology,
    model: RankedModel,
    task_profile: Mapping[str, Any],
    analyzer_schema_valid: bool,
    session_intent: str,
    request_context: Mapping[str, Any],
    proposer_count: int,
    cost_weight: float,
    ranking_config: Mapping[str, Any],
    frozen_adjustments: Mapping[
        str,
        Mapping[str, CacheAffinityScoreAdjustment],
    ]
    | None = None,
) -> CacheAffinityScoreAdjustment | None:
    intent = task_profile.get("session_intent")
    intent_map = intent if isinstance(intent, Mapping) else {}
    if frozen_adjustments is not None:
        role_adjustments = frozen_adjustments.get(role)
        if role_adjustments is not None and not isinstance(role_adjustments, Mapping):
            raise DynamicRankingError(
                "router_dynamic frozen cache adjustments must be role mappings"
            )
        frozen = (
            role_adjustments.get(model.identity) if isinstance(role_adjustments, Mapping) else None
        )
        if frozen is not None:
            _validate_frozen_cache_adjustment_for_model(
                frozen,
                policy=policy,
                role=role,
                model=model,
                task_profile=task_profile,
                analyzer_schema_valid=analyzer_schema_valid,
                session_intent=session_intent,
                request_context=request_context,
                proposer_count=proposer_count,
                cost_weight=cost_weight,
                ranking_config=ranking_config,
            )
            return frozen
    if (
        analyzer_schema_valid is not True
        or session_intent != "continue"
        or _as_float(intent_map.get("confidence"), 0.0)
        < _ranking_number(
            ranking_config,
            "session",
            "intent_confidence_threshold",
        )
    ):
        return None
    evidence = _cache_affinity_evidence(
        cache_affinity_inputs,
        role=role,
        identity=model.identity,
    )
    if evidence is None:
        return None
    budget = _routing_budget(request_context, ranking_config)
    price_components = _model_price_components(model)
    input_price, output_price = price_components if price_components is not None else (None, None)
    ranking_price_source = _model_price_source(model)
    if str(policy.get("strategy") or "") == "expected_cost" and (
        ranking_price_source is None or evidence.ranking_price_source != ranking_price_source
    ):
        return None
    adjustment = cache_affinity_score_adjustment(
        policy=policy,
        evidence=evidence,
        role=role,
        topology=topology,
        intent_type=session_intent,
        intent_confidence=_as_float(intent_map.get("confidence"), 0.0),
        intent_confidence_threshold=_ranking_number(
            ranking_config,
            "session",
            "intent_confidence_threshold",
        ),
        estimated_input_tokens=budget["input"],
        tool_log_tokens=budget["tools"],
        candidate_output_tokens=budget["candidate"],
        proposer_count=proposer_count,
        ranking_input_per_million=input_price,
        ranking_output_per_million=output_price,
        ranking_price_source=ranking_price_source,
        price_input_weight=_ranking_number(
            ranking_config,
            "normalization",
            "price_input_weight",
        ),
        price_output_weight=_ranking_number(
            ranking_config,
            "normalization",
            "price_output_weight",
        ),
        price_reference_usd_per_million=_ranking_number(
            ranking_config,
            "normalization",
            "price_reference_usd_per_million",
        ),
        cost_weight=cost_weight,
    )
    if adjustment.strategy == "expected_cost" and not adjustment.price_source:
        return None
    return adjustment


def _cache_affinity_trace_number(
    raw: Mapping[str, Any],
    key: str,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DynamicRankingError(f"router_dynamic frozen cache adjustment {key} must be numeric")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise DynamicRankingError(
            f"router_dynamic frozen cache adjustment {key} must be finite"
        ) from exc
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
        raise DynamicRankingError(f"router_dynamic frozen cache adjustment {key} is out of range")
    return number


def _cache_affinity_trace_int(
    raw: Mapping[str, Any],
    key: str,
) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DynamicRankingError(
            f"router_dynamic frozen cache adjustment {key} must be an exact integer"
        )
    return value


def _cache_affinity_adjustment_from_trace(
    raw: Mapping[str, Any],
    *,
    policy: Mapping[str, Any],
) -> CacheAffinityScoreAdjustment:
    strategy = str(policy.get("strategy") or "")
    common_keys = {
        "identity",
        "role",
        "strategy",
        "evidence_kind",
        "decay_factor",
        "score_adjustment",
    }
    expected_cost_keys = {
        "N",
        "K",
        "cache_evidence_tokens",
        "p",
        "price_source",
        "cache_read_per_million",
        "cache_write_per_million",
        "C0",
        "Chit",
        "Cmiss",
        "r_input_eff",
        "cost_normalized_before",
        "cost_normalized_after",
    }
    expected_keys = common_keys | (expected_cost_keys if strategy == "expected_cost" else set())
    if set(raw) != expected_keys:
        raise DynamicRankingError(
            "router_dynamic frozen cache adjustment has unknown or missing keys"
        )
    identity = str(raw.get("identity") or "").strip()
    role = str(raw.get("role") or "")
    evidence_kind = str(raw.get("evidence_kind") or "")
    if (
        not identity
        or role not in {"single", "proposer", "aggregator"}
        or raw.get("strategy") != strategy
        or evidence_kind not in {"read_hit", "write_only"}
    ):
        raise DynamicRankingError(
            "router_dynamic frozen cache adjustment identity or discriminator is invalid"
        )
    decay_factor = _cache_affinity_trace_number(
        raw,
        "decay_factor",
        maximum=1.0,
    )
    score_adjustment = _cache_affinity_trace_number(
        raw,
        "score_adjustment",
        minimum=-math.inf,
    )
    if strategy == "bonus":
        configured = policy.get("bonus_by_evidence")
        if not isinstance(configured, Mapping):
            raise DynamicRankingError("router_dynamic frozen cache bonus policy is invalid")
        expected_score = float(configured[evidence_kind]) * decay_factor
        if score_adjustment != expected_score or not math.isfinite(expected_score):
            raise DynamicRankingError("router_dynamic frozen cache bonus does not match the policy")
        return CacheAffinityScoreAdjustment(
            identity=identity,
            role=role,  # type: ignore[arg-type]
            strategy=strategy,
            evidence_kind=evidence_kind,  # type: ignore[arg-type]
            decay_factor=decay_factor,
            score_adjustment=score_adjustment,
        )
    if strategy != "expected_cost":
        raise DynamicRankingError("router_dynamic frozen cache strategy is invalid")
    input_tokens = _cache_affinity_trace_int(raw, "N")
    cache_tokens = _cache_affinity_trace_int(raw, "K")
    observed_cache_tokens = _cache_affinity_trace_int(
        raw,
        "cache_evidence_tokens",
    )
    if (
        input_tokens <= 0
        or cache_tokens <= 0
        or observed_cache_tokens <= 0
        or cache_tokens > input_tokens
        or cache_tokens != min(input_tokens, observed_cache_tokens)
    ):
        raise DynamicRankingError("router_dynamic frozen cache token projection is invalid")
    price_source = str(raw.get("price_source") or "").strip()
    if not price_source:
        raise DynamicRankingError("router_dynamic frozen cache price source is missing")
    return CacheAffinityScoreAdjustment(
        identity=identity,
        role=role,  # type: ignore[arg-type]
        strategy=strategy,
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        decay_factor=decay_factor,
        score_adjustment=score_adjustment,
        input_tokens=input_tokens,
        cache_tokens=cache_tokens,
        observed_cache_tokens=observed_cache_tokens,
        hit_probability=_cache_affinity_trace_number(
            raw,
            "p",
            maximum=1.0,
        ),
        price_source=price_source,
        cache_read_per_million=_cache_affinity_trace_number(
            raw,
            "cache_read_per_million",
        ),
        cache_write_per_million=_cache_affinity_trace_number(
            raw,
            "cache_write_per_million",
        ),
        baseline_input_cost_usd=_cache_affinity_trace_number(raw, "C0"),
        cache_hit_input_cost_usd=_cache_affinity_trace_number(raw, "Chit"),
        cache_miss_input_cost_usd=_cache_affinity_trace_number(raw, "Cmiss"),
        effective_input_per_million=_cache_affinity_trace_number(
            raw,
            "r_input_eff",
        ),
        cost_normalized_before=_cache_affinity_trace_number(
            raw,
            "cost_normalized_before",
            maximum=1.0,
        ),
        cost_normalized_after=_cache_affinity_trace_number(
            raw,
            "cost_normalized_after",
            maximum=1.0,
        ),
    )


def _validate_frozen_cache_adjustment_for_model(
    adjustment: CacheAffinityScoreAdjustment,
    *,
    policy: Mapping[str, Any],
    role: CacheRole,
    model: RankedModel,
    task_profile: Mapping[str, Any],
    analyzer_schema_valid: bool,
    session_intent: str,
    request_context: Mapping[str, Any],
    proposer_count: int,
    cost_weight: float,
    ranking_config: Mapping[str, Any],
) -> None:
    if (
        adjustment.identity != model.identity
        or adjustment.role != role
        or adjustment.strategy != str(policy.get("strategy") or "")
    ):
        raise DynamicRankingError("router_dynamic frozen cache adjustment model binding is invalid")
    intent = task_profile.get("session_intent")
    intent_map = intent if isinstance(intent, Mapping) else {}
    if (
        analyzer_schema_valid is not True
        or session_intent != "continue"
        or _as_float(intent_map.get("confidence"), 0.0)
        < _ranking_number(
            ranking_config,
            "session",
            "intent_confidence_threshold",
        )
        or adjustment.decay_factor <= 0.0
    ):
        raise DynamicRankingError("router_dynamic frozen cache adjustment violates the intent gate")
    if adjustment.strategy == "bonus":
        configured = policy.get("bonus_by_evidence")
        expected = (
            float(configured[adjustment.evidence_kind]) * adjustment.decay_factor
            if isinstance(configured, Mapping)
            else math.nan
        )
        if adjustment.score_adjustment != expected:
            raise DynamicRankingError("router_dynamic frozen cache bonus does not match the model")
        return
    budget = _routing_budget(request_context, ranking_config)
    expected_input_tokens = budget["input"] + budget["tools"]
    if role == "aggregator":
        expected_input_tokens += proposer_count * budget["candidate"]
    probabilities = policy.get("hit_probability_by_evidence")
    expected_probability = (
        float(probabilities[adjustment.evidence_kind]) * adjustment.decay_factor
        if isinstance(probabilities, Mapping)
        else math.nan
    )
    expected_score = cost_weight * (
        adjustment.cost_normalized_before - adjustment.cost_normalized_after
    )
    price_components = _model_price_components(model)
    if price_components is None:
        raise DynamicRankingError(
            "router_dynamic frozen expected-cost adjustment lacks decomposable pricing"
        )
    ranking_price_source = _model_price_source(model)
    if ranking_price_source is None or adjustment.price_source != ranking_price_source:
        raise DynamicRankingError(
            "router_dynamic frozen expected-cost adjustment price source does not match the model"
        )
    input_price, output_price = price_components
    input_weight = _ranking_number(
        ranking_config,
        "normalization",
        "price_input_weight",
    )
    output_weight = _ranking_number(
        ranking_config,
        "normalization",
        "price_output_weight",
    )
    price_reference = _ranking_number(
        ranking_config,
        "normalization",
        "price_reference_usd_per_million",
    )
    expected_baseline = expected_input_tokens * input_price / 1_000_000
    expected_cache_tokens = min(
        expected_input_tokens,
        adjustment.observed_cache_tokens,
    )
    expected_normal_tokens = expected_input_tokens - expected_cache_tokens
    expected_hit = (
        expected_normal_tokens * input_price
        + expected_cache_tokens * adjustment.cache_read_per_million
    ) / 1_000_000
    expected_miss = (
        expected_normal_tokens * input_price
        + expected_cache_tokens * adjustment.cache_write_per_million
    ) / 1_000_000
    expected_input_cost = (
        adjustment.hit_probability * expected_hit
        + (1.0 - adjustment.hit_probability) * expected_miss
    )
    expected_effective_rate = expected_input_cost / expected_input_tokens * 1_000_000
    old_rate = input_weight * input_price + output_weight * output_price
    new_rate = old_rate + input_weight * (expected_effective_rate - input_price)
    expected_normalized_before = _clamp(old_rate / price_reference)
    expected_normalized_after = _clamp(new_rate / price_reference)
    if (
        adjustment.input_tokens != expected_input_tokens
        or adjustment.cache_tokens != expected_cache_tokens
        or adjustment.hit_probability != expected_probability
        or adjustment.baseline_input_cost_usd != expected_baseline
        or adjustment.cache_hit_input_cost_usd != expected_hit
        or adjustment.cache_miss_input_cost_usd != expected_miss
        or adjustment.effective_input_per_million != expected_effective_rate
        or adjustment.cost_normalized_before != expected_normalized_before
        or adjustment.cost_normalized_after != expected_normalized_after
        or adjustment.score_adjustment != expected_score
    ):
        raise DynamicRankingError(
            "router_dynamic frozen expected-cost adjustment does not match ranking inputs"
        )


def _cache_affinity_adjustments_from_trace(
    raw_inputs: object,
    *,
    policy: Mapping[str, Any],
    candidate_pool_size: int,
) -> dict[str, dict[str, CacheAffinityScoreAdjustment]]:
    if not isinstance(raw_inputs, list) or len(raw_inputs) > 2 * candidate_pool_size:
        raise DynamicRankingError(
            "router_dynamic frozen cache adjustment table is invalid or unbounded"
        )
    result: dict[str, dict[str, CacheAffinityScoreAdjustment]] = {}
    for raw in raw_inputs:
        if not isinstance(raw, Mapping):
            raise DynamicRankingError(
                "router_dynamic frozen cache adjustment row must be an object"
            )
        adjustment = _cache_affinity_adjustment_from_trace(raw, policy=policy)
        role_rows = result.setdefault(adjustment.role, {})
        if adjustment.identity in role_rows:
            raise DynamicRankingError("router_dynamic frozen cache adjustment table has duplicates")
        role_rows[adjustment.identity] = adjustment
    return result


def _cost_latency_weights(
    task_profile: Mapping[str, Any],
    user_profile: Mapping[str, Any] | None,
    ranking_config: Mapping[str, Any],
) -> tuple[float, float]:
    penalties = _ranking_mapping(ranking_config, "penalties")
    latency_penalty_enabled = (
        _ranking_bool(ranking_config, "penalties", "latency_penalty_enabled")
        if "latency_penalty_enabled" in penalties
        else True
    )
    constraints = task_profile.get("constraints")
    constraints_map = constraints if isinstance(constraints, Mapping) else {}
    default_cost = _ranking_number(ranking_config, "penalties", "default_cost_weight")
    default_latency = _ranking_number(ranking_config, "penalties", "default_latency_weight")
    task_cost_weights = _ranking_mapping(ranking_config, "penalties", "task_cost_weights")
    task_latency_weights = _ranking_mapping(ranking_config, "penalties", "task_latency_weights")
    cost_weight = _as_float(
        task_cost_weights.get(str(constraints_map.get("cost") or "medium")),
        default_cost,
    )
    latency_weight = _as_float(
        task_latency_weights.get(str(constraints_map.get("latency") or "normal")),
        default_latency,
    )
    if user_profile is None:
        return cost_weight, latency_weight if latency_penalty_enabled else 0.0
    preference = user_profile.get("preference")
    preference_map = preference if isinstance(preference, Mapping) else {}
    sensitivity = str(preference_map.get("cost_sensitivity") or "medium")
    sensitivity_weights = _ranking_mapping(
        ranking_config, "penalties", "user_cost_sensitivity_weights"
    )
    cost_weight = max(
        cost_weight,
        _as_float(sensitivity_weights.get(sensitivity), default_cost),
    )
    tradeoff = str(preference_map.get("quality_latency_tradeoff") or "balanced")
    if tradeoff == "latency_first":
        latency_weight += _ranking_number(ranking_config, "penalties", "latency_first_adjustment")
    elif tradeoff == "quality_first":
        minimum = _ranking_number(ranking_config, "penalties", "quality_first_minimum_weight")
        latency_weight = max(
            minimum,
            latency_weight
            - _ranking_number(ranking_config, "penalties", "quality_first_latency_reduction"),
        )
        cost_weight = max(
            minimum,
            cost_weight
            - _ranking_number(ranking_config, "penalties", "quality_first_cost_reduction"),
        )
    return cost_weight, latency_weight if latency_penalty_enabled else 0.0


def _resource_aware_proposer_rerank_enabled(
    ranking_config: Mapping[str, Any],
) -> bool:
    """Return whether proposer marginal scores include resource penalties."""

    return "latency_penalty_enabled" in _ranking_mapping(ranking_config, "penalties")


def _base_score_row(
    model: RankedModel,
    task_profile: Mapping[str, Any],
    user_profile: Mapping[str, Any] | None,
    request_context: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
    *,
    single_route: bool = False,
    single_route_calibration_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    task_match = _task_match(model, task_profile, ranking_config, role="proposer")
    user_score = (
        _user_score(model, user_profile, ranking_config) if user_profile is not None else 0.0
    )
    session_score = _session_score(model, task_profile, request_context, ranking_config)
    quality_before_reliability = task_match
    if user_profile is not None:
        quality_before_reliability = (
            _ranking_number(ranking_config, "quality", "task_match_weight") * task_match
            + _ranking_number(ranking_config, "quality", "user_score_weight") * user_score
        )
    reliability = _role_reliability_score(model, "proposer", ranking_config)
    quality_raw_clean = quality_before_reliability - _as_float(
        reliability["penalty"]
    )
    calibration_policy = single_route_calibration_policy if single_route else None
    calibration: dict[str, Any] | None = None
    if calibration_policy is not None:
        calibration = _single_route_quality_calibration(
            model,
            task_profile,
            ranking_config,
            quality_raw_clean=quality_raw_clean,
            policy=calibration_policy,
        )
        quality_clean = _as_float(calibration["quality_calibrated_clean"])
    else:
        quality_clean = quality_raw_clean
    quality = _clamp(quality_clean + session_score)
    price_reference = _ranking_number(
        ranking_config, "normalization", "price_reference_usd_per_million"
    )
    latency_reference = _ranking_number(ranking_config, "normalization", "latency_reference_ms")
    predicted_total_cost: dict[str, Any] | None = None
    if (
        calibration_policy is not None
        and calibration_policy.get("predicted_total_cost_enabled") is True
    ):
        predicted_total_cost = _single_route_predicted_total_cost(
            model,
            task_profile,
            ranking_config,
            calibration_policy,
        )
        cost_normalized = _as_float(predicted_total_cost["normalized"])
    else:
        cost_normalized = _clamp(
            _model_price(model, ranking_config) / price_reference
        )
    latency = _as_float(
        model.registry_facts.get("latency_p95_ms", model.registry_facts.get("latency_p95")),
        latency_reference,
    )
    latency_normalized = _clamp(latency / latency_reference)
    cost_weight, latency_weight = _cost_latency_weights(task_profile, user_profile, ranking_config)
    row = {
        "model": model,
        "task_match": task_match,
        "user_score": user_score,
        "session_score": session_score,
        "quality_before_reliability": quality_before_reliability,
        "quality_clean": quality_clean,
        "quality": quality,
        "reliability": reliability,
        "cost_normalized": cost_normalized,
        "latency_normalized": latency_normalized,
        "cost_weight": cost_weight,
        "latency_weight": latency_weight,
        "base_clean": quality_clean
        - cost_weight * cost_normalized
        - latency_weight * latency_normalized,
        "base": quality - cost_weight * cost_normalized - latency_weight * latency_normalized,
    }
    if calibration is not None:
        row["single_route_calibration"] = calibration
        if predicted_total_cost is not None:
            row["predicted_total_cost"] = predicted_total_cost
    return row


def _score_trace(row: Mapping[str, Any], ranking_config: Mapping[str, Any]) -> dict[str, Any]:
    model = row["model"]
    decimal_places = _ranking_int(ranking_config, "trace", "score_decimal_places")
    trace = {
        "identity": model.identity,
        "model": model.model_id,
        "provider": model.provider,
        "S_match": round(_as_float(row.get("task_match")), decimal_places),
        "S_user": round(_as_float(row.get("user_score")), decimal_places),
        "S_session": round(_as_float(row.get("session_score")), decimal_places),
        "S_qual_clean": round(_as_float(row.get("quality_clean")), decimal_places),
        "S_qual": round(_as_float(row.get("quality")), decimal_places),
        "cost": round(_as_float(row.get("cost_normalized")), decimal_places),
        "latency": round(_as_float(row.get("latency_normalized")), decimal_places),
        "cost_weight": round(_as_float(row.get("cost_weight")), decimal_places),
        "latency_weight": round(_as_float(row.get("latency_weight")), decimal_places),
        "S_base_clean": round(_as_float(row.get("base_clean")), decimal_places),
        "S_base": round(_as_float(row.get("base")), decimal_places),
    }
    cache_adjustment = row.get("cache_affinity_adjustment")
    if isinstance(cache_adjustment, CacheAffinityScoreAdjustment):
        trace["cache_affinity"] = cache_adjustment.trace()
        if "final_score" in row:
            trace["S_final"] = round(
                _as_float(row.get("final_score")),
                decimal_places,
            )
    if "role_reliability" in ranking_config:
        trace.update(
            {
                "S_qual_before_reliability": round(
                    _as_float(row.get("quality_before_reliability")),
                    decimal_places,
                ),
                "role_reliability": _role_reliability_trace(
                    row.get("reliability"),
                    ranking_config,
                ),
            }
        )
    calibration = row.get("single_route_calibration")
    if isinstance(calibration, Mapping):
        raw_features = calibration.get("match_features")
        features = raw_features if isinstance(raw_features, Mapping) else {}
        calibration_trace: dict[str, Any] = {
            "quality_raw_clean": round(
                _as_float(calibration.get("quality_raw_clean")),
                decimal_places,
            ),
            "model_quality_prior": round(
                _as_float(calibration.get("model_quality_prior")),
                decimal_places,
            ),
            "model_prior_weight": round(
                _as_float(calibration.get("model_prior_weight")),
                decimal_places,
            ),
            "quality_prior_blended": round(
                _as_float(calibration.get("quality_prior_blended")),
                decimal_places,
            ),
            "match_features": {
                feature: round(
                    _as_float(features.get(feature)),
                    decimal_places,
                )
                for feature in sorted(_SINGLE_ROUTE_RESIDUAL_FEATURE_KEYS)
            },
            "residual_raw": round(
                _as_float(calibration.get("residual_raw")),
                decimal_places,
            ),
            "residual_clipped": round(
                _as_float(calibration.get("residual_clipped")),
                decimal_places,
            ),
            "residual_clip": round(
                _as_float(calibration.get("residual_clip")),
                decimal_places,
            ),
            "residual_weight": round(
                _as_float(calibration.get("residual_weight")),
                decimal_places,
            ),
            "quality_calibrated_clean": round(
                _as_float(calibration.get("quality_calibrated_clean")),
                decimal_places,
            ),
        }
        predicted_cost = row.get("predicted_total_cost")
        if isinstance(predicted_cost, Mapping):
            calibration_trace["predicted_total_cost"] = {
                "effective_tier": str(
                    predicted_cost.get("effective_tier") or ""
                ),
                "input_tokens": _as_int(predicted_cost.get("input_tokens")),
                "output_tokens": _as_int(
                    predicted_cost.get("output_tokens")
                ),
                "input_price_usd_per_million": round(
                    _as_float(
                        predicted_cost.get("input_price_usd_per_million")
                    ),
                    decimal_places,
                ),
                "output_price_usd_per_million": round(
                    _as_float(
                        predicted_cost.get("output_price_usd_per_million")
                    ),
                    decimal_places,
                ),
                "predicted_cost_usd": round(
                    _as_float(predicted_cost.get("predicted_cost_usd")),
                    decimal_places,
                ),
                "reference_usd": round(
                    _as_float(predicted_cost.get("reference_usd")),
                    decimal_places,
                ),
                "normalized": round(
                    _as_float(predicted_cost.get("normalized")),
                    decimal_places,
                ),
            }
        trace["single_route_calibration"] = calibration_trace
    return trace


def _shift_tier_distribution(
    tier_dist: Mapping[str, Any], ranking_config: Mapping[str, Any]
) -> dict[str, float]:
    shifted: dict[str, float] = {}
    tier_values = _router_tier_mapping(ranking_config).values()
    minimum_tier = min(tier_values)
    maximum_tier = max(tier_values)
    for tier, weight in tier_dist.items():
        destination = str(
            min(
                maximum_tier,
                max(minimum_tier, _as_int(tier, minimum_tier)) + 1,
            )
        )
        shifted[destination] = shifted.get(destination, 0.0) + _as_float(weight)
    total = sum(shifted.values()) or 1.0
    return {tier: weight / total for tier, weight in shifted.items()}


def _apply_session_adjustment(
    task_profile: Mapping[str, Any],
    request_context: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
    *,
    cache_continuity_available: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(cache_continuity_available, bool):
        raise DynamicRankingError("router_dynamic cache_continuity_available must be a boolean")
    before = copy.deepcopy(dict(task_profile))
    adjusted = copy.deepcopy(dict(task_profile))
    intent = adjusted.get("session_intent")
    intent_map = intent if isinstance(intent, Mapping) else {}
    intent_type = str(intent_map.get("type") or "new_task")
    intent_confidence = _as_float(intent_map.get("confidence"), 0.0)
    confidence_threshold = _ranking_number(ranking_config, "session", "intent_confidence_threshold")
    max_escalation_level = _ranking_int(ranking_config, "session", "max_escalation_level")
    profile_decimal_places = _ranking_int(ranking_config, "trace", "profile_decimal_places")
    if intent_confidence < confidence_threshold:
        intent_type = "new_task"
    last_route = request_context.get("last_route")
    last_route_map = last_route if isinstance(last_route, Mapping) else {}
    if (
        intent_type != "new_task"
        and not last_route_map
        and not (
            cache_continuity_available
            and intent_type == "continue"
            and intent_confidence >= confidence_threshold
        )
    ):
        intent_type = "new_task"
    previous_escalation = max(0, _as_int(last_route_map.get("escalation_level"), 0))
    escalation_level = previous_escalation
    tier_shifted = False
    if intent_type == "redo" and previous_escalation < max_escalation_level:
        tier_dist = adjusted.get("tier_dist")
        if isinstance(tier_dist, Mapping):
            adjusted["tier_dist"] = _shift_tier_distribution(tier_dist, ranking_config)
            tier_shifted = True
        escalation_level += 1
    elif intent_type == "new_task":
        escalation_level = 0
    return adjusted, {
        "intent": intent_type,
        "intent_confidence": round(intent_confidence, profile_decimal_places),
        "sticky_applied": intent_type == "continue",
        "tier_shifted": tier_shifted,
        "previous_escalation_level": previous_escalation,
        "escalation_level": min(max_escalation_level, escalation_level),
        "task_profile_pre_escalation": before,
        "task_profile_post_escalation": copy.deepcopy(adjusted),
    }


def _effective_tier(task_profile: Mapping[str, Any], ranking_config: Mapping[str, Any]) -> int:
    raw = task_profile.get("tier_dist")
    router_tier_mapping = _router_tier_mapping(ranking_config)
    default_router_tier = _ranking_string(ranking_config, "routing_tiers", "default_router_tier")
    default_tier = router_tier_mapping[default_router_tier]
    tier_dist = raw if isinstance(raw, Mapping) else {str(default_tier): 1.0}
    expected = sum(
        _as_int(tier, default_tier) * _as_float(weight) for tier, weight in tier_dist.items()
    )
    rounding_offset = _ranking_number(
        ranking_config, "proposer_count", "effective_tier_rounding_offset"
    )
    tier_values = _router_tier_mapping(ranking_config).values()
    return max(
        min(tier_values),
        min(max(tier_values), math.floor(expected + rounding_offset)),
    )


def _shift_thinking_level(
    level: str,
    steps: int,
    *,
    level_order: Sequence[str],
) -> str:
    index = level_order.index(level)
    destination = max(0, min(len(level_order) - 1, index + steps))
    return str(level_order[destination])


def _thinking_target_for_role(
    *,
    role: str,
    effective_tier: int,
    task_profile: Mapping[str, Any],
    session_trace: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> tuple[str, list[str], str | None]:
    """Apply the section-6.6 corrections in their specified order."""

    level_order = tuple(str(level) for level in policy["level_order"])
    target = str(policy["tier_mapping"][str(effective_tier)])
    reasons = [f"tier_{effective_tier}_base_{target}"]
    if role == "aggregator":
        before = target
        target = _shift_thinking_level(
            target,
            int(policy["aggregator_level_step"]),
            level_order=level_order,
        )
        reasons.append(
            "aggregator_level_step" if target != before else "aggregator_level_step_capped"
        )

    constraints = task_profile.get("constraints")
    constraint_map = constraints if isinstance(constraints, Mapping) else {}
    risk = str(constraint_map.get("risk") or "low").strip().lower()
    risk_floor = policy["risk_floor"].get(risk)
    floor_index: int | None = None
    if risk_floor:
        risk_floor = str(risk_floor)
        floor_index = level_order.index(risk_floor)
        if level_order.index(target) < floor_index:
            target = risk_floor
            reasons.append(f"risk_{risk}_floor_{risk_floor}")
        else:
            reasons.append(f"risk_{risk}_floor_satisfied")

    resource_policy = policy["resource_constraints"]
    cost = str(constraint_map.get("cost") or "").strip().lower()
    latency = str(constraint_map.get("latency") or "").strip().lower()
    cost_constrained = cost in resource_policy["cost_values"]
    latency_constrained = latency in resource_policy["latency_values"]
    if cost_constrained or latency_constrained:
        before = target
        shifted = _shift_thinking_level(
            target,
            -int(resource_policy["downshift_levels"]),
            level_order=level_order,
        )
        if floor_index is not None and level_order.index(shifted) < floor_index:
            shifted = str(level_order[floor_index])
        target = shifted
        constraint_kinds = "_and_".join(
            kind
            for kind, constrained in (
                (f"cost_{cost}", cost_constrained),
                (f"latency_{latency}", latency_constrained),
            )
            if constrained
        )
        reasons.append(
            f"resource_{constraint_kinds}_downshift"
            if target != before
            else f"resource_{constraint_kinds}_downshift_blocked"
        )

    if str(session_trace.get("intent") or "") == "redo":
        # Redo already changed the tier distribution in section 6.5. Keeping
        # this no-op reason makes it replay-auditable that no second bump was
        # applied here.
        reasons.append("redo_uses_session_adjusted_tier_only")
    return target, reasons, (str(risk_floor) if risk_floor else None)


def _ordered_neighbor_thinking_fallbacks(
    *,
    initial_level: str,
    eligible_levels: Sequence[str],
    level_order: Sequence[str],
    high_risk: bool,
) -> list[str]:
    """Walk to the nearest remaining level after each rejected attempt."""

    remaining = [level for level in eligible_levels if level != initial_level]
    ordered: list[str] = []
    current_level = initial_level
    while remaining:
        current_index = level_order.index(current_level)
        next_level = min(
            remaining,
            key=lambda level: (
                abs(level_order.index(level) - current_index),
                -level_order.index(level) if high_risk else level_order.index(level),
            ),
        )
        ordered.append(next_level)
        remaining.remove(next_level)
        current_level = next_level
    return ordered


def _resolve_model_thinking_level(
    model: RankedModel,
    *,
    role: str,
    requested_level: str,
    reasons: Sequence[str],
    risk_floor: str | None,
    policy: Mapping[str, Any],
) -> tuple[RankedModel, dict[str, Any], dict[str, Any] | None]:
    """Resolve unsupported unified targets deterministically and map native."""

    level_order = tuple(str(level) for level in policy["level_order"])
    supported = tuple(
        str(level)
        for level in model.registry_facts.get("thinking_levels") or []
        if str(level) in level_order
    )
    floor_index = level_order.index(risk_floor) if risk_floor else None
    eligible = [
        level
        for level in supported
        if floor_index is None or level_order.index(level) >= floor_index
    ]
    if not eligible:
        raise DynamicRankingError(
            "router_dynamic selected a model without an eligible unified "
            f"thinking level: {model.identity}"
        )

    requested_index = level_order.index(requested_level)
    high_risk = risk_floor is not None
    eligible.sort(
        key=lambda level: (
            abs(level_order.index(level) - requested_index),
            -level_order.index(level) if high_risk else level_order.index(level),
        )
    )
    effective_level = eligible[0]
    mapping = model.registry_facts.get("thinking_level_mapping")
    mapping_map = mapping if isinstance(mapping, Mapping) else {}
    provider_level = str(mapping_map.get(effective_level) or "").strip().lower()
    if not provider_level:
        raise DynamicRankingError(
            "router_dynamic selected a model with an incomplete "
            f"thinking_level_mapping: {model.identity}"
        )

    fallback_reason = ""
    effective_reasons = list(reasons)
    unsupported_fallback: dict[str, Any] | None = None
    if effective_level != requested_level:
        direction = "higher" if level_order.index(effective_level) > requested_index else "lower"
        fallback_reason = f"unsupported_requested_level_nearest_{direction}"
        effective_reasons.append(fallback_reason)
        unsupported_fallback = {
            "identity": model.identity,
            "model_id": model.model_id,
            "role": role,
            "requested_level": requested_level,
            "effective_level": effective_level,
            "provider_level": provider_level,
            "reason": fallback_reason,
        }

    fallback_candidates = _ordered_neighbor_thinking_fallbacks(
        initial_level=effective_level,
        eligible_levels=eligible,
        level_order=level_order,
        high_risk=high_risk,
    )
    provider_fallbacks = tuple(
        {
            "unified_level": level,
            "provider_level": str(mapping_map[level]),
            "reason": "provider_rejection_fallback",
        }
        for level in fallback_candidates
    )
    assigned = replace(
        model,
        thinking=provider_level,
        requested_thinking_level=requested_level,
        effective_thinking_level=effective_level,
        thinking_fallback_reason=fallback_reason,
        thinking_policy_version=str(policy["policy_version"]),
        thinking_fallbacks=provider_fallbacks,
    )
    detail = {
        "identity": model.identity,
        "model_id": model.model_id,
        "role": role,
        "requested_level": requested_level,
        "effective_level": effective_level,
        "provider_level": provider_level,
        "fallback_reason": fallback_reason,
        "reasons": effective_reasons,
        "provider_rejection_fallbacks": [
            copy.deepcopy(dict(fallback)) for fallback in provider_fallbacks
        ],
    }
    return assigned, detail, unsupported_fallback


def _assign_thinking_levels(
    *,
    proposers: Sequence[RankedModel],
    aggregator: RankedModel,
    effective_tier: int,
    task_profile: Mapping[str, Any],
    session_trace: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> tuple[
    tuple[RankedModel, ...],
    RankedModel,
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
]:
    proposer_target, proposer_reasons, risk_floor = _thinking_target_for_role(
        role="proposer",
        effective_tier=effective_tier,
        task_profile=task_profile,
        session_trace=session_trace,
        policy=policy,
    )
    aggregator_target, aggregator_reasons, aggregator_risk_floor = _thinking_target_for_role(
        role="aggregator",
        effective_tier=effective_tier,
        task_profile=task_profile,
        session_trace=session_trace,
        policy=policy,
    )

    assigned_proposers: list[RankedModel] = []
    proposer_details: list[dict[str, Any]] = []
    unsupported_fallbacks: list[dict[str, Any]] = []
    for proposer in proposers:
        assigned, detail, unsupported = _resolve_model_thinking_level(
            proposer,
            role="proposer",
            requested_level=proposer_target,
            reasons=proposer_reasons,
            risk_floor=risk_floor,
            policy=policy,
        )
        assigned_proposers.append(assigned)
        proposer_details.append(detail)
        if unsupported is not None:
            unsupported_fallbacks.append(unsupported)

    assigned_aggregator, aggregator_detail, unsupported = _resolve_model_thinking_level(
        aggregator,
        role="aggregator",
        requested_level=aggregator_target,
        reasons=aggregator_reasons,
        risk_floor=aggregator_risk_floor,
        policy=policy,
    )
    if unsupported is not None:
        unsupported_fallbacks.append(unsupported)

    assignment = {
        "proposers": {
            model.identity: model.effective_thinking_level for model in assigned_proposers
        },
        "aggregator": assigned_aggregator.effective_thinking_level,
        "thinking_policy_version": str(policy["policy_version"]),
    }
    details = {
        "effective_tier": effective_tier,
        "proposers": proposer_details,
        "aggregator": aggregator_detail,
    }
    return (
        tuple(assigned_proposers),
        assigned_aggregator,
        assignment,
        details,
        unsupported_fallbacks,
    )


def _proposer_global_ceiling(
    ranking_config: Mapping[str, Any],
) -> int:
    """Largest proposer set allowed by any configured routing envelope."""

    return max(
        *(_ranking_int(ranking_config, "proposer_count", "by_tier", tier, "max") for tier in TIERS),
        _ranking_int(ranking_config, "proposer_count", "high_risk", "max"),
    )


def _proposer_bounds(
    task_profile: Mapping[str, Any],
    user_profile: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
) -> tuple[int, int, list[str]]:
    tier = _effective_tier(task_profile, ranking_config)
    tier_key = str(tier)
    minimum = _ranking_int(ranking_config, "proposer_count", "by_tier", tier_key, "min")
    maximum = _ranking_int(ranking_config, "proposer_count", "by_tier", tier_key, "max")
    constraints = task_profile.get("constraints")
    constraints_map = constraints if isinstance(constraints, Mapping) else {}
    reasons = [f"tier_{tier}"]
    if str(constraints_map.get("risk") or "low") == "high":
        minimum = max(
            minimum,
            _ranking_int(ranking_config, "proposer_count", "high_risk", "min"),
        )
        maximum = max(
            maximum,
            _ranking_int(ranking_config, "proposer_count", "high_risk", "max"),
        )
        reasons.append("high_risk_cross_validation")
    preference = user_profile.get("preference")
    preference_map = preference if isinstance(preference, Mapping) else {}
    constrained = (
        str(constraints_map.get("cost"))
        in _ranking_string_set(ranking_config, "proposer_count", "constrained_cost_values")
        or str(constraints_map.get("latency"))
        in _ranking_string_set(ranking_config, "proposer_count", "constrained_latency_values")
        or str(preference_map.get("cost_sensitivity"))
        in _ranking_string_set(ranking_config, "proposer_count", "constrained_user_cost_values")
        or str(preference_map.get("quality_latency_tradeoff"))
        in _ranking_string_set(ranking_config, "proposer_count", "constrained_user_tradeoffs")
    )
    if constrained:
        maximum = min(
            maximum,
            _ranking_int(ranking_config, "proposer_count", "constrained_max"),
        )
        minimum = min(minimum, maximum)
        reasons.append("cost_or_latency_constrained")
    return minimum, maximum, reasons


def _capability_vector(model: RankedModel, ranking_config: Mapping[str, Any]) -> list[float]:
    return [
        _strength(model, "capability_dist_prior", capability, ranking_config)
        for capability in CAPABILITIES
    ]


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return _clamp(numerator / (left_norm * right_norm))


def _similarity(
    left: RankedModel,
    right: RankedModel,
    ranking_config: Mapping[str, Any],
) -> float:
    capability_similarity = _cosine(
        _capability_vector(left, ranking_config),
        _capability_vector(right, ranking_config),
    )
    if left.family == right.family:
        family_similarity = _ranking_number(
            ranking_config, "rerank", "similarity", "same_family_score"
        )
    elif left.vendor == right.vendor:
        family_similarity = _ranking_number(
            ranking_config, "rerank", "similarity", "same_vendor_score"
        )
    else:
        family_similarity = _ranking_number(
            ranking_config, "rerank", "similarity", "unrelated_score"
        )
    return _clamp(
        _ranking_number(ranking_config, "rerank", "similarity", "capability_weight")
        * capability_similarity
        + _ranking_number(ranking_config, "rerank", "similarity", "lineage_weight")
        * family_similarity
    )


def _coverage_gain(
    candidate: RankedModel,
    selected: Sequence[RankedModel],
    task_profile: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
) -> float:
    raw = task_profile.get("capability_dist")
    capability_dist = raw if isinstance(raw, Mapping) else {}
    gain = 0.0
    for capability, weight in capability_dist.items():
        candidate_strength = _strength(
            candidate, "capability_dist_prior", str(capability), ranking_config
        )
        selected_max = max(
            (
                _strength(
                    model,
                    "capability_dist_prior",
                    str(capability),
                    ranking_config,
                )
                for model in selected
            ),
            default=0.0,
        )
        gain += _as_float(weight) * max(0.0, candidate_strength - selected_max)
    return gain


def _error_vector(model: RankedModel, ranking_config: Mapping[str, Any]) -> list[float]:
    raw = model.online_profile.get("error_rates")
    values = raw if isinstance(raw, Mapping) else {}
    raw_dimensions = _ranking_value(ranking_config, "rerank", "error_dimensions")
    dimensions = [str(name) for name in raw_dimensions]
    return [_clamp(_as_float(values.get(name), 0.0)) for name in dimensions]


def _error_complementarity(
    candidate: RankedModel,
    selected: Sequence[RankedModel],
    ranking_config: Mapping[str, Any],
) -> float:
    if not selected:
        return 0.0
    vector = _error_vector(candidate, ranking_config)
    if not any(vector):
        return 0.0
    similarities = [
        _cosine(vector, other_vector)
        for model in selected
        if any(other_vector := _error_vector(model, ranking_config))
    ]
    return 1.0 - max(similarities, default=1.0)


def _aggregator_filter_rows(
    models: Sequence[RankedModel],
    *,
    proposer_count: int,
    task_profile: Mapping[str, Any],
    user_profile: Mapping[str, Any] | None,
    request_context: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
    thinking_policy: Mapping[str, Any] | None = None,
) -> tuple[list[RankedModel], list[dict[str, Any]]]:
    eligible: list[RankedModel] = []
    filters: list[dict[str, Any]] = []
    effective_user_profile = user_profile if user_profile is not None else {}
    for model in models:
        reasons, context_need = _hard_filter_reasons(
            model,
            role="aggregator",
            task_profile=task_profile,
            user_profile=effective_user_profile,
            request_context=request_context,
            proposer_count=proposer_count,
            ranking_config=ranking_config,
            thinking_policy=thinking_policy,
        )
        filters.append(
            {
                "identity": model.identity,
                "model": model.model_id,
                "role": "aggregator",
                "eligible": not reasons,
                "reasons": reasons,
                "context_need_tokens": context_need,
            }
        )
        if not reasons:
            eligible.append(model)
    return eligible, filters


def _aggregator_rows(
    models: Sequence[RankedModel],
    *,
    proposers: Sequence[RankedModel],
    task_profile: Mapping[str, Any],
    user_profile: Mapping[str, Any] | None,
    request_context: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
    thinking_policy: Mapping[str, Any] | None = None,
    cache_affinity_policy: Mapping[str, Any] | None = None,
    cache_affinity_inputs: Mapping[
        str,
        Mapping[str, CacheAffinityEvidenceInput],
    ]
    | None = None,
    cache_analyzer_schema_valid: bool = False,
    cache_session_intent: str = "new_task",
    frozen_cache_adjustments: Mapping[
        str,
        Mapping[str, CacheAffinityScoreAdjustment],
    ]
    | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scored: list[dict[str, Any]] = []
    eligible, filters = _aggregator_filter_rows(
        models,
        proposer_count=len(proposers),
        task_profile=task_profile,
        user_profile=user_profile,
        request_context=request_context,
        ranking_config=ranking_config,
        thinking_policy=thinking_policy,
    )
    cost_weight, latency_weight = _cost_latency_weights(task_profile, user_profile, ranking_config)
    task_weight = _ranking_number(ranking_config, "aggregator", "task_match_weight")
    role_weight = _ranking_number(ranking_config, "aggregator", "role_fit_weight")
    same_model_penalty = _ranking_number(ranking_config, "aggregator", "same_model_penalty")
    related_penalty = _ranking_number(ranking_config, "aggregator", "same_family_or_vendor_penalty")
    price_reference = _ranking_number(
        ranking_config, "normalization", "price_reference_usd_per_million"
    )
    latency_reference = _ranking_number(ranking_config, "normalization", "latency_reference_ms")
    context_need_by_identity = {row["identity"]: row["context_need_tokens"] for row in filters}
    for model in eligible:
        context_need = context_need_by_identity[model.identity]
        task_match = _task_match(model, task_profile, ranking_config, role=None)
        role_fit = _role_fit(model, "aggregator", ranking_config)
        quality_before_reliability = task_weight * task_match + role_weight * role_fit
        reliability = _role_reliability_score(model, "aggregator", ranking_config)
        quality = quality_before_reliability - _as_float(reliability["penalty"])
        session_score = _session_score(model, task_profile, request_context, ranking_config)
        self_overlap = any(model.identity == proposer.identity for proposer in proposers)
        family_overlap = any(model.family == proposer.family for proposer in proposers)
        vendor_overlap = any(model.vendor == proposer.vendor for proposer in proposers)
        related_overlap = family_overlap or vendor_overlap
        bias = same_model_penalty * int(self_overlap) + related_penalty * int(related_overlap)
        cost = _clamp(_model_price(model, ranking_config) / price_reference)
        latency = _clamp(
            _as_float(
                model.registry_facts.get("latency_p95_ms", model.registry_facts.get("latency_p95")),
                latency_reference,
            )
            / latency_reference
        )
        score = quality + session_score - bias - cost_weight * cost - latency_weight * latency
        row: dict[str, Any] = {
            "model": model,
            "score": score,
            "quality": quality,
            "quality_before_reliability": quality_before_reliability,
            "reliability": reliability,
            "task_match": task_match,
            "role_fit": role_fit,
            "session_score": session_score,
            "bias": bias,
            "self_overlap": self_overlap,
            "family_overlap": family_overlap,
            "vendor_overlap": vendor_overlap,
            "cost": cost,
            "latency": latency,
            "cost_weight": cost_weight,
            "latency_weight": latency_weight,
            "context_need_tokens": context_need,
        }
        if cache_affinity_policy is not None:
            adjustment = _cache_affinity_adjustment_for_model(
                policy=cache_affinity_policy,
                cache_affinity_inputs=cache_affinity_inputs,
                role="aggregator",
                topology="multiple",
                model=model,
                task_profile=task_profile,
                analyzer_schema_valid=cache_analyzer_schema_valid,
                session_intent=cache_session_intent,
                request_context=request_context,
                proposer_count=len(proposers),
                cost_weight=cost_weight,
                ranking_config=ranking_config,
                frozen_adjustments=frozen_cache_adjustments,
            )
            if adjustment is not None:
                row["cache_affinity_adjustment"] = adjustment
                row["score"] = score + adjustment.score_adjustment
        scored.append(row)
    scored.sort(
        key=lambda row: (
            -_as_float(row["score"]),
            -_as_float(row["quality"]),
            row["model"].identity,
        )
    )
    return scored, filters


def _aggregator_score_trace(
    row: Mapping[str, Any], ranking_config: Mapping[str, Any]
) -> dict[str, Any]:
    model = row["model"]
    decimal_places = _ranking_int(ranking_config, "trace", "score_decimal_places")
    trace = {
        "identity": model.identity,
        "model": model.model_id,
        "provider": model.provider,
        "Score_agg": round(_as_float(row.get("score")), decimal_places),
        "S_agg_qual": round(_as_float(row.get("quality")), decimal_places),
        "S_match": round(_as_float(row.get("task_match")), decimal_places),
        "role_fit": round(_as_float(row.get("role_fit")), decimal_places),
        "S_session": round(_as_float(row.get("session_score")), decimal_places),
        "bias_penalty": round(_as_float(row.get("bias")), decimal_places),
        "cost": round(_as_float(row.get("cost")), decimal_places),
        "latency": round(_as_float(row.get("latency")), decimal_places),
        "cost_weight": round(_as_float(row.get("cost_weight")), decimal_places),
        "latency_weight": round(_as_float(row.get("latency_weight")), decimal_places),
        "context_need_tokens": _as_int(row.get("context_need_tokens"), 0),
        "self_overlap": bool(row.get("self_overlap")),
        "family_overlap": bool(row.get("family_overlap")),
        "vendor_overlap": bool(row.get("vendor_overlap")),
    }
    cache_adjustment = row.get("cache_affinity_adjustment")
    if isinstance(cache_adjustment, CacheAffinityScoreAdjustment):
        trace["cache_affinity"] = cache_adjustment.trace()
    if "role_reliability" in ranking_config:
        trace.update(
            {
                "S_agg_qual_before_reliability": round(
                    _as_float(row.get("quality_before_reliability")),
                    decimal_places,
                ),
                "role_reliability": _role_reliability_trace(
                    row.get("reliability"),
                    ranking_config,
                ),
            }
        )
    return trace


def _single_model_context_need(
    *,
    task_profile: Mapping[str, Any],
    request_context: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
    direct_output_tokens: int,
) -> int:
    raw_budget = request_context.get("routing_budget")
    budget = raw_budget if isinstance(raw_budget, Mapping) else {}
    if (
        isinstance(direct_output_tokens, bool)
        or not isinstance(direct_output_tokens, int)
        or direct_output_tokens <= 0
    ):
        raise DynamicRankingError(
            "router_dynamic single-model request context requires a positive "
            "direct_output_tokens budget"
        )
    constraints = task_profile.get("constraints")
    constraint_map = constraints if isinstance(constraints, Mapping) else {}
    default_bucket = _ranking_string(ranking_config, "context", "default_bucket")
    bucket = str(constraint_map.get("context") or default_bucket)
    bucket_min_tokens = _context_bucket_min_tokens(ranking_config)
    input_tokens = max(
        max(0, _as_int(budget.get("estimated_input_tokens"), 0)),
        bucket_min_tokens.get(bucket, bucket_min_tokens[default_bucket]),
    )
    minimum_tokens = _ranking_int(ranking_config, "context", "output_budget", "minimum_tokens")
    return (
        input_tokens
        + max(0, _as_int(budget.get("tool_log_tokens"), 0))
        + max(minimum_tokens, direct_output_tokens)
    )


def _single_model_direct_output_tokens(
    model: RankedModel,
    *,
    request_context: Mapping[str, Any],
) -> tuple[int, str]:
    raw_budget = request_context.get("routing_budget")
    budget = raw_budget if isinstance(raw_budget, Mapping) else {}
    request_output_tokens = budget.get("direct_output_tokens")
    if (
        isinstance(request_output_tokens, bool)
        or not isinstance(request_output_tokens, int)
        or request_output_tokens <= 0
    ):
        raise DynamicRankingError(
            "router_dynamic single-model request context requires a positive "
            "direct_output_tokens budget"
        )

    runtime_output_tokens = model.registry_facts.get("runtime_direct_output_tokens")
    if runtime_output_tokens is None:
        # Compatibility for archived/external snapshots that predate runtime
        # deployment enrichment. The request scalar is the audited safe upper
        # bound used before a candidate-specific deployment is selected.
        return request_output_tokens, "request_context_fallback"
    if (
        isinstance(runtime_output_tokens, bool)
        or not isinstance(runtime_output_tokens, int)
        or runtime_output_tokens <= 0
    ):
        raise DynamicRankingError(
            "router_dynamic model registry "
            f"{model.identity} has invalid runtime_direct_output_tokens"
        )
    return runtime_output_tokens, "runtime_registry_fact"


def _single_model_filter_reasons(
    model: RankedModel,
    *,
    task_profile: Mapping[str, Any],
    user_profile: Mapping[str, Any],
    request_context: Mapping[str, Any],
    ranking_config: Mapping[str, Any],
    thinking_policy: Mapping[str, Any] | None,
    requires_tools: bool,
) -> tuple[list[str], int, int, str]:
    reasons, _ = _hard_filter_reasons(
        model,
        role="proposer",
        task_profile=task_profile,
        user_profile=user_profile,
        request_context=request_context,
        proposer_count=1,
        ranking_config=ranking_config,
        thinking_policy=thinking_policy,
    )
    reasons = [
        reason
        for reason in reasons
        if reason
        not in {
            "context_exceeded",
            "required_parameter_tools_unsupported",
            "status_unavailable",
        }
    ]
    if str(model.registry_facts.get("status") or "").strip().casefold() != "enabled":
        reasons.append("status_not_enabled")
    if requires_tools and model.registry_facts.get("supports_tools") is not True:
        reasons.append("required_parameter_tools_unsupported")
    direct_output_tokens, direct_output_tokens_source = _single_model_direct_output_tokens(
        model,
        request_context=request_context,
    )
    context_need = _single_model_context_need(
        task_profile=task_profile,
        request_context=request_context,
        ranking_config=ranking_config,
        direct_output_tokens=direct_output_tokens,
    )
    if _as_int(model.registry_facts.get("context_window"), 0) < context_need:
        reasons.append("context_exceeded")
    return (
        list(dict.fromkeys(reasons)),
        context_need,
        direct_output_tokens,
        direct_output_tokens_source,
    )


def rank_single_model(
    *,
    task_analysis: TaskAnalysisResult,
    user_profile: Mapping[str, Any] | None,
    request_context: Mapping[str, Any],
    registry_snapshot: Mapping[str, Any],
    routed_tier: str,
    routing_confidence: float,
    requires_tools: bool,
    ranking_config: Mapping[str, Any] | None = None,
    decision_id: str = "",
    ranking_thinking_assignment_enabled: bool = False,
    cache_continuity_available: bool = False,
    cache_affinity_inputs: Mapping[
        str,
        Mapping[str, CacheAffinityEvidenceInput],
    ]
    | None = None,
    _cache_affinity_unavailable_reasons: Sequence[Mapping[str, Any]] | None = None,
    _cache_affinity_replay_adjustments: Mapping[
        str,
        Mapping[str, CacheAffinityScoreAdjustment],
    ]
    | None = None,
) -> SingleModelRankingDecision:
    """Select one enabled direct-call model by proposer base score."""

    if not isinstance(requires_tools, bool):
        raise DynamicRankingError("router_dynamic single-model requires_tools must be a boolean")
    if not isinstance(ranking_thinking_assignment_enabled, bool):
        raise DynamicRankingError(
            "router_dynamic ranking_thinking_assignment_enabled must be a boolean"
        )
    if not isinstance(registry_snapshot, Mapping):
        raise DynamicRankingError("router_dynamic single-model registry snapshot must be an object")

    effective_ranking_config = _prepare_effective_ranking_config(
        ranking_config,
        thinking_assignment_enabled=ranking_thinking_assignment_enabled,
    )
    configured_calibration_policy = _single_route_calibration_policy(
        effective_ranking_config
    )
    cache_affinity_policy = _cache_affinity_policy_for_topology(
        effective_ranking_config,
        "single",
    )
    if _cache_affinity_replay_adjustments is not None and cache_affinity_policy is None:
        raise DynamicRankingError(
            "router_dynamic frozen cache adjustments require an enabled single policy"
        )
    model_registry_snapshot = {
        "schema_version": copy.deepcopy(registry_snapshot.get("schema_version")),
        "snapshot_version": copy.deepcopy(registry_snapshot.get("snapshot_version")),
        "models": copy.deepcopy(registry_snapshot.get("models")),
    }
    if not ranking_thinking_assignment_enabled:
        model_registry_snapshot = _legacy_registry_snapshot_projection(model_registry_snapshot)
    if (
        ranking_thinking_assignment_enabled
        and effective_ranking_config.get("schema_version") != RANKING_CONFIG_SCHEMA_VERSION
    ):
        raise DynamicRankingError(
            f"router_dynamic thinking_assignment requires {RANKING_CONFIG_SCHEMA_VERSION}"
        )
    if (
        ranking_thinking_assignment_enabled
        and _ranking_number(
            effective_ranking_config,
            "proposer_count",
            "effective_tier_rounding_offset",
        )
        != 0.5
    ):
        raise DynamicRankingError(
            "router_dynamic thinking-policy-v1 requires "
            "proposer_count.effective_tier_rounding_offset to be 0.5"
        )
    thinking_policy = (
        _thinking_assignment_policy(
            effective_ranking_config,
            allow_legacy_external_switch=True,
        )
        if ranking_thinking_assignment_enabled
        else None
    )
    rows = model_registry_snapshot.get("models")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise DynamicRankingError(
            "router_dynamic single-model registry snapshot contains no models"
        )
    if any(not isinstance(row, Mapping) for row in rows):
        raise DynamicRankingError(
            "router_dynamic single-model registry snapshot contains a malformed model row"
        )
    models = [
        _normalize_model(
            row,
            effective_ranking_config,
            thinking_policy=thinking_policy,
        )
        for row in rows
    ]
    if not models:
        raise DynamicRankingError(
            "router_dynamic single-model registry snapshot is empty",
            reason="no_eligible_single_model",
        )
    model_identities = [model.identity.casefold() for model in models]
    if len(set(model_identities)) != len(model_identities):
        raise DynamicRankingError(
            "router_dynamic single-model registry snapshot contains duplicate model identities"
        )
    cache_affinity_unavailable_reasons = _cache_affinity_unavailable_reason_trace(
        _cache_affinity_unavailable_reasons,
        policy=cache_affinity_policy,
        topology="single",
        candidate_identities=[model.identity for model in models],
    )

    user_profile_enabled = user_profile is not None
    effective_user_profile = user_profile if user_profile is not None else {}
    task_profile, session_trace = _apply_session_adjustment(
        task_analysis.profile,
        request_context,
        effective_ranking_config,
        cache_continuity_available=(
            cache_continuity_available and cache_affinity_policy is not None
        ),
    )
    effective_tier = _effective_tier(task_profile, effective_ranking_config)

    proposer_filters: list[dict[str, Any]] = []
    eligible: list[RankedModel] = []
    for model in models:
        (
            reasons,
            context_need,
            direct_output_tokens,
            direct_output_tokens_source,
        ) = _single_model_filter_reasons(
            model,
            task_profile=task_profile,
            user_profile=effective_user_profile,
            request_context=request_context,
            ranking_config=effective_ranking_config,
            thinking_policy=thinking_policy,
            requires_tools=requires_tools,
        )
        proposer_filters.append(
            {
                "identity": model.identity,
                "model": model.model_id,
                "role": "proposer",
                "eligible": not reasons,
                "reasons": reasons,
                "context_need_tokens": context_need,
                "direct_output_tokens": direct_output_tokens,
                "direct_output_tokens_source": direct_output_tokens_source,
            }
        )
        if not reasons:
            eligible.append(model)
    if not eligible:
        reason_counts: dict[str, int] = {}
        for filter_row in proposer_filters:
            for reason in filter_row["reasons"]:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        log.warning(
            "llm_ensemble.router_dynamic.no_eligible_single_model",
            decision_id=decision_id,
            registry_snapshot_version=model_registry_snapshot.get("snapshot_version"),
            filter_reason_counts=reason_counts,
        )
        raise DynamicRankingError(
            "router_dynamic has no enabled direct-call model after hard filtering",
            reason="no_eligible_single_model",
        )

    calibration_policy = (
        configured_calibration_policy
        if _validate_single_route_calibration_coverage(
            configured_calibration_policy,
            eligible,
        )
        else None
    )
    single_ranking_version = (
        CALIBRATED_SINGLE_MODEL_RANKING_VERSION
        if calibration_policy is not None
        else SINGLE_MODEL_RANKING_VERSION
    )
    score_rows = [
        _base_score_row(
            model,
            task_profile,
            user_profile,
            request_context,
            effective_ranking_config,
            single_route=True,
            single_route_calibration_policy=calibration_policy,
        )
        for model in eligible
    ]
    if cache_affinity_policy is not None:
        for row in score_rows:
            adjustment = _cache_affinity_adjustment_for_model(
                policy=cache_affinity_policy,
                cache_affinity_inputs=cache_affinity_inputs,
                role="single",
                topology="single",
                model=row["model"],
                task_profile=task_profile,
                analyzer_schema_valid=task_analysis.schema_valid,
                session_intent=str(session_trace["intent"]),
                request_context=request_context,
                proposer_count=1,
                cost_weight=_as_float(row["cost_weight"]),
                ranking_config=effective_ranking_config,
                frozen_adjustments=_cache_affinity_replay_adjustments,
            )
            if adjustment is not None:
                row["cache_affinity_adjustment"] = adjustment
                row["final_score"] = _as_float(row["base"]) + adjustment.score_adjustment
    score_rows.sort(
        key=lambda row: (
            -_as_float(row.get("final_score"), row["base"]),
            -_as_float(row["quality"]),
            row["model"].identity,
        )
    )
    provisional_row = score_rows[0]
    selected_row = provisional_row
    quality_guard_decision: dict[str, Any] | None = None
    if (
        calibration_policy is not None
        and calibration_policy.get("quality_guard_enabled") is True
    ):
        quality_best_row = min(
            score_rows,
            key=lambda row: (
                -_as_float(row["quality_clean"]),
                -_as_float(row.get("final_score"), row["base"]),
                row["model"].identity,
            ),
        )
        quality_drop = _as_float(
            quality_best_row["quality_clean"]
        ) - _as_float(provisional_row["quality_clean"])
        maximum_drop = _as_float(calibration_policy.get("max_quality_drop"))
        guard_applied = quality_drop > maximum_drop
        if guard_applied:
            selected_row = quality_best_row
        quality_guard_decision = {
            "enabled": True,
            "max_quality_drop": maximum_drop,
            "provisional_model": provisional_row["model"].identity,
            "quality_best_model": quality_best_row["model"].identity,
            "selected_model": selected_row["model"].identity,
            "provisional_quality_clean": _as_float(
                provisional_row["quality_clean"]
            ),
            "quality_best_clean": _as_float(
                quality_best_row["quality_clean"]
            ),
            "quality_drop": quality_drop,
            "applied": guard_applied,
            "quality_best_tie_breakers": [
                "S_qual_clean_desc",
                (
                    "S_final_desc"
                    if cache_affinity_policy is not None
                    else "S_base_desc"
                ),
                "identity_asc",
            ],
        }
    selected = selected_row["model"]
    session_nonzero_epsilon = _ranking_number(
        effective_ranking_config, "trace", "session_nonzero_epsilon"
    )
    session_adjusted_ids = sorted(
        row["model"].identity
        for row in score_rows
        if abs(_as_float(row.get("session_score"))) > session_nonzero_epsilon
    )
    session_trace["sticky_applied"] = session_trace["intent"] == "continue" and bool(
        session_adjusted_ids
    )
    session_trace["adjusted_model_ids"] = session_adjusted_ids

    assigned_model = selected
    thinking_assignment: dict[str, Any] = {}
    thinking_assignment_details: dict[str, Any] = {}
    thinking_assignment_reasons: dict[str, Any] = {}
    thinking_unsupported_fallbacks: list[dict[str, Any]] = []
    if thinking_policy is not None:
        target, target_reasons, risk_floor = _thinking_target_for_role(
            role="proposer",
            effective_tier=effective_tier,
            task_profile=task_profile,
            session_trace=session_trace,
            policy=thinking_policy,
        )
        assigned_model, detail, unsupported = _resolve_model_thinking_level(
            selected,
            role="proposer",
            requested_level=target,
            reasons=target_reasons,
            risk_floor=risk_floor,
            policy=thinking_policy,
        )
        thinking_assignment = {
            "proposers": {assigned_model.identity: assigned_model.effective_thinking_level},
            "thinking_policy_version": str(thinking_policy["policy_version"]),
        }
        thinking_assignment_details = {
            "effective_tier": effective_tier,
            "proposers": [detail],
        }
        thinking_assignment_reasons = {"proposers": {detail["identity"]: list(detail["reasons"])}}
        if unsupported is not None:
            thinking_unsupported_fallbacks.append(unsupported)

    reason_counts = {}
    for filter_row in proposer_filters:
        for reason in filter_row["reasons"]:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    ranking_config_hash = _canonical_hash(effective_ranking_config)
    registry_snapshot_hash = _canonical_hash(model_registry_snapshot)
    profile_decimal_places = _ranking_int(
        effective_ranking_config, "trace", "profile_decimal_places"
    )
    router_tier_mapping = _router_tier_mapping(effective_ranking_config)
    router_tier_by_effective_tier = {
        tier: router_tier for router_tier, tier in router_tier_mapping.items()
    }
    trace_registry_snapshot = copy.deepcopy(model_registry_snapshot)
    trace_request_context = copy.deepcopy(dict(request_context))
    trace_request_context["snapshot_hash"] = _request_context_hash(trace_request_context)
    _assert_public_ranking_trace_payload(
        trace_registry_snapshot,
        label="registry_snapshot",
    )
    _assert_public_ranking_trace_payload(
        trace_request_context,
        label="request_context",
    )
    base_ranking_version = (
        RANKING_VERSION if ranking_thinking_assignment_enabled else LEGACY_RANKING_VERSION
    )
    trace = {
        "strategy": "router_dynamic",
        "execution_mode": "router_single",
        "decision_id": decision_id,
        "ranking_version": single_ranking_version,
        "base_ranking_version": base_ranking_version,
        "ranking_config_schema_version": str(effective_ranking_config["schema_version"]),
        "ranking_config_version": str(effective_ranking_config["config_version"]),
        "ranking_config_hash": ranking_config_hash,
        "task_profile_schema_version": TASK_PROFILE_SCHEMA_VERSION,
        "registry_snapshot_version": str(model_registry_snapshot.get("snapshot_version") or ""),
        "registry_snapshot_hash": registry_snapshot_hash,
        "registry_snapshot": trace_registry_snapshot,
        "routed_tier": _router_tier(routed_tier, effective_ranking_config),
        "routing_confidence": round(_clamp(routing_confidence), profile_decimal_places),
        "effective_tier": effective_tier,
        "effective_router_tier": router_tier_by_effective_tier[effective_tier],
        "task_analyzer": task_analysis.trace(effective_ranking_config),
        "task_profile": copy.deepcopy(task_profile),
        "task_profile_hash": _canonical_hash(task_profile),
        "task_profile_pre_escalation": session_trace.pop("task_profile_pre_escalation"),
        "task_profile_post_escalation": session_trace.pop("task_profile_post_escalation"),
        "session": session_trace,
        "user_profile_enabled": user_profile_enabled,
        "user_profile_version": str(effective_user_profile.get("profile_version") or ""),
        "user_profile_source": str(effective_user_profile.get("profile_source") or ""),
        "request_context_hash": trace_request_context["snapshot_hash"],
        "request_context": trace_request_context,
        "candidate_pool_size": len(models),
        "candidate_pool": [
            model.trace(include_thinking_contract=ranking_thinking_assignment_enabled)
            for model in models
        ],
        "hard_filter": {
            "proposer_results": proposer_filters,
            "eligible_proposer_ids": [model.identity for model in eligible],
            "filter_reason_counts": reason_counts,
        },
        "model_scores": [_score_trace(row, effective_ranking_config) for row in score_rows],
        "selection_policy": "base_score_top1",
        "selection_tie_breakers": [
            "S_base_desc",
            "S_qual_desc",
            "identity_asc",
        ],
        "selected_model": assigned_model.identity,
        "selected_P": [assigned_model.identity],
        "proposer_count": 1,
        "stop_reason": "single_model_top1_selected",
    }
    if configured_calibration_policy is not None:
        configured_scope = configured_calibration_policy.get(
            "activation_model_identities"
        )
        trace["single_route_calibration_scope"] = {
            "configured": True,
            "applied": calibration_policy is not None,
            "activation_model_identities": (
                list(configured_scope)
                if isinstance(configured_scope, Sequence)
                and not isinstance(configured_scope, (str, bytes))
                else []
            ),
        }
    if calibration_policy is not None:
        trace["selection_policy"] = "calibrated_base_score_top1"
        trace["quality_guard"] = (
            {
                "enabled": False,
                "max_quality_drop": round(
                    _as_float(calibration_policy.get("max_quality_drop")),
                    profile_decimal_places,
                ),
            }
            if quality_guard_decision is None
            else {
                **quality_guard_decision,
                "max_quality_drop": round(
                    _as_float(
                        quality_guard_decision.get("max_quality_drop")
                    ),
                    profile_decimal_places,
                ),
                "provisional_quality_clean": round(
                    _as_float(
                        quality_guard_decision.get(
                            "provisional_quality_clean"
                        )
                    ),
                    profile_decimal_places,
                ),
                "quality_best_clean": round(
                    _as_float(
                        quality_guard_decision.get("quality_best_clean")
                    ),
                    profile_decimal_places,
                ),
                "quality_drop": round(
                    _as_float(quality_guard_decision.get("quality_drop")),
                    profile_decimal_places,
                ),
            }
        )
        if quality_guard_decision is not None:
            trace["selection_policy"] = "calibrated_base_score_with_quality_guard"
            if quality_guard_decision["applied"] is True:
                trace["stop_reason"] = "single_model_quality_guard_selected"
    if (
        cache_affinity_policy is not None
        or configured_calibration_policy is not None
    ):
        trace["ranking_parameters"] = _detached_ranking_config(effective_ranking_config)
        trace["requires_tools"] = requires_tools
        trace["ranking_thinking_assignment_enabled"] = ranking_thinking_assignment_enabled
        if cache_affinity_policy is not None or calibration_policy is not None:
            trace["cache_continuity_available"] = bool(cache_continuity_available)
            trace["cache_affinity_inputs"] = (
                [
                    adjustment.trace()
                    for row in score_rows
                    if isinstance(
                        adjustment := row.get("cache_affinity_adjustment"),
                        CacheAffinityScoreAdjustment,
                    )
                ]
                if cache_affinity_policy is not None
                else []
            )
        if cache_affinity_policy is not None:
            trace["selection_policy"] = "cache_adjusted_" + str(trace["selection_policy"])
            trace["selection_tie_breakers"][0] = "S_final_desc"
            if cache_affinity_unavailable_reasons:
                trace["cache_affinity_unavailable_reasons"] = cache_affinity_unavailable_reasons
    if thinking_policy is not None:
        trace.update(
            {
                "ranking_thinking_assignment_enabled": True,
                "thinking_physical_evidence_schema": (THINKING_PHYSICAL_EVIDENCE_SCHEMA),
                "thinking_policy_version": str(thinking_policy["policy_version"]),
                "thinking_assignment": copy.deepcopy(thinking_assignment),
                "thinking_assignment_details": copy.deepcopy(thinking_assignment_details),
                "assignment_reasons": copy.deepcopy(thinking_assignment_reasons),
                "unsupported_level_fallbacks": copy.deepcopy(thinking_unsupported_fallbacks),
                "policy_versions": {
                    "ranking": base_ranking_version,
                    "thinking": str(thinking_policy["policy_version"]),
                },
            }
        )
    log.info(
        "llm_ensemble.router_dynamic.single_model_selection_recorded",
        decision_id=decision_id,
        selected_model=assigned_model.identity,
        ranking_version=single_ranking_version,
        candidate_pool_size=len(models),
        eligible_model_count=len(eligible),
        requires_tools=requires_tools,
    )
    return SingleModelRankingDecision(
        model=assigned_model,
        effective_tier=effective_tier,
        trace=trace,
        thinking_assignment=copy.deepcopy(thinking_assignment),
        thinking_assignment_details=copy.deepcopy(thinking_assignment_details),
    )


def _selection_roster_counts(
    ranking_config: Mapping[str, Any],
    *,
    legacy_proposer_backup_count: int | None = None,
) -> tuple[int, int]:
    """Return config-owned aggregator/proposer roster sizes.

    Only replay of the two archived pre-roster config versions may supply the
    historical proposer count that used to live in gateway configuration.
    Every current runtime config must carry both counts explicitly.
    """

    proposer_count = _ranking_mapping(ranking_config, "proposer_count")
    aggregator = _ranking_mapping(ranking_config, "aggregator")
    has_roster_policy = "backup_count" in proposer_count and "candidate_count" in aggregator
    if has_roster_policy:
        if legacy_proposer_backup_count is not None:
            raise DynamicRankingError(
                "router_dynamic legacy proposer backup count cannot override "
                "the ranking config roster policy"
            )
        return (
            _ranking_int(ranking_config, "aggregator", "candidate_count"),
            _ranking_int(ranking_config, "proposer_count", "backup_count"),
        )
    if not _is_pre_roster_ranking_config_version(ranking_config.get("config_version")):
        raise DynamicRankingError("router_dynamic ranking config lacks the selection roster policy")
    if (
        legacy_proposer_backup_count is None
        or isinstance(legacy_proposer_backup_count, bool)
        or not isinstance(legacy_proposer_backup_count, int)
        or legacy_proposer_backup_count < 0
    ):
        raise DynamicRankingError(
            "router_dynamic pre-roster replay requires a non-negative legacy proposer backup count"
        )
    return 3, legacy_proposer_backup_count


def rank_models(
    *,
    task_analysis: TaskAnalysisResult,
    user_profile: Mapping[str, Any] | None,
    request_context: Mapping[str, Any],
    registry_snapshot: Mapping[str, Any],
    routed_tier: str,
    routing_confidence: float,
    ranking_config: Mapping[str, Any] | None = None,
    decision_id: str = "",
    ranking_thinking_assignment_enabled: bool = False,
    legacy_proposer_backup_count: int | None = None,
    proposer_recovery_max_additional_calls: int = 3,
    proposer_max_tokens_cap: int = 65_536,
    proposer_visible_answer_reserve_tokens: int = 4_096,
    proposer_recovery_quorum: int | None = None,
    cache_continuity_available: bool = False,
    cache_affinity_inputs: Mapping[
        str,
        Mapping[str, CacheAffinityEvidenceInput],
    ]
    | None = None,
    _cache_affinity_unavailable_reasons: Sequence[Mapping[str, Any]] | None = None,
    _cache_affinity_replay_adjustments: Mapping[
        str,
        Mapping[str, CacheAffinityScoreAdjustment],
    ]
    | None = None,
    _emit_logs: bool = True,
    _stage_observability_out: dict[str, Any] | None = None,
) -> RankingDecision:
    """Select ``(P, A)`` using the Step2 chapter-6 ranking pipeline."""

    if not isinstance(_emit_logs, bool):
        raise DynamicRankingError("router_dynamic _emit_logs must be a boolean")
    if _stage_observability_out is not None and type(_stage_observability_out) is not dict:
        raise DynamicRankingError("router_dynamic stage observability output must be a dict")
    if _stage_observability_out is not None:
        _stage_observability_out.pop("hard_filter_ms", None)
        _stage_observability_out.pop("score_ms", None)

    def emit_info(event: str, **fields: Any) -> None:
        if _emit_logs:
            log.info(event, **fields)

    if not isinstance(ranking_thinking_assignment_enabled, bool):
        raise DynamicRankingError(
            "router_dynamic ranking_thinking_assignment_enabled must be a boolean"
        )
    if (
        isinstance(proposer_recovery_max_additional_calls, bool)
        or not isinstance(proposer_recovery_max_additional_calls, int)
        or not 0 <= proposer_recovery_max_additional_calls <= 3
    ):
        raise DynamicRankingError(
            "router_dynamic proposer recovery max additional calls must be "
            "an integer between zero and three"
        )
    if (
        isinstance(proposer_max_tokens_cap, bool)
        or not isinstance(proposer_max_tokens_cap, int)
        or proposer_max_tokens_cap < 2
        or isinstance(proposer_visible_answer_reserve_tokens, bool)
        or not isinstance(proposer_visible_answer_reserve_tokens, int)
        or not 1 <= proposer_visible_answer_reserve_tokens < proposer_max_tokens_cap
    ):
        raise DynamicRankingError(
            "router_dynamic proposer visible reserve must be positive and "
            "smaller than proposer max tokens cap"
        )
    if proposer_recovery_quorum is not None and (
        isinstance(proposer_recovery_quorum, bool)
        or not isinstance(proposer_recovery_quorum, int)
        or proposer_recovery_quorum <= 0
    ):
        raise DynamicRankingError(
            "router_dynamic proposer recovery quorum must be a positive integer"
        )
    effective_ranking_config = _prepare_effective_ranking_config(
        ranking_config,
        thinking_assignment_enabled=ranking_thinking_assignment_enabled,
    )
    cache_affinity_policy = _cache_affinity_policy_for_topology(
        effective_ranking_config,
        "multiple",
    )
    if _cache_affinity_replay_adjustments is not None and cache_affinity_policy is None:
        raise DynamicRankingError(
            "router_dynamic frozen cache adjustments require an enabled multiple policy"
        )
    if not ranking_thinking_assignment_enabled:
        registry_snapshot = _legacy_registry_snapshot_projection(registry_snapshot)
    if (
        ranking_thinking_assignment_enabled
        and effective_ranking_config.get("schema_version") != RANKING_CONFIG_SCHEMA_VERSION
    ):
        raise DynamicRankingError(
            f"router_dynamic thinking_assignment requires {RANKING_CONFIG_SCHEMA_VERSION}"
        )
    if (
        ranking_thinking_assignment_enabled
        and _ranking_number(
            effective_ranking_config,
            "proposer_count",
            "effective_tier_rounding_offset",
        )
        != 0.5
    ):
        raise DynamicRankingError(
            "router_dynamic thinking-policy-v1 requires "
            "proposer_count.effective_tier_rounding_offset to be 0.5"
        )
    aggregator_candidate_count, proposer_backup_count = _selection_roster_counts(
        effective_ranking_config,
        legacy_proposer_backup_count=legacy_proposer_backup_count,
    )
    proposer_global_ceiling = _proposer_global_ceiling(effective_ranking_config)
    if proposer_recovery_quorum is not None and proposer_recovery_quorum > proposer_global_ceiling:
        raise DynamicRankingError(
            "router_dynamic configured proposer recovery quorum "
            f"{proposer_recovery_quorum} exceeds the global proposer ceiling "
            f"{proposer_global_ceiling}",
            reason="proposer_recovery_quorum_unreachable",
        )
    thinking_policy = (
        _thinking_assignment_policy(
            effective_ranking_config,
            allow_legacy_external_switch=True,
        )
        if ranking_thinking_assignment_enabled
        else None
    )
    ranking_config_hash = _canonical_hash(effective_ranking_config)
    profile_decimal_places = _ranking_int(
        effective_ranking_config, "trace", "profile_decimal_places"
    )
    score_decimal_places = _ranking_int(effective_ranking_config, "trace", "score_decimal_places")
    role_reliability_enabled = "role_reliability" in effective_ranking_config
    session_nonzero_epsilon = _ranking_number(
        effective_ranking_config, "trace", "session_nonzero_epsilon"
    )
    rows = registry_snapshot.get("models")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise DynamicRankingError("router_dynamic registry snapshot contains no models")
    if any(not isinstance(row, Mapping) for row in rows):
        raise DynamicRankingError("router_dynamic registry snapshot contains a malformed model row")
    models = [
        _normalize_model(
            row,
            effective_ranking_config,
            thinking_policy=thinking_policy,
        )
        for row in rows
    ]
    if not models:
        raise DynamicRankingError("router_dynamic registry snapshot is empty")
    model_identities = [model.identity.lower() for model in models]
    if len(set(model_identities)) != len(model_identities):
        raise DynamicRankingError(
            "router_dynamic registry snapshot contains duplicate model identities"
        )
    cache_affinity_unavailable_reasons = _cache_affinity_unavailable_reason_trace(
        _cache_affinity_unavailable_reasons,
        policy=cache_affinity_policy,
        topology="multiple",
        candidate_identities=[model.identity for model in models],
    )
    registry_snapshot_hash = _canonical_hash(registry_snapshot)
    user_profile_enabled = user_profile is not None
    effective_user_profile = user_profile if user_profile is not None else {}

    task_profile, session_trace = _apply_session_adjustment(
        task_analysis.profile,
        request_context,
        effective_ranking_config,
        cache_continuity_available=(
            cache_continuity_available and cache_affinity_policy is not None
        ),
    )
    effective_tier = _effective_tier(task_profile, effective_ranking_config)
    minimum, maximum, bound_reasons = _proposer_bounds(
        task_profile, effective_user_profile, effective_ranking_config
    )
    if proposer_recovery_quorum is not None and (
        proposer_recovery_quorum > minimum or proposer_recovery_quorum > maximum
    ):
        minimum = max(minimum, proposer_recovery_quorum)
        maximum = max(maximum, proposer_recovery_quorum)
        bound_reasons.append("proposer_recovery_quorum")

    proposer_filter_started_ns = (
        time.monotonic_ns() if _stage_observability_out is not None else None
    )
    proposer_filters: list[dict[str, Any]] = []
    eligible: list[RankedModel] = []
    for model in models:
        reasons, context_need = _hard_filter_reasons(
            model,
            role="proposer",
            task_profile=task_profile,
            user_profile=effective_user_profile,
            request_context=request_context,
            proposer_count=1,
            ranking_config=effective_ranking_config,
            thinking_policy=thinking_policy,
        )
        proposer_filters.append(
            {
                "identity": model.identity,
                "model": model.model_id,
                "role": "proposer",
                "eligible": not reasons,
                "reasons": reasons,
                "context_need_tokens": context_need,
            }
        )
        if not reasons:
            eligible.append(model)
    proposer_filter_ms = (
        _ranking_stage_elapsed_ms(proposer_filter_started_ns)
        if proposer_filter_started_ns is not None
        else 0
    )
    generation_policy_exclusions = [
        {
            "identity": row["identity"],
            "model": row["model"],
            "reasons": [
                reason
                for reason in row["reasons"]
                if reason.startswith(GENERATION_POLICY_FILTER_REASON_PREFIX)
            ],
        }
        for row in proposer_filters
        if any(
            reason.startswith(GENERATION_POLICY_FILTER_REASON_PREFIX) for reason in row["reasons"]
        )
    ]
    proposer_thinking_unavailable = any(
        "thinking_level_unavailable" in row["reasons"] for row in proposer_filters
    )
    if proposer_recovery_quorum is not None and len(eligible) < proposer_recovery_quorum:
        raise DynamicRankingError(
            "router_dynamic proposer recovery quorum requires "
            f"{proposer_recovery_quorum} proposer(s), but hard filtering left "
            f"{len(eligible)} eligible",
            reason="proposer_recovery_quorum_unreachable",
        )
    if generation_policy_exclusions and len(eligible) < minimum:
        excluded = ", ".join(row["identity"] for row in generation_policy_exclusions)
        raise DynamicRankingError(
            "router_dynamic generation-policy filtering left "
            f"{len(eligible)} eligible proposer(s), fewer than N_min={minimum}; "
            f"excluded: {excluded}"
            + ("; thinking_level_unavailable" if proposer_thinking_unavailable else ""),
            reason=("thinking_level_unavailable" if proposer_thinking_unavailable else ""),
        )
    if not eligible:
        no_eligible_reason_counts: dict[str, int] = {}
        for row in proposer_filters:
            for reason in row["reasons"]:
                no_eligible_reason_counts[reason] = no_eligible_reason_counts.get(reason, 0) + 1
        log.warning(
            "llm_ensemble.router_dynamic.no_eligible_proposer",
            decision_id=decision_id,
            user_profile_enabled=user_profile_enabled,
            registry_snapshot_version=registry_snapshot.get("snapshot_version"),
            filter_reason_counts=no_eligible_reason_counts,
        )
        thinking_suffix = (
            ": thinking_level_unavailable"
            if no_eligible_reason_counts.get("thinking_level_unavailable")
            else ""
        )
        raise DynamicRankingError(
            "router_dynamic has no proposer after hard filtering" + thinking_suffix,
            reason=(
                "thinking_level_unavailable"
                if no_eligible_reason_counts.get("thinking_level_unavailable")
                else ""
            ),
        )

    score_started_ns = time.monotonic_ns() if _stage_observability_out is not None else None
    score_rows = [
        _base_score_row(
            model,
            task_profile,
            user_profile,
            request_context,
            effective_ranking_config,
        )
        for model in eligible
    ]
    score_rows.sort(
        key=lambda row: (
            -_as_float(row["base"]),
            -_as_float(row["quality"]),
            row["model"].identity,
        )
    )
    top_l = min(
        len(score_rows),
        max(
            _ranking_int(effective_ranking_config, "rerank", "top_l_min"),
            maximum * _ranking_int(effective_ranking_config, "rerank", "top_l_multiplier"),
        ),
    )
    candidate_rows = score_rows[:top_l]
    best_clean = max(_as_float(row["base_clean"]) for row in candidate_rows)
    constraints = task_profile.get("constraints")
    constraints_map = constraints if isinstance(constraints, Mapping) else {}
    floor_margins = _ranking_mapping(
        effective_ranking_config, "rerank", "quality_floor_margin_by_risk"
    )
    floor_margin = _as_float(
        floor_margins.get(str(constraints_map.get("risk") or "low")),
        _ranking_number(effective_ranking_config, "rerank", "default_quality_floor_margin"),
    )
    quality_floor = best_clean - floor_margin
    rerank_quality_weight = _ranking_number(effective_ranking_config, "rerank", "quality_weight")
    rerank_coverage_weight = _ranking_number(
        effective_ranking_config, "rerank", "coverage_gain_weight"
    )
    rerank_error_weight = _ranking_number(
        effective_ranking_config, "rerank", "error_complementarity_weight"
    )
    rerank_similarity_penalty = _ranking_number(
        effective_ranking_config, "rerank", "similarity_penalty_weight"
    )
    stop_threshold = _ranking_number(effective_ranking_config, "rerank", "stop_threshold")
    trace_top_candidates = _ranking_int(effective_ranking_config, "rerank", "trace_top_candidates")
    rerank_candidate_pool: list[dict[str, Any]] = []
    quality_candidate_rows: list[dict[str, Any]] = []
    for base_rank, row in enumerate(candidate_rows, start=1):
        passes_quality_floor = _as_float(row["base_clean"]) >= quality_floor
        rerank_candidate_pool.append(
            {
                "base_rank": base_rank,
                "identity": row["model"].identity,
                **(
                    {
                        "quality_before_reliability": round(
                            _as_float(row["quality_before_reliability"]),
                            score_decimal_places,
                        ),
                        "reliability_failure_rate": round(
                            _as_float(row["reliability"]["failure_rate"]),
                            score_decimal_places,
                        ),
                        "reliability_penalty": round(
                            _as_float(row["reliability"]["penalty"]),
                            score_decimal_places,
                        ),
                    }
                    if role_reliability_enabled
                    else {}
                ),
                "base_clean": round(_as_float(row["base_clean"]), score_decimal_places),
                "passes_quality_floor": passes_quality_floor,
            }
        )
        if passes_quality_floor:
            quality_candidate_rows.append(row)

    if cache_affinity_policy is not None:
        for row in quality_candidate_rows:
            adjustment = _cache_affinity_adjustment_for_model(
                policy=cache_affinity_policy,
                cache_affinity_inputs=cache_affinity_inputs,
                role="proposer",
                topology="multiple",
                model=row["model"],
                task_profile=task_profile,
                analyzer_schema_valid=task_analysis.schema_valid,
                session_intent=str(session_trace["intent"]),
                request_context=request_context,
                proposer_count=0,
                cost_weight=_as_float(row["cost_weight"]),
                ranking_config=effective_ranking_config,
                frozen_adjustments=_cache_affinity_replay_adjustments,
            )
            if adjustment is not None:
                row["cache_affinity_adjustment"] = adjustment

    if (
        proposer_recovery_quorum is not None
        and len(quality_candidate_rows) < proposer_recovery_quorum
    ):
        raise DynamicRankingError(
            "router_dynamic proposer recovery quorum requires "
            f"{proposer_recovery_quorum} proposer(s), but the quality floor left "
            f"{len(quality_candidate_rows)} eligible",
            reason="proposer_recovery_quorum_unreachable",
        )
    selected: list[RankedModel] = []
    selection_steps: list[dict[str, Any]] = []
    aggregator_feasibility: list[dict[str, Any]] = []
    stop_reason = "n_max_reached"
    stop_detail: dict[str, Any] = {}
    while len(selected) < min(maximum, len(candidate_rows)):
        remaining_rows = [row for row in quality_candidate_rows if row["model"] not in selected]
        if not remaining_rows:
            stop_reason = "quality_floor_or_pool_exhausted"
            stop_detail = {
                "quality_floor_excluded_count": len(candidate_rows) - len(quality_candidate_rows),
                "remaining_candidate_count": 0,
            }
            break

        target_proposer_count = len(selected) + 1
        feasible_aggregators, feasibility_filters = _aggregator_filter_rows(
            models,
            proposer_count=target_proposer_count,
            task_profile=task_profile,
            user_profile=user_profile,
            request_context=request_context,
            ranking_config=effective_ranking_config,
            thinking_policy=thinking_policy,
        )
        feasibility_reason_counts: dict[str, int] = {}
        for filter_row in feasibility_filters:
            for reason in filter_row["reasons"]:
                feasibility_reason_counts[reason] = feasibility_reason_counts.get(reason, 0) + 1
        aggregator_feasibility.append(
            {
                "proposer_count": target_proposer_count,
                "eligible_aggregator_ids": [model.identity for model in feasible_aggregators],
                "filter_reason_counts": feasibility_reason_counts,
            }
        )
        if not feasible_aggregators:
            stop_reason = "aggregator_infeasible"
            stop_detail = {"proposer_count": target_proposer_count}
            break

        marginal_rows: list[dict[str, Any]] = []
        for row in remaining_rows:
            model = row["model"]
            coverage = _coverage_gain(model, selected, task_profile, effective_ranking_config)
            similarity = max(
                (_similarity(model, other, effective_ranking_config) for other in selected),
                default=0.0,
            )
            error_complementarity = _error_complementarity(
                model, selected, effective_ranking_config
            )
            marginal = (
                rerank_quality_weight * _as_float(row["quality"])
                + rerank_coverage_weight * coverage
                + rerank_error_weight * error_complementarity
                - rerank_similarity_penalty * similarity
            )
            if _resource_aware_proposer_rerank_enabled(effective_ranking_config):
                marginal -= _as_float(row["cost_weight"]) * _as_float(row["cost_normalized"])
                marginal -= _as_float(row["latency_weight"]) * _as_float(row["latency_normalized"])
            cache_adjustment = row.get("cache_affinity_adjustment")
            if isinstance(cache_adjustment, CacheAffinityScoreAdjustment):
                marginal += cache_adjustment.score_adjustment
            marginal_row = {
                "model": model,
                "marginal": marginal,
                "quality": row["quality"],
                "coverage_gain": coverage,
                "max_similarity": similarity,
                "error_complementarity": error_complementarity,
                "base_clean": row["base_clean"],
                "reliability": row["reliability"],
            }
            if isinstance(cache_adjustment, CacheAffinityScoreAdjustment):
                marginal_row["cache_affinity_adjustment"] = cache_adjustment
            marginal_rows.append(marginal_row)
        marginal_rows.sort(
            key=lambda row: (
                -_as_float(row["marginal"]),
                -_as_float(row["base_clean"]),
                -_as_float(row["quality"]),
                row["model"].identity,
            )
        )
        best = marginal_rows[0]
        if len(selected) >= minimum and _as_float(best["marginal"]) < stop_threshold:
            stop_reason = "marginal_below_threshold"
            stop_detail = {
                "identity": best["model"].identity,
                "marginal_gain": round(_as_float(best["marginal"]), score_decimal_places),
                "threshold": round(stop_threshold, score_decimal_places),
            }
            break
        selected.append(best["model"])
        selection_steps.append(
            {
                "step": len(selected),
                "selected": best["model"].identity,
                "marginal_gain": round(_as_float(best["marginal"]), score_decimal_places),
                "quality": round(_as_float(best["quality"]), score_decimal_places),
                **(
                    {
                        "reliability_failure_rate": round(
                            _as_float(best["reliability"]["failure_rate"]),
                            score_decimal_places,
                        ),
                        "reliability_penalty": round(
                            _as_float(best["reliability"]["penalty"]),
                            score_decimal_places,
                        ),
                    }
                    if role_reliability_enabled
                    else {}
                ),
                "coverage_gain": round(_as_float(best["coverage_gain"]), score_decimal_places),
                "max_similarity": round(_as_float(best["max_similarity"]), score_decimal_places),
                "error_complementarity": round(
                    _as_float(best["error_complementarity"]), score_decimal_places
                ),
                "candidate_count": len(marginal_rows),
                "eligible_aggregator_count": len(feasible_aggregators),
                **(
                    {"cache_affinity": best["cache_affinity_adjustment"].trace()}
                    if isinstance(
                        best.get("cache_affinity_adjustment"),
                        CacheAffinityScoreAdjustment,
                    )
                    else {}
                ),
                "top_candidates": [
                    {
                        "identity": candidate["model"].identity,
                        "marginal_gain": round(
                            _as_float(candidate["marginal"]), score_decimal_places
                        ),
                        "quality": round(_as_float(candidate["quality"]), score_decimal_places),
                        **(
                            {
                                "reliability_failure_rate": round(
                                    _as_float(candidate["reliability"]["failure_rate"]),
                                    score_decimal_places,
                                ),
                                "reliability_penalty": round(
                                    _as_float(candidate["reliability"]["penalty"]),
                                    score_decimal_places,
                                ),
                            }
                            if role_reliability_enabled
                            else {}
                        ),
                        "coverage_gain": round(
                            _as_float(candidate["coverage_gain"]), score_decimal_places
                        ),
                        "max_similarity": round(
                            _as_float(candidate["max_similarity"]), score_decimal_places
                        ),
                        "error_complementarity": round(
                            _as_float(candidate["error_complementarity"]),
                            score_decimal_places,
                        ),
                        **(
                            {"cache_affinity": candidate["cache_affinity_adjustment"].trace()}
                            if isinstance(
                                candidate.get("cache_affinity_adjustment"),
                                CacheAffinityScoreAdjustment,
                            )
                            else {}
                        ),
                    }
                    for candidate in marginal_rows[:trace_top_candidates]
                ],
            }
        )

    if stop_reason == "n_max_reached" and len(selected) < maximum:
        stop_reason = "candidate_pool_exhausted"
        stop_detail = {
            "selected_count": len(selected),
            "candidate_pool_count": len(candidate_rows),
        }
    elif stop_reason == "n_max_reached":
        stop_detail = {
            "selected_count": len(selected),
            "N_max": maximum,
        }

    if generation_policy_exclusions and len(selected) < minimum:
        raise DynamicRankingError(
            "router_dynamic generation-policy filtering allowed only "
            f"{len(selected)} feasible proposer(s), fewer than N_min={minimum}"
            + ("; thinking_level_unavailable" if proposer_thinking_unavailable else ""),
            reason=("thinking_level_unavailable" if proposer_thinking_unavailable else ""),
        )
    if not selected:
        if proposer_recovery_quorum is not None:
            raise DynamicRankingError(
                "router_dynamic proposer recovery quorum requires "
                f"{proposer_recovery_quorum} proposer(s), but only 0 have a feasible "
                f"aggregator (stop_reason={stop_reason})",
                reason="proposer_recovery_quorum_unreachable",
            )
        thinking_infeasible = any(
            row.get("filter_reason_counts", {}).get("thinking_level_unavailable")
            for row in aggregator_feasibility
            if isinstance(row.get("filter_reason_counts"), Mapping)
        )
        raise DynamicRankingError(
            "router_dynamic cannot select a proposer with a feasible aggregator"
            + (": thinking_level_unavailable" if thinking_infeasible else ""),
            reason=("thinking_level_unavailable" if thinking_infeasible else ""),
        )
    if proposer_recovery_quorum is not None and len(selected) < proposer_recovery_quorum:
        raise DynamicRankingError(
            "router_dynamic proposer recovery quorum requires "
            f"{proposer_recovery_quorum} proposer(s), but only {len(selected)} have "
            f"a feasible aggregator (stop_reason={stop_reason})",
            reason="proposer_recovery_quorum_unreachable",
        )
    score_ms = _ranking_stage_elapsed_ms(score_started_ns) if score_started_ns is not None else 0
    aggregator_filter_started_ns = (
        time.monotonic_ns() if _stage_observability_out is not None else None
    )
    aggregator_rows, aggregator_filters = _aggregator_rows(
        models,
        proposers=selected,
        task_profile=task_profile,
        user_profile=user_profile,
        request_context=request_context,
        ranking_config=effective_ranking_config,
        thinking_policy=thinking_policy,
        cache_affinity_policy=cache_affinity_policy,
        cache_affinity_inputs=cache_affinity_inputs,
        cache_analyzer_schema_valid=task_analysis.schema_valid,
        cache_session_intent=str(session_trace["intent"]),
        frozen_cache_adjustments=_cache_affinity_replay_adjustments,
    )
    aggregator_filter_ms = (
        _ranking_stage_elapsed_ms(aggregator_filter_started_ns)
        if aggregator_filter_started_ns is not None
        else 0
    )
    if _stage_observability_out is not None:
        _stage_observability_out["hard_filter_ms"] = min(
            (1 << 63) - 1,
            proposer_filter_ms + aggregator_filter_ms,
        )
        _stage_observability_out["score_ms"] = score_ms
    if not aggregator_rows:
        thinking_unavailable = any(
            "thinking_level_unavailable" in row["reasons"] for row in aggregator_filters
        )
        aggregator_generation_exclusions = [
            row["identity"]
            for row in aggregator_filters
            if any(
                reason.startswith(GENERATION_POLICY_FILTER_REASON_PREFIX)
                for reason in row["reasons"]
            )
        ]
        if aggregator_generation_exclusions:
            raise DynamicRankingError(
                "router_dynamic generation-policy filtering left no feasible aggregator; "
                f"excluded: {', '.join(aggregator_generation_exclusions)}"
                + ("; thinking_level_unavailable" if thinking_unavailable else ""),
                reason=("thinking_level_unavailable" if thinking_unavailable else ""),
            )
        raise DynamicRankingError(
            "router_dynamic has no feasible aggregator"
            + (": thinking_level_unavailable" if thinking_unavailable else ""),
            reason=("thinking_level_unavailable" if thinking_unavailable else ""),
        )
    aggregator_candidate_rows = aggregator_rows[:aggregator_candidate_count]
    aggregator_row = aggregator_candidate_rows[0]
    aggregator = aggregator_row["model"]
    aggregator_candidates = tuple(row["model"] for row in aggregator_candidate_rows)
    aggregator_candidate_identities = {model.identity for model in aggregator_candidates}
    backup_rows = [
        row
        for row in quality_candidate_rows
        if row["model"] not in selected
        and row["model"].identity not in aggregator_candidate_identities
    ][:proposer_backup_count]
    backup_proposers = tuple(row["model"] for row in backup_rows)
    recovery_quorum = (
        proposer_recovery_quorum
        if proposer_recovery_quorum is not None
        else min(minimum, len(selected))
    )
    coverage_shortfall = len(selected) < minimum
    session_adjusted_ids = sorted(
        {
            row["model"].identity
            for row in [*score_rows, *aggregator_rows]
            if abs(_as_float(row.get("session_score"))) > session_nonzero_epsilon
        }
    )
    session_trace["sticky_applied"] = session_trace["intent"] == "continue" and bool(
        session_adjusted_ids
    )
    session_trace["adjusted_model_ids"] = session_adjusted_ids

    assigned_proposers = tuple(selected)
    assigned_aggregator = aggregator
    assigned_aggregator_candidates = aggregator_candidates
    assigned_backup_proposers = backup_proposers
    thinking_assignment: dict[str, Any] = {}
    thinking_assignment_details: dict[str, Any] = {}
    thinking_assignment_reasons: dict[str, Any] = {}
    thinking_unsupported_fallbacks: list[dict[str, Any]] = []
    if thinking_policy is not None:
        (
            assigned_proposers,
            assigned_aggregator,
            thinking_assignment,
            thinking_assignment_details,
            thinking_unsupported_fallbacks,
        ) = _assign_thinking_levels(
            proposers=selected,
            aggregator=aggregator,
            effective_tier=effective_tier,
            task_profile=task_profile,
            session_trace=session_trace,
            policy=thinking_policy,
        )
        thinking_assignment_reasons = {
            "proposers": {
                row["identity"]: list(row["reasons"])
                for row in thinking_assignment_details["proposers"]
            },
            "aggregator": list(thinking_assignment_details["aggregator"]["reasons"]),
        }
        aggregator_target, aggregator_reasons, aggregator_risk_floor = _thinking_target_for_role(
            role="aggregator",
            effective_tier=effective_tier,
            task_profile=task_profile,
            session_trace=session_trace,
            policy=thinking_policy,
        )
        assigned_fallbacks: list[RankedModel] = []
        aggregator_candidate_details = [copy.deepcopy(thinking_assignment_details["aggregator"])]
        for fallback in aggregator_candidates[1:]:
            assigned_fallback, fallback_detail, unsupported = _resolve_model_thinking_level(
                fallback,
                role="aggregator_fallback",
                requested_level=aggregator_target,
                reasons=aggregator_reasons,
                risk_floor=aggregator_risk_floor,
                policy=thinking_policy,
            )
            assigned_fallbacks.append(assigned_fallback)
            aggregator_candidate_details.append(fallback_detail)
            if unsupported is not None:
                thinking_unsupported_fallbacks.append(unsupported)
        assigned_aggregator_candidates = (
            assigned_aggregator,
            *assigned_fallbacks,
        )
        # The primary scalar assignment remains the selected_A assignment.
        # Recovery candidates need their own replay-bound initial/native
        # levels and ordered provider-rejection chain so an execution receipt
        # for a secondary aggregator cannot masquerade as a primary mutation.
        thinking_assignment_details["aggregator_candidates"] = aggregator_candidate_details
        (
            proposer_target,
            proposer_reasons,
            proposer_risk_floor,
        ) = _thinking_target_for_role(
            role="proposer",
            effective_tier=effective_tier,
            task_profile=task_profile,
            session_trace=session_trace,
            policy=thinking_policy,
        )
        assigned_backups: list[RankedModel] = []
        backup_details: list[dict[str, Any]] = []
        for backup in backup_proposers:
            assigned_backup, backup_detail, unsupported = _resolve_model_thinking_level(
                backup,
                role="proposer_backup",
                requested_level=proposer_target,
                reasons=proposer_reasons,
                risk_floor=proposer_risk_floor,
                policy=thinking_policy,
            )
            assigned_backups.append(assigned_backup)
            backup_details.append(backup_detail)
            if unsupported is not None:
                thinking_unsupported_fallbacks.append(unsupported)
        assigned_backup_proposers = tuple(assigned_backups)
        if assigned_backup_proposers:
            thinking_assignment["backup_proposers"] = {
                model.identity: model.effective_thinking_level
                for model in assigned_backup_proposers
            }
            thinking_assignment_details["backup_proposers"] = backup_details

    reason_counts: dict[str, int] = {}
    for filter_row in [*proposer_filters, *aggregator_filters]:
        for reason in filter_row["reasons"]:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    selected_ids = [model.identity for model in selected]
    overlap = bool(
        aggregator_row["self_overlap"]
        or aggregator_row["family_overlap"]
        or aggregator_row["vendor_overlap"]
    )
    router_tier_mapping = _router_tier_mapping(effective_ranking_config)
    router_tier_by_effective_tier = {
        tier: router_tier for router_tier, tier in router_tier_mapping.items()
    }
    effective_ranking_version = (
        RANKING_VERSION if ranking_thinking_assignment_enabled else LEGACY_RANKING_VERSION
    )
    trace_registry_snapshot = copy.deepcopy(dict(registry_snapshot))
    trace_request_context = copy.deepcopy(dict(request_context))
    trace_request_context["snapshot_hash"] = _request_context_hash(trace_request_context)
    _assert_public_ranking_trace_payload(
        trace_registry_snapshot,
        label="registry_snapshot",
    )
    _assert_public_ranking_trace_payload(
        trace_request_context,
        label="request_context",
    )
    trace = {
        "strategy": "router_dynamic",
        "decision_id": decision_id,
        "ranking_version": effective_ranking_version,
        "ranking_config_schema_version": str(effective_ranking_config["schema_version"]),
        "ranking_config_version": str(effective_ranking_config["config_version"]),
        "ranking_config_hash": ranking_config_hash,
        "ranking_parameters": _detached_ranking_config(effective_ranking_config),
        "task_profile_schema_version": TASK_PROFILE_SCHEMA_VERSION,
        "registry_snapshot_version": str(registry_snapshot.get("snapshot_version") or ""),
        "registry_snapshot_hash": registry_snapshot_hash,
        "registry_snapshot": trace_registry_snapshot,
        "routed_tier": _router_tier(routed_tier, effective_ranking_config),
        "routing_confidence": round(_clamp(routing_confidence), profile_decimal_places),
        "effective_tier": effective_tier,
        "effective_router_tier": router_tier_by_effective_tier[effective_tier],
        "task_analyzer": task_analysis.trace(effective_ranking_config),
        "task_profile": copy.deepcopy(task_profile),
        "task_profile_hash": _canonical_hash(task_profile),
        "task_profile_pre_escalation": session_trace.pop("task_profile_pre_escalation"),
        "task_profile_post_escalation": session_trace.pop("task_profile_post_escalation"),
        "session": session_trace,
        "user_profile_enabled": user_profile_enabled,
        "user_profile_version": str(effective_user_profile.get("profile_version") or ""),
        "user_profile_source": str(effective_user_profile.get("profile_source") or ""),
        "request_context_hash": trace_request_context["snapshot_hash"],
        "request_context": trace_request_context,
        "candidate_pool_size": len(models),
        "candidate_pool": [
            model.trace(
                include_thinking_contract=ranking_thinking_assignment_enabled,
            )
            for model in models
        ],
        "hard_filter": {
            "proposer_results": proposer_filters,
            "aggregator_results": aggregator_filters,
            "eligible_proposer_ids": [model.identity for model in eligible],
            "eligible_aggregator_ids": [row["model"].identity for row in aggregator_rows],
            "filter_reason_counts": reason_counts,
        },
        "model_scores": [_score_trace(row, effective_ranking_config) for row in score_rows],
        "top_l": top_l,
        "quality_floor": round(quality_floor, score_decimal_places),
        "rerank_candidate_pool": rerank_candidate_pool,
        "quality_floor_excluded_ids": [
            row["identity"] for row in rerank_candidate_pool if not row["passes_quality_floor"]
        ],
        "N_min": minimum,
        "N_max": maximum,
        "bound_reasons": bound_reasons,
        "selection_steps": selection_steps,
        "aggregator_feasibility": aggregator_feasibility,
        "selected_P": selected_ids,
        "backup_P": [model.identity for model in assigned_backup_proposers],
        "configured_proposer_backup_count": proposer_backup_count,
        "effective_proposer_backup_count": len(assigned_backup_proposers),
        "proposer_recovery_policy": {
            "schema": "opensquilla.router-dynamic-proposer-recovery/v1",
            "configured_backup_count": proposer_backup_count,
            "effective_backup_count": len(assigned_backup_proposers),
            "max_additional_physical_requests": (proposer_recovery_max_additional_calls),
            "quorum_required": recovery_quorum,
            "max_tokens_cap": proposer_max_tokens_cap,
            "visible_answer_reserve_tokens": (proposer_visible_answer_reserve_tokens),
            "thinking_downgrade_order": ["one_strictly_lower"],
            "transient_same_model_retries": 1,
            "backup_reasoning_downgrades": 1,
        },
        "selected_A": aggregator.identity,
        "configured_aggregator_candidate_count": aggregator_candidate_count,
        "effective_aggregator_candidate_count": len(assigned_aggregator_candidates),
        "aggregator_candidates": [model.identity for model in assigned_aggregator_candidates],
        "exploration": copy.deepcopy(
            dict(_ranking_mapping(effective_ranking_config, "exploration"))
        ),
        "proposer_count": len(selected),
        "coverage_shortfall": coverage_shortfall,
        "stop_reason": stop_reason,
        "stop_detail": stop_detail,
        "aggregator": {
            "selected": _aggregator_score_trace(aggregator_row, effective_ranking_config),
            "scores": [
                _aggregator_score_trace(row, effective_ranking_config) for row in aggregator_rows
            ],
            "overlap_flag": overlap,
            "candidate_anonymization": True,
            "requires_order_randomization": overlap,
        },
    }
    if cache_affinity_policy is not None:
        trace["cache_continuity_available"] = bool(cache_continuity_available)
        trace["cache_affinity_inputs"] = [
            adjustment.trace()
            for row in [*quality_candidate_rows, *aggregator_rows]
            if isinstance(
                adjustment := row.get("cache_affinity_adjustment"),
                CacheAffinityScoreAdjustment,
            )
        ]
        if cache_affinity_unavailable_reasons:
            trace["cache_affinity_unavailable_reasons"] = cache_affinity_unavailable_reasons
    if thinking_policy is not None:
        trace.update(
            {
                "ranking_thinking_assignment_enabled": True,
                "thinking_physical_evidence_schema": (THINKING_PHYSICAL_EVIDENCE_SCHEMA),
                "thinking_policy_version": str(thinking_policy["policy_version"]),
                "thinking_assignment": copy.deepcopy(thinking_assignment),
                "thinking_assignment_details": copy.deepcopy(thinking_assignment_details),
                "assignment_reasons": copy.deepcopy(thinking_assignment_reasons),
                "unsupported_level_fallbacks": copy.deepcopy(thinking_unsupported_fallbacks),
                "policy_versions": {
                    "ranking": effective_ranking_version,
                    "thinking": str(thinking_policy["policy_version"]),
                },
            }
        )
        emit_info(
            "llm_ensemble.router_dynamic.thinking_assignment_recorded",
            decision_id=decision_id,
            thinking_assignment=trace["thinking_assignment"],
            assignment_reasons=trace["assignment_reasons"],
            unsupported_level_fallbacks=trace["unsupported_level_fallbacks"],
            policy_versions=trace["policy_versions"],
        )
    emit_info(
        "llm_ensemble.router_dynamic.candidate_pool_recorded",
        decision_id=decision_id,
        user_profile_enabled=user_profile_enabled,
        registry_snapshot_version=trace["registry_snapshot_version"],
        registry_snapshot_hash=registry_snapshot_hash,
        candidate_pool_size=len(models),
        eligible_proposer_count=len(eligible),
        eligible_aggregator_count=len(aggregator_rows),
        filter_reason_counts=reason_counts,
    )
    emit_info(
        "llm_ensemble.router_dynamic.model_scores_recorded",
        decision_id=decision_id,
        ranking_version=effective_ranking_version,
        score_count=len(score_rows),
        top_l=top_l,
        quality_floor=round(quality_floor, score_decimal_places),
    )
    emit_info(
        "llm_ensemble.router_dynamic.proposer_selection_recorded",
        decision_id=decision_id,
        selected_P=selected_ids,
        N_min=minimum,
        N_max=maximum,
        stop_reason=stop_reason,
        coverage_shortfall=coverage_shortfall,
    )
    emit_info(
        "llm_ensemble.router_dynamic.aggregator_selection_recorded",
        decision_id=decision_id,
        selected_A=aggregator.identity,
        context_need_tokens=aggregator_row["context_need_tokens"],
        overlap_flag=overlap,
        bias_penalty=round(_as_float(aggregator_row["bias"]), score_decimal_places),
    )
    router_decision_log_fields: dict[str, Any] = {
        "decision_id": decision_id,
        "user_profile_enabled": user_profile_enabled,
        "ranking_version": effective_ranking_version,
        "selected_P": selected_ids,
        "selected_A": aggregator.identity,
        "session_intent": session_trace["intent"],
        "escalation_level": session_trace["escalation_level"],
        "sticky_applied": session_trace["sticky_applied"],
    }
    if thinking_policy is not None:
        router_decision_log_fields.update(
            {
                "thinking_assignment_enabled": True,
                "thinking_policy_version": trace["thinking_policy_version"],
                "thinking_assignment": trace["thinking_assignment"],
                "assignment_reasons": trace["assignment_reasons"],
                "unsupported_level_fallbacks": trace["unsupported_level_fallbacks"],
                "policy_versions": trace["policy_versions"],
            }
        )
    emit_info(
        "llm_ensemble.router_dynamic.router_decision_recorded",
        **router_decision_log_fields,
    )
    return RankingDecision(
        proposers=assigned_proposers,
        backup_proposers=assigned_backup_proposers,
        aggregator=assigned_aggregator,
        aggregator_candidates=assigned_aggregator_candidates,
        effective_tier=effective_tier,
        trace=trace,
        thinking_assignment=copy.deepcopy(thinking_assignment),
        thinking_assignment_details=copy.deepcopy(thinking_assignment_details),
    )


def single_ranking_trace_replay_reasons(trace: Mapping[str, Any]) -> list[str]:
    """Replay one cache-aware or calibrated single-route trace."""

    ranking_version = trace.get("ranking_version")
    if (
        trace.get("execution_mode") != "router_single"
        or ranking_version
        not in {
            SINGLE_MODEL_RANKING_VERSION,
            CALIBRATED_SINGLE_MODEL_RANKING_VERSION,
        }
    ):
        return ["wrong_single_ranking_replay_mode"]
    if trace.get("user_profile_enabled") is not False:
        return ["single_ranking_replay_requires_disabled_user_profile"]
    registry_snapshot = trace.get("registry_snapshot")
    request_context = trace.get("request_context")
    ranking_parameters = trace.get("ranking_parameters")
    raw_profile = trace.get("task_profile_pre_escalation")
    analyzer = trace.get("task_analyzer")
    if not all(
        isinstance(value, Mapping)
        for value in (
            registry_snapshot,
            request_context,
            ranking_parameters,
            raw_profile,
            analyzer,
        )
    ):
        return ["missing_single_ranking_replay_evidence"]
    assert isinstance(registry_snapshot, Mapping)
    assert isinstance(request_context, Mapping)
    assert isinstance(ranking_parameters, Mapping)
    assert isinstance(raw_profile, Mapping)
    assert isinstance(analyzer, Mapping)
    try:
        _assert_public_ranking_trace_payload(
            registry_snapshot,
            label="registry_snapshot",
        )
        _assert_public_ranking_trace_payload(
            request_context,
            label="request_context",
        )
    except DynamicRankingError:
        return ["single_ranking_replay_secret_evidence"]
    if _canonical_hash(registry_snapshot) != str(trace.get("registry_snapshot_hash") or ""):
        return ["single_replay_registry_snapshot_hash_mismatch"]
    context_hash = _request_context_hash(request_context)
    if (
        str(request_context.get("snapshot_hash") or "") != context_hash
        or str(trace.get("request_context_hash") or "") != context_hash
    ):
        return ["single_replay_request_context_hash_mismatch"]
    if _canonical_hash(ranking_parameters) != str(trace.get("ranking_config_hash") or ""):
        return ["single_replay_ranking_config_hash_mismatch"]
    cache_policy = _cache_affinity_policy_for_topology(
        ranking_parameters,
        "single",
    )
    calibration_policy = _single_route_calibration_policy(ranking_parameters)
    calibration_scope = trace.get("single_route_calibration_scope")
    if isinstance(calibration_scope, Mapping):
        calibration_applied = calibration_scope.get("applied")
        if (
            not isinstance(calibration_applied, bool)
            or calibration_policy is None
            or (
                ranking_version == CALIBRATED_SINGLE_MODEL_RANKING_VERSION
            )
            != calibration_applied
        ):
            return ["single_replay_calibration_version_mismatch"]
    elif (
        ranking_version == CALIBRATED_SINGLE_MODEL_RANKING_VERSION
    ) != (calibration_policy is not None):
        # Compatibility for frozen pre-scope calibration traces.
        return ["single_replay_calibration_version_mismatch"]
    raw_continuity = trace.get("cache_continuity_available")
    requires_tools = trace.get("requires_tools")
    thinking_enabled = trace.get("ranking_thinking_assignment_enabled")
    if (
        not isinstance(requires_tools, bool)
        or not isinstance(thinking_enabled, bool)
        or (cache_policy is None and calibration_policy is None)
    ):
        return ["invalid_single_ranking_replay_cache_policy"]
    try:
        if cache_policy is None:
            calibration_was_bypassed = (
                isinstance(calibration_scope, Mapping)
                and calibration_scope.get("applied") is False
            )
            if raw_continuity is None and calibration_was_bypassed:
                frozen_cache_continuity = False
            elif isinstance(raw_continuity, bool):
                frozen_cache_continuity = raw_continuity
            else:
                return ["invalid_single_ranking_replay_cache_policy"]
            frozen_cache_inputs = trace.get("cache_affinity_inputs")
            if frozen_cache_inputs is None and calibration_was_bypassed:
                pass
            elif frozen_cache_inputs != []:
                return ["invalid_single_ranking_replay_cache_affinity_inputs"]
            frozen_adjustments = None
        else:
            if not isinstance(raw_continuity, bool):
                return ["invalid_single_ranking_replay_cache_policy"]
            frozen_cache_continuity = raw_continuity
            frozen_adjustments = _cache_affinity_adjustments_from_trace(
                trace.get("cache_affinity_inputs"),
                policy=cache_policy,
                candidate_pool_size=len(registry_snapshot.get("models") or []),
            )
        analysis = TaskAnalysisResult(
            profile=copy.deepcopy(dict(raw_profile)),
            source=str(analyzer.get("source") or "replay"),
            schema_valid=analyzer.get("schema_valid") is True,
            confidence=_clamp(_as_float(analyzer.get("confidence"), 0.0)),
            analyzer_version=str(analyzer.get("analyzer_version") or TASK_ANALYZER_VERSION),
            fallback_reason=str(analyzer.get("fallback_reason") or ""),
            usage=(
                copy.deepcopy(dict(analyzer["usage"]))
                if isinstance(analyzer.get("usage"), Mapping)
                else {}
            ),
            provider_id=str(analyzer.get("provider") or ""),
            model_id=str(analyzer.get("model") or ""),
            normalization_warnings=tuple(
                str(value) for value in analyzer.get("normalization_warnings") or []
            ),
            replay=(
                copy.deepcopy(dict(analyzer["replay"]))
                if isinstance(analyzer.get("replay"), Mapping)
                else {}
            ),
            chain_trace=(
                copy.deepcopy(dict(analyzer["chain"]))
                if isinstance(analyzer.get("chain"), Mapping)
                else {}
            ),
        )
        replayed = rank_single_model(
            task_analysis=analysis,
            user_profile=None,
            request_context=copy.deepcopy(dict(request_context)),
            registry_snapshot=copy.deepcopy(dict(registry_snapshot)),
            routed_tier=str(trace.get("routed_tier") or ""),
            routing_confidence=_as_float(trace.get("routing_confidence"), 0.0),
            requires_tools=requires_tools,
            ranking_config=copy.deepcopy(dict(ranking_parameters)),
            decision_id=str(trace.get("decision_id") or ""),
            ranking_thinking_assignment_enabled=thinking_enabled,
            cache_continuity_available=frozen_cache_continuity,
            _cache_affinity_unavailable_reasons=trace.get("cache_affinity_unavailable_reasons"),
            _cache_affinity_replay_adjustments=frozen_adjustments,
        ).trace
    except (
        DynamicRankingError,
        KeyError,
        OverflowError,
        TypeError,
        ValueError,
    ):
        return ["invalid_single_ranking_replay_cache_affinity_inputs"]
    raw_calibration_policy = ranking_parameters.get("single_route_calibration")
    if (
        "single_route_calibration_scope" not in trace
        and isinstance(raw_calibration_policy, Mapping)
        and "activation_model_identities" not in raw_calibration_policy
    ):
        # Frozen calibration-v2 traces predate explicit activation-scope
        # evidence. Their policy is still replayed strictly; only the additive
        # scope annotation is omitted for byte-shape compatibility.
        replayed.pop("single_route_calibration_scope", None)
    if dict(trace) != replayed:
        return ["single_frozen_ranker_replay_mismatch"]
    return []


_RANKING_REPLAY_FIELDS = (
    "strategy",
    "decision_id",
    "ranking_version",
    "ranking_config_schema_version",
    "ranking_config_version",
    "ranking_config_hash",
    "task_profile_schema_version",
    "registry_snapshot_version",
    "registry_snapshot_hash",
    "routed_tier",
    "routing_confidence",
    "ranking_thinking_assignment_enabled",
    "thinking_physical_evidence_schema",
    "effective_tier",
    "effective_router_tier",
    "task_profile",
    "task_profile_hash",
    "task_profile_pre_escalation",
    "task_profile_post_escalation",
    "task_analyzer",
    "session",
    "cache_continuity_available",
    "cache_affinity_inputs",
    "cache_affinity_unavailable_reasons",
    "user_profile_enabled",
    "user_profile_version",
    "user_profile_source",
    "request_context_hash",
    "candidate_pool_size",
    "candidate_pool",
    "hard_filter",
    "model_scores",
    "top_l",
    "quality_floor",
    "rerank_candidate_pool",
    "quality_floor_excluded_ids",
    "N_min",
    "N_max",
    "bound_reasons",
    "selection_steps",
    "aggregator_feasibility",
    "selected_P",
    "backup_P",
    "configured_proposer_backup_count",
    "effective_proposer_backup_count",
    "proposer_recovery_policy",
    "selected_A",
    "configured_aggregator_candidate_count",
    "effective_aggregator_candidate_count",
    "aggregator_candidates",
    "thinking_policy_version",
    "thinking_assignment",
    "thinking_assignment_details",
    "assignment_reasons",
    "unsupported_level_fallbacks",
    "policy_versions",
    "exploration",
    "proposer_count",
    "coverage_shortfall",
    "stop_reason",
    "stop_detail",
    "aggregator",
)


def ranking_trace_replay_reasons(
    trace: Mapping[str, Any],
    *,
    allow_legacy_managed_v3: bool = False,
) -> list[str]:
    """Replay a frozen G1 ranker trace from embedded public evidence."""

    reasons: list[str] = []
    legacy_trace = (
        trace.get("ranking_version") == "step2-ranking-v2"
        and trace.get("ranking_config_schema_version") == LEGACY_RANKING_CONFIG_SCHEMA_VERSION
    )
    legacy_thinking_trace = (
        allow_legacy_managed_v3
        and trace.get("ranking_version") == LEGACY_THINKING_RANKING_VERSION
        and trace.get("ranking_thinking_assignment_enabled") is True
    )
    if "ranking_thinking_assignment_enabled" not in trace and not legacy_trace:
        reasons.append("missing_g1_replay_thinking_assignment_switch")
    raw_thinking_assignment_enabled = trace.get(
        "ranking_thinking_assignment_enabled",
        False,
    )
    if not isinstance(raw_thinking_assignment_enabled, bool):
        reasons.append("invalid_g1_replay_thinking_assignment_switch")
    thinking_assignment_enabled = raw_thinking_assignment_enabled is True
    proposer_recovery_fields = {
        "backup_P",
        "configured_proposer_backup_count",
        "effective_proposer_backup_count",
        "proposer_recovery_policy",
    }
    present_proposer_recovery_fields = {
        field_name for field_name in proposer_recovery_fields if field_name in trace
    }
    if present_proposer_recovery_fields and (
        present_proposer_recovery_fields != proposer_recovery_fields
    ):
        reasons.append("incomplete_g1_replay_proposer_recovery_policy")
    aggregator_roster_fields = {
        "configured_aggregator_candidate_count",
        "effective_aggregator_candidate_count",
    }
    present_aggregator_roster_fields = {
        field_name for field_name in aggregator_roster_fields if field_name in trace
    }
    if present_aggregator_roster_fields and (
        present_aggregator_roster_fields != aggregator_roster_fields
    ):
        reasons.append("incomplete_g1_replay_aggregator_roster_policy")
    if trace.get("user_profile_enabled") is not False:
        reasons.append("g1_ranking_replay_requires_disabled_user_profile")
    registry_snapshot = trace.get("registry_snapshot")
    request_context = trace.get("request_context")
    ranking_parameters = trace.get("ranking_parameters")
    raw_profile = trace.get("task_profile_pre_escalation")
    if not isinstance(registry_snapshot, Mapping):
        reasons.append("missing_g1_replay_registry_snapshot")
    if not isinstance(request_context, Mapping):
        reasons.append("missing_g1_replay_request_context")
    if not isinstance(ranking_parameters, Mapping):
        reasons.append("missing_g1_replay_ranking_parameters")
    if not isinstance(raw_profile, Mapping):
        reasons.append("missing_g1_replay_raw_task_profile")
    if reasons:
        return reasons
    try:
        _assert_public_ranking_trace_payload(
            registry_snapshot,
            label="registry_snapshot",
        )
        _assert_public_ranking_trace_payload(
            request_context,
            label="request_context",
        )
    except DynamicRankingError:
        return ["g1_ranking_replay_secret_evidence"]

    registry_hash = _canonical_hash(registry_snapshot)
    if registry_hash != str(trace.get("registry_snapshot_hash") or ""):
        reasons.append("g1_replay_registry_snapshot_hash_mismatch")
    embedded_context_hash = str(request_context.get("snapshot_hash") or "")
    recomputed_context_hash = _request_context_hash(request_context)
    if (
        not embedded_context_hash
        or embedded_context_hash != recomputed_context_hash
        or str(trace.get("request_context_hash") or "") != recomputed_context_hash
    ):
        reasons.append("g1_replay_request_context_hash_mismatch")
    if _canonical_hash(ranking_parameters) != str(trace.get("ranking_config_hash") or ""):
        reasons.append("g1_replay_ranking_config_hash_mismatch")
    if reasons:
        return list(dict.fromkeys(reasons))

    frozen_cache_adjustments: (
        dict[
            str,
            dict[str, CacheAffinityScoreAdjustment],
        ]
        | None
    ) = None
    frozen_cache_continuity = False
    replay_cache_policy = _cache_affinity_policy_for_topology(
        ranking_parameters,
        "multiple",
    )
    if replay_cache_policy is not None:
        raw_continuity = trace.get("cache_continuity_available")
        if not isinstance(raw_continuity, bool):
            return ["invalid_g1_replay_cache_continuity"]
        frozen_cache_continuity = raw_continuity
        try:
            frozen_cache_adjustments = _cache_affinity_adjustments_from_trace(
                trace.get("cache_affinity_inputs"),
                policy=replay_cache_policy,
                candidate_pool_size=len(registry_snapshot.get("models") or []),
            )
        except (
            DynamicRankingError,
            KeyError,
            OverflowError,
            TypeError,
            ValueError,
        ):
            return ["invalid_g1_replay_cache_affinity_inputs"]
    elif (
        "cache_continuity_available" in trace
        or "cache_affinity_inputs" in trace
        or "cache_affinity_unavailable_reasons" in trace
    ):
        return ["unexpected_g1_replay_cache_affinity_inputs"]

    ranking_proposer_policy = ranking_parameters.get("proposer_count")
    ranking_proposer_policy = (
        ranking_proposer_policy if isinstance(ranking_proposer_policy, Mapping) else {}
    )
    legacy_replay_backup_count: int | None = None
    if "backup_count" not in ranking_proposer_policy:
        configured_backup_count = trace.get("configured_proposer_backup_count")
        legacy_replay_backup_count = (
            int(configured_backup_count)
            if isinstance(configured_backup_count, int)
            and not isinstance(configured_backup_count, bool)
            else 0
        )

    analyzer = trace.get("task_analyzer")
    analyzer = analyzer if isinstance(analyzer, Mapping) else {}
    try:
        analysis = TaskAnalysisResult(
            profile=copy.deepcopy(dict(raw_profile)),
            source=str(analyzer.get("source") or "replay"),
            schema_valid=analyzer.get("schema_valid") is True,
            confidence=_clamp(_as_float(analyzer.get("confidence"), 0.0)),
            analyzer_version=str(analyzer.get("analyzer_version") or TASK_ANALYZER_VERSION),
            fallback_reason=str(analyzer.get("fallback_reason") or ""),
            usage=(
                copy.deepcopy(dict(analyzer["usage"]))
                if isinstance(analyzer.get("usage"), Mapping)
                else {}
            ),
            provider_id=str(analyzer.get("provider") or ""),
            model_id=str(analyzer.get("model") or ""),
            normalization_warnings=tuple(
                str(value) for value in analyzer.get("normalization_warnings") or []
            ),
            replay=(
                copy.deepcopy(dict(analyzer["replay"]))
                if isinstance(analyzer.get("replay"), Mapping)
                else {}
            ),
            chain_trace=(
                copy.deepcopy(dict(analyzer["chain"]))
                if isinstance(analyzer.get("chain"), Mapping)
                else {}
            ),
        )
        replayed = rank_models(
            task_analysis=analysis,
            user_profile=None,
            request_context=copy.deepcopy(dict(request_context)),
            registry_snapshot=copy.deepcopy(dict(registry_snapshot)),
            routed_tier=str(trace.get("routed_tier") or ""),
            routing_confidence=_as_float(
                trace.get("routing_confidence"),
                0.0,
            ),
            ranking_config=copy.deepcopy(dict(ranking_parameters)),
            decision_id=str(trace.get("decision_id") or ""),
            ranking_thinking_assignment_enabled=thinking_assignment_enabled,
            cache_continuity_available=frozen_cache_continuity,
            _cache_affinity_unavailable_reasons=trace.get("cache_affinity_unavailable_reasons"),
            _cache_affinity_replay_adjustments=frozen_cache_adjustments,
            legacy_proposer_backup_count=legacy_replay_backup_count,
            proposer_recovery_max_additional_calls=(
                int(
                    (trace.get("proposer_recovery_policy") or {}).get(
                        "max_additional_physical_requests"
                    )
                )
                if isinstance(trace.get("proposer_recovery_policy"), Mapping)
                and isinstance(
                    (trace.get("proposer_recovery_policy") or {}).get(
                        "max_additional_physical_requests"
                    ),
                    int,
                )
                else 0
            ),
            proposer_max_tokens_cap=(
                int((trace.get("proposer_recovery_policy") or {}).get("max_tokens_cap"))
                if isinstance(trace.get("proposer_recovery_policy"), Mapping)
                and isinstance(
                    (trace.get("proposer_recovery_policy") or {}).get("max_tokens_cap"),
                    int,
                )
                else 65_536
            ),
            proposer_visible_answer_reserve_tokens=(
                int(
                    (trace.get("proposer_recovery_policy") or {}).get(
                        "visible_answer_reserve_tokens"
                    )
                )
                if isinstance(trace.get("proposer_recovery_policy"), Mapping)
                and isinstance(
                    (trace.get("proposer_recovery_policy") or {}).get(
                        "visible_answer_reserve_tokens"
                    ),
                    int,
                )
                else 4_096
            ),
            proposer_recovery_quorum=(
                int((trace.get("proposer_recovery_policy") or {}).get("quorum_required"))
                if isinstance(trace.get("proposer_recovery_policy"), Mapping)
                and isinstance(
                    (trace.get("proposer_recovery_policy") or {}).get("quorum_required"),
                    int,
                )
                else None
            ),
        ).trace
    except Exception:  # noqa: BLE001 - malformed replay evidence fails closed
        return ["g1_frozen_ranker_replay_failed"]

    additive_thinking_fields = {
        "ranking_thinking_assignment_enabled",
        "thinking_physical_evidence_schema",
        "thinking_policy_version",
        "thinking_assignment",
        "thinking_assignment_details",
        "assignment_reasons",
        "unsupported_level_fallbacks",
        "policy_versions",
    }
    legacy_disabled_replay = not thinking_assignment_enabled and legacy_trace
    for field_name in _RANKING_REPLAY_FIELDS:
        if field_name in proposer_recovery_fields and not present_proposer_recovery_fields:
            # Pre-recovery frozen traces remain replayable as legacy evidence,
            # but cannot acquire a partial/new policy projection.
            continue
        if field_name in aggregator_roster_fields and not present_aggregator_roster_fields:
            # Pre-roster frozen traces bind the historical hard-coded
            # three-candidate aggregator chain, but do not acquire the new
            # explicit configured/effective count fields during replay.
            continue
        if field_name == "ranking_version" and (legacy_disabled_replay or legacy_thinking_trace):
            continue
        # Managed v3 already bound the top-level aggregator recovery chain.
        # Only unmanaged v2 predates that field.
        if field_name == "aggregator_candidates" and field_name not in trace and legacy_trace:
            continue
        if (
            field_name == "thinking_assignment_details"
            and legacy_thinking_trace
            and isinstance(trace.get(field_name), Mapping)
            and isinstance(replayed.get(field_name), Mapping)
        ):
            observed_details = copy.deepcopy(dict(trace[field_name]))
            replayed_details = copy.deepcopy(dict(replayed[field_name]))
            if "aggregator_candidates" not in observed_details:
                replayed_details.pop("aggregator_candidates", None)
            if observed_details != replayed_details:
                reasons.append("g1_frozen_ranker_replay_mismatch_thinking_assignment_details")
            continue
        if (
            field_name == "policy_versions"
            and legacy_thinking_trace
            and isinstance(trace.get(field_name), Mapping)
            and isinstance(replayed.get(field_name), Mapping)
        ):
            observed_versions = dict(trace[field_name])
            replayed_versions = dict(replayed[field_name])
            if observed_versions.get("ranking") != LEGACY_THINKING_RANKING_VERSION:
                reasons.append("g1_frozen_ranker_replay_mismatch_policy_versions")
                continue
            observed_versions["ranking"] = replayed_versions.get("ranking")
            if observed_versions != replayed_versions:
                reasons.append("g1_frozen_ranker_replay_mismatch_policy_versions")
            continue
        if (
            field_name in additive_thinking_fields
            and field_name not in trace
            and (legacy_disabled_replay or legacy_thinking_trace)
        ):
            continue
        if trace.get(field_name) != replayed.get(field_name):
            reasons.append(f"g1_frozen_ranker_replay_mismatch_{field_name}")
    return list(dict.fromkeys(reasons))
