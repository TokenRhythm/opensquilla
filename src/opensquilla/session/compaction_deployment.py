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
from opensquilla.provider.deployment import (
    CredentialPoolAcquirer,
    resolve_provider_deployment,
)
from opensquilla.provider.model_catalog import shared_catalog
from opensquilla.provider.protocol import (
    LLMProvider,
    configured_provider_id,
    provider_metadata,
)
from opensquilla.provider.selector import ProviderConfig, build_provider_from_config

MAX_COMPACTION_LLM_CALLS = 2
DEFAULT_COMPACTION_OUTPUT_TOKENS = 1024
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
    provider_request_max_chars: int = 0
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
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.provider_request_max_chars < 0:
            raise ValueError("provider_request_max_chars must be non-negative")
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
    max_calls: int = MAX_COMPACTION_LLM_CALLS

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError("compaction execution plan needs at least one target")
        if not 1 <= self.max_calls <= MAX_COMPACTION_LLM_CALLS:
            raise ValueError(
                f"compaction max_calls must be between 1 and {MAX_COMPACTION_LLM_CALLS}"
            )

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
        catalog_window = int(
            catalog.resolve_context_window(model, provider=provider_id) or 0
        )
        resolved_window = catalog_window
        window_source = "model_catalog" if catalog_window > 0 else "bounded_fallback"
    resolved_window = max(1, resolved_window)

    catalog_output = int(
        catalog.resolve_max_tokens(model, user_override=0, provider=provider_id)
        or 0
    )
    requested_output = max(1, int(max_output_tokens or 0))
    resolved_output = min(
        DEFAULT_COMPACTION_OUTPUT_TOKENS,
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
    max_calls: int = MAX_COMPACTION_LLM_CALLS,
    max_output_tokens: int = DEFAULT_COMPACTION_OUTPUT_TOKENS,
    deployment_fingerprint: str = "",
    portable: bool = True,
    source: str = "provider_config",
) -> CompactionExecutionPlan:
    """Build an isolated auxiliary provider from a complete deployment config."""

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
        max_output_tokens=max_output_tokens,
        provider_request_max_chars=provider_request_max_chars,
    )
    isolated = replace(
        config,
        model=model,
        provider_routing=dict(config.provider_routing),
        # A summary request contains freshly serialized portable messages.
        # Provider-private state from the consumer turn must never be replayed.
        replay_provider_state=False,
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
                provider_request_max_chars=resolved_chars,
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
    max_calls: int = MAX_COMPACTION_LLM_CALLS,
    max_output_tokens: int = DEFAULT_COMPACTION_OUTPUT_TOKENS,
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
        max_output_tokens=max_output_tokens,
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
                provider_request_max_chars=resolved_chars,
                deployment_fingerprint=deployment_fingerprint,
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
) -> CompactionExecutionPlan | None:
    """Freeze the ordered physical targets for one compaction operation.

    Composite providers contribute only their concrete aggregator deployment;
    proposer fanout is never a compaction target. The routed/base deployment
    and configured single-provider fallbacks follow it. Explicit provider and
    model configuration, when complete and executable, takes precedence.
    """

    candidates: list[CompactionExecutionTarget] = []
    seen: set[str] = set()

    def add_config(
        config: ProviderConfig | None,
        *,
        source: str,
        credential_pool: object | None = None,
    ) -> None:
        if config is None:
            return
        try:
            plan = build_compaction_execution_plan_from_provider_config(
                config,
                context_window_tokens=(
                    context_window_tokens
                    if source in {"routed_deployment", "active_deployment"}
                    else 0
                ),
                source=source,
            )
        except Exception:
            return
        target = plan.primary
        if isinstance(credential_pool, Mapping):
            pool_provider = str(credential_pool.get("provider") or "").strip()
            pool_session_key = str(credential_pool.get("session_key") or "").strip()
            if pool_provider and pool_session_key:
                target = replace(
                    target,
                    credential_pool_provider=pool_provider,
                    credential_pool_session_key=pool_session_key,
                    credential_pool_failure_reporter=(
                        credential_pool_failure_reporter
                    ),
                )
        if target.deployment_fingerprint in seen:
            return
        seen.add(target.deployment_fingerprint)
        candidates.append(target)

    def add_identity(identity: CompactionDeploymentIdentity) -> None:
        resolution_metadata: dict[str, Any] = {}
        resolution = resolve_provider_deployment(
            app_config,
            identity.provider_id,
            identity.model,
            inherited_provider_config=active_provider_config,
            session_key=session_key,
            turn_metadata=resolution_metadata,
            replay_provider_state=False,
            credential_pool_acquirer=credential_pool_acquirer,
        )
        if resolution.ready:
            add_config(
                resolution.provider_config,
                source=identity.source,
                credential_pool=resolution_metadata.get("credential_pool"),
            )

    explicit_provider = str(
        getattr(compaction_config, "provider", "") or ""
    ).strip()
    explicit_model = str(getattr(compaction_config, "model", "") or "").strip()
    if explicit_provider and explicit_model:
        resolution_metadata: dict[str, Any] = {}
        resolution = resolve_provider_deployment(
            app_config,
            explicit_provider,
            explicit_model,
            inherited_provider_config=active_provider_config,
            session_key=session_key,
            turn_metadata=resolution_metadata,
            replay_provider_state=False,
            credential_pool_acquirer=credential_pool_acquirer,
        )
        if resolution.ready:
            add_config(
                resolution.provider_config,
                source="explicit",
                credential_pool=resolution_metadata.get("credential_pool"),
            )
    elif explicit_model and active_provider_config is not None:
        add_config(
            replace(
                active_provider_config,
                model=explicit_model,
                provider_routing=dict(active_provider_config.provider_routing),
                replay_provider_state=False,
            ),
            source="explicit_model_current_provider",
        )

    aggregator = getattr(active_provider, "aggregator", None)
    aggregator_config = getattr(aggregator, "provider_config", None)
    aggregator_ready = bool(getattr(aggregator, "ready", True))
    if isinstance(aggregator_config, ProviderConfig) and aggregator_ready:
        add_config(aggregator_config, source="ensemble_aggregator")

    add_config(active_provider_config, source="routed_deployment")
    for identity in previous_deployment_identities:
        add_identity(identity)
    for config in fallback_provider_configs:
        add_config(config, source="selector_fallback")

    if not candidates:
        fallback_plan = build_compaction_execution_plan_from_provider(
            active_provider,
            context_window_tokens=context_window_tokens,
            source="active_deployment",
        )
        if fallback_plan is not None:
            candidates.extend(fallback_plan.candidates)

    if not candidates:
        return None
    return CompactionExecutionPlan(
        candidates=tuple(candidates),
        max_calls=MAX_COMPACTION_LLM_CALLS,
    )
