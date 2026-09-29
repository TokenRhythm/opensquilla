"""Runtime-only deployment plan for provider-native context compaction."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from opensquilla.context_budget import ContextBudgetGovernor
from opensquilla.provider.deployment import CredentialPoolAcquirer
from opensquilla.provider.model_catalog import shared_catalog
from opensquilla.provider.protocol import (
    LLMProvider,
    configured_provider_id,
    provider_connection_config,
    provider_metadata,
)
from opensquilla.provider.selector import ProviderConfig, build_provider_from_config
from opensquilla.provider.types import ChatConfig

# Compatibility names: None delegates necessary chunks to the operation deadline;
# zero requests the current deployment's generation allowance as a soft target.
MAX_COMPACTION_LLM_CALLS: int | None = None
DEFAULT_COMPACTION_OUTPUT_TOKENS = 0
_COMPACTION_CONTEXT_THRESHOLD = 0.85
# Fingerprints reach telemetry, so make credential guesses unverifiable off-process.
_DEPLOYMENT_FINGERPRINT_KEY = secrets.token_bytes(32)


def _default_deployment_fingerprint(provider_id: str, model: str) -> str:
    """Return a stable non-secret identity suitable for staleness checks."""

    safe_identity = f"{provider_id.strip().lower()}\0{model.strip()}"
    return hashlib.sha256(safe_identity.encode("utf-8")).hexdigest()[:24]


def compaction_deployment_fingerprint(
    *,
    provider: str,
    model: str,
    api_key: str = "",
    base_url: str = "",
    org_id: str = "",
    proxy: str = "",
    provider_routing: Mapping[str, str] | None = None,
    replay_provider_state: bool = False,
) -> str:
    """Return a process-local opaque identity for one compaction deployment."""

    identity = {
        "provider": str(provider or "").strip().lower(),
        "model": str(model or "").strip(),
        "api_key": str(api_key or ""),
        "base_url": str(base_url or "").strip(),
        "org_id": str(org_id or "").strip(),
        "proxy": str(proxy or "").strip(),
        "provider_routing": sorted(
            (str(key), str(value))
            for key, value in (provider_routing or {}).items()
        ),
        "replay_provider_state": bool(replay_provider_state),
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hmac.new(
        _DEPLOYMENT_FINGERPRINT_KEY,
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:24]


def _provider_config_fingerprint(config: ProviderConfig) -> str:
    """Return a process-local opaque identity for one physical deployment."""

    return compaction_deployment_fingerprint(
        provider=config.provider,
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        org_id=config.org_id,
        proxy=config.proxy,
        provider_routing=config.provider_routing,
        replay_provider_state=config.replay_provider_state,
    )


@dataclass(frozen=True, slots=True)
class CompactionExecutionTarget:
    """One physical model deployment used only to generate a summary."""

    provider: LLMProvider = field(repr=False, compare=False)
    provider_id: str
    model: str
    context_window_tokens: int = 0
    context_window_source: str = "model_catalog"
    max_output_tokens: int = DEFAULT_COMPACTION_OUTPUT_TOKENS
    # A planning/prompt target, never a summary-body acceptance limit.
    # The API generation allowance may also include unavoidable reasoning.
    max_generation_tokens: int | None = None
    provider_request_max_chars: int = 0
    provider_request_max_chars_explicit_cap: int | None = field(default=None, repr=False)
    deployment_fingerprint: str = ""
    portable: bool = True
    source: str = "active_provider"
    credential_pool_provider: str = field(default="", repr=False)
    credential_pool_session_key: str = field(default="", repr=False)
    credential_pool_failure_reporter: Callable[[str, str, Any], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not callable(getattr(self.provider, "chat", None)):
            raise TypeError("compaction deployment provider must implement chat()")
        if not self.model.strip():
            raise ValueError("compaction deployment model must not be empty")
        if self.context_window_tokens < 0:
            raise ValueError("context_window_tokens must be non-negative")
        if self.max_output_tokens < 0:
            raise ValueError("max_output_tokens must be non-negative")
        if self.max_generation_tokens is not None and self.max_generation_tokens <= 0:
            raise ValueError("max_generation_tokens must be positive")
        if self.provider_request_max_chars < 0:
            raise ValueError("provider_request_max_chars must be non-negative")
        if not self.max_output_tokens:
            output = self.max_generation_tokens or shared_catalog().resolve_max_tokens(
                self.model, user_override=0, provider=self.provider_id,
            )
            if self.context_window_tokens > 0:
                output = min(output, self.context_window_tokens)
            object.__setattr__(self, "max_output_tokens", max(1, int(output)))
        if not self.deployment_fingerprint:
            object.__setattr__(
                self,
                "deployment_fingerprint",
                _default_deployment_fingerprint(self.provider_id, self.model),
            )


@dataclass(frozen=True, slots=True)
class CompactionDeploymentIdentity:
    """Non-secret identity for a target that must be resolved per operation.

    Session provenance can outlive a credential rotation.  Keeping only this
    identity in a turn-scoped resolver closure prevents an already-resolved
    ``ProviderConfig`` (and its API key) from being reused by a later
    compaction operation.
    """

    provider_id: str
    model: str
    source: str = "previous_session_deployment"

    def __post_init__(self) -> None:
        provider_id = self.provider_id.strip().lower()
        model = self.model.strip()
        if not provider_id:
            raise ValueError("compaction deployment provider must not be empty")
        if not model:
            raise ValueError("compaction deployment model must not be empty")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "model", model)


@dataclass(frozen=True, slots=True)
class CompactionExecutionPlan:
    """Bounded, secret-free description of auxiliary summary calls.

    The provider instance is hidden by ``CompactionExecutionTarget.__repr__``.
    Per-operation counters and the absolute deadline remain on
    ``CompactionConfig`` so a plan can be reused safely by a new operation.
    """

    candidates: tuple[CompactionExecutionTarget, ...]
    max_calls: int | None = None

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError("compaction execution plan needs at least one target")
        if self.max_calls is not None and self.max_calls < 1:
            raise ValueError("compaction max_calls must be positive when specified")

    @property
    def primary(self) -> CompactionExecutionTarget:
        return self.candidates[0]

    @property
    def deployment(self) -> CompactionExecutionTarget:
        """Compatibility spelling for the original single-target P0 API."""

        return self.primary

    @property
    def max_output_tokens(self) -> int:
        """Compatibility projection of the active target's output budget."""

        return self.primary.max_output_tokens


def _resolved_target_budgets(
    *,
    provider_id: str,
    model: str,
    context_window_tokens: int,
    max_output_tokens: int,
    provider_request_max_chars: int,
) -> tuple[int, int, int, str]:
    """Bind budgets to one physical deployment without trusting another leg."""

    catalog = shared_catalog()
    resolved_window = int(context_window_tokens or 0)
    # A positive caller value was already resolved against its deployment
    # before this runtime-only plan was built. Do not mislabel it as a user
    # override when it may have come from a catalog or per-model profile.
    window_source = "caller_resolved"
    if resolved_window <= 0:
        resolve_with_source = getattr(catalog, "resolve_context_window_with_source", None)
        if callable(resolve_with_source):
            catalog_window, catalog_source = resolve_with_source(model, provider=provider_id)
        else:
            catalog_window = catalog.resolve_context_window(model, provider=provider_id)
            catalog_source = "catalog"
        resolved_window = int(catalog_window or 0)
        window_source = (
            "model_catalog"
            if resolved_window > 0 and catalog_source != "default"
            else "bounded_fallback"
        )
    resolved_window = max(1, resolved_window)

    catalog_output = int(
        catalog.resolve_max_tokens(model, user_override=0, provider=provider_id)
        or 0
    )
    requested_output = max(1, int(max_output_tokens or catalog_output or resolved_window))
    resolved_output = min(
        requested_output,
        catalog_output if catalog_output > 0 else requested_output,
        resolved_window,
    )
    derived_chars = ContextBudgetGovernor.from_values(
        context_window_tokens=resolved_window,
        max_output_tokens=resolved_output,
        thinking_budget_tokens=0,
        context_overflow_threshold=_COMPACTION_CONTEXT_THRESHOLD,
    ).snapshot().provider_request_max_chars
    requested_chars = max(0, int(provider_request_max_chars or 0))
    resolved_chars = (
        min(requested_chars, derived_chars)
        if requested_chars > 0
        else derived_chars
    )
    return resolved_window, resolved_output, max(1, resolved_chars), window_source


# Compatibility aliases for the first P0 API draft.
CompactionDeployment = CompactionExecutionTarget
CompactionLlmPlan = CompactionExecutionPlan


def build_compaction_llm_plan_from_provider_config(
    config: ProviderConfig,
    *,
    model_override: str | None = None,
    context_window_tokens: int = 0,
    provider_request_max_chars: int = 0,
    max_calls: int | None = None,
    max_output_tokens: int = DEFAULT_COMPACTION_OUTPUT_TOKENS,
    max_generation_tokens: int | None = None,
    deployment_fingerprint: str = "",
    portable: bool = True,
    source: str = "provider_config",
    replay_provider_state: bool | None = None,
) -> CompactionExecutionPlan:
    """Build an isolated auxiliary provider from a complete deployment config.

    Preserve the current deployment's serialization policy by default.
    """

    model = str(model_override or config.model or "").strip()
    (
        resolved_window,
        resolved_output,
        resolved_chars,
        window_source,
    ) = _resolved_target_budgets(
        provider_id=str(config.provider or "").strip(),
        model=model,
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_generation_tokens or max_output_tokens,
        provider_request_max_chars=provider_request_max_chars,
    )
    isolated = replace(
        config,
        model=model,
        provider_routing=dict(config.provider_routing),
        replay_provider_state=(
            config.replay_provider_state
            if replay_provider_state is None
            else replay_provider_state
        ),
    )
    provider = build_provider_from_config(isolated)
    return CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=provider,
                provider_id=str(isolated.provider or "").strip(),
                model=model,
                context_window_tokens=resolved_window,
                context_window_source=window_source,
                max_output_tokens=resolved_output,
                max_generation_tokens=resolved_output,
                provider_request_max_chars=resolved_chars,
                provider_request_max_chars_explicit_cap=max(
                    0, int(provider_request_max_chars or 0),
                ),
                deployment_fingerprint=(
                    deployment_fingerprint
                    or _provider_config_fingerprint(isolated)
                ),
                portable=portable,
                source=source,
            ),
        ),
        max_calls=max_calls,
    )


def build_compaction_llm_plan_from_provider(
    provider: object | None,
    *,
    model: str | None = None,
    context_window_tokens: int = 0,
    provider_request_max_chars: int = 0,
    max_calls: int | None = None,
    max_output_tokens: int = DEFAULT_COMPACTION_OUTPUT_TOKENS,
    max_generation_tokens: int | None = None,
    deployment_fingerprint: str = "",
    portable: bool = True,
    source: str = "resolved_provider",
) -> CompactionExecutionPlan | None:
    """Wrap an already-resolved physical provider when its model is unambiguous.

    ``ChatConfig`` has no model override.  If the caller asks for a model that
    differs from the provider's bound model, returning ``None`` is safer than
    silently sending the summary to the wrong deployment.  Composite ensemble
    wrappers are also refused: their routed/base physical provider must be
    supplied through ``build_compaction_llm_plan_from_provider_config``.
    """

    if provider is None or not callable(getattr(provider, "chat", None)):
        return None
    metadata = provider_metadata(provider)
    requested_model = str(model or "").strip()
    bound_model = str(metadata.model or "").strip()
    if requested_model and bound_model and requested_model != bound_model:
        return None
    resolved_model = bound_model or requested_model
    if not resolved_model:
        return None

    provider_id = configured_provider_id(provider)
    connection = provider_connection_config(provider)
    provider_kind = str(metadata.provider_kind or "").strip().lower()
    if provider_kind == "ensemble" or str(provider_id).strip().lower() == "ensemble":
        return None
    (
        resolved_window,
        resolved_output,
        resolved_chars,
        window_source,
    ) = _resolved_target_budgets(
        provider_id=provider_id or metadata.provider_name or provider_kind,
        model=resolved_model,
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_generation_tokens or max_output_tokens,
        provider_request_max_chars=provider_request_max_chars,
    )

    return CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=provider,  # type: ignore[arg-type]
                provider_id=provider_id or metadata.provider_name or provider_kind,
                model=resolved_model,
                context_window_tokens=resolved_window,
                context_window_source=window_source,
                max_output_tokens=resolved_output,
                max_generation_tokens=resolved_output,
                provider_request_max_chars=resolved_chars,
                provider_request_max_chars_explicit_cap=max(
                    0, int(provider_request_max_chars or 0),
                ),
                deployment_fingerprint=(
                    deployment_fingerprint or compaction_deployment_fingerprint(
                        provider=provider_id or metadata.provider_name or provider_kind,
                        model=resolved_model,
                        api_key=connection.api_key,
                        base_url=connection.base_url,
                    )
                ),
                portable=portable,
                source=source,
            ),
        ),
        max_calls=max_calls,
    )


# Canonical execution-oriented spellings used by runtime/manual resolvers.
build_compaction_execution_plan_from_provider_config = (
    build_compaction_llm_plan_from_provider_config
)
build_compaction_execution_plan_from_provider = build_compaction_llm_plan_from_provider


def resolve_compaction_execution_plan(
    *,
    app_config: Any | None,
    active_provider: object | None,
    active_provider_config: ProviderConfig | None,
    previous_deployment_identities: Sequence[CompactionDeploymentIdentity] = (),
    fallback_provider_configs: Sequence[ProviderConfig] = (),
    compaction_config: Any | None = None,
    context_window_tokens: int = 0,
    session_key: str = "",
    credential_pool_acquirer: CredentialPoolAcquirer | None = None,
    credential_pool_failure_reporter: Callable[[str, str, Any], None] | None = None,
    active_only: bool = False,
    active_chat_config: ChatConfig | None = None,
) -> CompactionExecutionPlan | None:
    """Freeze only the current physical responder, never route for a summary.

    Legacy override/fallback/active_only arguments remain accepted for callers
    upgrading in place. They cannot change the summary deployment. A composite
    provider resolves its already-selected response leg, including a sticky
    fixed takeover; this never executes proposers or selects a new fallback.
    """

    config = active_provider_config
    provider = active_provider
    chat_config = active_chat_config
    source = "active_deployment"
    resolve_current = getattr(provider, "current_response_deployment", None)
    if callable(resolve_current):
        try:
            provider, config, chat_config = resolve_current(chat_config or ChatConfig())
        except Exception:
            return None
        source = "current_response_deployment"
        if provider is None and config is None:
            return None
    else:
        # Extension composites may expose a resolved aggregator config without
        # implementing the richer current-response protocol.
        aggregator = getattr(provider, "aggregator", None)
        aggregator_config = getattr(aggregator, "provider_config", None)
        if isinstance(aggregator_config, ProviderConfig):
            if not bool(getattr(aggregator, "ready", True)):
                return None
            config = aggregator_config
            source = "ensemble_aggregator"
            resolve_chat = getattr(provider, "compaction_chat_config", None)
            if callable(resolve_chat):
                chat_config = resolve_chat(chat_config or ChatConfig())
            provider = None

    window = (
        int(chat_config.provider_context_window_tokens or 0)
        if chat_config is not None else int(context_window_tokens or 0)
    )
    generation = int(chat_config.max_tokens or 0) if chat_config is not None else None
    char_cap = (
        int(chat_config.provider_request_max_chars_explicit_cap or 0)
        if chat_config is not None else 0
    )
    # The already-bound physical adapter is authoritative for every connection
    # and serialization option, including ones the public identity protocol
    # does not expose. The summary supplies a detached ChatConfig and never
    # mutates the selector or the adapter's model binding.
    try:
        if provider is None and isinstance(config, ProviderConfig):
            plan = build_compaction_execution_plan_from_provider_config(
                config, context_window_tokens=window,
                max_generation_tokens=generation,
                provider_request_max_chars=char_cap,
                source=source,
            )
        else:
            plan = build_compaction_execution_plan_from_provider(
                provider, context_window_tokens=window,
                max_generation_tokens=generation,
                provider_request_max_chars=char_cap,
                source=source,
            )
        if plan is not None and session_key and credential_pool_failure_reporter is not None:
            # Report against the existing session pin only. Never acquire a
            # different credential to run a summary; an unpinned session is a
            # no-op in the shared pool manager.
            plan = replace(plan, candidates=(replace(
                plan.primary,
                credential_pool_provider=plan.primary.provider_id,
                credential_pool_session_key=session_key,
                credential_pool_failure_reporter=credential_pool_failure_reporter,
            ),))
        return plan
    except Exception:
        # A broken current deployment is a summary failure, not permission to
        # send history to another model or credential configuration.
        return None
