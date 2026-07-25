"""GatewayConfig — Pydantic Settings for the gateway."""

from __future__ import annotations

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
from pydantic_settings import BaseSettings, SettingsConfigDict

from opensquilla import __version__
from opensquilla.gateway.config_migration import (
    backup_and_write_migrated_config,
    migrate_config_payload,
)
from opensquilla.paths import default_opensquilla_home
from opensquilla.router_tiers import (
    DEFAULT_TEXT_TIER,
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


class CorsConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_CORS_")

    allowed_origins: list[str] = Field(default_factory=lambda: ["*"])
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


class RateLimitConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_RATE_")

    enabled: bool = True
    max_requests: int = 100
    window_seconds: int = 60


class ControlUiConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_CONTROL_UI_")

    enabled: bool = True
    base_path: str = "/control"
    frontend: Literal["vue", "legacy"] = "vue"
    # Default UI locale served on first paint when the browser has no saved
    # preference. The client (localStorage) and a manual switch always override
    # it. Anything zh* clamps to zh-Hans; anything else to en.
    default_locale: Literal["en", "zh-Hans", "ja", "fr", "de", "es"] = "en"
    allowed_origins: list[str] = Field(default_factory=list)

    @field_validator("base_path")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("frontend", mode="before")
    @classmethod
    def _normalize_frontend(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip().lower()
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

    profile: Literal["full", "minimal", "memory_only", "coding", "messaging"] | None = None
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    also_allow: list[str] = Field(default_factory=list)
    workspace_write_deny_globs: list[str] = Field(default_factory=list)
    trusted_fake_ip_cidrs: list[str] = Field(default_factory=list)

    @field_validator("trusted_fake_ip_cidrs")
    @classmethod
    def _validate_trusted_fake_ip_cidrs(cls, values: list[str]) -> list[str]:
        from opensquilla.tools.ssrf import validate_trusted_fake_ip_cidrs

        return validate_trusted_fake_ip_cidrs(values)


class PermissionsConfig(BaseModel):
    """Default owner permission posture for local/operator turns."""

    model_config = ConfigDict(extra="forbid")

    default_mode: Literal["off", "on", "bypass", "full"] = "bypass"


class TaskRuntimeConfig(BaseModel):
    """Server-side task-runtime queue settings."""

    max_concurrency: int = Field(default=4, ge=1)
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


class LlmProviderConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_LLM_")

    provider: str = "openrouter"
    model: str = "deepseek/deepseek-v4-pro"
    api_key: str = ""
    api_key_env: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    proxy: str = ""  # explicit HTTP proxy URL (e.g. http://127.0.0.1:7890)
    max_tokens: int = 0  # 0 = auto-resolve from model catalog; >0 = explicit override
    # Optional global thinking level: off|minimal|low|medium|high|xhigh|adaptive.
    # When unset, squilla_router may suggest thinking for selected tiers.
    thinking: str | None = None
    # Alias for thinking: accepts the same values. When both are set,
    # thinking_level takes precedence. This allows [llm] configs to use
    # the same field name as squilla_router.tiers for consistency.
    thinking_level: str | None = None
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

    platform_hint_enabled: bool = True


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
    """Default model routing config."""
    return {
        "c0": {
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-flash",
            "description": (
                "fast DeepSeek V4 Flash route for trivial chat, short rewrites, "
                "extraction, and low-risk simple Q&A"
            ),
            "supports_image": False,
            "thinking_level": "high",
        },
        "c1": {
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "description": (
                "default balanced text model for normal agent work, coding assistance, "
                "debugging, and moderate analysis"
            ),
            "supports_image": False,
            "thinking_level": "high",
        },
        "c2": {
            "provider": "openrouter",
            "model": "z-ai/glm-5.2",
            "description": (
                "stronger GLM 5.2 route for multi-step coding, structured reasoning, "
                "larger context synthesis, and harder analysis"
            ),
            "supports_image": False,
            "thinking_level": "high",
        },
        "c3": {
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "description": (
                "Highest-quality text reasoning model for difficult planning, "
                "deep review, complex debugging, and high-stakes synthesis"
            ),
            "supports_image": False,
            "thinking_level": "high",
        },
        "image_model": {
            "provider": "openrouter",
            "model": "moonshotai/kimi-k2.6",
            "description": (
                "Image model: vision-capable route for user-supplied image attachments, "
                "screenshots, diagrams, and visual question answering"
            ),
            "supports_image": True,
            "image_only": True,
            "thinking_level": "medium",
        },
    }


ROUTER_TIER_PROFILE_IDS = frozenset(
    {
        "openrouter",
        "dashscope",
        "deepseek",
        "gemini",
        "volcengine",
        "byteplus",
        "openai",
        "zhipu",
        "moonshot",
    }
)


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
    normalized = (profile or "openrouter").strip().lower()
    if normalized not in ROUTER_TIER_PROFILE_IDS:
        allowed = ", ".join(sorted(ROUTER_TIER_PROFILE_IDS))
        raise ValueError(
            f"unknown squilla_router.tier_profile {profile!r}; expected one of {allowed}"
        )
    if normalized == "openrouter":
        return _default_tiers()
    profiles = {
        "openai": {
            "c0": {
                "provider": "openai",
                "model": "gpt-5.4-nano",
                "description": (
                    "OpenAI fast route: GPT-5.4 Nano for fast, high-throughput simple work."
                ),
                "supports_image": False,
                "thinking_level": "none",
            },
            "c1": {
                "provider": "openai",
                "model": "gpt-5.4-mini",
                "description": "OpenAI balanced route: GPT-5.4 Mini for normal agent work.",
                "supports_image": False,
                "thinking_level": "low",
            },
            "c2": {
                "provider": "openai",
                "model": "gpt-5.5",
                "description": "OpenAI strong route: GPT-5.5 for complex text tasks.",
                "supports_image": False,
                "thinking_level": "medium",
            },
            "c3": {
                "provider": "openai",
                "model": "gpt-5.5",
                "description": (
                    "OpenAI highest route: GPT-5.5 with high reasoning; GPT-5.5 Pro is "
                    "excluded because it is not streaming-compatible."
                ),
                "supports_image": False,
                "thinking_level": "high",
            },
        },
        "dashscope": {
            "c0": {
                "provider": "dashscope",
                "model": "qwen3.6-flash",
                "description": (
                    "DashScope fast route: Qwen3.6 Flash for simple text tasks; "
                    "pending live smoke."
                ),
                "supports_image": False,
            },
            "c1": {
                "provider": "dashscope",
                "model": "qwen3.6-plus",
                "description": (
                    "DashScope balanced route: Qwen3.6 Plus for normal agent and "
                    "coding work; pending live smoke."
                ),
                "supports_image": False,
            },
            "c2": {
                "provider": "dashscope",
                "model": "qwen3-max",
                "description": "DashScope strong route: Qwen3 Max for complex text tasks.",
                "supports_image": False,
            },
            "c3": {
                "provider": "dashscope",
                "model": "qwen3-max",
                "description": (
                    "DashScope highest route: Qwen3 Max; higher-thinking behavior "
                    "requires future payload support."
                ),
                "supports_image": False,
            },
        },
        "deepseek": {
            "c0": {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "description": (
                    "DeepSeek fast route: V4 Flash with no router-requested thinking; "
                    "request ID pending live smoke."
                ),
                "supports_image": False,
                "thinking_level": "off",
            },
            "c1": {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "description": (
                    "DeepSeek balanced route: V4 Flash with thinking enabled; request "
                    "ID pending live smoke."
                ),
                "supports_image": False,
                "thinking_level": "low",
            },
            "c2": {
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "description": (
                    "DeepSeek strong route: V4 Pro with thinking enabled; request ID "
                    "pending live smoke."
                ),
                "supports_image": False,
                "thinking_level": "medium",
            },
            "c3": {
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "description": (
                    "DeepSeek highest route: same V4 Pro wire behavior until "
                    "effort-level support is added."
                ),
                "supports_image": False,
                "thinking_level": "high",
            },
        },
        "gemini": {
            "c0": {
                "provider": "gemini",
                "model": "gemini-2.5-flash-lite",
                "description": "Gemini fast route: 2.5 Flash-Lite for low-latency tasks.",
                "supports_image": False,
            },
            "c1": {
                "provider": "gemini",
                "model": "gemini-2.5-flash",
                "description": "Gemini balanced route: 2.5 Flash for normal agent work.",
                "supports_image": False,
                "thinking_level": "low",
            },
            "c2": {
                "provider": "gemini",
                "model": "gemini-2.5-pro",
                "description": "Gemini strong route: 2.5 Pro for complex coding and reasoning.",
                "supports_image": False,
                "thinking_level": "medium",
            },
            "c3": {
                "provider": "gemini",
                "model": "gemini-2.5-pro",
                "description": (
                    "Gemini highest route: 2.5 Pro with high thinking; 3.1 preview "
                    "remains opt-in."
                ),
                "supports_image": False,
                "thinking_level": "high",
            },
        },
        "zhipu": {
            "c0": {
                "provider": "zhipu",
                "model": "glm-4.7-flashx",
                "description": (
                    "Zhipu fast route: GLM-4.7 FlashX for simple text tasks; live smoke "
                    "may require fallback."
                ),
                "supports_image": False,
            },
            "c1": {
                "provider": "zhipu",
                "model": "glm-5",
                "description": "Zhipu balanced route: GLM-5 for normal agent work.",
                "supports_image": False,
                "thinking_level": "low",
            },
            "c2": {
                "provider": "zhipu",
                "model": "glm-5.1",
                "description": "Zhipu strong route: GLM-5.1 for complex text tasks.",
                "supports_image": False,
                "thinking_level": "medium",
            },
            "c3": {
                "provider": "zhipu",
                "model": "glm-5.1",
                "description": "Zhipu highest route: GLM-5.1 with high reasoning effort.",
                "supports_image": False,
                "thinking_level": "high",
            },
        },
        "moonshot": {
            "c0": {
                "provider": "moonshot",
                "model": "kimi-k2.5",
                "description": (
                    "Moonshot fast route: Kimi K2.5 for cost-efficient agent work "
                    "with 256K context."
                ),
                "supports_image": True,
                "thinking_level": "low",
            },
            "c1": {
                "provider": "moonshot",
                "model": "kimi-k2.5",
                "description": (
                    "Moonshot balanced route: Kimi K2.5 for normal multimodal "
                    "agent work."
                ),
                "supports_image": True,
                "thinking_level": "medium",
            },
            "c2": {
                "provider": "moonshot",
                "model": "kimi-k2.6",
                "description": (
                    "Moonshot strong route: Kimi K2.6 for complex coding, reasoning, "
                    "and multimodal tasks."
                ),
                "supports_image": True,
                "thinking_level": "medium",
            },
            "c3": {
                "provider": "moonshot",
                "model": "kimi-k2.6",
                "description": (
                    "Moonshot highest route: Kimi K2.6 for the hardest long-horizon "
                    "agent work."
                ),
                "supports_image": True,
                "thinking_level": "high",
            },
        },
        "volcengine": {
            "c0": {
                "provider": "volcengine",
                "model": "doubao-seed-2-0-mini-260215",
                "description": (
                    "Volcengine fast route: Doubao Seed 2.0 Mini for low-latency, "
                    "low-cost simple text tasks."
                ),
                "supports_image": False,
                "thinking_level": "off",
            },
            "c1": {
                "provider": "volcengine",
                "model": "doubao-seed-2-0-lite-260215",
                "description": (
                    "Volcengine balanced route: Doubao Seed 2.0 Lite for daily agent "
                    "work with lower cost than Pro."
                ),
                "supports_image": False,
                "thinking_level": "low",
            },
            "c2": {
                "provider": "volcengine",
                "model": "doubao-seed-2-0-pro-260215",
                "description": (
                    "Volcengine strong route: Doubao Seed 2.0 Pro for complex "
                    "reasoning and multimodal-capable text work."
                ),
                "supports_image": False,
                "thinking_level": "medium",
            },
            "c3": {
                "provider": "volcengine",
                "model": "doubao-seed-2-0-code-preview-260215",
                "description": (
                    "Volcengine highest route: Doubao Seed 2.0 Code Preview for the "
                    "hardest coding and code-review routes."
                ),
                "supports_image": False,
                "thinking_level": "high",
            },
        },
        "byteplus": {
            "c0": {
                "provider": "byteplus",
                "model": "seed-2-0-lite-260228",
                "description": (
                    "BytePlus fast route: Seed 2.0 Lite for low-latency, "
                    "low-cost simple text tasks."
                ),
                "supports_image": False,
                "thinking_level": "off",
            },
            "c1": {
                "provider": "byteplus",
                "model": "seed-2-0-lite-260228",
                "description": (
                    "BytePlus balanced route: Seed 2.0 Lite for normal agent "
                    "work with provider thinking available."
                ),
                "supports_image": False,
                "thinking_level": "low",
            },
            "c2": {
                "provider": "byteplus",
                "model": "seed-2-0-lite-260228",
                "description": (
                    "BytePlus strong route: Seed 2.0 Lite with medium thinking "
                    "for multi-step text tasks."
                ),
                "supports_image": False,
                "thinking_level": "medium",
            },
            "c3": {
                "provider": "byteplus",
                "model": "seed-2-0-lite-260228",
                "description": (
                    "BytePlus highest route: Seed 2.0 Lite with high thinking "
                    "for harder long-horizon text tasks."
                ),
                "supports_image": False,
                "thinking_level": "high",
            },
        },
    }
    return {name: dict(value) for name, value in profiles[normalized].items()}


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
    visual_mode: str = "real_candidates"
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
    estimated_output_savings_pct: float = 0.03
    upgrade_to_c3_compaction_enabled: bool = True
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


class AgentTokenSavingConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_AGENT_TOKEN_SAVING_")

    # Tokenjuice projection is the default tool-result path.
    tool_result_projection_max_inline_chars: int = Field(default=60_000, ge=1000)
    tool_result_store_max_bytes: int = Field(default=8 * 1024 * 1024, ge=0)
    tool_result_store_disk_budget_bytes: int = Field(default=256 * 1024 * 1024, ge=0)
    tool_result_store_retention_seconds: int = Field(default=7 * 24 * 60 * 60, ge=0)


class CompactionLlmConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_COMPACTION_")

    model: str | None = None  # None = use session model
    timeout_seconds: float = 90.0
    enabled: bool = True
    compaction_profile: Literal["conversation", "coding", "research", "support"] = "conversation"
    protected_recent_messages: int = Field(default=0, ge=0)


class SessionNamingConfig(BaseSettings):
    """LLM-generated session titles (auto-naming).

    After the first user message, a one-shot LLM call summarizes it into a short
    title written to SessionNode.derived_title. Model selection mirrors compaction
    but defaults to the router's default text tier rather than the session model:
    ``model`` (explicit) > ``tier`` model > squilla_router.default_tier model.
    """

    model_config = SettingsConfigDict(env_prefix="OPENSQUILLA_NAMING_")

    enabled: bool = True
    # Surfaces eligible for auto-naming. webchat/cli are chat; channel covers
    # inbound channel conversations. cron/subagent intentionally excluded.
    surfaces: list[str] = Field(default_factory=lambda: ["webchat", "cli", "channel"])
    tier: str | None = None  # None = use squilla_router.default_tier
    model: str | None = None  # None = use the resolved tier's model
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


class ImageGenerationProvidersConfig(BaseModel):
    openai: ImageGenerationOpenAIProviderConfig = Field(
        default_factory=ImageGenerationOpenAIProviderConfig
    )
    openrouter: ImageGenerationOpenRouterProviderConfig = Field(
        default_factory=ImageGenerationOpenRouterProviderConfig
    )


class ImageGenerationConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPENSQUILLA_IMAGE_GENERATION_",
        env_nested_delimiter="__",
    )

    enabled: bool = False
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


class DiscordChannelEntry(ConfiguredChannelEntry):
    """Gateway config entry for a Discord channel."""

    type: Literal["discord"] = "discord"
    token: str
    application_id: str = ""
    default_channel_id: str = ""
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 33281


class DingTalkChannelEntry(ConfiguredChannelEntry):
    """Gateway config entry for a DingTalk channel."""

    type: Literal["dingtalk"] = "dingtalk"
    client_id: str
    client_secret: str


class WeComChannelEntry(ConfiguredChannelEntry):
    """Gateway config entry for a WeCom corp-app channel."""

    type: Literal["wecom"] = "wecom"
    corp_id: str
    corp_secret: str
    agent_id_int: int
    token: str
    encoding_aes_key: str
    webhook_path: str = "/wecom/events"
    api_base: str = "https://qyapi.weixin.qq.com"


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
    diagnostics_enabled: bool = False
    channel_admin_senders: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _default_squilla_router_profile_for_direct_provider(self) -> GatewayConfig:
        router = self.squilla_router
        if not router or not getattr(router, "enabled", False):
            return self
        if getattr(router, "tier_profile", None):
            return self
        provider = str(getattr(self.llm, "provider", "") or "").strip().lower()
        if provider == "openrouter" or provider not in ROUTER_TIER_PROFILE_IDS:
            return self
        fields_set = set(getattr(router, "model_fields_set", set()))
        has_custom_tiers = (
            "tiers" in fields_set and getattr(router, "tiers", {}) != _default_tiers()
        )
        if "tier_profile" in fields_set or has_custom_tiers:
            return self
        payload = router.model_dump(mode="python")
        payload["tier_profile"] = provider
        payload.pop("tiers", None)
        self.squilla_router = SquillaRouterConfig(**payload)
        return self

    @model_validator(mode="after")
    def _validate_squilla_router_tier_profile_provider(self) -> GatewayConfig:
        profile = getattr(self.squilla_router, "tier_profile", None)
        if not profile:
            return self
        provider = str(getattr(self.llm, "provider", "") or "").strip().lower()
        normalized_profile = str(profile).strip().lower()
        if provider != normalized_profile:
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

    def to_toml_dict(self) -> dict[str, Any]:
        """Convert config to a TOML-writable dict."""
        data: dict[str, Any] = self.model_dump(exclude_none=True, exclude_defaults=False)
        if not data.get("agents"):
            data.pop("agents", None)
        llm = data.get("llm")
        if isinstance(llm, dict):
            if not llm.get("api_key_env"):
                llm.pop("api_key_env", None)
            elif not llm.get("api_key"):
                llm.pop("api_key", None)
        if not data.get("search_api_key_env"):
            data.pop("search_api_key_env", None)
        elif not data.get("search_api_key"):
            data.pop("search_api_key", None)
        _delete_env_sourced_secret(
            data,
            "audio.providers.elevenlabs.api_key",
            "audio.providers.elevenlabs.api_key_env",
            default_env="ELEVENLABS_API_KEY",
            settings_env="OPENSQUILLA_AUDIO_PROVIDERS__ELEVENLABS__API_KEY",
        )
        router = data.get("squilla_router")
        if isinstance(router, dict) and router.get("tier_profile"):
            try:
                defaults = _router_tier_profile_defaults(str(router["tier_profile"]))
            except ValueError:
                defaults = None
            if defaults is not None and router.get("tiers") == defaults:
                router.pop("tiers", None)
        for path in sorted(self._runtime_secret_paths):
            _delete_path(data, path)
        return data

    def to_public_dict(self) -> dict[str, Any]:
        """Return a redacted config view safe for public control surfaces."""
        return cast(dict[str, Any], redact_public_config(self.model_dump()))

    def mark_runtime_secret(self, path: str) -> None:
        self._runtime_secret_paths.add(path)

    def clear_runtime_secret(self, path: str) -> None:
        self._runtime_secret_paths.discard(path)

    def inherit_runtime_secrets(self, other: GatewayConfig) -> None:
        self._runtime_secret_paths = set(other._runtime_secret_paths)

    @classmethod
    def load_from_toml(cls, path: str | Path) -> GatewayConfig:
        """Load config from a TOML file."""
        import tomllib

        target = Path(path)
        with open(target, "rb") as f:
            data = tomllib.load(f)
        migration = migrate_config_payload(data)
        cfg = cls(**migration.payload)
        if migration.changed:
            backup_and_write_migrated_config(target, migration.payload, migration)
        return cfg

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> GatewayConfig:
        """Auto-discover and load config.

        Precedence: explicit path > current-directory config > user config > defaults.
        Environment variables always override TOML values (Pydantic Settings behavior).
        """
        import tomllib

        candidates: list[Path] = []
        if config_path:
            candidates.append(Path(config_path))
        else:
            candidates.append(Path.cwd() / "opensquilla.toml")
            candidates.append(default_opensquilla_home() / "config.toml")

        for path in candidates:
            if path.is_file():
                with open(path, "rb") as f:
                    data = tomllib.load(f)
                migration = migrate_config_payload(data)
                cfg = cls(**migration.payload)
                if migration.changed:
                    backup_and_write_migrated_config(path, migration.payload, migration)
                cfg.config_path = str(path)
                return cfg

        return cls()


# --- bind-address resolution ----------------------------------------------

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
