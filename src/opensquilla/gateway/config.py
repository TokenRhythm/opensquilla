"""GatewayConfig — Pydantic Settings for the gateway."""

from __future__ import annotations

import copy
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
    model: str = "deepseek-v4-pro"
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


LEGACY_OPENROUTER_MODEL_OPTIONS = [
    "deepseek/deepseek-v4-pro",
    "z-ai/glm-5.2",
    "qwen/qwen3.7-plus",
    "deepseek/deepseek-v4-flash",
    "qwen/qwen3.7-max",
    "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k2.7-code",
    "minimax/minimax-m3",
]

# Backward-compatible alias for older imports. New configs do not use these as
# defaults; they are only recognized as the old OpenRouter preset payload.
DEFAULT_LLM_ENSEMBLE_MODEL_OPTIONS = LEGACY_OPENROUTER_MODEL_OPTIONS


def _default_llm_ensemble_model_options() -> list[str]:
    """Legacy model_options default is intentionally empty for new configs."""
    return []


# Candidate roles for the custom B5 lineup. Proposer roles are advisory
# labels surfaced in the UI and the decision trace; "aggregator" is
# structural — it marks the single member that fuses drafts and produces
# the final answer. Empty string = unassigned (runs as a proposer).
LLM_ENSEMBLE_CANDIDATE_ROLES = (
    "",
    "primary",
    "contrast",
    "fast_check",
    "critic",
    "aggregator",
)

# custom_b5 lineup bounds. The proposer cap covers total per-turn proposer
# calls; the aggregator adds one more. See the ensemble builder for how the
# lineup maps onto the shared B5 fusion defaults.
CUSTOM_B5_MIN_PROPOSERS = 2
CUSTOM_B5_MAX_PROPOSERS = 6
CUSTOM_B5_MAX_TOTAL_CALLS = 8


class LlmEnsembleCandidateConfig(BaseModel):
    provider: str
    model: str
    source: Literal["custom", "legacy_model_options"] = "custom"
    enabled: bool = True
    # Advisory role label; unknown values coerce to "" (unassigned) instead of
    # failing validation so a hand-edited config never blocks gateway boot.
    # Strict role/lineup checks live on the RPC save path (upsert mutation).
    role: str = ""
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
        return normalized if normalized in LLM_ENSEMBLE_CANDIDATE_ROLES else ""

    @field_validator("thinking_level", mode="before")
    @classmethod
    def _normalize_thinking_level(cls, value: object) -> s

... [OUTPUT TRUNCATED - 96,061 chars omitted out of 145,987 total] ...

ILLA_CHANNEL_INFLIGHT_CAP=%r is below minimum 1; "
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