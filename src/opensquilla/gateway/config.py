"""GatewayConfig — Pydantic Settings for the gateway."""

from __future__ import annotations

import copy
import ipaddress
import logging
import os
import threading
import warnings
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SerializeAsAny,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from opensquilla import __version__
from opensquilla.gateway.config_migration import (
    LATEST_CONFIG_VERSION,
    ConfigParseError,
    backup_and_write_migrated_config,
    migrate_config_payload,
)
from opensquilla.paths import default_opensquilla_home, native_io_path
from opensquilla.provider.credentials import (
    credential_provider_hint,
    endpoint_provider_hint,
)
from opensquilla.provider.preset_registry import get_preset, legacy_profile_ids
from opensquilla.router_tiers import (
    CUSTOM_B5_MAX_PROPOSERS,
    CUSTOM_B5_MAX_TOTAL_CALLS,
    CUSTOM_B5_MIN_PROPOSERS,
    CUSTOM_B5_SELECTION_MODE,
    DEFAULT_TEXT_TIER,
    ENSEMBLE_CANDIDATE_ROLES,
    LEGACY_OPENROUTER_MODEL_OPTIONS,
    ROUTER_TIER_ENSEMBLE_SELECTION_MODES,
    STATIC_B5_SELECTION_MODE_PROVIDERS,  # noqa: F401 - legacy import surface
    STATIC_B5_SELECTION_MODES,
    STATIC_OPENROUTER_B5_SELECTION_MODE,
    STATIC_TOKENRHYTHM_B5_SELECTION_MODE,  # noqa: F401 - legacy import surface
    TEXT_TIERS,
    EnsembleSelectionMode,
    TierConfig,
    effective_ensemble_selection_mode,
    effective_tier_ensemble_selection_modes,
    normalize_text_tier,
    normalize_tier_mapping,
)
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.search.types import DEFAULT_SEARCH_MAX_RESULTS, MAX_SEARCH_RESULTS
from opensquilla.session.compaction_lifecycle import (
    DEFAULT_FLUSH_TRIGGERS,
    FlushTrigger,
    normalize_flush_triggers_strict,
)

logger = logging.getLogger(__name__)

_LEGACY_CONTROL_UI_FRONTEND_WARNING = (
    "control_ui.frontend='legacy' is deprecated and no longer selects the "
    "retired vanilla-JS UI; Vue is always served. Remove this setting or set "
    "it to 'vue'."
)


class ContextOverflowPolicy(StrEnum):
    """What to do when a turn's effective input size exceeds the budget.

    The default is :attr:`AUTO_SUMMARIZE` so that
    existing deployments degrade gracefully — older history is summarised
    and the turn retried once. ``HARD_TRUNCATE`` drops oldest turns until
    the payload fits. ``REFUSE`` short-circuits the turn with a stable
    error envelope for operators who want explicit backpressure.
    """

    AUTO_SUMMARIZE = "auto_summarize"
    HARD_TRUNCATE = "hard_truncate"
    REFUSE = "refuse"


class AuthConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_AUTH_")

    token: str | None = None
    password: str | None = None
    mode: str = "none"  # none | token | password | trusted-proxy
    trusted_proxy: str | None = None
    token_scopes: list[str] = Field(default_factory=lambda: ["operator.admin"])
    allowed_roles: list[str] = Field(default_factory=lambda: ["operator", "node"])
    # Empty means the built-in loopback/RFC1918/ULA set.  Custom values may
    # narrow that set but can never widen it to public address space.
    allowed_client_cidrs: list[str] = Field(default_factory=list)

    @field_validator("allowed_client_cidrs")
    @classmethod
    def _validate_allowed_client_cidrs(cls, values: list[str]) -> list[str]:
        private_v4 = tuple(
            ipaddress.IPv4Network(value)
            for value in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
        private_v6 = tuple(
            ipaddress.IPv6Network(value)
            for value in ("::1/128", "fc00::/7")
        )
        normalized: list[str] = []
        for raw in values:
            network = ipaddress.ip_network(str(raw).strip(), strict=False)
            allowed = (
                any(network.subnet_of(parent) for parent in private_v4)
                if isinstance(network, ipaddress.IPv4Network)
                else any(network.subnet_of(parent) for parent in private_v6)
            )
            if not allowed:
                raise ValueError(
                    "auth.allowed_client_cidrs may only narrow loopback, "
                    "RFC1918, or IPv6 ULA networks"
                )
            text = network.with_prefixlen
            if text not in normalized:
                normalized.append(text)
        return normalized


class CorsConfig(BaseSettings):
    """Cross-origin resource sharing headers for the gateway's HTTP surface.

    ``allowed_origins`` defaults to empty — no CORS headers are emitted, so
    browsers refuse cross-origin reads. The Web UI is served same-origin from
    the gateway itself and non-browser clients (CLI, desktop app, curl) are
    unaffected, so nothing needs CORS out of the box. Operators hosting a
    separate frontend opt in by listing its exact origins here.
    """

    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_CORS_")

    allowed_origins: list[str] = Field(default_factory=list)
    allow_credentials: bool = True
    allowed_methods: list[str] = Field(default_factory=lambda: ["*"])
    allowed_headers: list[str] = Field(default_factory=lambda: ["*"])


class AttachmentsConfig(BaseSettings):
    """Transcript attachment persistence settings."""

    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_ATTACHMENTS_")

    persist_transcripts: bool = True
    media_root: str | None = None  # default resolved from cache dir at boot
    transcript_disk_budget_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GB
    artifact_max_bytes: int = 30 * 1024 * 1024
    artifact_disk_budget_bytes: int = 512 * 1024 * 1024
    # Admission policy for opaque attachment types (archives, binaries,
    # audio/video, unknown formats). Opaque bytes are never parsed or inlined
    # into a provider prompt — they are staged into the agent workspace for
    # tool access only. False restores the rendered-types-only admission gate.
    accept_opaque: bool = True
    opaque_max_bytes: int = 30 * 1024 * 1024
    # Aggregate RAM ceiling for the in-memory staged-upload store. When
    # reached, new uploads are rejected (HTTP 507 UPLOAD_STORE_FULL) instead
    # of evicting staged entries, preserving the file_uuid TTL promise.
    # Applied at gateway construction; changing it requires a restart.
    upload_store_max_total_bytes: int = 300 * 1024 * 1024
    # Disk budget for attachment copies materialized into an agent workspace
    # (<workspace>/.opensquilla/attachments). When exceeded, new
    # materializations degrade to an unavailable marker; nothing is evicted.
    workspace_attachment_disk_budget_bytes: int = 1024 * 1024 * 1024


class RateLimitConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_RATE_")

    enabled: bool = True
    max_requests: int = 100
    window_seconds: int = 60


class ControlUiConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_CONTROL_UI_")

    enabled: bool = True
    base_path: str = "/control"
    # Retained temporarily so existing TOML files and environment overrides do
    # not fail gateway validation after the vanilla-JS client is retired. Vue
    # is the only runtime value; the validator below maps the historical
    # ``legacy`` spelling to it with a deprecation warning.
    frontend: Literal["vue"] = "vue"
    # Default UI locale served on first paint when the browser has no saved
    # preference, and the Gateway-wide language for fixed channel notices.
    # The client (localStorage) and a manual switch always override it. Anything
    # zh* clamps to zh-Hans; anything else to en.
    default_locale: Literal["en", "zh-Hans", "ja", "fr", "de", "es"] = "en"
    allowed_origins: list[str] = Field(default_factory=list)

    @field_validator("base_path")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        # Keep the root mount explicit.  Returning ``""`` for ``"/"`` makes
        # prefix checks such as ``path.startswith(base_path)`` match every
        # request, including authenticated API routes.
        return v.rstrip("/") or "/"

    @field_validator("frontend", mode="before")
    @classmethod
    def _normalize_frontend(cls, v: object) -> object:
        if isinstance(v, str):
            normalized = v.strip().lower()
            if normalized == "legacy":
                warnings.warn(
                    _LEGACY_CONTROL_UI_FRONTEND_WARNING,
                    DeprecationWarning,
                    stacklevel=2,
                )
                logger.warning(_LEGACY_CONTROL_UI_FRONTEND_WARNING)
                return "vue"
            return normalized
        return v

    @field_validator("default_locale", mode="before")
    @classmethod
    def _normalize_locale(cls, v: object) -> object:
        if isinstance(v, str):
            s = v.strip().lower()
            if s.startswith("zh"):
                return "zh-Hans"
            for code in ("ja", "fr", "de", "es"):
                if s.startswith(code):
                    return code
            return "en"
        return v


class PrivacyConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_PRIVACY_")

    disable_network_observability: bool = False


class SkillsConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_SKILLS_")

    workspace_dir: str | None = None
    managed_dir: str | None = None
    allow_bundled: bool = True
    extra_dirs: list[str] = Field(default_factory=list)
    # Names of skills the operator has turned off (e.g. via the control-UI
    # plugin toggle). A disabled skill is gated out of the agent's view.
    disabled: list[str] = Field(default_factory=list)
    # Coding mode (control-UI toggle). When ON, the agent operates in a
    # locked coding mode: the code-task plugin is available and a directive
    # steers every turn through it. When OFF, code-task is unreachable through
    # every skill API. Default OFF — coding mode is opt-in.
    coding_mode: bool = False
    max_skills_prompt_chars: int = 8000
    filter_enabled: bool = False
    filter_top_k: int = 5
    # "system" = full system prompt (default)
    # "user_context" = ephemeral user-role context, after history and before current user
    # "user_message" = legacy compact system-prompt index
    injection_mode: str = "system"

    # Relevance filtering is opt-in. Keep the default path dependency-free.
    filter_strategy: Literal["lexical", "semantic", "hybrid"] = "lexical"
    filter_lexical_top_n: int = 20
    filter_semantic_top_n: int = 20
    filter_rrf_k: int = 60
    filter_embedding_model: str = "BAAI/bge-small-zh-v1.5"


class ToolsConfig(BaseModel):
    """Top-level runtime tool policy configuration."""

    profile: (
        Literal[
            "full",
            "minimal",
            "memory_only",
            "coding",
            "messaging",
            "repo_coding_source_edit",
            "repo_coding_source_edit_strict",
            "repo_coding_source_edit_v2",
            "repo_coding_source_edit_balanced",
            "repo_coding_source_edit_patch_fallback",
            "repo_coding_scaffold_edit",
            "repo_coding_scaffold_patch",
        ]
        | None
    ) = None
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    also_allow: list[str] = Field(default_factory=list)
    # Model-facing tool description overrides. Keys name a tool
    # ("exec_command") or a parameter ("exec_command.command" — dotted keys
    # must be quoted in TOML); values replace the matching description
    # verbatim. Inert unless the OPENSQUILLA_TOOL_DESCRIPTION_OVERRIDES env
    # var enables them ("config"/"on", or a .toml/.json override file path).
    description_overrides: dict[str, str] = Field(default_factory=dict)
    workspace_write_deny_globs: list[str] = Field(default_factory=list)
    file_edit_requires_fresh_read: bool | None = None
    file_edit_flexible_recovery: bool | None = None
    trusted_fake_ip_cidrs: list[str] = Field(default_factory=list)

    @field_validator("trusted_fake_ip_cidrs")
    @classmethod
    def _validate_trusted_fake_ip_cidrs(cls, values: list[str]) -> list[str]:
        from opensquilla.tools.ssrf import validate_trusted_fake_ip_cidrs

        return validate_trusted_fake_ip_cidrs(values)


class PermissionsConfig(BaseModel):
    """Default owner permission posture for local/operator turns."""

    model_config = ConfigDict(extra="forbid")

    default_mode: Literal["off", "on", "bypass", "full"] = "off"


class TaskRuntimeConfig(BaseModel):
    """Server-side task-runtime queue settings."""

    max_concurrency: int = Field(default=8, ge=1)
    max_pending_per_session: int = Field(default=64, ge=1)
    # Per-channel-adapter in-flight semaphore (separate from
    # task_runtime._global_sem). Configured here so OPENSQUILLA_CHANNEL_INFLIGHT_CAP
    # has a stable env name regardless of channel adapter wiring.
    channel_inflight_cap: int = Field(default=8, ge=1)
    # Hard ceiling on how long a single turn may hold the OUTER per-session
    # lock before the dead-turn breaker fires. ``None`` keeps the historical
    # behaviour (no breaker, jam tolerated).
    turn_hard_deadline_s: float | None = Field(default=None, gt=0)
    # Global default policy when ``max_pending_per_session`` is exceeded.
    # ``reject_newest`` preserves legacy reject-on-overflow. ``drop_oldest``
    # evicts the oldest QUEUED pending task on the session and accepts the
    # new turn — useful for noisy realtime channels where the freshest
    # message matters more than the queued backlog.
    pending_overflow_policy: str = Field(default="reject_newest")
    # Per-channel override map. Keys are channel ids (e.g. ``"feishu"``),
    # values are policy strings.  Channels not listed fall back to
    # ``pending_overflow_policy``. Empty dict by default — no channel is
    # tuned independently.
    pending_overflow_policy_per_channel: dict[str, str] = Field(default_factory=dict)
    # Stream relay coalescing window. Consecutive text deltas inside a single
    # window are concatenated into one chunk before being yielded to the
    # channel adapter's ``send_streaming``. ``0`` (default) preserves the
    # historical one-chunk-per-delta behaviour. Operators tune this for
    # adapters that incur a per-call cost on ``send_streaming`` updates.
    stream_relay_coalesce_ms: float = Field(default=0.0, ge=0)
    # Hard cap on the size of a coalesced chunk. ``0`` (default) keeps the
    # historical behaviour — used together with
    # ``stream_relay_coalesce_ms`` to enable batching.
    stream_relay_coalesce_chars: int = Field(default=0, ge=0)

    @field_validator("pending_overflow_policy")
    @classmethod
    def _validate_overflow_policy(cls, value: str) -> str:
        from opensquilla.gateway.task_runtime import PendingOverflowPolicy

        try:
            PendingOverflowPolicy(value)
        except ValueError as exc:
            valid = ", ".join(member.value for member in PendingOverflowPolicy)
            raise ValueError(
                f"pending_overflow_policy must be one of {{{valid}}}"
            ) from exc
        return value

    @field_validator("pending_overflow_policy_per_channel")
    @classmethod
    def _validate_per_channel_policy(cls, value: dict[str, str]) -> dict[str, str]:
        from opensquilla.gateway.task_runtime import PendingOverflowPolicy

        valid = ", ".join(member.value for member in PendingOverflowPolicy)
        for channel, policy in value.items():
            try:
                PendingOverflowPolicy(policy)
            except ValueError as exc:
                raise ValueError(
                    f"pending_overflow_policy_per_channel[{channel!r}] "
                    f"must be one of {{{valid}}}"
                ) from exc
        return value


# Pre-tokenrhythm built-in defaults. Configs authored while openrouter was
# the built-in default may rely on them without naming a provider, so the
# load-time resolution in ``GatewayConfig._resolve_default_llm_provider``
# restores this trio whenever such a config is detected.
LEGACY_DEFAULT_LLM_PROVIDER = "openrouter"
LEGACY_DEFAULT_LLM_MODEL = "deepseek/deepseek-v4-pro"
LEGACY_DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"
TOKENRHYTHM_DEFAULT_LLM_PROVIDER = "tokenrhythm"
TOKENRHYTHM_DEFAULT_LLM_BASE_URL = "https://tokenrhythm.studio/v1"


class LlmProviderConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_LLM_")

    provider: str = "tokenrhythm"
    model: str = "deepseek-v4-flash-0731"
    api_key: str = ""
    api_key_env: str = ""
    base_url: str = "https://tokenrhythm.studio/v1"
    proxy: str = ""  # explicit HTTP proxy URL (e.g. http://127.0.0.1:7890)
    max_tokens: int = 0  # 0 = auto-resolve from model catalog; >0 = explicit override
    # 0 = auto-resolve from model catalog; >0 = explicit context-window override
    # in tokens. Drives the provider-context budget ladder and context usage
    # reporting for models the catalog does not know (e.g. direct DashScope
    # model ids that never appear in the OpenRouter catalog fetch).
    context_window_tokens: int = 0
    temperature: float | None = None
    top_p: float | None = None
    # Optional global thinking level: off|minimal|low|medium|high|xhigh|adaptive.
    # When unset, squilla_router may suggest thinking for selected tiers.
    # Accepts both "thinking" and "thinking_level" spellings in TOML and env
    # (OPENSQUILLA_LLM_THINKING / OPENSQUILLA_LLM_THINKING_LEVEL) for parity
    # with squilla_router.tiers field names. model_dump emits only "thinking".
    thinking: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "thinking",
            "thinking_level",
            "OPENSQUILLA_LLM_THINKING",
            "OPENSQUILLA_LLM_THINKING_LEVEL",
        ),
    )
    # Explicit provider-request proof budget in characters. 0 = derive from the
    # context-budget ladder (window minus output+thinking reserve, times the
    # overflow threshold). A positive value bypasses that derivation and feeds
    # request-proof projection directly, so operators can size provider payloads
    # for models whose output reserve would otherwise dominate the window.
    provider_request_proof_max_chars: int = 0
    # OpenRouter-only: map model id -> upstream provider name. Mapped models
    # send provider.order=[name] so the provider is preferred without disabling
    # OpenRouter fallback.
    provider_routing: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize_direct_deepseek_model(self) -> LlmProviderConfig:
        if str(self.provider or "").strip().lower() != "deepseek":
            return self
        aliases = {
            "deepseek/deepseek-v4-flash": "deepseek-v4-flash",
            "deepseek/deepseek-v4-pro": "deepseek-v4-pro",
        }
        model = str(self.model or "").strip()
        if model in aliases:
            self.model = aliases[model]
        return self


# Backward-compatible alias for older imports. New configs do not use these as
# defaults; they are only recognized as the old OpenRouter preset payload.
DEFAULT_LLM_ENSEMBLE_MODEL_OPTIONS = LEGACY_OPENROUTER_MODEL_OPTIONS


def _default_llm_ensemble_model_options() -> list[str]:
    """Legacy model_options default is intentionally empty for new configs."""
    return []


# Candidate roles for the custom B5 lineup. "proposer" drafts independently;
# "aggregator" fuses those drafts and produces the final answer.
# Backward-compatible symbol for callers that imported the old gateway table.
LLM_ENSEMBLE_CANDIDATE_ROLES = ENSEMBLE_CANDIDATE_ROLES


class LlmEnsembleCandidateConfig(BaseModel):
    provider: str
    model: str
    source: Literal["custom", "legacy_model_options"] = "custom"
    enabled: bool = True
    # Released advisory aliases and unknown values coerce to "proposer"
    # instead of failing validation, so old or hand-edited configs still boot.
    # Strict role/lineup checks live on the RPC save path (upsert mutation).
    role: str = "proposer"
    # Per-candidate thinking level override: off|minimal|low|medium|high|xhigh.
    # Coerced to "" (inherit from turn config) on invalid input so a hand-edited
    # config never blocks gateway boot, matching the role field policy above.
    thinking_level: str = ""

    @field_validator("provider", "model", mode="before")
    @classmethod
    def _strip_required_text(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("role", mode="before")
    @classmethod
    def _normalize_role(cls, value: object) -> str:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in ENSEMBLE_CANDIDATE_ROLES else "proposer"

    @field_validator("thinking_level", mode="before")
    @classmethod
    def _normalize_thinking_level(cls, value: object) -> str:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return ""
        valid = {"off", "minimal", "low", "medium", "high", "xhigh", "adaptive"}
        return normalized if normalized in valid else ""

    @model_validator(mode="after")
    def _validate_candidate(self) -> LlmEnsembleCandidateConfig:
        if not self.provider:
            raise ValueError("llm_ensemble.candidates.provider must be non-empty")
        if not self.model:
            raise ValueError("llm_ensemble.candidates.model must be non-empty")
        self.provider = self.provider.lower()
        return self


class LlmEnsembleConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPENSQUILLA_LLM_ENSEMBLE_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Model router is the default routing surface. The legacy static selection
    # value remains below for read compatibility, but it is dormant until an
    # operator explicitly enables the ensemble surface.
    enabled: bool = False
    mode: Literal["b5_fusion"] = "b5_fusion"
    selection_mode: EnsembleSelectionMode = cast(
        EnsembleSelectionMode,
        STATIC_OPENROUTER_B5_SELECTION_MODE,
    )
    # Expose tool schemas to proposers as advisory vocabulary only. Proposer
    # output is never dispatched; only the aggregator owns an executable tool
    # boundary.
    proposer_tools: bool = False
    min_successful_proposers: int = Field(default=1, ge=1)
    # Optional quality target above the minimum floor. When set, proposer
    # collection keeps waiting for this many successful drafts; if the target
    # becomes unreachable, the turn may still aggregate once the minimum
    # floor is satisfied.
    target_successful_proposers: int | None = Field(default=None, ge=1)
    # Number of in-place retries after the initial proposer request. Zero
    # preserves the historical single-attempt behavior.
    proposer_max_retries: int = Field(default=0, ge=0, le=10)
    all_failed_policy: Literal["fallback_single", "error"] = "fallback_single"
    model_options: list[str] = Field(default_factory=_default_llm_ensemble_model_options)
    candidates: list[LlmEnsembleCandidateConfig] = Field(default_factory=list)
    candidate_max_chars: int = Field(default=24_000, ge=0)
    proposer_timeout_seconds: float = Field(default=3600.0, gt=0.0)
    aggregator_timeout_seconds: float = Field(default=3600.0, gt=0.0)
    # Deprecated read-compatibility field. The ensemble runtime intentionally
    # ignores it and relies on the per-call proposer/aggregator/fixed-provider
    # idle timeouts instead.
    total_timeout_seconds: float | None = Field(default=None, ge=0.0)
    shuffle_candidates: bool = True
    record_candidates: bool = False

    @model_validator(mode="after")
    def _validate_model_options(self) -> LlmEnsembleConfig:
        model_options: list[str] = []
        seen_options: set[str] = set()
        for model in self.model_options:
            normalized = str(model or "").strip()
            if not normalized or normalized in seen_options:
                continue
            seen_options.add(normalized)
            model_options.append(normalized)
        self.model_options = model_options
        return self

    @model_validator(mode="after")
    def _validate_success_targets(self) -> LlmEnsembleConfig:
        if (
            self.target_successful_proposers is not None
            and self.target_successful_proposers < self.min_successful_proposers
        ):
            raise ValueError(
                "llm_ensemble.target_successful_proposers cannot be lower than "
                "llm_ensemble.min_successful_proposers"
            )
        if (
            self.selection_mode
            in STATIC_B5_SELECTION_MODES
            and self.target_successful_proposers is not None
            and self.target_successful_proposers > 4
        ):
            raise ValueError(
                "llm_ensemble.target_successful_proposers cannot exceed the "
                "static B5 proposer count (4)"
            )
        return self

    @model_validator(mode="after")
    def _validate_custom_b5_lineup(self) -> LlmEnsembleConfig:
        """Bound the explicit custom_b5 lineup.

        Only the custom mode is checked: static profiles carry fixed lineups
        and router_dynamic selects members per turn, so neither can exceed the
        cap. The aggregator is structural — at most one candidate may carry
        the role; the last enabled proposer count must stay within
        [CUSTOM_B5_MIN_PROPOSERS, CUSTOM_B5_MAX_PROPOSERS]. Disabled rows are
        kept (read compatibility) but never counted.
        """
        aggregators = [
            candidate
            for candidate in self.candidates
            if candidate.enabled and candidate.role == "aggregator"
        ]
        if len(aggregators) > 1:
            raise ValueError(
                "llm_ensemble.candidates may mark at most one enabled "
                "candidate with role='aggregator'"
            )
        if self.selection_mode != CUSTOM_B5_SELECTION_MODE:
            return self
        proposers = [
            candidate
            for candidate in self.candidates
            if candidate.enabled and candidate.role != "aggregator"
        ]
        if len(proposers) < CUSTOM_B5_MIN_PROPOSERS:
            raise ValueError(
                "llm_ensemble.selection_mode='custom_b5' needs at least "
                f"{CUSTOM_B5_MIN_PROPOSERS} enabled proposer candidates"
            )
        if len(proposers) > CUSTOM_B5_MAX_PROPOSERS:
            raise ValueError(
                "llm_ensemble.selection_mode='custom_b5' allows at most "
                f"{CUSTOM_B5_MAX_PROPOSERS} enabled proposer candidates"
            )
        # Total per-turn call ceiling (proposers + aggregator). Today k is
        # fixed at 1 per member; the ceiling still guards a future k surface.
        if len(proposers) + 1 > CUSTOM_B5_MAX_TOTAL_CALLS:
            raise ValueError(
                "llm_ensemble custom_b5 lineup exceeds "
                f"{CUSTOM_B5_MAX_TOTAL_CALLS} total per-turn calls"
            )
        if self.min_successful_proposers > len(proposers):
            raise ValueError(
                "llm_ensemble.min_successful_proposers cannot exceed the "
                f"custom_b5 proposer count ({len(proposers)})"
            )
        if (
            self.target_successful_proposers is not None
            and self.target_successful_proposers > len(proposers)
        ):
            raise ValueError(
                "llm_ensemble.target_successful_proposers cannot exceed the "
                f"custom_b5 proposer count ({len(proposers)})"
            )
        return self


STATIC_OPENROUTER_B5_MIN_AGENT_STREAM_IDLE_TIMEOUT_SECONDS = 1200.0
STATIC_OPENROUTER_B5_MIN_WEBUI_STREAM_IDLE_GRACE_SECONDS = 1260.0


def _non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, parsed)


def _configured_static_b5_selection_modes(config: Any) -> tuple[str, ...]:
    modes: list[str] = []
    ensemble_cfg = getattr(config, "llm_ensemble", None)
    global_mode = effective_ensemble_selection_mode(config)
    if bool(getattr(ensemble_cfg, "enabled", False)) and global_mode in STATIC_B5_SELECTION_MODES:
        modes.append(global_mode)

    router = getattr(config, "squilla_router", None)
    if bool(getattr(router, "enabled", False)):
        tier_modes = effective_tier_ensemble_selection_modes(
            getattr(router, "tiers", None),
            shared_selection_mode=global_mode,
        )
        modes.extend(
            mode for mode in tier_modes.values() if mode in STATIC_B5_SELECTION_MODES
        )
    return tuple(dict.fromkeys(modes))


def static_b5_ensemble_enabled(config: Any) -> bool:
    """Whether global or tier-managed configuration can run static B5."""

    return bool(_configured_static_b5_selection_modes(config))


def static_b5_ensemble_active(config: Any) -> bool:
    """True when a static-B5 profile is enabled *and* resolves a credential.

    The stream-idle floors below exist for the real (slow) static-B5
    ensembles. A keyless install can never run those members — the wrap is
    skipped at turn time — so it keeps the default hang-detection budgets.
    The credential check is the shared ensemble-side helper (lazy import;
    ``provider`` never imports from ``gateway``, so no cycle) and therefore
    cannot disagree with the turn-time wrap guard.
    """
    selection_modes = _configured_static_b5_selection_modes(config)
    if not selection_modes:
        return False
    from opensquilla.provider.ensemble import static_b5_credential_available

    return any(
        static_b5_credential_available(
            config,
            getattr(config, "llm", None),
            selection_mode,
        )
        for selection_mode in selection_modes
    )


def _global_static_b5_ensemble_active(config: Any) -> bool:
    """Whether the global ensemble surface will run a static-B5 profile.

    Tier-managed fusion only affects turns routed to that tier, so it must not
    inflate the gateway-wide stream-idle budget for every other request.
    """

    ensemble_cfg = getattr(config, "llm_ensemble", None)
    selection_mode = str(getattr(ensemble_cfg, "selection_mode", "") or "")
    if not bool(getattr(ensemble_cfg, "enabled", False)):
        return False
    if selection_mode not in STATIC_B5_SELECTION_MODES:
        return False
    from opensquilla.provider.ensemble import static_b5_credential_available

    return static_b5_credential_available(
        config,
        getattr(config, "llm", None),
        selection_mode,
    )


def effective_agent_stream_idle_timeout_seconds(config: Any) -> float:
    value = _non_negative_float(
        getattr(config, "agent_stream_idle_timeout_seconds", 600.0),
        600.0,
    )
    if _global_static_b5_ensemble_active(config):
        value = max(value, STATIC_OPENROUTER_B5_MIN_AGENT_STREAM_IDLE_TIMEOUT_SECONDS)
    return value


def effective_webui_stream_idle_grace_seconds(config: Any) -> float:
    value = _non_negative_float(
        getattr(config, "webui_stream_idle_grace_seconds", 630.0),
        630.0,
    )
    if _global_static_b5_ensemble_active(config):
        server_idle = effective_agent_stream_idle_timeout_seconds(config)
        value = max(
            value,
            STATIC_OPENROUTER_B5_MIN_WEBUI_STREAM_IDLE_GRACE_SECONDS,
            server_idle + 60.0,
        )
    return value


# Module-level dedupe state for the legacy ``enabled`` deprecation warning.
# A plain ``bool`` flag guarded by a ``Lock`` makes the check-and-set atomic
# across concurrent constructors; ``threading.Event`` is *not* atomic for
# the test-then-set pattern (two threads can both observe is_set()==False
# before either calls set()), which would emit duplicate warnings.
_LEGACY_ENABLED_WARN_LOCK = threading.Lock()
_LEGACY_ENABLED_WARNED = False

# Pydantic-style truthy/falsy string sets (case-insensitive). Mirrors the
# loose ``bool`` validator semantics so the migrated ``enabled`` key behaves
# the way pydantic-settings v2 would have validated it before the field was
# removed.
_TRUTHY_STRINGS = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSY_STRINGS = frozenset({"0", "false", "no", "off", "n", "f"})


def _coerce_legacy_enabled(value: Any) -> bool:
    """Strict bool coercion for the deprecated ``enabled`` legacy key.

    Matches pydantic v2 loose-bool semantics for strings (case-insensitive
    accept of ``{1, true, y, yes, on, t}`` / ``{0, false, n, no, off, f}``)
    and ints ``0``/``1``. Any other value raises ``ValueError`` so invalid
    inputs (e.g. ``"maybe"``) surface as a ``ValidationError`` rather than
    being silently mapped to ``mode="off"``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUTHY_STRINGS:
            return True
        if normalized in _FALSY_STRINGS:
            return False
    elif isinstance(value, int):
        # ``bool`` is a subclass of ``int`` so it was handled above; only the
        # unambiguous 0/1 ints match pydantic loose bool.
        if value == 0:
            return False
        if value == 1:
            return True
    raise ValueError(f"prompt_cache.enabled: cannot coerce {value!r} to bool")


class PromptCacheConfig(BaseSettings):
    # ``env_prefix`` stays so ``OPENSQUILLA_CACHE_MODE`` continues to bind the
    # ``mode`` field. The legacy ``OPENSQUILLA_CACHE_ENABLED`` env var is no
    # longer a field — it is probed explicitly in ``__init__`` below and
    # routed through the legacy migration validator, because pydantic-
    # settings only surfaces env keys that correspond to declared fields
    # to ``model_validator(mode='before')``.
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_CACHE_")

    mode: Literal["off", "auto", "on"] = "auto"

    def __init__(self, **data: Any) -> None:
        # Surface the legacy ``OPENSQUILLA_CACHE_ENABLED`` env var to the
        # before-validator. Without this probe the env var would be
        # silently dropped after the field was removed from the model.
        if "enabled" not in data:
            legacy_env = os.environ.get("OPENSQUILLA_CACHE_ENABLED")
            if legacy_env is not None and legacy_env != "":
                data["enabled"] = legacy_env
        super().__init__(**data)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_enabled(cls, data: Any) -> Any:
        """Map the deprecated ``enabled`` key onto ``mode`` (one warn/proc)."""
        if not isinstance(data, dict):
            return data
        if "enabled" in data and "mode" not in data:
            legacy = _coerce_legacy_enabled(data.pop("enabled"))
            data["mode"] = "on" if legacy else "off"
            # Atomic check-and-set under the lock so concurrent constructors
            # cannot both win the dedupe race. The actual ``warnings.warn``
            # call is performed *outside* the lock to avoid holding it across
            # user-supplied warning filters/handlers (which could deadlock or
            # be slow).
            global _LEGACY_ENABLED_WARNED
            with _LEGACY_ENABLED_WARN_LOCK:
                should_warn = not _LEGACY_ENABLED_WARNED
                if should_warn:
                    _LEGACY_ENABLED_WARNED = True
            if should_warn:
                warnings.warn(
                    f"prompt_cache.enabled is deprecated; use prompt_cache.mode "
                    f"({{off|auto|on}}). Mapped enabled={legacy!r} -> "
                    f"mode={data['mode']!r}. Removal target: 0.next+2.",
                    DeprecationWarning,
                    stacklevel=2,
                )
        elif "enabled" in data:
            # Explicit ``mode`` wins; drop legacy silently.
            data.pop("enabled")
        return data

    @property
    def effective_mode(self) -> Literal["off", "auto", "on"]:
        """Return the product-facing prompt-cache mode.

        ``mode`` is the single source of truth; legacy ``enabled`` keys
        are migrated by ``_migrate_legacy_enabled`` before they reach
        this property.
        """
        return self.mode


class DreamConfig(BaseModel):
    """Per-agent Dream consolidation cron configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval_h: int = Field(default=24, ge=1)
    cron: str | None = None  # e.g. "0 3 * * *"; overrides interval_h when set
    max_batch_size: int = Field(default=20, ge=1)
    max_iterations: int = Field(default=15, ge=1)
    min_batch_size: int = Field(default=1, ge=1)
    preview_mode: bool = True
    auto_schedule: bool = False
    input_slimming: Literal["off", "shadow", "on"] = "off"
    memory_max_chars: int = Field(default=12_000, ge=0)
    candidate_file_max_chars: int = Field(default=4_000, ge=0)
    candidate_total_max_chars: int = Field(default=24_000, ge=0)
    fallback_total_max_chars: int = Field(default=80_000, ge=0)
    evidence_min_score: float = Field(default=0.55, ge=0.0, le=1.0)
    evidence_min_seen_count: int = Field(default=1, ge=1)
    evidence_negative_recurrence_threshold: int = Field(default=2, ge=1)
    evidence_curated_writes_enabled: bool = True
    evidence_quarantine_enabled: bool = True


class SafetyConfig(BaseModel):
    """Prompt-ingress safety controls."""

    wrap_untrusted_workspace: bool = True
    injection_scan_mode: Literal["off", "report", "enforce"] = "report"


class PromptConfig(BaseModel):
    """Prompt-layer feature flags."""

    mode: Literal[
        "auto",
        "full",
        "minimal",
        "none",
        "headless_source_edit",
        "headless_repo_coding_scaffold",
    ] = "auto"
    platform_hint_enabled: bool = True
    # Opt-in additive "Patch Evidence Protocol" system-prompt section for
    # repo-coding/patching sessions. Overridable per run via the
    # OPENSQUILLA_PATCH_EVIDENCE_PROTOCOL env var ("on"/"off").
    patch_evidence_protocol: bool = False
    # Opt-in additive "Reproduction Evidence" system-prompt section plus the
    # loop-side finalize-time red-evidence gate (engine.finalize_evidence_gate).
    # Overridable per run via the OPENSQUILLA_FINALIZE_EVIDENCE_GATE env var
    # ("on"/"off").
    finalize_evidence_gate: bool = False
    # Opt-in switch restoring the earlier compact "Tool Call Style" and
    # "Reply Guidelines" system-prompt directives (single-line narration,
    # concise replies) for deployments tuned against the previous wording.
    # Overridable per run via the OPENSQUILLA_LEGACY_PROMPT_STYLE env var
    # ("on"/"off"). Off keeps the current wording unchanged.
    legacy_prompt_style: bool = False


MemoryEmbeddingProvider = Literal[
    "auto",
    "none",
    "local",
    "openai",
    "openai-compatible",
    "ollama",
]


class MemoryEmbeddingLocalConfig(BaseModel):
    """Local memory embedding settings."""

    onnx_dir: str | None = None


class MemoryEmbeddingRemoteConfig(BaseModel):
    """OpenAI-compatible remote memory embedding settings."""

    api_key: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    model: str | None = None
    dimensions: int | None = Field(default=None, ge=1)


class MemoryEmbeddingOllamaConfig(BaseModel):
    """Ollama memory embedding settings."""

    base_url: str | None = None
    model: str | None = None


class MemoryEmbeddingConfig(BaseModel):
    """Embedding provider selection for the stable memory search index.

    ``provider`` is the canonical field. ``mode`` and the flat
    ``api_key``/``base_url``/``model`` fields remain for older configs.
    Concrete ``provider`` values win over legacy ``mode``. The default
    ``provider="auto"`` still honors legacy ``mode`` so old configs keep
    round-tripping safely.
    """

    provider: MemoryEmbeddingProvider = "auto"
    mode: MemoryEmbeddingProvider | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    local: MemoryEmbeddingLocalConfig = Field(default_factory=MemoryEmbeddingLocalConfig)
    remote: MemoryEmbeddingRemoteConfig = Field(default_factory=MemoryEmbeddingRemoteConfig)
    ollama: MemoryEmbeddingOllamaConfig = Field(default_factory=MemoryEmbeddingOllamaConfig)

    @property
    def requested_provider(self) -> MemoryEmbeddingProvider:
        if self.provider == "auto" and self.mode:
            return self.mode
        return self.provider


class MemoryCostConfig(BaseModel):
    """Stable memory implementation cost knobs."""

    model_config = ConfigDict(extra="forbid")

    query_embedding_cache: Literal["off", "shadow", "on"] = "on"


class MemoryConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPENSQUILLA_MEMORY_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    cost: MemoryCostConfig = Field(default_factory=MemoryCostConfig)

    # Markdown memory source location: "state" keeps internal state layout;
    # "workspace" stores MEMORY.md and memory/*.md under the active agent workspace.
    source: Literal["state", "workspace"] = "workspace"
    retrieval_mode: Literal["hybrid", "fts_only"] = "hybrid"
    embedding: MemoryEmbeddingConfig = Field(default_factory=MemoryEmbeddingConfig)
    sync_interval_minutes: float = Field(default=0.0, ge=0.0)
    session_source_enabled: bool = False

    # Passive injection
    inject_limit: int = 4000  # max chars for passive memory injection into system prompt

    # Size limits (0 = disabled)
    max_file_size_kb: int = 1024  # 1 MB per file
    max_total_size_kb: int = 102400  # 100 MB total
    max_files: int = 500  # max number of memory files

    # TTL (0 = disabled, no auto-prune)
    entry_ttl_days: int = 0
    # Background TTL sweep cadence (minutes). Set to 0 to opt out of
    # background sweep while keeping in-line TTL on memory_save. No-op
    # when entry_ttl_days = 0.
    ttl_sweep_interval_minutes: float = Field(default=60.0, ge=0.0)

    # Flush (pre-compaction memory save)
    flush_enabled: bool = False
    flush_triggers: list[FlushTrigger] = Field(
        default_factory=lambda: list(DEFAULT_FLUSH_TRIGGERS)
    )
    flush_pre_compaction: bool = False
    flush_timeout_seconds: float = 15.0
    flush_background_timeout_seconds: float = 120.0
    flush_backoff_initial_seconds: float = 30.0
    flush_backoff_max_seconds: float = 300.0
    flush_archive_max_bytes: int = 800_000
    flush_compaction_requires_safe_receipt: bool = False
    flush_compaction_safety_mode: Literal["protect", "best_effort", "block", "off"] = "protect"
    repair_enabled: bool = True
    repair_interval_seconds: float = Field(default=60.0, ge=0.0)
    repair_max_items_per_tick: int = Field(default=5, ge=1)

    @field_validator("flush_triggers", mode="before")
    @classmethod
    def _normalize_flush_triggers(cls, value: object) -> list[FlushTrigger]:
        return list(normalize_flush_triggers_strict(value))

    # Per-turn auto capture / recall
    auto_capture_enabled: bool = True
    capture_mode: Literal["turn_pair", "off"] = "turn_pair"
    capture_user: bool = True
    capture_assistant: bool = False
    capture_excluded_run_kinds: list[str] = Field(
        default_factory=lambda: ["recall", "session_recall"]
    )
    capture_excluded_provenance_kinds: list[str] = Field(
        default_factory=lambda: ["recall", "tool_result", "memory_injected", "internal_system"]
    )
    capture_max_chars: int = 2000
    capture_roll_max_chars: int = Field(default=50_000, ge=0)
    daily_note_max_chars: int = Field(default=4000, ge=0)
    daily_notes_total_max_chars: int = Field(default=8000, ge=0)

    # Retriever tuning
    temporal_decay_enabled: bool = False
    temporal_decay_half_life_days: float = 30.0
    mmr_enabled: bool = False
    mmr_lambda: float = 0.7
    vector_weight: float = 0.7
    text_weight: float = 0.3

    # Dream consolidation
    dream: DreamConfig = Field(default_factory=DreamConfig)


def _default_tiers() -> dict:
    """Default model routing config (the packaged openrouter preset)."""
    return _router_tier_profile_defaults("openrouter")


# Accepted (persistable) squilla_router.tier_profile ids. Derived from the
# packaged preset registry's non-synthesized ids, with an internal equality
# check against the pinned legacy-nine literal (see preset_registry). Synthesized
# presets are deliberately EXCLUDED: an rc1 gateway bricks on an unknown
# tier_profile, so the accepted set stays pinned to the legacy nine (downgrade
# contract). Kept as a module-level frozenset for compat with existing importers.
ROUTER_TIER_PROFILE_IDS = legacy_profile_ids()


def _merge_tier_dicts(defaults: dict, overrides: object) -> dict:
    merged = {name: dict(value) for name, value in defaults.items()}
    if not overrides:
        return merged
    if not isinstance(overrides, dict):
        return merged
    for tier_name, override in overrides.items():
        if isinstance(override, dict) and isinstance(merged.get(tier_name), dict):
            tier = dict(merged[tier_name])
            tier.update(override)
            merged[tier_name] = tier
        else:
            merged[tier_name] = override
    return merged


def _router_tier_profile_defaults(profile: str | None) -> dict:
    """Effective tier defaults for a legacy tier_profile id.

    Thin adapter over the packaged preset registry. Membership stays pinned
    to the legacy nine ids (ROUTER_TIER_PROFILE_IDS): synthesized presets
    exist in the registry but are rejected here, because a persisted
    tier_profile outside the legacy set bricks rc1 loaders on downgrade.
    """
    normalized = (profile or "openrouter").strip().lower()
    if normalized not in ROUTER_TIER_PROFILE_IDS:
        allowed = ", ".join(sorted(ROUTER_TIER_PROFILE_IDS))
        raise ValueError(
            f"unknown squilla_router.tier_profile {profile!r}; expected one of {allowed}"
        )
    preset = get_preset(normalized)
    if preset is None or preset.synthesized:  # pragma: no cover - packaging drift guard
        allowed = ", ".join(sorted(ROUTER_TIER_PROFILE_IDS))
        raise ValueError(
            f"unknown squilla_router.tier_profile {profile!r}; expected one of {allowed}"
        )
    return preset.tier_defaults()


class RouterBudgetConfig(BaseModel):
    """Additive, opt-in per-session spend gate for the router.

    Default state is a complete no-op: with no ``limit_usd`` set (or
    ``action = "off"``) the routing policy's budget stage never runs, so the
    chosen tier is byte-identical to a build without this block (the routing
    parity golden pins that). The gate READS the session's already-accumulated
    billed/estimated spend — it never recomputes cost math — and, when that
    spend crosses ``limit_usd``, takes ``action``:

    - ``"warn"`` (the default action) — annotate routing metadata + emit a
      ``router_budget.warn`` log, but leave the routed tier UNCHANGED.
    - ``"cap"`` — lower the routed tier to ``cap_tier`` (or the router
      ``default_tier`` when unset), never raising it.
    - ``"off"`` — disable the gate regardless of ``limit_usd``.

    When the accumulated spend (or a required forward price) cannot be
    determined, the gate SUSPENDS (a no-op) rather than acting on missing data.

    ``extra="ignore"`` keeps the block downgrade-tolerant: an older loader
    reading a config that carries it simply drops it and falls back to today's
    un-gated routing. Nothing that activates the gate is persisted while it is
    unset — ``limit_usd``/``cap_tier`` default to ``None`` and drop out of the
    ``exclude_none`` TOML dump.
    """

    model_config = ConfigDict(extra="ignore")

    action: Literal["off", "warn", "cap"] = "warn"
    # Per-session ceiling in USD. ``None`` (or a non-positive value) disables
    # the gate — this is the default, byte-identical no-op state.
    limit_usd: float | None = None
    # Cap target for ``action = "cap"``. ``None`` falls back to the router's
    # ``default_tier`` at gather time. Normalized through router_tiers.
    cap_tier: str | None = None
    # Opt-in forward projection: add an estimate of the next turn's marginal
    # input cost to the accumulated spend before comparing to ``limit_usd``.
    # When the estimate cannot be determined the gate SUSPENDS.
    include_next_turn_estimate: bool = False


class RouterSelfLearningConfig(BaseModel):
    """Squilla Router self-learning loop (capture + offline retrain).

    Opt-in. ``enabled`` is the master switch; capture and training each have
    their own sub-toggle so an operator can collect data without yet training.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False  # master switch; off => zero overhead on the hot path
    capture_enabled: bool = True  # gates inference-time feature emission
    enable_mlp: bool = False  # also store raw_bge_1536 for MLP fine-tune (phase 2)
    store_audit_summary: bool = False  # opt-in redacted summary, audit only
    # Trigger gating (evaluated cheaply on each post-dream hook).
    train_min_samples: int = Field(default=200, ge=1)
    idle_hours: float = Field(default=2.0, ge=0.0)
    cooldown_hours: float = Field(default=72.0, ge=0.0)
    retention_days: int = Field(default=30, ge=1)
    # Training (LightGBM incremental) — consumed by the offline trainer/worker.
    num_boost_round: int = Field(default=60, ge=1)
    train_timeout_seconds: float = Field(default=900.0, gt=0.0)
    # Promotion / rollback.
    auto_rollback: bool = True
    golden_eval_path: str | None = None
    cost_tolerance_pct: float = Field(default=5.0, ge=0.0)
    max_critical_under_routing: float = Field(default=0.30, ge=0.0, le=1.0)
    min_golden_agreement: float = Field(default=0.5, ge=0.0, le=1.0)
    # Online rollback monitor (M4).
    min_monitor_samples: int = Field(default=30, ge=1)
    complaint_regression_delta: float = Field(default=0.05, ge=0.0, le=1.0)
    # Second rollback trigger: explicit down-vote-rate regression (F7).
    # Feedback is far sparser than samples, hence its own minimum and a
    # wider delta than the complaint monitor.
    min_feedback_monitor_samples: int = Field(default=5, ge=1)
    downvote_regression_delta: float = Field(default=0.15, ge=0.0, le=1.0)
    # Rolling holdout (per-agent progress metric).
    holdout_pct: float = Field(default=0.10, ge=0.0, le=0.5)
    holdout_repeats: int = Field(default=5, ge=1)
    holdout_min_size: int = Field(default=30, ge=1)
    holdout_granularity: Literal["session", "alignment_group"] = "session"


# Resolve this model's own forward refs (Literal) before it is nested below, so
# rebuilding the parent does not leave it "not fully defined" under the
# unregistered-module exec path used by tests. See the rebuild note below.
RouterSelfLearningConfig.model_rebuild()


class SquillaRouterConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPENSQUILLA_SQUILLA_ROUTER_",
        extra="ignore",  # tolerate removed legacy router fields in old configs
    )

    enabled: bool = True
    auto_thinking: bool = True
    rollout_phase: str = "full"  # "observe" | "prompt_only" | "full"
    strategy: str = "v4_phase3"
    tier_profile: str | None = None
    # Explicit ownership for the router ladder.  ``follow_primary`` means
    # OpenSquilla owns the ladder and may replace it with the active
    # provider's preset when the primary provider changes.  ``custom`` means
    # the operator owns the ladder and provider switches must preserve it.
    # ``None`` is deliberately retained for pre-field configs: treating an
    # unclassified historical ladder as custom is the only non-destructive
    # upgrade behavior.  A later explicit Router save records one of the two
    # concrete values.  Additive and downgrade-safe because this settings
    # section ignores unknown fields in older gateways.
    preset_binding: Literal["follow_primary", "custom"] | None = None
    visual_mode: str = "real_candidates"
    # Preview: execute router tiers whose provider differs from llm.provider,
    # resolving credentials from [llm_profiles.<id>] or the provider's env
    # key. Off: such tiers run on the active provider (with a logged
    # mismatch warning), preserving the historical behavior.
    cross_provider_tiers: bool = False
    # What routing does when it lands on a tier naming a provider other than
    # llm.provider while cross_provider_tiers is off. "route" preserves the
    # historical, documented-intentional behavior: flag the mismatch loudly
    # (warning log + router_tier_provider_mismatch metadata) but still run
    # the tier's model id on the active provider's credentials. "veto"
    # rebinds the turn to the nearest tier that executes on the active
    # provider (or the default tier), recording the veto in the routing
    # trail; without a usable rebind target it falls back to route-and-flag.
    # Downgrade note: additive key. This section is extra="ignore", so an
    # rc1 loader reading a config that carries this key simply drops it and
    # falls back to the historical route-and-flag behavior — persisting it
    # never bricks a downgraded gateway.
    tier_provider_mismatch: Literal["route", "veto"] = "route"
    tiers: dict = Field(default_factory=_default_tiers)
    default_tier: str = DEFAULT_TEXT_TIER
    confidence_threshold: float = 0.5
    confidence_high_tier_margin: float = Field(default=0.05, ge=0.0)
    v4_bundle_dir: str | None = None  # V4 Phase 3 bundle root; defaults to bundled assets
    v4_use_aux_head: bool | None = True  # override router.runtime.yaml aux head when set
    routing_timeout_seconds: float = Field(default=5.0, gt=0.0)
    kv_cache_anti_downgrade_enabled: bool = True
    kv_cache_anti_downgrade_window_seconds: int = 600
    complaint_upgrade_enabled: bool = True
    complaint_upgrade_steps: int = 1
    complaint_upgrade_max_chars: int = 160
    require_router_runtime: bool = True
    # Days router decision records (V017 router_decisions) are retained in
    # the session DB before the writer's write-time opportunistic pruning
    # deletes them. Additive key: the class-level extra="ignore" keeps old
    # builds rollback-tolerant when this key is present in config files.
    decision_retention_days: int = Field(default=30, ge=1)
    # Opt-in on-device router calibration. When true, the routing policy applies
    # the hard-clamped adjustment in <state>/router_calibration.json as a bias
    # on the confidence gate, and the gateway runs a 24h in-process calibration
    # job over local decision records. Default-off: the confidence gate stays
    # byte-identical to today and no calibration job is scheduled. Additive key
    # (class extra="ignore" keeps old builds rollback-tolerant). Never touches
    # the router savings/cost math.
    calibration_enabled: bool = False
    # Opt-in per-session spend gate. Default (no limit) is a complete no-op:
    # the routing policy's budget stage never runs, so the chosen tier stays
    # byte-identical to today. Reads accumulated session spend; never
    # recomputes cost. Additive key (class extra="ignore" keeps old builds
    # rollback-tolerant).
    budget: RouterBudgetConfig = Field(default_factory=RouterBudgetConfig)
    estimated_output_savings_pct: float = 0.03
    upgrade_to_c3_compaction_enabled: bool = True
    self_learning: RouterSelfLearningConfig = Field(default_factory=RouterSelfLearningConfig)
    vision_history_lookback_turns: int = Field(default=8, ge=0)
    vision_history_candidate_turns: int = Field(default=8, ge=0)
    vision_sticky_followup_turns: int = Field(default=3, ge=0)
    vision_followup_gate_enabled: bool = True
    vision_followup_gate_tier: str = "c0"
    vision_followup_gate_model: str | None = None
    vision_followup_gate_timeout_seconds: float = Field(default=10.0, ge=0.1)
    vision_followup_gate_max_output_tokens: int = Field(default=512, ge=16)
    vision_followup_gate_fallback_recent_turns: int = Field(default=2, ge=0)
    vision_followup_gate_unknown_policy: str = "image_if_recent"

    @field_validator("visual_mode", mode="before")
    @classmethod
    def _normalize_visual_mode(cls, value: Any) -> str:
        raw = "real_candidates" if value is None else str(value).strip().lower()
        normalized = raw.replace("-", "_")
        if normalized in {"", "real_candidates", "candidates"}:
            return "real_candidates"
        if normalized in {"legacy_grid", "model_space", "modelspace"}:
            return "legacy_grid"
        raise ValueError("visual_mode must be one of: real_candidates, legacy_grid")

    @model_validator(mode="before")
    @classmethod
    def _resolve_tier_profile_defaults(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        values = dict(values)
        if (
            "upgrade_to_c3_compaction_enabled" not in values
            and "upgrade_to_t3_compaction_enabled" in values
        ):
            values["upgrade_to_c3_compaction_enabled"] = values[
                "upgrade_to_t3_compaction_enabled"
            ]
        if "default_tier" in values:
            values["default_tier"] = normalize_text_tier(values.get("default_tier")) or values.get(
                "default_tier"
            )
        if isinstance(values.get("tiers"), dict):
            values["tiers"] = normalize_tier_mapping(values["tiers"])
        profile = values.get("tier_profile")
        if profile is None:
            return values
        if (
            "tiers" in values
            and values["tiers"] is not None
            and not isinstance(values["tiers"], dict)
        ):
            raise ValueError(
                "squilla_router.tiers must be a mapping when squilla_router.tier_profile is set"
            )
        normalized = str(profile).strip().lower()
        defaults = _router_tier_profile_defaults(normalized)
        merged = _merge_tier_dicts(defaults, values.get("tiers"))
        next_values = dict(values)
        next_values["tier_profile"] = normalized
        next_values["tiers"] = merged
        return next_values

    @model_validator(mode="after")
    def _validate_tier_ensemble_selection_modes(self) -> SquillaRouterConfig:
        allowed = ", ".join(sorted(ROUTER_TIER_ENSEMBLE_SELECTION_MODES))
        for tier_name in TEXT_TIERS:
            raw_tier = self.tiers.get(tier_name) if isinstance(self.tiers, dict) else None
            if isinstance(raw_tier, dict):
                raw_enabled = raw_tier.get(
                    "ensemble_enabled",
                    raw_tier.get("ensembleEnabled"),
                )
                if raw_enabled is not None and not isinstance(raw_enabled, bool):
                    raise ValueError(
                        f"squilla_router.tiers.{tier_name}.ensemble_enabled "
                        "must be a boolean"
                    )
            selection_mode = TierConfig.from_value(raw_tier).ensemble_selection_mode
            if selection_mode and selection_mode not in ROUTER_TIER_ENSEMBLE_SELECTION_MODES:
                raise ValueError(
                    f"squilla_router.tiers.{tier_name}.ensemble_selection_mode "
                    f"must be one of: {allowed}"
                )
        return self


# Eagerly resolve the ``self_learning: RouterSelfLearningConfig`` forward ref
# (``from __future__ import annotations`` makes it a string). Without this the
# model stays "not fully defined" when this file is exec'd as an unregistered
# module (e.g. tests load it via spec_from_file_location), since pydantic falls
# back to ``sys.modules[__module__]`` which is absent in that scenario.
SquillaRouterConfig.model_rebuild()


class AgentTokenSavingConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_AGENT_TOKEN_SAVING_")

    # Tokenjuice projection is the default tool-result path.
    tool_result_projection_max_inline_chars: int = Field(default=60_000, ge=1000)
    tool_result_fresh_diagnostic_policy_enabled: bool = Field(default=False)
    tool_result_diagnostic_retrieval_gate_enabled: bool = Field(default=False)
    tool_result_fresh_diagnostic_inline_max_chars: int = Field(default=64_000, ge=0)
    tool_result_dispatch_max_chars: int = Field(default=0, ge=0)
    tool_result_dispatch_turn_max_chars: int = Field(default=0, ge=0)
    tool_result_store_full_trace: bool = Field(default=False)
    tool_result_store_max_bytes: int = Field(default=8 * 1024 * 1024, ge=0)
    tool_result_store_disk_budget_bytes: int = Field(default=256 * 1024 * 1024, ge=0)
    tool_result_store_retention_seconds: int = Field(default=7 * 24 * 60 * 60, ge=0)


class CompactionLlmConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_COMPACTION_")

    # A provider is meaningful only together with ``model``.  Model-only
    # remains the backwards-compatible "override the selected provider's
    # model" form; a complete pair selects an explicit physical deployment.
    provider: str | None = None
    model: str | None = None  # None = use session model
    timeout_seconds: float = 90.0
    # Absolute budget shared by all summarization chunks and fallbacks.  The
    # durable commit has separate bounded SQLite semantics and is reconciled if
    # cancellation races with commit completion.
    total_timeout_seconds: float = Field(default=120.0, gt=0.0)
    heartbeat_interval_seconds: float = Field(default=15.0, gt=0.0)
    enabled: bool = True
    compaction_profile: Literal["conversation", "coding", "research", "support"] = "conversation"
    protected_recent_messages: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _normalize_explicit_deployment(self) -> CompactionLlmConfig:
        self.provider = str(self.provider or "").strip() or None
        self.model = str(self.model or "").strip() or None
        if self.provider and not self.model:
            logger.warning(
                "Ignoring compaction.provider=%s because compaction.model is not set",
                self.provider,
            )
            self.provider = None
        return self


def validate_compaction_deployment_write(payload: dict[str, Any]) -> None:
    """Reject newly saved provider-only compaction deployments.

    Load-time validation remains deliberately tolerant so an older hand-written
    config cannot brick gateway startup. Config writers call this stricter
    validator on their post-mutation payload before persistence.
    """

    compaction = payload.get("compaction")
    if not isinstance(compaction, dict):
        return
    provider = str(compaction.get("provider") or "").strip()
    model = str(compaction.get("model") or "").strip()
    if provider and not model:
        raise ValueError(
            "compaction.provider requires compaction.model when saving config"
        )


class SessionNamingConfig(BaseSettings):
    """LLM-generated session titles (auto-naming).

    After the first user message, a one-shot LLM call summarizes it into a short
    title written to SessionNode.derived_title. Explicit ``model`` and ``tier``
    settings override the mode-specific default: direct routing follows the
    resolved session/provider model, while Router and Ensemble use the router's
    default text tier.
    """

    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_NAMING_")

    enabled: bool = True
    # Surfaces eligible for auto-naming. webchat/cli are chat; channel covers
    # inbound channel conversations. cron/subagent intentionally excluded.
    surfaces: list[str] = Field(default_factory=lambda: ["webchat", "cli", "channel"])
    tier: str | None = None  # None = use the routing mode's default target
    model: str | None = None  # None = use the explicit tier or mode default
    timeout_seconds: float = 30.0
    max_chars: int = Field(default=48, ge=8)
    language: str = "auto"  # follow the conversation language


class MCPServerEntry(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_MCP_SERVER_")

    name: str = ""
    transport: str = "stdio"  # "stdio" | "sse"
    command: str | None = None  # for stdio
    args: list[str] = Field(default_factory=list)  # for stdio
    url: str | None = None  # for sse
    env: dict[str, str] = Field(default_factory=dict)
    tool_timeout_seconds: float = 30.0


class MCPConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_MCP_")

    enabled: bool = False
    servers: list[MCPServerEntry] = Field(default_factory=list)
    connect_timeout_seconds: float = 5.0


class HeartbeatConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPENSQUILLA_HEARTBEAT_",
        extra="ignore",
        populate_by_name=True,
    )

    enabled: bool = False
    interval_ms: int = Field(
        default=60000,
        ge=1,
        validation_alias=AliasChoices("interval_ms", "intervalMs"),
    )
    target: str = "last"
    to: str = ""
    account_id: str = Field(default="", validation_alias=AliasChoices("account_id", "accountId"))
    thread_id: str = Field(default="", validation_alias=AliasChoices("thread_id", "threadId"))
    prompt: str | None = "Reply HEARTBEAT_OK."
    ack_max_chars: int = Field(
        default=500,
        ge=0,
        validation_alias=AliasChoices("ack_max_chars", "ackMaxChars"),
    )
    light_context: bool = Field(
        default=False,
        validation_alias=AliasChoices("light_context", "lightContext"),
    )
    # Path to HEARTBEAT.md for live-reload of cadence + Loop overrides.
    # ``None`` resolves to ``<workspace_dir>/HEARTBEAT.md``
    # at boot. When the file is absent the loop falls back to the bootstrap
    # values above; a malformed frontmatter is fail-open (defaults).
    config_path: str | None = Field(
        default=None,
        validation_alias=AliasChoices("config_path", "configPath"),
    )

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("target must be a non-empty string")
        return value.strip()


class ImageGenerationOpenAIProviderConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_env: str = "OPENAI_API_KEY"


class ImageGenerationOpenRouterProviderConfig(BaseModel):
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: str = ""
    api_key_env: str = "OPENROUTER_API_KEY"


class ImageGenerationTokenRhythmProviderConfig(BaseModel):
    base_url: str = "https://tokenrhythm.studio/v1"
    api_key: str = ""
    api_key_env: str = "TOKENRHYTHM_API_KEY"


class ImageGenerationQwenTokenPlanProviderConfig(BaseModel):
    base_url: str = "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1"
    api_key: str = ""
    api_key_env: str = "QWEN_TOKEN_PLAN_API_KEY"


class ImageGenerationProvidersConfig(BaseModel):
    openai: ImageGenerationOpenAIProviderConfig = Field(
        default_factory=ImageGenerationOpenAIProviderConfig
    )
    openrouter: ImageGenerationOpenRouterProviderConfig = Field(
        default_factory=ImageGenerationOpenRouterProviderConfig
    )
    tokenrhythm: ImageGenerationTokenRhythmProviderConfig = Field(
        default_factory=ImageGenerationTokenRhythmProviderConfig
    )
    qwen_token_plan: ImageGenerationQwenTokenPlanProviderConfig = Field(
        default_factory=ImageGenerationQwenTokenPlanProviderConfig
    )


class ImageGenerationConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPENSQUILLA_IMAGE_GENERATION_",
        env_nested_delimiter="__",
    )

    enabled: bool = False
    # ``follow_llm`` is written only by an explicit provider-configuration
    # intent. Existing image sections load as ``custom`` so upgrades never
    # silently replace an operator-owned route or re-enable a disabled tool.
    binding: Literal["custom", "follow_llm"] = "custom"
    primary: str = "openai/gpt-image-1"
    fallbacks: list[str] = Field(default_factory=list)
    size: str = "1024x1024"
    timeout_seconds: float = 180.0
    output_format: Literal["png", "jpeg", "webp"] = "png"
    providers: ImageGenerationProvidersConfig = Field(
        default_factory=ImageGenerationProvidersConfig
    )


class AudioElevenLabsProviderConfig(BaseModel):
    base_url: str = "https://api.elevenlabs.io"
    api_key: str = ""
    api_key_env: str = "ELEVENLABS_API_KEY"
    speech_to_text_model: str = "scribe_v2"
    voice_conversion_model: str = "eleven_multilingual_sts_v2"
    music_model: str = "music_v1"
    music_output_format: str = "mp3_44100_128"


class AudioProvidersConfig(BaseModel):
    elevenlabs: AudioElevenLabsProviderConfig = Field(
        default_factory=AudioElevenLabsProviderConfig
    )


class AudioTTSConfig(BaseModel):
    model: str = "eleven_multilingual_v2"
    voice: str = "21m00Tcm4TlvDq8ikWAM"
    language_code: str = ""
    output_format: str = "mp3_44100_128"
    timeout_seconds: float = 120.0
    stability: float | None = None
    similarity_boost: float | None = None
    style: float | None = None
    use_speaker_boost: bool | None = None
    speed: float = 1.0


class AudioConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPENSQUILLA_AUDIO_",
        env_nested_delimiter="__",
    )

    enabled: bool = False
    tts: AudioTTSConfig = Field(default_factory=AudioTTSConfig)
    providers: AudioProvidersConfig = Field(default_factory=AudioProvidersConfig)


# ---------------------------------------------------------------------------
# Channel config (BaseModel — no env-var binding, validated at TOML load)
# Names use *Entry suffix to avoid shadowing adapter-level *ChannelConfig.
# ---------------------------------------------------------------------------


class ConfiguredChannelEntry(BaseModel):
    """Common fields shared by gateway-managed channel entries."""

    name: str
    type: str
    enabled: bool = True
    agent_id: str = "main"
    debounce_window_s: float = 0.0
    status_reactions_enabled: bool = False
    dm_access: Literal["pairing", "open", "allowlist"] = "pairing"
    # When a pairing is approved, tell the sender they can start — otherwise
    # approval is silent and they never know to send another message.
    pairing_approved_notice: bool = True
    allowed_senders: list[str] = Field(default_factory=list)

    @field_validator("allowed_senders", mode="before")
    @classmethod
    def _normalize_allowed_senders(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, list | tuple | set | frozenset):
            return value
        return list(
            dict.fromkeys(str(item).strip() for item in value if str(item).strip())
        )

    @model_validator(mode="after")
    def _validate_dm_access(self) -> ConfiguredChannelEntry:
        if self.dm_access == "allowlist" and not self.allowed_senders:
            raise ValueError("dm_access=allowlist requires allowed_senders")
        return self
    # Group conversations are isolated by sender by default. Deployments that
    # intentionally want one transcript shared by the whole room can opt in.
    group_session_scope: Literal["per_sender", "shared_room"] = "per_sender"
    # Controls what happens when another channel message arrives while the
    # same session is busy. TaskRuntime validates and executes these modes.
    busy_input_mode: Literal["followup", "queue", "steer", "interrupt"] = "followup"

    @field_validator("debounce_window_s")
    @classmethod
    def _validate_debounce_window(cls, value: float) -> float:
        if value != 0.0 and not 0.1 <= value <= 30.0:
            raise ValueError("debounce_window_s must be 0 or in [0.1, 30.0]")
        return value


class SlackChannelEntry(ConfiguredChannelEntry):
    """Gateway config entry for a Slack channel."""

    type: Literal["slack"] = "slack"
    token: str
    slack_channel_id: str = ""
    signing_secret: str | None = None
    reply_in_thread: bool = False
    # ``socket`` uses Slack Socket Mode (an outbound websocket long-connection,
    # like Feishu) and needs no public Request URL; ``webhook`` keeps the
    # Events API webhook. Socket Mode additionally requires ``app_token``.
    connection_mode: Literal["webhook", "socket"] = "webhook"
    app_token: str = ""

    @model_validator(mode="after")
    def _validate_socket_app_token(self) -> SlackChannelEntry:
        if self.connection_mode == "socket" and not self.app_token.strip():
            raise ValueError("slack socket channels require app_token")
        return self


class FeishuChannelEntry(ConfiguredChannelEntry):
    """Gateway config entry for a Feishu (Lark) channel."""

    type: Literal["feishu"] = "feishu"
    status_reactions_enabled: bool = True
    app_id: str
    app_secret: str
    encrypt_key: str = ""
    verification_token: str = ""
    default_chat_id: str = ""
    webhook_path: str = "/feishu/events"
    api_base: str = "https://open.feishu.cn/open-apis"
    connection_mode: Literal["webhook", "websocket"] = "websocket"
    domain: Literal["feishu", "lark"] = "feishu"

    @model_validator(mode="after")
    def _validate_webhook_verification(self) -> FeishuChannelEntry:
        # Webhook ingress authenticates via the encrypt_key HMAC signature
        # and/or the verification token — either one is a real credential
        # (configs from earlier releases commonly carry encrypt_key alone).
        # Disabled entries stay loadable so config migration can park an
        # unverifiable legacy entry instead of failing the whole boot.
        if (
            self.enabled
            and self.connection_mode == "webhook"
            and not self.verification_token.strip()
            and not self.encrypt_key.strip()
        ):
            raise ValueError(
                "feishu webhook channels require verification_token or encrypt_key"
            )
        return self


class DiscordChannelEntry(ConfiguredChannelEntry):
    """Gateway config entry for a Discord channel."""

    type: Literal["discord"] = "discord"
    token: str
    application_id: str = ""
    default_channel_id: str = ""
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    # GUILDS | GUILD_MESSAGES | GUILD_MESSAGE_REACTIONS | DIRECT_MESSAGES |
    # DIRECT_MESSAGE_REACTIONS | MESSAGE_CONTENT. Keep this aligned with
    # channels.discord.GATEWAY_INTENTS.
    intents: int = 46593


class DingTalkChannelEntry(ConfiguredChannelEntry):
    """Gateway config entry for a DingTalk channel."""

    type: Literal["dingtalk"] = "dingtalk"
    client_id: str
    client_secret: str
    # Existing stream-only entries predate native artifact delivery, so these
    # stay safely defaulted. The onboarding catalog requires robot_code for
    # newly created entries while legacy TOML continues to load unchanged.
    robot_code: str = ""
    cool_app_code: str = ""


class WeComChannelEntry(ConfiguredChannelEntry):
    """Gateway config entry for WeCom AI Bot or corp-app callback."""

    type: Literal["wecom"] = "wecom"
    connection_mode: Literal["webhook", "websocket"] = "webhook"
    bot_id: str = ""
    bot_secret: str = ""
    websocket_url: str = "wss://openws.work.weixin.qq.com"
    corp_id: str = ""
    corp_secret: str = ""
    agent_id_int: int = 0
    token: str = ""
    encoding_aes_key: str = ""
    webhook_path: str = "/wecom/events"
    api_base: str = "https://qyapi.weixin.qq.com"

    @model_validator(mode="after")
    def validate_wecom_mode(self) -> WeComChannelEntry:
        if self.connection_mode == "websocket":
            missing = [
                field
                for field in ("bot_id", "bot_secret")
                if not str(getattr(self, field)).strip()
            ]
            if missing:
                raise ValueError(
                    "wecom websocket mode requires bot_id and bot_secret; "
                    "corp_id/corp_secret/access_token are for webhook mode"
                )
            if not self.websocket_url.strip():
                raise ValueError("wecom websocket mode requires websocket_url")
            return self

        missing = [
            field
            for field in (
                "corp_id",
                "corp_secret",
                "token",
                "encoding_aes_key",
            )
            if not str(getattr(self, field)).strip()
        ]
        if self.agent_id_int <= 0:
            missing.append("agent_id_int")
        if missing:
            raise ValueError(
                "wecom webhook mode requires corp_id, corp_secret, agent_id_int, "
                "token, and encoding_aes_key"
            )
        return self


class QQChannelEntry(ConfiguredChannelEntry):
    """Gateway config entry for a QQ Bot channel."""

    type: Literal["qq"] = "qq"
    app_id: str
    app_secret: str


class MSTeamsChannelEntry(ConfiguredChannelEntry):
    """Gateway config entry for an MS Teams channel."""

    type: Literal["msteams"] = "msteams"
    app_id: str
    app_password: str
    webhook_path: str = "/msteams/messages"


class MatrixChannelEntry(ConfiguredChannelEntry):
    """Gateway config entry for a Matrix channel."""

    type: Literal["matrix"] = "matrix"
    homeserver_url: str
    user_id: str
    password: str = ""
    access_token: str = ""
    device_id: str = ""
    encryption: Literal["off", "required", "best_effort"] = "off"


class TelegramChannelEntry(ConfiguredChannelEntry):
    """Gateway config entry for a Telegram Bot API channel."""

    type: Literal["telegram"] = "telegram"
    token: str
    default_chat_id: str = ""
    api_base: str = "https://api.telegram.org"
    transport_name: Literal["polling", "webhook"] = "polling"
    webhook_path: str = "/telegram/events"
    webhook_url: str = ""
    webhook_secret_token: str = ""
    drop_pending_updates: bool = False
    poll_timeout_s: int = 30
    poll_limit: int = 100
    poll_idle_sleep_s: float = 0.1

    @model_validator(mode="after")
    def _validate_webhook_auth(self) -> TelegramChannelEntry:
        if self.transport_name == "webhook":
            if not self.webhook_url:
                raise ValueError("webhook_url is required for telegram webhook mode")
            if not self.webhook_secret_token:
                raise ValueError(
                    "webhook_secret_token is required for telegram webhook mode"
                )
        return self


ChannelConfigEntry = ConfiguredChannelEntry


class ChannelsConfig(BaseModel):
    """Container for all channel entries."""

    channels: list[SerializeAsAny[ChannelConfigEntry]] = Field(default_factory=list)

    @field_validator("channels", mode="before")
    @classmethod
    def _resolve_channel_entries(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, tuple):
            value = list(value)
        if not isinstance(value, list):
            return value

        from opensquilla.channels.registry import parse_channel_entry

        return [parse_channel_entry(item) for item in value]


class AgentSubagentDefaults(BaseModel):
    """Per-agent subagent governance defaults.

    All fields are optional. ``None`` means "unset"; downstream code falls
    back to ``GatewayConfig.agents_defaults.subagents`` and then to "preserve
    current behavior". Only ``cascade_on_parent_kill`` has a non-None default
    because killing children is the safer behavior when in doubt.
    """

    model: str | None = None
    """Default LLM model for subagents spawned under this agent. ``None`` →
    fall back to caller's model (current behavior)."""

    max_children_per_session: int | None = None
    """Max active children one parent session can hold. ``None`` → no
    enforcement (current behavior)."""

    allow_agents: list[str] | None = None
    """Cross-agent spawn allowlist. ``None`` = unset (current behavior); ``[]``
    = self only; ``["*"]`` = any. Other values are exact agent_id matches."""

    cascade_on_parent_kill: bool = True
    """When ``True``, killing a parent session also cancels its descendants."""


class AgentRoutingConfig(BaseModel):
    """Per-agent router tier overrides for a durable agent profile.

    Both fields are optional and default to ``None`` ("unset"). Tier strings are
    canonicalized to ``c0``–``c3`` exactly the way :class:`SquillaRouterConfig`
    normalizes its ``default_tier``: legacy ``t0``–``t3`` aliases and a leading
    ``tier:`` prefix are accepted, and an unrecognized value is kept verbatim
    (normalize-or-keep). This matches every other tier-accepting surface in the
    codebase (``SquillaRouterConfig``, ``engine/routing/policy.py``) rather than
    inventing a stricter rejection path the rest of the codebase lacks.

    Ordering between ``default_tier`` and ``max_tier`` is intentionally NOT
    enforced: ``SquillaRouterConfig`` enforces no analogous ceiling constraint,
    so this block mirrors that rigor. The block is additive schema only — no
    consumer reads it yet, so it does not change routing behavior for any
    existing config. Wiring ``max_tier`` as a routing ceiling / ``default_tier``
    as a per-agent starting tier is a documented follow-up.
    """

    default_tier: str | None = None
    """Preferred starting tier for turns run under this agent. ``None`` → unset;
    routing falls back to ``squilla_router.default_tier`` (current behavior)."""

    max_tier: str | None = None
    """Ceiling tier this agent's turns may be routed to. ``None`` → unset; no
    per-agent ceiling is applied (current behavior)."""

    @model_validator(mode="before")
    @classmethod
    def _normalize_tiers(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        values = dict(values)
        for key in ("default_tier", "max_tier"):
            raw = values.get(key)
            if raw is None:
                continue
            text = str(raw).strip()
            if text[:5].lower() == "tier:":
                text = text[5:]
            values[key] = normalize_text_tier(text) or raw
        return values


class AgentEntryConfig(BaseModel):
    """Gateway config entry for a durable, user-managed agent."""

    id: str
    name: str | None = None
    description: str | None = None
    model: str | None = None
    workspace: str | None = None
    agent_dir: str | None = None
    tools: dict[str, Any] | list[str] | str | None = None
    enabled: bool = True
    system_prompt: str | None = None
    subagents: AgentSubagentDefaults | None = None
    routing: AgentRoutingConfig | None = None
    """Additive per-agent tier overrides. ``None`` → unset (nothing persisted;
    current routing behavior preserved)."""

    @field_validator("id")
    @classmethod
    def _normalize_id(cls, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("agent id must be non-empty")
        from opensquilla.session.keys import normalize_agent_id

        return normalize_agent_id(raw)


class AgentDefaults(BaseModel):
    """Global fallback defaults applied when an agent does not override."""

    subagents: AgentSubagentDefaults | None = None


class SubagentsGatewayConfig(BaseModel):
    """Gateway-level subagent governance knobs."""

    enforce_disabled_agents: bool = False
    """When True, ``sessions_spawn`` rejects requests targeting an agent whose
    ``enabled=False``. Default off so existing deployments are unaffected."""

    subagent_reserved_slots: int = Field(default=2, ge=0)
    """Number of slots in ``task_runtime.max_concurrency`` reserved for
    non-subagent tasks so a fan-out parent never starves itself."""

    archive_after_minutes: int = Field(default=60, ge=0)
    """Minutes after a subagent session goes terminal before its transcript
    is archived. ``0`` disables auto-archive."""

    prompt_compact: bool = False
    """When enabled, subagent bootstrap prompts keep only AGENTS.md and TOOLS.md."""


class MetaSkillPersistenceConfig(BaseSettings):
    """Persistence/audit ledger for meta-skill executions (G4)."""

    model_config = SettingsConfigDict(
        env_prefix="OPENSQUILLA_META_SKILL_PERSISTENCE_",
        extra="forbid",
    )
    enabled: bool = True
    orphan_cleanup_age_seconds: int = 3600
    # Per-DAG memory persist: when False the orchestrator skips any step
    # whose ``skill`` is "memory" (the conventional last-step pattern that
    # archives DAG output to memory/*.md). Defaults to True to preserve
    # existing behaviour. Toggle off for exploratory runs where polluting
    # the long-term memory store is undesirable.
    memory_persist_enabled: bool = True


class MetaSkillAutoProposeConfig(BaseSettings):
    """Unattended synthesis: drive meta-skill-creator from co-occurrence
    patterns observed in ``~/.opensquilla/logs/decisions-*.jsonl``.

    Two independent triggers feed the same library function
    (``skills.creator.auto_propose``):
      * ``enabled`` schedules a recurring cron job
      * ``on_dream_complete`` piggybacks on memory-consolidation dreams

    Both default off. Operators flip them on after reviewing
    meta-skill-creator's gated output once.
    """

    model_config = SettingsConfigDict(
        env_prefix="OPENSQUILLA_META_SKILL_AUTO_PROPOSE_",
        extra="forbid",
    )

    enabled: bool = False
    """Path 1: schedule the auto-propose cron job. When false no
    handler is registered at all (zero-impact code path)."""

    cron: str = "0 5 * * *"
    """Cron expression (5-field, local time) for the scheduled job."""

    window_days: int = Field(default=30, ge=1, le=365)
    """How many days of decision-log history to aggregate."""

    min_freq: int = Field(default=3, ge=1)
    """Drop co-occurrence chains observed fewer than this many times."""

    top_k: int = Field(default=5, ge=1, le=50)
    """At most this many distinct patterns considered per fire."""

    on_dream_complete: bool = False
    """Path 2: also run after a successful memory-consolidation dream.
    Independent of ``enabled`` — either, both, or neither may be on."""

    auto_enable: bool = False
    """When true, eligible low-risk proposals are promoted to MANAGED
    automatically after the creator gates pass. Defaults off."""

    auto_enable_max_risk: Literal["low", "medium", "high"] = "low"
    """Highest deterministic risk class that unattended promotion may accept."""

    agent_ids: list[str] = Field(default_factory=list)
    """Restrict to these agent IDs; empty = all configured agents."""


class MetaSkillConfig(BaseSettings):
    """Top-level meta-skill subsystem configuration."""

    model_config = SettingsConfigDict(
        env_prefix="OPENSQUILLA_META_SKILL_",
        env_nested_delimiter="__",
        extra="forbid",
    )
    enabled: bool = True
    auto_trigger: bool = False
    """When False (default), meta-skills are manual-only: no prompt guidance, no
    keyword/semantic auto-trigger, ``meta_invoke`` is not exposed for automatic
    invocation, and meta-skills are hidden from ``<available_skills>``. They run
    only via the explicit ``/meta`` command. Set True to restore automatic
    activation."""
    persistence: MetaSkillPersistenceConfig = Field(
        default_factory=MetaSkillPersistenceConfig,
    )
    auto_propose: MetaSkillAutoProposeConfig = Field(
        default_factory=MetaSkillAutoProposeConfig,
    )


class TlsConfig(BaseSettings):
    """Optional TLS termination at the gateway itself.

    When ``keyfile`` and ``certfile`` are set, ``run_gateway`` passes
    ``ssl_keyfile`` / ``ssl_certfile`` to uvicorn so the gateway speaks
    HTTPS / WSS on its bound port. Disabled by default — gateways
    behind a reverse proxy (nginx + LetsEncrypt) keep using plain HTTP.

    Self-signed certs are fine for IP-based access (browser prints a
    one-time "not trusted" warning); for a real CA-signed cert wire
    via the same fields.
    """

    model_config = SettingsConfigDict(
        env_prefix="OPENSQUILLA_TLS_",
        extra="forbid",
    )
    keyfile: str = ""
    certfile: str = ""


class LlmProviderProfile(BaseModel):
    """Credential profile for a non-primary or session-pinned LLM deployment.

    Provider defaults are written as ``[llm_profiles.<provider_id>]`` and are
    referenced by router tiers through their existing ``provider`` field.
    A session-pinned named profile may use an exact qualified key such as
    ``[llm_profiles."openai:work"]``; manual compaction resolves that key only
    when ``auth_profile_override`` names it and never treats it as a provider
    default.
    Resolution order per field matches the primary provider: explicit value,
    then ``api_key_env_pool`` (when non-empty), then ``api_key_env`` (or the
    registry env key), then the registry default base URL.
    """

    # Profiles are config records, not independent settings roots.  Reading
    # process variables here would give every profile the same unscoped
    # ``API_KEY`` / ``API_KEY_ENV`` values whenever a field is absent (for
    # example after an explicit credential clear).  Named environment sources
    # are resolved deliberately by provider.deployment instead.
    model_config = ConfigDict(extra="ignore")

    # Remember the provider-scoped direct/fallback model while this deployment
    # is not primary.  Older configs omit it and continue to resolve the
    # provider catalog default; older binaries ignore this additive field.
    model: str = ""
    api_key: str = ""
    api_key_env: str = ""
    # Rotation pool of env-var NAMES (never key values) for this profile,
    # e.g. ["OPENAI_KEY_A", "OPENAI_KEY_B"]. Resolved from the environment at
    # runtime; secrets are never persisted or logged. Profiles-only this
    # cycle: the top-level [llm] model must NOT gain this field — [llm] is
    # extra="forbid", so a stamped default would brick downgrade to 0.5.0rc1,
    # while this profile model is extra="ignore" and rc1 tolerates the field
    # on load (rc1's first persist silently strips it; release-noted).
    api_key_env_pool: list[str] = Field(default_factory=list)
    base_url: str = ""
    proxy: str = ""


class ModelCatalogConfig(BaseSettings):
    """Model metadata catalog behavior (offline-first).

    Schema-first landing: these knobs are validated and persisted now; the
    provider layer consumes them in a follow-up change. ``refresh`` controls
    whether the gateway may fetch model *metadata* (context windows, output
    caps, pricing, capability flags) from the network at all. The default
    ``"off"`` keeps every install fully offline — metadata resolves from
    bundled/registry defaults, an optional ``pin_path`` file, and per-model
    ``[models.*]`` overrides. Today's OpenRouter live model-list fetch is a
    separate, existing mechanism that this flag does not govern yet.
    """

    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_MODEL_CATALOG_")

    # "off" = never fetch model metadata (offline-first default);
    # "startup" = allow one refresh when the gateway boots.
    refresh: Literal["off", "startup"] = "off"
    # Local JSON/TOML catalog override file for air-gapped deploys. Validated
    # only as a string here; existence/shape checks happen when the catalog
    # wiring consumes it.
    pin_path: str = ""
    # Advisory age threshold (days) for doctor warnings about stale pinned or
    # cached model metadata. Never blocks a turn.
    stale_after_days: int = Field(default=45, ge=1)


# Known provider reasoning-format dialects accepted by
# ``ModelOverrideConfig.reasoning_format``. Deliberately a conservative
# literal rather than an import: the canonical dialect registry may land in
# a parallel branch, and this set must stay valid either way. Fold it into
# the registry (and keep the names identical) once that lands.
KNOWN_REASONING_FORMATS: frozenset[str] = frozenset(
    {
        "openrouter",
        "openai",
        "deepseek",
        "gemini",
        "zai",
        "dashscope",
        "moonshot",
        "volcengine",
        "none",
    }
)


class ModelOverrideConfig(BaseModel):
    """Per-model metadata override, written as ``[models.<provider_id>."<model_id>"]``.

    Every field is optional; ``None`` means "no override" and the value keeps
    resolving from the model catalog / provider registry as before. Model ids
    are matched *exactly* (TOML quoted keys carry dots and slashes verbatim).
    Globs are deliberately not supported in user config: exact ids keep
    override semantics unambiguous, and the shipped catalog corrections layer
    owns pattern matching. Keys are not screened for glob-looking syntax
    either — a key that matches no model is simply inert, and rejecting
    characters here could break unusual but legitimate model ids.
    """

    model_config = ConfigDict(extra="forbid")

    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning_format: str | None = None
    supports_reasoning: bool | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    input_cost_per_mtok: float | None = Field(default=None, ge=0)
    output_cost_per_mtok: float | None = Field(default=None, ge=0)
    cache_read_cost_per_mtok: float | None = Field(default=None, ge=0)
    cache_write_cost_per_mtok: float | None = Field(default=None, ge=0)
    thinking_level_map: dict[str, str] | None = None

    @field_validator("reasoning_format")
    @classmethod
    def _validate_reasoning_format(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in KNOWN_REASONING_FORMATS:
            allowed = ", ".join(sorted(KNOWN_REASONING_FORMATS))
            raise ValueError(
                f"reasoning_format {value!r} is not a known dialect; "
                f"expected one of {allowed}"
            )
        return normalized


class _EnvWithoutConfigVersion(PydanticBaseSettingsSource):
    """Env-backed settings source that never yields ``config_version``.

    ``config_version`` is the migration stamp owned by the config payload:
    ``migrate_config_payload`` injects it at every disk-load boundary, and
    injected payload keys already outrank environment values. This wrapper
    closes the remaining gap — bare ``GatewayConfig()`` constructions with no
    payload at all (e.g. the no-file default branch of ``GatewayConfig.load``
    and ``config_store.load_config``) — so
    ``OPENSQUILLA_GATEWAY_CONFIG_VERSION`` can never gate or skip migrations.
    Only this one key is filtered; every other env override is untouched.
    """

    def __init__(self, inner: PydanticBaseSettingsSource) -> None:
        super().__init__(inner.settings_cls)
        self._inner = inner

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._inner.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        values = dict(self._inner())
        values.pop("config_version", None)
        return values


class GoalConfig(BaseSettings):
    """Guardrails for session-level Goal execution.

    TOML section ``[goal]``; keys mirror the field names (snake_case).
    Automatic execution is enabled by default, with a fail-closed operator
    switch and bounded per-resume execution windows.
    """

    model_config = SettingsConfigDict(
        env_prefix="OPENSQUILLA_GOAL_",
        extra="ignore",
        populate_by_name=True,
    )

    execution_enabled: bool = True
    max_turns: int = Field(default=50, ge=1, le=500)
    # Accumulated running time only. Queued, paused, and process downtime are
    # deliberately excluded from this limit.
    runtime_budget_seconds: int = Field(default=3_600, ge=60, le=86_400)


class GatewayConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPENSQUILLA_GATEWAY_",
        env_nested_delimiter="__",
    )

    tls: TlsConfig = Field(default_factory=TlsConfig)

    # bind defaults to 127.0.0.1 (loopback only).
    # OPENSQUILLA_LISTEN is recognised as a short-name env alias for ``host``
    # alongside the canonical OPENSQUILLA_GATEWAY_HOST; resolution is performed
    # by ``resolve_listen_address`` below at the CLI boundary so the
    # precedence order (explicit kwarg/flag > OPENSQUILLA_LISTEN > OPENSQUILLA_GATEWAY_HOST
    # > default) is testable without the pydantic-settings env cache.
    host: str = "127.0.0.1"
    port: int = 18791
    # Resolved from installed distribution metadata (opensquilla.__version__),
    # not operator config. UI/RPC surfaces read __version__ directly, so any
    # stale value persisted in config.toml has no display effect.
    version: str = __version__
    debug: bool = False
    log_file_enabled: bool = True
    log_level: str = "DEBUG"
    log_file_max_bytes: int = Field(default=5_000_000, ge=0)
    log_file_backup_count: int = Field(default=3, ge=0)
    workspace_dir: str | None = Field(
        default_factory=lambda: str(default_opensquilla_home() / "workspace")
    )
    workspace_strict: bool | None = None
    bootstrap_max_chars: int = Field(default=20_000, ge=1)
    bootstrap_total_max_chars: int = Field(default=50_000, ge=1)

    auth: AuthConfig = Field(default_factory=AuthConfig)
    cors: CorsConfig = Field(default_factory=CorsConfig)
    attachments: AttachmentsConfig = Field(default_factory=AttachmentsConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    task_runtime: TaskRuntimeConfig = Field(default_factory=TaskRuntimeConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    llm: LlmProviderConfig = Field(default_factory=LlmProviderConfig)
    # Credential profiles for non-primary providers, normally keyed by
    # registry provider id and consumed by cross-provider router tiers.
    # Qualified ``provider:name`` keys are reserved for exact session-pinned
    # profile selection and are never implicit provider defaults.
    llm_profiles: dict[str, LlmProviderProfile] = Field(default_factory=dict)
    llm_ensemble: LlmEnsembleConfig = Field(default_factory=LlmEnsembleConfig)
    # Model metadata catalog behavior (offline-first; see ModelCatalogConfig).
    model_catalog: ModelCatalogConfig = Field(default_factory=ModelCatalogConfig)
    # Per-model metadata overrides keyed [models.<provider_id>."<model_id>"].
    # Exact model ids only (quoted TOML keys); see ModelOverrideConfig.
    models: dict[str, dict[str, ModelOverrideConfig]] = Field(default_factory=dict)
    prompt_cache: PromptCacheConfig = Field(default_factory=PromptCacheConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    squilla_router: SquillaRouterConfig = Field(default_factory=SquillaRouterConfig)
    agent_token_saving: AgentTokenSavingConfig = Field(default_factory=AgentTokenSavingConfig)
    compaction: CompactionLlmConfig = Field(default_factory=CompactionLlmConfig)
    naming: SessionNamingConfig = Field(default_factory=SessionNamingConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    goal: GoalConfig = Field(default_factory=GoalConfig)
    image_generation: ImageGenerationConfig = Field(default_factory=ImageGenerationConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    agents: list[AgentEntryConfig] = Field(default_factory=list)
    agents_defaults: AgentDefaults = Field(default_factory=AgentDefaults)
    subagents: SubagentsGatewayConfig = Field(default_factory=SubagentsGatewayConfig)
    meta_skill: MetaSkillConfig = Field(default_factory=MetaSkillConfig)

    # Component enable flags
    control_ui: ControlUiConfig = Field(default_factory=ControlUiConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    diagnostics_enabled: bool = False
    channel_admin_senders: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def effective_run_mode(self) -> str:
        """Return the canonical sandbox run mode for this validated config."""

        from opensquilla.run_mode import config_run_mode

        return config_run_mode(self).value

    def _resolve_image_generation_llm_runtime(self) -> object:
        """Expose primary LLM resolution through a provider-safe capability."""

        from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config

        return resolve_llm_runtime_config(self)

    def _acquire_image_generation_profile_credential(
        self,
        provider_id: str,
        env_pool: list[str],
        session_key: str,
    ) -> object | None:
        """Acquire from the shared profile pool without provider back-imports."""

        from opensquilla.gateway.llm_runtime import profile_credential_pools

        return profile_credential_pools().acquire_for_session(
            provider_id,
            env_pool,
            session_key,
        )

    def _report_image_generation_profile_credential_failure(
        self,
        provider_id: str,
        session_key: str,
        kind: object,
        retry_after_seconds: float | None,
    ) -> None:
        """Report an Image request failure to the shared profile pool."""

        from typing import cast

        from opensquilla.gateway.llm_runtime import profile_credential_pools
        from opensquilla.provider.failures import ProviderFailureKind

        profile_credential_pools().report_failure(
            provider_id,
            session_key,
            cast(ProviderFailureKind, kind),
            retry_after_seconds=retry_after_seconds,
        )

    @model_validator(mode="after")
    def _resolve_default_llm_provider(self) -> GatewayConfig:
        """Resolve the built-in provider default for configs that never chose one.

        The built-in default is tokenrhythm, but configs authored while
        openrouter was the default may rely on it without naming a provider.
        With ``llm.provider`` unset, openrouter intent is detected from
        provider-coupled fields authored against the old default (an explicit
        ``model``/``base_url``/``api_key``/``api_key_env``, or a persisted
        ``squilla_router.tier_profile = "openrouter"``, which validation
        requires to match the provider) or from the environment carrying only
        the openrouter credential. Detected intent restores the full legacy
        trio for whichever of provider/model/base_url is unset; the same
        model/base_url backfill applies to an explicit ``provider =
        "openrouter"``, whose unset fields meant the old defaults when they
        were written. Runs before the router-profile validators so the
        tier_profile match check sees the resolved provider.

        Resolution never persists: it lands in the load-time sparse-persist
        baseline, so saves keep the file provider-less and every boot
        re-resolves against the then-current environment.
        """
        llm = self.llm
        fields_set = set(getattr(llm, "model_fields_set", set()))
        provider = str(llm.provider or "").strip().lower()
        resolution = {
            "status": "explicit",
            "effective_provider": provider,
            "source": "config",
            "reason_code": "provider_explicit",
            "action_required": False,
            "action_recommended": False,
        }
        if "provider" not in fields_set:
            profile = str(getattr(self.squilla_router, "tier_profile", "") or "")
            router_fields_set = set(
                getattr(self.squilla_router, "model_fields_set", set())
            )
            legacy_intent = bool(
                {"model", "base_url", "api_key", "api_key_env"} & fields_set
            ) or (
                "tier_profile" in router_fields_set
                and profile.strip().lower() == LEGACY_DEFAULT_LLM_PROVIDER
            )
            credential_hints = {
                hint
                for hint in (
                    credential_provider_hint(
                        llm.api_key if "api_key" in fields_set else ""
                    ),
                    credential_provider_hint(
                        "",
                        api_key_env=(
                            llm.api_key_env if "api_key_env" in fields_set else ""
                        ),
                    ),
                )
                if hint
            }
            origin_hint = endpoint_provider_hint(
                llm.base_url if "base_url" in fields_set else ""
            )
            explicit_profile_hint = (
                LEGACY_DEFAULT_LLM_PROVIDER
                if "tier_profile" in router_fields_set
                and profile.strip().lower() == LEGACY_DEFAULT_LLM_PROVIDER
                else ""
            )
            env_openrouter = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())
            env_tokenrhythm = bool(os.environ.get("TOKENRHYTHM_API_KEY", "").strip())
            ambient_credential_hints = (
                {
                    hint
                    for hint, available in (
                        (LEGACY_DEFAULT_LLM_PROVIDER, env_openrouter),
                        (TOKENRHYTHM_DEFAULT_LLM_PROVIDER, env_tokenrhythm),
                    )
                    if available
                }
                if not ({"api_key", "api_key_env"} & fields_set)
                else set()
            )
            strong_hints = {
                hint
                for hint in (
                    *credential_hints,
                    *ambient_credential_hints,
                    origin_hint,
                    explicit_profile_hint,
                )
                if hint
            }
            if strong_hints == {TOKENRHYTHM_DEFAULT_LLM_PROVIDER}:
                provider = TOKENRHYTHM_DEFAULT_LLM_PROVIDER
                resolution = {
                    "status": "recovered",
                    "effective_provider": provider,
                    "source": "strong_evidence",
                    "reason_code": "providerless_tokenrhythm_recovered",
                    "action_required": False,
                    "action_recommended": True,
                }
            elif len(strong_hints) > 1:
                provider = LEGACY_DEFAULT_LLM_PROVIDER
                resolution = {
                    "status": "conflict",
                    "effective_provider": "",
                    "source": "conflicting_evidence",
                    "reason_code": "providerless_provider_conflict",
                    "action_required": True,
                    "action_recommended": True,
                }
            elif legacy_intent or (env_openrouter and not env_tokenrhythm):
                provider = LEGACY_DEFAULT_LLM_PROVIDER
                explicit_openrouter = (
                    strong_hints == {LEGACY_DEFAULT_LLM_PROVIDER}
                    or (env_openrouter and not env_tokenrhythm and not legacy_intent)
                )
                resolution = {
                    "status": "legacy_inferred",
                    "effective_provider": provider,
                    "source": (
                        "strong_evidence" if explicit_openrouter else "legacy_compat"
                    ),
                    "reason_code": (
                        "providerless_openrouter_evidence"
                        if explicit_openrouter
                        else "providerless_ambiguous_legacy_openrouter"
                    ),
                    "action_required": False,
                    "action_recommended": not explicit_openrouter,
                }
            else:
                resolution = {
                    "status": "default",
                    "effective_provider": provider,
                    "source": "built_in_default",
                    "reason_code": "provider_unset_default_tokenrhythm",
                    "action_required": False,
                    "action_recommended": False,
                }
        self._provider_resolution = resolution
        if provider != LEGACY_DEFAULT_LLM_PROVIDER:
            return self
        payload = llm.model_dump(mode="python")
        payload["provider"] = LEGACY_DEFAULT_LLM_PROVIDER
        if "model" not in fields_set:
            payload["model"] = LEGACY_DEFAULT_LLM_MODEL
        if "base_url" not in fields_set:
            payload["base_url"] = LEGACY_DEFAULT_LLM_BASE_URL
        if payload != llm.model_dump(mode="python"):
            self.llm = LlmProviderConfig(**payload)
            # The rebuild marks every field explicitly set; restore the
            # operator's actual field provenance so fresh-install detection
            # (e.g. onboard's keep-current router gate) still sees a config
            # that never chose these values.
            object.__setattr__(self.llm, "__pydantic_fields_set__", fields_set)
        return self

    @model_validator(mode="after")
    def _default_squilla_router_profile_for_direct_provider(self) -> GatewayConfig:
        router = self.squilla_router
        if not router or not getattr(router, "enabled", False):
            return self
        # An explicit custom binding is an operator ownership boundary.  Do
        # not turn a sparse custom ladder into a provider preset at load time.
        if getattr(router, "preset_binding", None) == "custom":
            return self
        if getattr(router, "tier_profile", None):
            return self
        provider = str(getattr(self.llm, "provider", "") or "").strip().lower()
        # Boot auto-default: persistable packaged profiles write the compact
        # tier_profile form; curated-inline presets (e.g. tokenrhythm) apply
        # their ladder as inline tiers because their ids must never persist
        # as a tier_profile (downgrade contract). Synthesized presets are
        # applied by onboarding/provider saves only, never at boot.
        if provider == "openrouter":
            return self
        curated_inline_preset = None
        if provider not in ROUTER_TIER_PROFILE_IDS:
            preset = get_preset(provider)
            if preset is None or preset.synthesized or preset.persistable:
                return self
            curated_inline_preset = preset
        fields_set = set(getattr(router, "model_fields_set", set()))
        has_custom_tiers = (
            "tiers" in fields_set and getattr(router, "tiers", {}) != _default_tiers()
        )
        follows_primary_preset = getattr(router, "preset_binding", None) == "follow_primary"
        if "tier_profile" in fields_set or (has_custom_tiers and not follows_primary_preset):
            return self
        payload = router.model_dump(mode="python")
        if curated_inline_preset is None:
            payload["tier_profile"] = provider
            payload.pop("tiers", None)
            self.squilla_router = SquillaRouterConfig(**payload)
            return self
        payload["tier_profile"] = None
        payload["tiers"] = curated_inline_preset.tier_defaults()
        self.squilla_router = SquillaRouterConfig(**payload)
        # The rebuild marks every field explicitly set; restore the original
        # provenance so the seeded ladder stays in-memory only (the load-time
        # sparse-persist baseline keeps it out of config.toml) and a fresh
        # default config still reads as one (onboard's keep-current router
        # gate and custom-tiers detection rely on it).
        object.__setattr__(self.squilla_router, "__pydantic_fields_set__", fields_set)
        return self

    @model_validator(mode="after")
    def _validate_squilla_router_tier_profile_provider(self) -> GatewayConfig:
        profile = getattr(self.squilla_router, "tier_profile", None)
        if not profile:
            return self
        provider = str(getattr(self.llm, "provider", "") or "").strip().lower()
        normalized_profile = str(profile).strip().lower()
        if provider != normalized_profile:
            # An atomic primary/profile swap deliberately leaves the router's
            # preset and switches untouched while demoting its old provider
            # into llm_profiles. That is a valid mixed-provider deployment:
            # keep rejecting stale mismatches unless the profile provider has
            # an actual stored deployment to execute against.
            has_profile_deployment = any(
                str(key or "").strip().lower() == normalized_profile
                for key in (getattr(self, "llm_profiles", None) or {})
            )
            if has_profile_deployment:
                return self
            raise ValueError(
                "squilla_router.tier_profile requires llm.provider to match "
                f"({normalized_profile!r} != {provider!r})"
            )
        return self

    # --- Context overflow policy -----------------------------------------
    # Budget and policy consulted in gateway/rpc_chat.py before dispatching
    # a turn. ``context_budget_tokens`` is a soft cap: when an estimated
    # turn payload exceeds this, the policy branch fires.
    context_budget_tokens: int = 100_000
    context_overflow_policy: ContextOverflowPolicy = ContextOverflowPolicy.AUTO_SUMMARIZE
    preflight_compact_ratio: float = Field(default=0.85, gt=0.0, le=1.0)

    # Agent runtime timeout (whole turn lifecycle). ``None`` means use the
    # long built-in runtime default; ``0`` disables the runtime budget.
    agent_runtime_timeout_seconds: float | None = None
    # Optional safety cap for ordinary interactive Web chat turns. Coding and
    # meta turns retain the regular agent runtime budget. Disabled by default;
    # an explicit TurnRunner timeout still has priority when the cap is enabled.
    web_chat_runtime_timeout_seconds: float = Field(default=0.0, ge=0.0)
    # Per-iteration timeout: one LLM call + its tool executions. ``None``
    # means use the AgentConfig default.
    agent_iteration_timeout_seconds: float | None = None
    # Per-tool execution timeout. ``None`` means use the AgentConfig default.
    agent_tool_timeout_seconds: float | None = None
    # Per-turn override for the single LLM HTTP/streaming request timeout.
    # ``None`` defers to ``llm_request_timeout_seconds`` so existing
    # deployments keep their tuned value.
    agent_request_timeout_seconds: float | None = None
    # Maximum provider-level retries for transient errors. ``None`` means
    # use the AgentConfig default.
    agent_max_provider_retries: int | None = None
    # Agent model/tool loop budget for a single turn. 0 disables this cap.
    agent_max_iterations: int = Field(default=0, ge=0)
    # Source diff preservation protects already-mutated source files from
    # high-confidence destructive git restore/checkout/reset/clean commands.
    source_diff_preservation_mode: Literal["off", "log", "block"] = "log"
    # Source diff candidate ledger records recoverable source edit patches and
    # can surface lost candidate ids in final-diff recovery diagnostics.
    source_diff_candidate_mode: Literal["off", "log", "warn_model"] = "log"
    # Runtime state capsule is an opt-in provider-visible factual summary for
    # coding turns. ``log`` records telemetry only; ``inject`` adds it to the
    # provider request view.
    runtime_state_capsule_mode: Literal["off", "log", "inject"] = "off"
    # Text-only tool recovery is an opt-in guard for tool-capable turns where
    # a model emits prose instead of a tool call.
    text_only_tool_recovery_mode: Literal["off", "log", "warn_model"] = "off"
    # Provider request timeout (single LLM HTTP/streaming request).
    llm_request_timeout_seconds: float = 120.0
    # Agent stream liveness events. The heartbeat interval only affects
    # non-persistent UI/CLI feedback while a turn is still active; the idle
    # timeout remains the real upstream stall detector.
    agent_stream_heartbeat_interval_seconds: float = 15.0
    agent_stream_idle_timeout_seconds: float = 600.0
    # Browser-side fallback grace. Keep this above the gateway stream idle
    # timeout so server terminal errors arrive before the WebUI local fallback.
    webui_stream_idle_grace_seconds: float = 630.0
    # Maximum time the WebUI WebSocket may sit silent before the gateway
    # closes it with code 1011 and emits ``gateway.client_ws_keepalive_timeout``.
    # ``0`` disables the keepalive deadline entirely (legacy behaviour).
    # Sleeping browsers commonly stop sending pings; without this knob the
    # server retains half-open connections after suspend.
    client_ws_keepalive_timeout_s: float = 120.0
    # WebSocket per-connection outbound writer queue. When enabled, every connection gets a
    # bounded asyncio.Queue + dedicated writer task; producers enqueue and
    # return immediately. Slow clients trigger a fast 1011 close instead of
    # back-pressuring the turn pipeline. Kill switch is read at connection
    # registration time only — affects new connections only; existing
    # connections retain their startup-time behavior.
    ws_writer_queue_enabled: bool = True
    # Per-connection outbox depth. 512 is ~17s of buffered text_delta at
    # 30 Hz, comfortably within the SessionStreamRegistry replay window
    # (max_events_per_session=500). Minimum 16 to avoid pathological
    # configurations that can never enqueue.
    ws_writer_queue_maxsize: int = Field(default=512, ge=16)
    # Legacy alias for the old runtime timeout setting. Kept so existing
    # configs that set llm_timeout_seconds still affect the agent runtime
    # budget until operators move to agent_runtime_timeout_seconds.
    llm_timeout_seconds: float | None = None

    # Search
    search_provider: str = "duckduckgo"
    search_api_key: str = ""
    search_api_key_env: str = ""
    search_max_results: int = Field(
        default=DEFAULT_SEARCH_MAX_RESULTS, ge=1, le=MAX_SEARCH_RESULTS
    )
    search_proxy: str = ""
    search_use_env_proxy: bool = False
    search_fallback_policy: Literal["off", "network"] = "off"
    search_diagnostics: bool = False

    # State/config paths
    state_dir: str | None = Field(default_factory=lambda: str(default_opensquilla_home() / "state"))
    config_path: str | None = None

    # Config schema migration stamp. Owned by migrate_config_payload
    # (gateway/config_migration.py), which injects LATEST_CONFIG_VERSION into
    # every payload it returns so version-gated one-time migrations run
    # exactly once per config file. Must stay optional with a default: a bare
    # GatewayConfig() is constructed throughout the codebase.
    config_version: int = LATEST_CONFIG_VERSION

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Default source order, with env-backed sources filtered so
        # OPENSQUILLA_GATEWAY_CONFIG_VERSION can never populate the migration
        # stamp — see _EnvWithoutConfigVersion for the full rationale.
        return (
            init_settings,
            _EnvWithoutConfigVersion(env_settings),
            _EnvWithoutConfigVersion(dotenv_settings),
            file_secret_settings,
        )

    def model_post_init(self, __context: Any) -> None:
        self._apply_concurrency_env_overrides()

    def _apply_concurrency_env_overrides(self) -> None:
        """Apply task/channel concurrency environment overrides.

        Invalid (non-integer) values fall back to the config default with a warning log.
        """
        import logging

        _log = logging.getLogger(__name__)

        task_env = os.environ.get("OPENSQUILLA_TASK_MAX_CONCURRENCY")
        if task_env is not None:
            try:
                task_val = int(task_env)
            except (ValueError, TypeError):
                _log.warning(
                    "OPENSQUILLA_TASK_MAX_CONCURRENCY=%r is not a valid integer; "
                    "falling back to default max_concurrency=%d",
                    task_env,
                    self.task_runtime.max_concurrency,
                )
            else:
                if task_val < 1:
                    _log.warning(
                        "OPENSQUILLA_TASK_MAX_CONCURRENCY=%r is below minimum 1; "
                        "falling back to default max_concurrency=%d",
                        task_env,
                        self.task_runtime.max_concurrency,
                    )
                else:
                    self.task_runtime.max_concurrency = task_val

        channel_env = os.environ.get("OPENSQUILLA_CHANNEL_INFLIGHT_CAP")
        if channel_env is not None:
            try:
                channel_val = int(channel_env)
            except (ValueError, TypeError):
                _log.warning(
                    "OPENSQUILLA_CHANNEL_INFLIGHT_CAP=%r is not a valid integer; "
                    "falling back to default channel_inflight_cap=%d",
                    channel_env,
                    self.task_runtime.channel_inflight_cap,
                )
            else:
                if channel_val < 1:
                    _log.warning(
                        "OPENSQUILLA_CHANNEL_INFLIGHT_CAP=%r is below minimum 1; "
                        "falling back to default channel_inflight_cap=%d",
                        channel_env,
                        self.task_runtime.channel_inflight_cap,
                    )
                else:
                    self.task_runtime.channel_inflight_cap = channel_val

        ws_enabled_env = os.environ.get("OPENSQUILLA_WS_WRITER_QUEUE_ENABLED")
        if ws_enabled_env is not None:
            normalized = ws_enabled_env.strip().lower()
            if normalized in ("true", "1", "yes"):
                self.ws_writer_queue_enabled = True
            elif normalized in ("false", "0", "no"):
                self.ws_writer_queue_enabled = False
            else:
                _log.warning(
                    "OPENSQUILLA_WS_WRITER_QUEUE_ENABLED=%r is not a valid bool; "
                    "falling back to default ws_writer_queue_enabled=%s",
                    ws_enabled_env,
                    self.ws_writer_queue_enabled,
                )

        ws_maxsize_env = os.environ.get("OPENSQUILLA_WS_WRITER_QUEUE_MAXSIZE")
        if ws_maxsize_env is not None:
            try:
                ws_maxsize_val = int(ws_maxsize_env)
            except (ValueError, TypeError):
                _log.warning(
                    "OPENSQUILLA_WS_WRITER_QUEUE_MAXSIZE=%r is not a valid integer; "
                    "falling back to default ws_writer_queue_maxsize=%d",
                    ws_maxsize_env,
                    self.ws_writer_queue_maxsize,
                )
            else:
                if ws_maxsize_val < 16:
                    _log.warning(
                        "OPENSQUILLA_WS_WRITER_QUEUE_MAXSIZE=%r is below minimum 16; "
                        "falling back to default ws_writer_queue_maxsize=%d",
                        ws_maxsize_env,
                        self.ws_writer_queue_maxsize,
                    )
                else:
                    self.ws_writer_queue_maxsize = ws_maxsize_val

    def memory_mode_fingerprint(self) -> dict[str, str]:
        """Return the stable memory knobs used for attribution."""
        capture_effective_enabled = (
            self.memory.auto_capture_enabled and self.memory.capture_mode != "off"
        )
        return {
            "mode": "stable",
            "prompt_cache_mode": self.prompt_cache.effective_mode,
            "query_embedding_cache": self.memory.cost.query_embedding_cache,
            "dream_input_slimming": self.memory.dream.input_slimming,
            "dream_preview_mode": str(self.memory.dream.preview_mode).lower(),
            "dream_auto_schedule": str(self.memory.dream.auto_schedule).lower(),
            "daily_note_max_chars": str(self.memory.daily_note_max_chars),
            "daily_notes_total_max_chars": str(self.memory.daily_notes_total_max_chars),
            "auto_capture_enabled": str(self.memory.auto_capture_enabled).lower(),
            "capture_effective_enabled": str(capture_effective_enabled).lower(),
            "capture_mode": self.memory.capture_mode,
            "capture_user": str(self.memory.capture_user).lower(),
            "capture_assistant": str(self.memory.capture_assistant).lower(),
            "capture_excluded_run_kinds": ",".join(self.memory.capture_excluded_run_kinds),
            "capture_excluded_provenance_kinds": ",".join(
                self.memory.capture_excluded_provenance_kinds
            ),
            "capture_roll_max_chars": str(self.memory.capture_roll_max_chars),
            "dream_enabled": str(self.memory.dream.enabled).lower(),
        }
    _runtime_secret_paths: set[str] = PrivateAttr(default_factory=set)
    # Paths whose secret value was explicitly entered by the operator (set by
    # ``clear_runtime_secret``): value-coincidence redaction heuristics in
    # ``to_toml_dict`` must not strip them, even when the entered value
    # happens to equal the corresponding environment variable.
    _explicit_secret_paths: set[str] = PrivateAttr(default_factory=set)
    # Sparse-persist provenance (consumed by onboarding.config_store):
    # - _persist_baseline: the TOML dump captured when THIS instance was
    #   loaded (or last persisted). Instance-scoped by design — a path-keyed
    #   global baseline lets a second live object for the same file diff
    #   against another writer's snapshot and silently revert its changes.
    # - _runtime_field_overrides: path -> (stored_value, applied_value) for
    #   fields the runtime resolved in place from the environment (e.g.
    #   llm.base_url from OPENAI_BASE_URL). Persisting restores stored_value
    #   whenever the field still equals applied_value, so env-derived values
    #   never leak into config.toml.
    # - _force_persist_paths: unambiguous path-segment tuples for explicit
    #   mutations that must be written even when equal to the model default
    #   (e.g. a deliberate image_generation.enabled = false on a fresh config).
    #   Tuples preserve dynamic mapping keys that contain dots.
    _persist_baseline: dict[str, Any] | None = PrivateAttr(default=None)
    _persist_raw_base: dict[str, Any] | None = PrivateAttr(default=None)
    _runtime_field_overrides: dict[str, tuple[Any, Any]] = PrivateAttr(default_factory=dict)
    _force_persist_paths: set[tuple[str, ...]] = PrivateAttr(default_factory=set)
    _provider_resolution: dict[str, Any] = PrivateAttr(default_factory=dict)

    def to_toml_dict(self) -> dict[str, Any]:
        """Convert config to a TOML-writable dict."""
        data: dict[str, Any] = self.model_dump(exclude_none=True, exclude_defaults=False)
        if not data.get("agents"):
            data.pop("agents", None)
        llm = data.get("llm")
        if isinstance(llm, dict):
            if not llm.get("api_key_env"):
                llm.pop("api_key_env", None)
            if not llm.get("api_key"):
                llm.pop("api_key", None)
        llm_profiles = data.get("llm_profiles")
        if isinstance(llm_profiles, dict):
            # Empty credential fields are absence, not a stored credential.
            # Keeping them out of the canonical dump also lets the sparse
            # persister delete a previously populated field when an explicit
            # credential-clear mutation sets it back to empty.
            for profile in llm_profiles.values():
                if not isinstance(profile, dict):
                    continue
                if not profile.get("api_key"):
                    profile.pop("api_key", None)
                if not profile.get("api_key_env"):
                    profile.pop("api_key_env", None)
                if not profile.get("api_key_env_pool"):
                    profile.pop("api_key_env_pool", None)
        if not data.get("search_api_key_env"):
            data.pop("search_api_key_env", None)
        elif not data.get("search_api_key"):
            data.pop("search_api_key", None)
        tools_table = data.get("tools")
        if isinstance(tools_table, dict) and not tools_table.get("description_overrides"):
            # Keep written configs byte-identical to pre-mechanism output when
            # no overrides are configured.
            tools_table.pop("description_overrides", None)
        # Heuristic guard for the pre-provenance era: a value equal to the
        # env var is assumed env-sourced and dropped. Skipped when the
        # operator explicitly entered the key (recorded by
        # ``clear_runtime_secret``) — an explicit entry must persist even
        # when it coincides with the env value.
        if "audio.providers.elevenlabs.api_key" not in self._explicit_secret_paths:
            _delete_env_sourced_secret(
                data,
                "audio.providers.elevenlabs.api_key",
                "audio.providers.elevenlabs.api_key_env",
                default_env="ELEVENLABS_API_KEY",
                settings_env="OPENSQUILLA_AUDIO_PROVIDERS__ELEVENLABS__API_KEY",
            )
        router = data.get("squilla_router")
        if isinstance(router, dict) and router.get("tier_profile"):
            profile = str(router["tier_profile"]).strip().lower()
            if profile not in ROUTER_TIER_PROFILE_IDS:
                # Downgrade-contract enforcement point: rc1 loaders hard-reject
                # unknown tier_profile ids at validation time, so persisting a
                # non-legacy id (e.g. a synthesized preset id) would brick the
                # config on downgrade. Keep the dump loadable everywhere by
                # omitting the unknown profile id and leaving the effective
                # tiers expanded inline. Unreachable today — validation still
                # rejects non-legacy ids — but this chokepoint enforces the
                # invariant independently of the validator.
                router.pop("tier_profile", None)
            else:
                try:
                    defaults = _router_tier_profile_defaults(profile)
                except ValueError:  # pragma: no cover - membership checked above
                    defaults = None
                if defaults is not None and router.get("tiers") == defaults:
                    router.pop("tiers", None)
        for path in sorted(self._runtime_secret_paths):
            _delete_path(data, path)
        return data

    def to_public_dict(self) -> dict[str, Any]:
        """Return a redacted config view safe for public control surfaces."""
        data = cast(dict[str, Any], redact_public_config(self.model_dump()))
        ensemble = data.get("llm_ensemble")
        if isinstance(ensemble, dict):
            from opensquilla.gateway.model_routing import (
                ensemble_activation_preview,
                ensemble_selection_configured,
            )

            ensemble["selection_configured"] = ensemble_selection_configured(self)
            ensemble["activation_preview"] = ensemble_activation_preview(self)
        privacy = data.get("privacy")
        if isinstance(privacy, dict):
            from opensquilla.observability.network_policy import (
                provider_request_correlation_disabled,
            )

            privacy["network_observability_disabled_effective"] = (
                provider_request_correlation_disabled(config=self)
            )
        return data

    def mark_runtime_secret(self, path: str) -> None:
        self._runtime_secret_paths.add(path)

    def clear_runtime_secret(self, path: str) -> None:
        self._runtime_secret_paths.discard(path)
        # Clearing records operator provenance: every mutation surface calls
        # this exactly when the user supplied an explicit new value for the
        # secret, so value-coincidence heuristics (the env == value deletion
        # in ``to_toml_dict``) must no longer strip the path from persist
        # dumps — an explicit key equal to the env value is still explicit.
        self._explicit_secret_paths.add(path)

    def forget_secret_provenance(self, path: str) -> None:
        """Forget both runtime and explicit authorship for a removed secret."""

        self._runtime_secret_paths.discard(path)
        self._explicit_secret_paths.discard(path)

    def inherit_runtime_secrets(self, other: GatewayConfig) -> None:
        self._runtime_secret_paths = set(other._runtime_secret_paths)
        self._explicit_secret_paths = set(other._explicit_secret_paths)

    def record_runtime_override(self, path: str, stored: Any, applied: Any) -> None:
        """Record that ``path`` was resolved in place from the environment.

        ``stored`` is the value the persisted config carried before the
        runtime override; ``applied`` is the value now living on the model.
        The sparse persister restores ``stored`` whenever the field still
        equals ``applied``, so a boot-time env override never gets baked
        into config.toml by an unrelated save.

        Repeated records for the same path keep the ORIGINAL stored slot and
        update only ``applied``: a re-resolve on the same instance reads the
        field AFTER the first resolution already wrote the env value into it,
        so its ``stored`` argument reflects the applied env value (or a later
        in-memory mutation), not disk provenance — chaining it would make a
        later persist "restore" a value that was never on disk.
        ``clear_runtime_override`` is the explicit reset used when an
        operator supplies a genuinely new stored value.
        """
        existing = self._runtime_field_overrides.get(path)
        if existing is not None:
            stored = existing[0]
        self._runtime_field_overrides[path] = (stored, applied)

    def clear_runtime_override(self, path: str) -> None:
        self._runtime_field_overrides.pop(path, None)

    def runtime_field_overrides(self) -> dict[str, tuple[Any, Any]]:
        return dict(self._runtime_field_overrides)

    def inherit_persist_provenance(self, other: GatewayConfig) -> None:
        """Adopt ``other``'s sparse-persist snapshot and mutation provenance.

        For mirroring a mutation clone back onto the live gateway config:
        the clone started from a deep copy of THIS instance's provenance and
        then applied the operator's ``clear_runtime_override`` /
        ``mark_force_persist`` decisions, so it is authoritative. Without
        this, a record cleared on the clone never reaches the live config,
        and the stale live record makes a later unrelated persist rewrite
        the field back to a value the operator just replaced.
        """
        self._persist_baseline = copy.deepcopy(other._persist_baseline)
        self._persist_raw_base = copy.deepcopy(other._persist_raw_base)
        self._runtime_field_overrides = dict(other._runtime_field_overrides)
        self._force_persist_paths = set(other._force_persist_paths)
        self._provider_resolution = dict(other._provider_resolution)

    def provider_resolution(self) -> dict[str, Any]:
        """Return non-secret provider identity provenance for diagnostics."""

        if self._provider_resolution:
            return dict(self._provider_resolution)
        provider = str(getattr(self.llm, "provider", "") or "").strip().lower()
        return {
            "status": "explicit",
            "effective_provider": provider,
            "source": "config",
            "reason_code": "provider_explicit",
            "action_required": False,
            "action_recommended": False,
        }

    def set_provider_resolution(
        self,
        *,
        status: str,
        effective_provider: str,
        source: str,
        reason_code: str,
        action_required: bool = False,
        action_recommended: bool = False,
    ) -> None:
        """Update runtime-only provider identity provenance after a mutation."""

        self._provider_resolution = {
            "status": str(status),
            "effective_provider": str(effective_provider),
            "source": str(source),
            "reason_code": str(reason_code),
            "action_required": bool(action_required),
            "action_recommended": bool(action_recommended),
        }

    def set_persist_snapshot(
        self,
        baseline: dict[str, Any],
        raw_base: dict[str, Any] | None,
    ) -> None:
        """Record the model and raw-disk state represented by this instance."""
        self._persist_baseline = copy.deepcopy(baseline)
        self._persist_raw_base = copy.deepcopy(raw_base)

    def reconcile_runtime_overrides(self, other: GatewayConfig) -> None:
        """Refresh override records after ``other``'s values are applied here.

        Rule for in-place config swaps (``config.set`` / ``patch`` /
        ``apply`` / ``reload``, where ``other`` was built independently of
        this instance and may carry freshly re-derived records):

        - a pre-existing record on THIS instance survives only while
          ``other``'s live value still equals the record's applied value —
          otherwise the recorded env application no longer describes the new
          state, and restoring its stored slot at persist time would rewrite
          provenance that no longer holds (e.g. reverting a hand-edited
          ``llm.base_url`` to the boot-time stored value);
        - ``other``'s own records win per path, except that when ``other``
          re-resolved on top of a value THIS instance had already
          env-applied (its stored slot equals our applied slot), the
          original disk-provenance stored slot is kept and only the applied
          value advances — mirroring ``record_runtime_override``'s
          non-chaining rule across instances.
        """

        def _live_value(model: Any, path: str) -> Any:
            current: Any = model
            for part in path.split("."):
                current = getattr(current, part, None)
                if current is None:
                    return None
            return current

        merged: dict[str, tuple[Any, Any]] = {}
        for path, (stored, applied) in self._runtime_field_overrides.items():
            if _live_value(other, path) == applied:
                merged[path] = (stored, applied)
        for path, (stored, applied) in other._runtime_field_overrides.items():
            prior = self._runtime_field_overrides.get(path)
            if prior is not None and prior[1] == stored:
                stored = prior[0]
            merged[path] = (stored, applied)
        self._runtime_field_overrides = merged
        # ``other`` is the config that was just loaded or successfully
        # persisted. Its snapshot is therefore the baseline the live object
        # must carry into the next sparse mutation.
        self._persist_baseline = copy.deepcopy(other._persist_baseline)
        self._persist_raw_base = copy.deepcopy(other._persist_raw_base)
        self._force_persist_paths = set(other._force_persist_paths)
        self._provider_resolution = dict(other._provider_resolution)

    def mark_force_persist(self, path: str) -> None:
        """Always write ``path`` on the next persist, even if it equals the
        model default — used for explicit boolean decisions (e.g. a
        deliberate ``image_generation.enabled = false``) that keep-current
        logic must be able to see in the file."""
        self.mark_force_persist_segments(tuple(path.split(".")))

    def mark_force_persist_segments(self, path: tuple[str, ...]) -> None:
        """Mark an exact config path while preserving dotted mapping keys."""
        if path:
            self._force_persist_paths.add(tuple(path))

    def force_persist_path_segments(self) -> set[tuple[str, ...]]:
        """Return exact one-shot force paths for the persistence layer."""
        return set(self._force_persist_paths)

    def consume_force_persist_path_segments(self, paths: set[tuple[str, ...]]) -> None:
        """Clear force paths after their write commits successfully."""
        self._force_persist_paths.difference_update(paths)

    def force_persist_paths(self) -> set[str]:
        """Return dotted display paths for compatibility with existing callers."""
        return {".".join(path) for path in self._force_persist_paths}

    @staticmethod
    def _resolve_profile_path(raw: str, config_path: Path) -> str:
        path = Path(raw).expanduser()
        if path.is_absolute():
            return str(path)
        return str((config_path.expanduser().absolute().parent / path).absolute())

    @classmethod
    def _apply_profile_path_overrides(cls, cfg: GatewayConfig, config_path: Path) -> None:
        """Resolve data roots relative to the profile config on every surface."""

        for field_name in ("state_dir", "workspace_dir"):
            raw = getattr(cfg, field_name, None)
            if isinstance(raw, str) and raw.strip():
                setattr(cfg, field_name, cls._resolve_profile_path(raw, config_path))

        # Explicit TOML kwargs outrank BaseSettings env sources. Apply the two
        # public data-root overrides here so runtime and recovery inspection
        # select exactly the same paths.
        path_overrides = {
            "state_dir": os.environ.get("OPENSQUILLA_GATEWAY_STATE_DIR", "").strip(),
            "workspace_dir": (
                os.environ.get("OPENSQUILLA_GATEWAY_WORKSPACE_DIR", "").strip()
                or os.environ.get("OPENSQUILLA_WORKSPACE_DIR", "").strip()
            ),
        }
        for field_name, override in path_overrides.items():
            if not override:
                continue
            stored = getattr(cfg, field_name, None)
            applied = cls._resolve_profile_path(override, config_path)
            setattr(cfg, field_name, applied)
            cfg.record_runtime_override(field_name, stored, applied)

    @classmethod
    def load_from_toml(cls, path: str | Path) -> GatewayConfig:
        """Load config from a TOML file."""
        import tomllib

        target = Path(path)
        with open(native_io_path(target), "rb") as f:
            try:
                data = tomllib.load(f)
            except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
                raise ConfigParseError(target, exc) from exc
        migration = migrate_config_payload(data)
        cfg = cls(**migration.payload)
        cfg._mark_env_absorbed_secrets(data)
        cls._apply_profile_path_overrides(cfg, target)
        if migration.changed:
            _rewrite_migrated_config_best_effort(target, migration)
        return cfg

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        *,
        read_only: bool = False,
    ) -> GatewayConfig:
        """Auto-discover and load config.

        Precedence: explicit path > current-directory config > user config > defaults.
        Environment variables always override TOML values (Pydantic Settings behavior).
        ``read_only`` applies compatibility transforms in memory without
        rewriting the source profile.
        """
        import tomllib

        candidates: list[Path] = []
        if config_path:
            # Expand ~ / $HOME so an explicit path like "~/cfg.toml" resolves,
            # mirroring config_store.resolve_config_path; without this an
            # explicit config was silently dropped and defaults loaded.
            candidates.append(Path(config_path).expanduser())
        else:
            candidates.append((Path.cwd() / "opensquilla.toml").expanduser())
            candidates.append((default_opensquilla_home() / "config.toml").expanduser())

        for path in candidates:
            native_path = native_io_path(path)
            if native_path.is_file():
                with open(native_path, "rb") as f:
                    try:
                        data = tomllib.load(f)
                    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
                        raise ConfigParseError(path, exc) from exc
                migration = migrate_config_payload(data, emit_diagnostics=not read_only)
                cfg = cls(**migration.payload)
                cls._apply_profile_path_overrides(cfg, path)
                if migration.changed and not read_only:
                    _rewrite_migrated_config_best_effort(path, migration)
                cfg.config_path = str(path)
                cfg._mark_env_absorbed_secrets(data)
                cfg.set_persist_snapshot(cfg.to_toml_dict(), migration.payload)
                return cfg

        cfg = cls()
        default_config_path = (
            candidates[0]
            if candidates
            else default_opensquilla_home().expanduser() / "config.toml"
        )
        cls._apply_profile_path_overrides(cfg, default_config_path)
        cfg._mark_env_absorbed_secrets(None)
        if config_path:
            cfg.config_path = str(candidates[0])
        cfg.set_persist_snapshot(cfg.to_toml_dict(), None)
        return cfg

    def _mark_env_absorbed_secrets(self, raw: Any) -> None:
        """Mark auth secrets present only because the environment supplied them.

        ``OPENSQUILLA_AUTH_TOKEN`` / ``_PASSWORD`` are absorbed into
        :class:`AuthConfig` at construction. If such an env-only value is not
        marked as a runtime secret, ``to_toml_dict`` would bake it into a
        full-dump persist, after which the on-disk value silently overrides
        later env rotation. The shared marking logic (which also covers
        ``llm.api_key`` and provider keys) lives in ``config_store``; import it
        lazily to avoid an import cycle.
        """
        try:
            from opensquilla.onboarding.config_store import (
                _mark_env_absorbed_runtime_secrets,
            )
        except Exception:  # pragma: no cover - defensive, keep boot resilient
            return
        _mark_env_absorbed_runtime_secrets(self, raw)


# --- bind-address resolution ----------------------------------------------


def _rewrite_migrated_config_best_effort(path: Path, migration: Any) -> None:
    """Persist a migrated config, degrading to a warning when not writable.

    The migrated payload already validated and the gateway can run from it;
    a read-only config location (mounted backup, locked-down home) must not
    turn that into a boot failure. The rewrite is retried on the next load.
    """
    try:
        backup_and_write_migrated_config(path, migration.payload, migration)
    except OSError as error:
        import logging

        logging.getLogger(__name__).warning(
            "OpenSquilla config migration could not rewrite %s (%s); running "
            "from the migrated payload in memory. Make the file writable to "
            "persist the migration and silence this warning.",
            path,
            error,
        )

# Wildcard addresses that expose the gateway on every interface. Used by the
# boot banner and the install-script post-install message.
PUBLIC_BIND_ADDRESSES: frozenset[str] = frozenset({"0.0.0.0", "::"})


def is_public_bind(host: str) -> bool:
    """Return True if ``host`` resolves to an IPv4/IPv6 wildcard."""
    return host in PUBLIC_BIND_ADDRESSES


def resolve_listen_address(
    flag_value: str | None,
    env: dict[str, str] | None = None,
    default: str = "127.0.0.1",
) -> str:
    """Resolve the gateway bind address with an explicit precedence order.

    Precedence (highest first):
      1. ``flag_value`` (e.g. ``opensquilla gateway run --listen 0.0.0.0``)
      2. ``OPENSQUILLA_LISTEN`` env var
      3. ``OPENSQUILLA_GATEWAY_HOST`` env var (legacy canonical)
      4. ``default`` (127.0.0.1)

    ``env`` defaults to ``os.environ`` for dependency injection in tests.
    """
    if flag_value:
        return flag_value
    env = env if env is not None else dict(os.environ)
    for key in ("OPENSQUILLA_LISTEN", "OPENSQUILLA_GATEWAY_HOST"):
        value = env.get(key)
        if value:
            return value
    return default


# --- Public config redaction (pilot) --------------------------------------

_PUBLIC_SECRET_EXACT_KEYS = frozenset(
    {
        "token",
        "password",
        "api_key",
        "authorization",
        "signing_secret",
        "app_secret",
        "verification_token",
        # Channel-crypto secrets that no generic suffix above catches:
        # channels.feishu.encrypt_key (event decryption key) and
        # channels.wecom.encoding_aes_key (callback AES key). Exact names on
        # purpose — NOT a blanket "_key" suffix: key-NAME/reference fields
        # must stay readable (llm.api_key_env and the other *_env fields name
        # WHICH env var a secret loads from and clients render them), and a
        # "_key" suffix would also swallow future non-secret identifiers
        # (session/public/idempotency keys). Add further crypto-material
        # fields here individually, never by widening the suffix set.
        "encrypt_key",
        "encoding_aes_key",
    }
)
_PUBLIC_SECRET_SUFFIXES = ("_token", "_secret", "_password", "_api_key")
_REDACTED = "[redacted]"


def is_sensitive_config_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _PUBLIC_SECRET_EXACT_KEYS or normalized.endswith(_PUBLIC_SECRET_SUFFIXES)


def redact_public_config(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive_config_key(key) and item:
                redacted[key] = _REDACTED
            else:
                redacted[key] = redact_public_config(item)
        return redacted
    if isinstance(value, list):
        return [redact_public_config(item) for item in value]
    return value


def _delete_path(obj: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    current = obj
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            return
        current = next_value
    current.pop(parts[-1], None)


def _get_path(obj: dict[str, Any], path: str) -> Any:
    current: Any = obj
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _delete_env_sourced_secret(
    obj: dict[str, Any],
    secret_path: str,
    env_path: str,
    *,
    default_env: str,
    settings_env: str | None = None,
) -> None:
    value = str(_get_path(obj, secret_path) or "").strip()
    if not value:
        _delete_path(obj, secret_path)
        return
    env_name = str(_get_path(obj, env_path) or default_env).strip() or default_env
    if os.environ.get(env_name) == value or (
        settings_env is not None and os.environ.get(settings_env) == value
    ):
        _delete_path(obj, secret_path)
