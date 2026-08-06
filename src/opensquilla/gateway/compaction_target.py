"""Resolve an isolated physical deployment for gateway-triggered compaction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, cast

import structlog

from opensquilla.context_budget import ContextBudgetGovernor
from opensquilla.provider.deployment import resolve_provider_deployment
from opensquilla.provider.environment import environment_value
from opensquilla.provider.model_catalog import (
    resolve_effective_context_window,
    shared_catalog,
)
from opensquilla.provider.protocol import (
    project_provider_final_request,
    provider_metadata,
)
from opensquilla.provider.registry import get_provider_spec
from opensquilla.provider.selector import ProviderConfig, build_provider_from_config
from opensquilla.provider.types import ChatConfig, Message
from opensquilla.session.compaction_deployment import (
    DEFAULT_COMPACTION_OUTPUT_TOKENS,
    CompactionExecutionPlan,
    CompactionExecutionTarget,
    build_compaction_execution_plan_from_provider,
    build_compaction_execution_plan_from_provider_config,
)

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GatewayCompactionTarget:
    """One gateway-resolved auxiliary target without persisted credentials."""

    provider: object | None = field(default=None, repr=False, compare=False)
    plan: CompactionExecutionPlan | None = field(default=None, repr=False, compare=False)
    provider_id: str = ""
    model: str = ""
    source: str = "unavailable"
    blocked_reason: str = ""


@dataclass(frozen=True, slots=True)
class GatewayConsumerBudget:
    """Stable physical consumer bounds for a durable manual checkpoint."""

    provider: object | None = field(default=None, repr=False, compare=False)
    provider_id: str = ""
    model: str = ""
    context_window_tokens: int = 1
    max_output_tokens: int = 1
    provider_request_max_chars: int = 1
    # Manual compaction runs between turns, so the next active prompt/media are
    # not known yet.  Keep a fixed/proportional part of the physical input
    # budget unavailable to durable history; canonical instructions, tools,
    # thinking and the next active turn are rebuilt from authoritative state.
    next_request_reserve_tokens: int = 1
    next_request_reserve_chars: int = 4
    deployment_fingerprint: str = ""
    source: str = "unavailable"
    blocked_reason: str = ""


@dataclass(frozen=True, slots=True)
class _NamedAuthProfileDeployment:
    """One exact named-profile resolution with only non-secret provenance."""

    provider_config: ProviderConfig | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    provider_id: str = ""
    model: str = ""
    profile_fingerprint: str = ""
    blocked_reason: str = ""


@dataclass(frozen=True, slots=True)
class _GatewayCompactionCandidate:
    """One ordered target candidate before physical deployment resolution."""

    provider_id: str
    model: str
    source: str
    provider_config: ProviderConfig | None = field(
        default=None,
        repr=False,
        compare=False,
    )


def _qualified_auth_profile_provider(profile_id: str) -> str:
    """Return the provider encoded by a valid ``provider:name`` profile id."""

    prefix, separator, suffix = _text(profile_id).partition(":")
    if not separator or not prefix or not suffix:
        return ""
    return prefix.lower()


def effective_session_model(session: object | None) -> str | None:
    """Return the session's physical model without guessing its provider."""

    if session is None:
        return None
    return _text(getattr(session, "model_override", None)) or _text(
        getattr(session, "model", None)
    )


def resolve_gateway_consumer_budget(
    ctx: object,
    session: object | None,
) -> GatewayConsumerBudget:
    """Resolve the stable session/base consumer without routed provenance."""

    selector = getattr(ctx, "provider_selector", None)
    inherited = _selector_base_config(selector)
    gateway_config = getattr(ctx, "config", None)
    session_provider = _text(getattr(session, "provider_override", None)).lower()
    session_model = _text(getattr(session, "model", None))
    session_override_model = _text(getattr(session, "model_override", None))
    recorded_provider = _text(getattr(session, "model_provider", None)).lower()
    recorded_model = session_override_model or session_model
    auth_profile_override = _text(
        getattr(session, "auth_profile_override", None)
    )

    if auth_profile_override:
        inherited_provider = _text(getattr(inherited, "provider", None)).lower()
        inherited_model = _text(getattr(inherited, "model", None))
        qualified_provider = _qualified_auth_profile_provider(auth_profile_override)
        # A bare legacy profile is scoped to the stable selector provider.
        # Never let last-turn provenance reinterpret the same credential as a
        # different provider after a routed/fallback call.
        bound_provider = session_provider or qualified_provider or inherited_provider
        recorded_matches_boundary = bool(
            recorded_provider
            and recorded_model
            and (not bound_provider or recorded_provider == bound_provider)
        )
        expected_provider = (
            bound_provider
            or (recorded_provider if recorded_matches_boundary else "")
            or inherited_provider
        )
        intended_model = (
            recorded_model
            if recorded_matches_boundary
            else session_model
            or (session_override_model if not recorded_provider else "")
            or (
                inherited_model
                if not expected_provider or inherited_provider == expected_provider
                else ""
            )
        )
        named = _resolve_named_auth_profile_deployment(
            gateway_config,
            auth_profile_override,
            expected_provider=expected_provider,
            model=intended_model,
            session_key=_text(getattr(session, "session_key", None)),
        )
        if named.blocked_reason or named.provider_config is None:
            return GatewayConsumerBudget(
                provider_id=named.provider_id or session_provider,
                model=named.model or session_model or session_override_model,
                deployment_fingerprint=named.profile_fingerprint,
                source="auth_profile_unresolved",
                blocked_reason=(
                    named.blocked_reason or "named_auth_profile_unavailable"
                ),
            )
        try:
            named_provider = build_provider_from_config(named.provider_config)
        except Exception as exc:  # noqa: BLE001 - exact admission fails closed
            log.warning(
                "compaction_consumer_provider_build_failed",
                provider=named.provider_id,
                model=named.model,
                source="session_auth_profile",
                auth_profile_fingerprint=named.profile_fingerprint,
                error=type(exc).__name__,
            )
            return GatewayConsumerBudget(
                provider_id=named.provider_id,
                model=named.model,
                deployment_fingerprint=named.profile_fingerprint,
                source="auth_profile_unresolved",
                blocked_reason="named_auth_profile_provider_build_failed",
            )
        (
            context_window_tokens,
            max_output_tokens,
            provider_request_max_chars,
            next_request_reserve_tokens,
            next_request_reserve_chars,
        ) = _consumer_execution_budget(
            ctx,
            named.provider_id,
            named.model,
        )
        return GatewayConsumerBudget(
            provider=named_provider,
            provider_id=named.provider_id,
            model=named.model,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
            provider_request_max_chars=provider_request_max_chars,
            next_request_reserve_tokens=next_request_reserve_tokens,
            next_request_reserve_chars=next_request_reserve_chars,
            deployment_fingerprint=named.profile_fingerprint,
            source="session_auth_profile",
        )

    provider_config: ProviderConfig | None = None
    source = "selector_base"
    session_deployment_model = session_model or session_override_model
    if session_provider and session_deployment_model:
        resolution = resolve_provider_deployment(
            gateway_config,
            session_provider,
            session_deployment_model,
            inherited_provider_config=inherited,
            session_key=_text(getattr(session, "session_key", None)),
            replay_provider_state=False,
        )
        if resolution.ready and resolution.provider_config is not None:
            provider_config = resolution.provider_config
            source = "session_override"

    if provider_config is None and inherited is not None:
        base_model = session_model or _text(inherited.model)
        resolution = resolve_provider_deployment(
            gateway_config,
            _text(inherited.provider).lower(),
            base_model,
            inherited_provider_config=inherited,
            session_key=_text(getattr(session, "session_key", None)),
            replay_provider_state=False,
        )
        if resolution.ready and resolution.provider_config is not None:
            provider_config = resolution.provider_config
            source = "session_model" if session_model else "selector_base"

    provider: object | None = None
    provider_id = ""
    model = ""
    if provider_config is not None:
        provider_id = _text(provider_config.provider).lower()
        model = _text(provider_config.model)
        try:
            provider = build_provider_from_config(provider_config)
        except Exception as exc:  # noqa: BLE001 - proof fails closed below
            log.warning(
                "compaction_consumer_provider_build_failed",
                provider=provider_id,
                model=model,
                source=source,
                error=type(exc).__name__,
            )
    else:
        # Extension compatibility: resolve a base clone, never the live
        # selector or the last routed session deployment.
        provider = resolve_selected_compaction_provider(
            ctx,
            None,
        )
        metadata = provider_metadata(provider)
        provider_id = _text(
            metadata.provider_id
            or metadata.provider_name
            or metadata.provider_kind
        ).lower()
        model = _text(metadata.model) or session_model
        source = "selected_provider_compat"

    (
        context_window_tokens,
        max_output_tokens,
        provider_request_max_chars,
        next_request_reserve_tokens,
        next_request_reserve_chars,
    ) = _consumer_execution_budget(ctx, provider_id, model)
    return GatewayConsumerBudget(
        provider=provider,
        provider_id=provider_id,
        model=model,
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_output_tokens,
        provider_request_max_chars=provider_request_max_chars,
        next_request_reserve_tokens=next_request_reserve_tokens,
        next_request_reserve_chars=next_request_reserve_chars,
        source=source if provider is not None else "unavailable",
    )


def build_gateway_consumer_admission(
    budget: GatewayConsumerBudget,
) -> tuple[Callable[[str, list[dict[str, Any]]], bool], str]:
    """Build an exact, fail-closed proof for checkpoint plus durable raw tail."""

    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "schema": "gateway_manual_durable_consumer_v2",
                "provider": budget.provider_id,
                "model": budget.model,
                "context_window_tokens": budget.context_window_tokens,
                "max_output_tokens": budget.max_output_tokens,
                "provider_request_max_chars": budget.provider_request_max_chars,
                "next_request_reserve_tokens": budget.next_request_reserve_tokens,
                "next_request_reserve_chars": budget.next_request_reserve_chars,
                "deployment_fingerprint": budget.deployment_fingerprint,
                "source": budget.source,
                "blocked_reason": budget.blocked_reason,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    def _admit(
        replay_summary: str,
        kept_entries: list[dict[str, Any]],
    ) -> bool:
        if budget.blocked_reason or budget.provider is None:
            return False
        messages = _manual_consumer_messages(replay_summary, kept_entries)
        if messages is None:
            return False
        projection = project_provider_final_request(
            budget.provider,
            messages,
            [],
            ChatConfig(
                max_tokens=max(1, budget.max_output_tokens),
                thinking=False,
                thinking_budget_tokens=0,
                provider_request_max_chars=max(
                    1,
                    budget.provider_request_max_chars,
                ),
            ),
        )
        if projection is None or not projection.fits:
            return False
        proof = projection.proof
        estimated_tokens = max(0, int(proof.get("estimated_tokens", 0) or 0))
        estimated_chars = max(0, int(proof.get("estimated_chars", 0) or 0))
        effective_token_budget = max(
            0,
            int(proof.get("effective_proof_token_budget", 0) or 0),
        )
        effective_char_budget = max(
            0,
            int(proof.get("effective_proof_budget", 0) or 0),
        )
        return bool(
            estimated_tokens + max(1, budget.next_request_reserve_tokens)
            <= effective_token_budget
            and estimated_chars + max(4, budget.next_request_reserve_chars)
            <= effective_char_budget
        )

    return _admit, fingerprint


def limit_gateway_consumer_budget(
    budget: GatewayConsumerBudget,
    context_window_tokens: int,
) -> GatewayConsumerBudget:
    """Apply a caller cap without ever enlarging any physical consumer bound."""

    window = min(
        budget.context_window_tokens,
        max(1, int(context_window_tokens)),
    )
    output_tokens = min(
        budget.max_output_tokens,
        max(1, window - 1),
    )
    derived_cap = ContextBudgetGovernor.from_values(
        context_window_tokens=window,
        max_output_tokens=output_tokens,
        thinking_budget_tokens=0,
        context_overflow_threshold=0.85,
    ).snapshot().provider_request_max_chars
    return replace(
        budget,
        context_window_tokens=window,
        max_output_tokens=output_tokens,
        provider_request_max_chars=min(
            budget.provider_request_max_chars,
            max(1, derived_cap),
        ),
        next_request_reserve_tokens=_manual_next_request_reserve_tokens(window),
        next_request_reserve_chars=(
            _manual_next_request_reserve_tokens(window) * 4
        ),
    )


def resolve_selected_compaction_provider(
    ctx: object,
    session: object | None,
    *,
    model_override: str | None = None,
) -> object | None:
    """Compatibility resolver for callers that only consume a provider object.

    Production compaction uses :func:`resolve_gateway_compaction_target`.  This
    clone-only path remains for auxiliary callers and selector-shaped test
    doubles that do not expose a complete ``ProviderConfig``.
    """

    selector = getattr(ctx, "provider_selector", None)
    if selector is None:
        return None

    resolved_selector = selector
    clone = getattr(selector, "clone", None)
    if callable(clone):
        try:
            resolved_selector = clone()
        except Exception:  # noqa: BLE001
            resolved_selector = selector

    model = _text(model_override) or effective_session_model(session)
    if model and resolved_selector is not selector:
        override = getattr(resolved_selector, "override_model", None)
        if callable(override):
            try:
                override(model)
            except Exception:  # noqa: BLE001
                pass

    resolver = getattr(resolved_selector, "resolve", None)
    if not callable(resolver):
        return None
    try:
        return cast(object | None, resolver())
    except Exception:  # noqa: BLE001
        return None


def resolve_gateway_compaction_target(
    ctx: object,
    session: object | None,
) -> GatewayCompactionTarget:
    """Resolve manual/preflight compaction without mutating selector state.

    A complete ``compaction.provider`` + ``compaction.model`` pair is explicit.
    A model-only compaction setting stays on the already selected provider.
    Unavailable explicit or session-provenance deployments fall through to
    the selector's current deployment and then its authorized clone fallback.
    """

    selector = getattr(ctx, "provider_selector", None)
    inherited = _selector_current_config(selector)
    gateway_config = getattr(ctx, "config", None)
    compaction_config = getattr(gateway_config, "compaction", None)
    configured_provider = _text(getattr(compaction_config, "provider", None)).lower()
    configured_model = _text(getattr(compaction_config, "model", None))
    auth_profile_override = _text(
        getattr(session, "auth_profile_override", None)
    )

    if configured_provider and not configured_model:
        log.warning(
            "compaction_provider_without_model_ignored",
            provider=configured_provider,
        )
        configured_provider = ""

    explicit = bool(configured_provider and configured_model)
    model_only = bool(configured_model and not configured_provider)
    recorded_provider = _text(getattr(session, "model_provider", None)).lower()
    override_provider = _text(getattr(session, "provider_override", None)).lower()
    selected_model = _text(getattr(session, "model", None))
    recorded_model = (
        _text(getattr(session, "model_override", None))
        or selected_model
    )
    inherited_provider = _text(getattr(inherited, "provider", None)).lower()
    inherited_model = _text(getattr(inherited, "model", None))
    qualified_profile_provider = _qualified_auth_profile_provider(
        auth_profile_override
    )
    requested_auth_profile_provider = (
        configured_provider
        if explicit
        else inherited_provider if model_only else ""
    )
    auth_profile_bound_provider = (
        override_provider
        or qualified_profile_provider
        or requested_auth_profile_provider
        or inherited_provider
    )
    auth_profile_provider_conflict = bool(
        auth_profile_override
        and (
            (
                qualified_profile_provider
                and override_provider
                and qualified_profile_provider != override_provider
            )
            or (
                auth_profile_bound_provider
                and requested_auth_profile_provider
                and auth_profile_bound_provider != requested_auth_profile_provider
            )
        )
    )

    candidates: list[_GatewayCompactionCandidate] = []
    seen_abstract: set[tuple[str, str]] = set()
    seen_configs: set[int] = set()

    def add_candidate(
        provider_id: str,
        model: str,
        source: str,
        *,
        provider_config: ProviderConfig | None = None,
    ) -> None:
        provider_id = provider_id.strip().lower()
        model = model.strip()
        if not provider_id or not model:
            return
        if provider_config is None:
            identity = (provider_id, model)
            if identity in seen_abstract:
                return
            seen_abstract.add(identity)
        else:
            # The exact ProviderConfig is the physical fallback authorization.
            # Do not collapse credential-, proxy-, or routing-distinct links by
            # their public provider/model pair. Exact duplicates are removed
            # after plan construction by the process-keyed deployment HMAC.
            config_identity = id(provider_config)
            if config_identity in seen_configs:
                return
            seen_configs.add(config_identity)
        candidates.append(
            _GatewayCompactionCandidate(
                provider_id=provider_id,
                model=model,
                source=source,
                provider_config=provider_config,
            )
        )

    if explicit:
        add_candidate(configured_provider, configured_model, "explicit_compaction")
    elif model_only:
        # Compatibility contract: compaction.model changes only the model on
        # the selector's live provider. Persisted session provenance may be
        # stale after a route/model switch and must not rebind the provider.
        add_candidate(inherited_provider, configured_model, "selector_current")
    elif recorded_provider and recorded_model:
        # The finalizer writes this pair atomically after each physical turn.
        # Do not combine an older explicit provider intent with a fallback
        # model that actually ran on another provider.
        add_candidate(
            recorded_provider,
            recorded_model,
            "session_model_provider",
        )
        if override_provider:
            add_candidate(
                override_provider,
                selected_model,
                "session_provider_override",
            )
    elif override_provider:
        # Legacy sessions without recorded physical provenance may still use
        # provider_override + model_override as their complete deployment.
        add_candidate(
            override_provider,
            selected_model or recorded_model or inherited_model,
            "session_provider_override",
        )
    else:
        add_candidate(
            inherited_provider,
            recorded_model or selected_model or inherited_model,
            "selector_current",
        )

    # A failed explicit/session/model override must not suppress the current
    # physical deployment. Use its native model for the recovery candidate.
    add_candidate(inherited_provider, inherited_model, "selector_current")
    remaining_chain = getattr(selector, "remaining_chain", None)
    if callable(remaining_chain):
        try:
            remaining_configs = list(remaining_chain())
        except Exception:  # noqa: BLE001 - optional read-only selector view
            remaining_configs = []
        for index, fallback_config in enumerate(remaining_configs):
            if not isinstance(fallback_config, ProviderConfig):
                continue
            add_candidate(
                _text(fallback_config.provider).lower(),
                _text(fallback_config.model),
                "selector_current" if index == 0 else "selector_fallback",
                provider_config=fallback_config,
            )

    if auth_profile_override and auth_profile_bound_provider:
        # A named profile's provider is a credential boundary, not routing
        # provenance.  A previous fallback may contribute its model only when
        # it ran on that same provider; it may never reinterpret a bare
        # profile's credential as belonging to a different provider.
        bound_candidates = [
            candidate
            for candidate in candidates
            if candidate.provider_id == auth_profile_bound_provider
        ]
        if not explicit and not model_only:
            bound_model = (
                recorded_model
                if recorded_provider == auth_profile_bound_provider
                else selected_model
            )
            if (
                not bound_model
                and inherited_provider == auth_profile_bound_provider
            ):
                bound_model = inherited_model
            if bound_model:
                bound_identity = (auth_profile_bound_provider, bound_model)
                bound_candidates = [
                    _GatewayCompactionCandidate(
                        provider_id=auth_profile_bound_provider,
                        model=bound_model,
                        source="session_auth_profile",
                    ),
                    *[
                        candidate
                        for candidate in bound_candidates
                        if (candidate.provider_id, candidate.model)
                        != bound_identity
                    ],
                ]
        candidates = bound_candidates

    preferred_provider = candidates[0].provider_id if candidates else ""
    preferred_model = candidates[0].model if candidates else ""
    preferred_source = (
        candidates[0].source
        if candidates
        else _automatic_source(session, inherited)
    )

    if auth_profile_override:
        # A pinned auth profile is part of the physical deployment identity.
        # Resolve exactly that profile and never fall through to the selector,
        # inherited credentials, or a provider registry environment key.
        if auth_profile_provider_conflict:
            blocked_provider = (
                requested_auth_profile_provider
                or auth_profile_bound_provider
                or preferred_provider
            )
            blocked_model = configured_model or preferred_model or selected_model
            return GatewayCompactionTarget(
                provider_id=blocked_provider,
                model=blocked_model,
                source="auth_profile_unresolved",
                blocked_reason="named_auth_profile_provider_mismatch",
            )
        named = _resolve_named_auth_profile_deployment(
            gateway_config,
            auth_profile_override,
            expected_provider=preferred_provider or auth_profile_bound_provider,
            model=preferred_model,
            session_key=_text(getattr(session, "session_key", None)),
        )
        named_source = (
            "explicit_compaction_auth_profile"
            if explicit
            else "session_auth_profile"
        )
        if named.blocked_reason or named.provider_config is None:
            log.warning(
                "compaction_auth_profile_unavailable",
                provider=named.provider_id or preferred_provider,
                model=named.model or preferred_model,
                source=preferred_source,
                auth_profile_fingerprint=named.profile_fingerprint,
                reason=(
                    named.blocked_reason or "named_auth_profile_unavailable"
                ),
            )
            return GatewayCompactionTarget(
                provider_id=named.provider_id or preferred_provider,
                model=named.model or preferred_model,
                source="auth_profile_unresolved",
                blocked_reason=(
                    named.blocked_reason or "named_auth_profile_unavailable"
                ),
            )
        try:
            plan = _build_plan(
                ctx,
                named.provider_config,
                source=named_source,
            )
        except Exception as exc:  # noqa: BLE001 - a named target cannot fall back
            log.warning(
                "compaction_target_build_failed",
                provider=named.provider_id,
                model=named.model,
                source=named_source,
                auth_profile_fingerprint=named.profile_fingerprint,
                error=type(exc).__name__,
            )
            return GatewayCompactionTarget(
                provider_id=named.provider_id,
                model=named.model,
                source="auth_profile_unresolved",
                blocked_reason="named_auth_profile_provider_build_failed",
            )
        return GatewayCompactionTarget(
            provider=plan.primary.provider,
            plan=plan,
            provider_id=plan.primary.provider_id,
            model=plan.primary.model,
            source=plan.primary.source,
        )

    resolved_targets: list[CompactionExecutionTarget] = []
    seen_targets: set[str] = set()
    for candidate in candidates:
        from opensquilla.engine.selector_override import acquire_profile_credential

        provider_config = candidate.provider_config
        resolution = None
        if provider_config is None:
            resolution = resolve_provider_deployment(
                gateway_config,
                candidate.provider_id,
                candidate.model,
                inherited_provider_config=inherited,
                session_key=_text(getattr(session, "session_key", None)),
                replay_provider_state=False,
                credential_pool_acquirer=acquire_profile_credential,
            )
            provider_config = resolution.provider_config
        if provider_config is not None and (
            resolution is None or resolution.ready
        ):
            try:
                plan = _build_plan(
                    ctx,
                    provider_config,
                    source=candidate.source,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "compaction_target_build_failed",
                    provider=candidate.provider_id,
                    model=candidate.model,
                    source=candidate.source,
                    error=type(exc).__name__,
                )
                continue
            for target in plan.candidates:
                if target.deployment_fingerprint in seen_targets:
                    continue
                seen_targets.add(target.deployment_fingerprint)
                resolved_targets.append(target)
            continue
        log.warning(
            "compaction_target_unavailable",
            provider=candidate.provider_id,
            model=candidate.model,
            source=candidate.source,
            reason=(
                resolution.reason
                if resolution is not None
                else "provider_config_unavailable"
            ),
        )

    if resolved_targets:
        plan = CompactionExecutionPlan(
            candidates=tuple(resolved_targets),
        )
        return GatewayCompactionTarget(
            provider=plan.primary.provider,
            plan=plan,
            provider_id=plan.primary.provider_id,
            model=plan.primary.model,
            source=plan.primary.source,
        )

    # Selector-shaped compatibility doubles may not expose a complete current
    # ProviderConfig. Preserve their historical session-model override only
    # when no physical candidate could be formed; after a failed concrete
    # candidate, resolve the selector's own deployment without stale session
    # provenance.
    compat_session = (
        session
        if not candidates and not explicit and not model_only
        else None
    )
    provider = resolve_selected_compaction_provider(
        ctx,
        compat_session,
        model_override=configured_model if model_only else None,
    )
    if provider is None:
        return GatewayCompactionTarget(
            provider_id=preferred_provider,
            model=preferred_model,
            source=preferred_source,
        )

    metadata = provider_metadata(provider)
    physical_provider = _text(metadata.provider_id or metadata.provider_kind).lower()
    physical_model = _text(metadata.model) or preferred_model
    compat_plan = None
    try:
        context_window, output_tokens, request_max_chars = _execution_budget(
            ctx,
            physical_provider,
            physical_model,
        )
        compat_plan = build_compaction_execution_plan_from_provider(
            provider,
            model=physical_model,
            context_window_tokens=context_window,
            max_output_tokens=output_tokens,
            provider_request_max_chars=request_max_chars,
            source="selected_provider_compat",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "compaction_compat_plan_build_failed",
            provider=physical_provider,
            model=physical_model,
            error=type(exc).__name__,
        )
    return GatewayCompactionTarget(
        provider=provider,
        plan=compat_plan,
        provider_id=physical_provider,
        model=physical_model,
        source="selected_provider_compat",
    )


def _resolve_named_auth_profile_deployment(
    gateway_config: object | None,
    profile_id: str,
    *,
    expected_provider: str,
    model: str,
    session_key: str,
    credential_pool_acquirer: Callable[[str, list[str], str], Any | None] | None = None,
) -> _NamedAuthProfileDeployment:
    """Resolve exactly one named profile without ambient credential fallback.

    Named session profiles reuse the existing ``llm_profiles`` value schema and
    are addressed by their exact mapping key. Qualified keys use
    ``"<provider>:<name>"``; a bare key is allowed only when the caller already
    supplies the provider. The physical target model deliberately wins over a
    profile's saved model so an explicit compaction model can share a credential
    profile on the same provider. Provider mismatch is never allowed.
    """

    normalized_profile_id = _text(profile_id).casefold()
    fallback_fingerprint = hashlib.sha256(
        normalized_profile_id.encode("utf-8")
    ).hexdigest()[:16]
    profiles = getattr(gateway_config, "llm_profiles", None) or {}
    matches = [
        (str(key), profile)
        for key, profile in profiles.items()
        if _text(key).casefold() == normalized_profile_id
    ]
    if not matches:
        return _NamedAuthProfileDeployment(
            provider_id=_text(expected_provider).lower(),
            model=_text(model),
            profile_fingerprint=fallback_fingerprint,
            blocked_reason="named_auth_profile_not_found",
        )
    if len(matches) != 1:
        return _NamedAuthProfileDeployment(
            provider_id=_text(expected_provider).lower(),
            model=_text(model),
            profile_fingerprint=fallback_fingerprint,
            blocked_reason="named_auth_profile_ambiguous",
        )

    matched_profile_id, profile = matches[0]
    profile_fingerprint = hashlib.sha256(
        _text(matched_profile_id).casefold().encode("utf-8")
    ).hexdigest()[:16]
    profile_provider = ""
    prefix, separator, suffix = _text(matched_profile_id).partition(":")
    if separator:
        if not prefix or not suffix:
            return _NamedAuthProfileDeployment(
                provider_id=_text(expected_provider).lower(),
                model=_text(model),
                profile_fingerprint=profile_fingerprint,
                blocked_reason="named_auth_profile_invalid",
            )
        profile_provider = prefix.lower()

    requested_provider = _text(expected_provider).lower()
    if profile_provider and requested_provider and profile_provider != requested_provider:
        return _NamedAuthProfileDeployment(
            provider_id=requested_provider,
            model=_text(model),
            profile_fingerprint=profile_fingerprint,
            blocked_reason="named_auth_profile_provider_mismatch",
        )
    provider_id = requested_provider or profile_provider
    if not provider_id:
        return _NamedAuthProfileDeployment(
            model=_text(model),
            profile_fingerprint=profile_fingerprint,
            blocked_reason="named_auth_profile_provider_unresolved",
        )

    # The target's physical model is authoritative. The profile model is only a
    # compatibility fallback when a complete session/explicit pair was not
    # available.
    model_id = _text(model) or _text(getattr(profile, "model", None))
    if not model_id:
        return _NamedAuthProfileDeployment(
            provider_id=provider_id,
            profile_fingerprint=profile_fingerprint,
            blocked_reason="named_auth_profile_model_unresolved",
        )

    try:
        provider_spec = get_provider_spec(provider_id)
    except Exception:  # noqa: BLE001 - shared resolver reports the stable code
        return _NamedAuthProfileDeployment(
            provider_id=provider_id,
            model=model_id,
            profile_fingerprint=profile_fingerprint,
            blocked_reason="unknown_provider",
        )
    if provider_spec.env_key == "OAuth":
        # The provider's OAuth adapter loads its own ambient account and the
        # current LlmProviderProfile schema cannot name that account. Treating a
        # keyed profile as authorization would silently cross credential scope.
        return _NamedAuthProfileDeployment(
            provider_id=provider_id,
            model=model_id,
            profile_fingerprint=profile_fingerprint,
            blocked_reason="named_auth_profile_oauth_unsupported",
        )

    allowed_environment_names = {
        _text(getattr(profile, "api_key_env", None)),
        *(
            _text(name)
            for name in (getattr(profile, "api_key_env_pool", None) or [])
        ),
    }
    allowed_environment_names.discard("")

    def read_named_profile_environment(name: str) -> str:
        if name not in allowed_environment_names:
            return ""
        return environment_value(name)

    # Re-key a runtime-only view to the provider expected by the shared
    # deployment resolver. This exact matched profile is the only visible
    # credential record: inherited config and other/default profiles cannot be
    # selected even when the provider id is the same.
    scoped_config = SimpleNamespace(
        llm=getattr(gateway_config, "llm", None),
        llm_profiles={provider_id: profile},
    )
    if credential_pool_acquirer is None:
        from opensquilla.engine.selector_override import acquire_profile_credential

        credential_pool_acquirer = acquire_profile_credential

    pool_session_key = (
        f"{session_key}:auth-profile:{profile_fingerprint}"
        if session_key
        else f"manual-compaction:auth-profile:{profile_fingerprint}"
    )
    resolution = resolve_provider_deployment(
        scoped_config,
        provider_id,
        model_id,
        inherited_provider_config=None,
        session_key=pool_session_key,
        replay_provider_state=False,
        credential_pool_acquirer=credential_pool_acquirer,
        environment_reader=read_named_profile_environment,
    )
    if (
        not resolution.ready
        or resolution.provider_config is None
        or resolution.credential_source
        not in {"profile", "profile_env", "profile_pool", "keyless"}
    ):
        return _NamedAuthProfileDeployment(
            provider_id=provider_id,
            model=model_id,
            profile_fingerprint=profile_fingerprint,
            blocked_reason=(
                resolution.reason
                or "named_auth_profile_credential_boundary_unresolved"
            ),
        )
    return _NamedAuthProfileDeployment(
        provider_config=resolution.provider_config,
        provider_id=provider_id,
        model=model_id,
        profile_fingerprint=profile_fingerprint,
    )


def validate_gateway_session_deployment_override(
    gateway_config: object | None,
    *,
    provider_id: str,
    model: str,
    auth_profile_id: str,
    session_key: str = "",
) -> str:
    """Return a stable reason when an RPC session deployment is not writable.

    Model-only pins remain a legacy-compatible shape. A provider pin requires a
    model, while a named auth profile additionally requires an explicit provider
    and must resolve to that exact provider without acquiring or pinning a
    credential-pool entry.
    """

    normalized_provider = _text(provider_id).lower()
    normalized_model = _text(model)
    normalized_profile = _text(auth_profile_id)
    if normalized_profile and not normalized_provider:
        return "named_auth_profile_requires_provider"
    if normalized_provider and not normalized_model:
        return "session_provider_requires_model"
    if not normalized_provider:
        return ""
    try:
        get_provider_spec(normalized_provider)
    except Exception:  # noqa: BLE001 - callers expose only this stable reason
        return "unknown_provider"
    if not normalized_profile:
        return ""

    from opensquilla.engine.selector_override import peek_profile_credential

    named = _resolve_named_auth_profile_deployment(
        gateway_config,
        normalized_profile,
        expected_provider=normalized_provider,
        model=normalized_model,
        session_key=session_key,
        credential_pool_acquirer=peek_profile_credential,
    )
    if named.blocked_reason or named.provider_config is None:
        return named.blocked_reason or "named_auth_profile_unavailable"
    return ""


def _build_plan(
    ctx: object,
    provider_config: ProviderConfig,
    *,
    source: str,
) -> CompactionExecutionPlan:
    provider_id = _text(provider_config.provider).lower()
    model = _text(provider_config.model)
    context_window, output_tokens, request_max_chars = _execution_budget(
        ctx,
        provider_id,
        model,
    )
    return build_compaction_execution_plan_from_provider_config(
        provider_config,
        context_window_tokens=context_window,
        max_output_tokens=output_tokens,
        provider_request_max_chars=request_max_chars,
        source=source,
    )


def _execution_budget(
    ctx: object,
    provider_id: str,
    model: str,
) -> tuple[int, int, int]:
    catalog = shared_catalog()
    gateway_config = getattr(ctx, "config", None)
    llm_config = getattr(gateway_config, "llm", None)
    configured_provider = _text(getattr(llm_config, "provider", None)).lower()
    global_window = (
        getattr(llm_config, "context_window_tokens", 0)
        if configured_provider == provider_id
        else 0
    )
    context_window, _ = resolve_effective_context_window(
        catalog,
        model,
        provider=provider_id,
        global_override=global_window,
    )
    provider_output_limit = int(
        catalog.resolve_max_tokens(model, user_override=0, provider=provider_id) or 0
    )
    output_tokens = min(
        DEFAULT_COMPACTION_OUTPUT_TOKENS,
        provider_output_limit or DEFAULT_COMPACTION_OUTPUT_TOKENS,
    )
    derived_cap = ContextBudgetGovernor.from_values(
        context_window_tokens=context_window,
        max_output_tokens=output_tokens,
        thinking_budget_tokens=0,
        context_overflow_threshold=0.85,
    ).snapshot().provider_request_max_chars
    explicit_cap = (
        int(getattr(llm_config, "provider_request_proof_max_chars", 0) or 0)
        if configured_provider == provider_id
        else 0
    )
    request_max_chars = min(explicit_cap, derived_cap) if explicit_cap > 0 else derived_cap
    return int(context_window), max(1, output_tokens), max(1, request_max_chars)


def _consumer_execution_budget(
    ctx: object,
    provider_id: str,
    model: str,
) -> tuple[int, int, int, int, int]:
    """Bind the durable consumer's window, output reserve, and wire cap."""

    catalog = shared_catalog()
    gateway_config = getattr(ctx, "config", None)
    llm_config = getattr(gateway_config, "llm", None)
    configured_provider = _text(getattr(llm_config, "provider", None)).lower()
    same_configured_provider = configured_provider == provider_id
    global_window = (
        int(getattr(llm_config, "context_window_tokens", 0) or 0)
        if same_configured_provider
        else 0
    )
    context_window, _ = resolve_effective_context_window(
        catalog,
        model,
        provider=provider_id,
        global_override=global_window,
    )
    application_cap = int(
        getattr(gateway_config, "context_budget_tokens", 0) or 0
    )
    if application_cap > 0:
        context_window = min(int(context_window), application_cap)

    configured_output = (
        int(getattr(llm_config, "max_tokens", 0) or 0)
        if same_configured_provider
        else 0
    )
    output_tokens = int(
        catalog.resolve_max_tokens(
            model,
            user_override=configured_output,
            provider=provider_id,
        )
        or 0
    )
    output_tokens = max(1, min(output_tokens or 1, max(1, int(context_window) - 1)))
    thinking_budget_tokens = _configured_thinking_reserve_tokens(llm_config)
    derived_cap = ContextBudgetGovernor.from_values(
        context_window_tokens=context_window,
        max_output_tokens=output_tokens,
        thinking_budget_tokens=thinking_budget_tokens,
        context_overflow_threshold=0.85,
    ).snapshot().provider_request_max_chars
    explicit_cap = (
        int(getattr(llm_config, "provider_request_proof_max_chars", 0) or 0)
        if same_configured_provider
        else 0
    )
    request_max_chars = min(explicit_cap, derived_cap) if explicit_cap > 0 else derived_cap
    next_request_reserve_tokens = _manual_next_request_reserve_tokens(
        int(context_window)
    )
    return (
        max(1, int(context_window)),
        output_tokens,
        max(1, int(request_max_chars)),
        next_request_reserve_tokens,
        next_request_reserve_tokens * 4,
    )


def _configured_thinking_reserve_tokens(llm_config: object | None) -> int:
    """Return the configured reasoning reserve without inspecting a prompt."""

    level = _text(getattr(llm_config, "thinking", None)).lower()
    return {
        "off": 0,
        "minimal": 1_024,
        "low": 4_096,
        "medium": 10_000,
        "high": 20_000,
        "xhigh": 50_000,
        # Adaptive cannot be resolved before the next active prompt exists.
        # The medium reserve is bounded and the separate next-request reserve
        # still protects the prompt/tool/media envelope.
        "adaptive": 10_000,
    }.get(level, 0)


def _manual_next_request_reserve_tokens(context_window_tokens: int) -> int:
    """Reserve a bounded share for the unknown next authoritative envelope."""

    window = max(1, int(context_window_tokens or 0))
    return min(max(1, window - 1), max(1_024, min(32_768, window // 5)))


def _manual_consumer_messages(
    replay_summary: str,
    kept_entries: list[dict[str, Any]],
) -> list[Message] | None:
    """Rebuild the provider-visible durable portion of the next request."""

    from opensquilla.engine.history import (
        reconstruct_messages_from_entry,
        repair_tool_pairing,
    )
    from opensquilla.engine.session_sanitize import (
        project_historical_tool_payloads,
        sanitize_session_messages,
    )
    from opensquilla.session.context_view import format_compaction_summary_context

    history: list[Message] = []
    for entry in kept_entries:
        if not isinstance(entry, dict):
            return None
        history.extend(
            reconstruct_messages_from_entry(
                _text(entry.get("role")),
                entry.get("content") or "",
                entry.get("tool_calls"),
                entry.get("reasoning_content"),
            )
        )
    history, _ = sanitize_session_messages(history)
    history, _ = project_historical_tool_payloads(
        history,
        preserve_reasoning_content=True,
    )
    history = repair_tool_pairing(history)

    summary_context = format_compaction_summary_context([replay_summary])
    if summary_context:
        history.append(
            Message(
                role="user",
                content="\n".join(
                    [
                        "[Request context for this turn]",
                        (
                            "This request-scoped context is not a user request "
                            "and is not transcript history."
                        ),
                        "Use it only when it is relevant to the current user request.",
                        summary_context.strip(),
                    ]
                ),
            )
        )
    return history


def _selector_base_config(selector: object | None) -> ProviderConfig | None:
    """Return a read-only configured base link, resetting only a clone."""

    if selector is None:
        return None
    clone = getattr(selector, "clone", None)
    if callable(clone):
        try:
            cloned = clone()
            current = getattr(cloned, "current_config", None)
        except Exception:  # noqa: BLE001 - compatibility selector
            current = None
        if isinstance(current, ProviderConfig):
            return current
    return _selector_current_config(selector)


def _selector_current_config(selector: object | None) -> ProviderConfig | None:
    if selector is None:
        return None
    try:
        current = getattr(selector, "current_config", None)
    except Exception:  # noqa: BLE001
        return None
    return current if isinstance(current, ProviderConfig) else None


def _automatic_source(
    session: object | None,
    inherited: ProviderConfig | None,
) -> str:
    if _text(getattr(session, "provider_override", None)):
        return "session_provider_override"
    if _text(getattr(session, "model_provider", None)):
        return "session_model_provider"
    if inherited is not None:
        return "selector_current"
    return "selected_provider_compat"


def _text(value: object) -> str:
    return str(value or "").strip()
