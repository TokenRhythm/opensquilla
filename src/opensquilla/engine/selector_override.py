"""Single implementation of applying a per-turn model to a cloned selector.

Two turn-path sites apply a model override — the pipeline tail applies the
*routed* model, PromptAssemblerStage applies an *explicit* per-turn model on
top of it. They previously carried textually near-identical blocks that had
already drifted once (the routed_model telemetry realignment existed only in
the stage copy). The mechanics live here exactly once, including the
cross-provider tier path (credential resolution + continuity gate).
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

_ROUTE_SAVINGS_KEYS = (
    "savings_pct",
    "savings_max_price_per_m",
    "savings_routed_price_per_m",
)


def acquire_profile_credential(
    provider_id: str,
    pool_names: list[str],
    session_key: str,
) -> Any | None:
    """Engine-layer adapter for the process-wide profile credential pools.

    The provider package stays below the gateway layer; runtime callers pass
    this adapter into the shared deployment resolver instead of introducing a
    provider -> gateway import cycle.
    """
    from opensquilla.gateway.llm_runtime import (
        NoCredentialsAvailable,
        profile_credential_pools,
    )
    from opensquilla.provider.deployment import CredentialPoolExhaustedError

    try:
        return profile_credential_pools().acquire_for_session(
            provider_id,
            pool_names,
            session_key,
        )
    except NoCredentialsAvailable as exc:
        raise CredentialPoolExhaustedError from exc


def peek_profile_credential(
    provider_id: str,
    pool_names: list[str],
    _session_key: str,
) -> Any | None:
    """Read process-wide pool readiness without acquiring or pinning a key."""
    from opensquilla.gateway.llm_runtime import (
        NoCredentialsAvailable,
        profile_credential_pools,
    )
    from opensquilla.provider.deployment import CredentialPoolExhaustedError

    try:
        return profile_credential_pools().peek_available(provider_id, pool_names)
    except NoCredentialsAvailable as exc:
        raise CredentialPoolExhaustedError from exc


def report_profile_credential_failure(
    provider_id: str,
    session_key: str,
    failure_kind: Any,
) -> None:
    """Report an ensemble member failure to the same process-wide pool."""
    try:
        from opensquilla.gateway.llm_runtime import profile_credential_pools

        profile_credential_pools().report_failure(
            provider_id,
            session_key,
            failure_kind,
        )
    except Exception:  # noqa: BLE001 - credential bookkeeping only
        log.debug("credential_pool.report_failed", provider=provider_id)


def resolve_tier_provider_config(
    config: Any,
    provider_id: str,
    model: str,
    *,
    session_key: str = "",
    turn_metadata: dict[str, Any] | None = None,
) -> Any | None:
    """Build a per-turn ProviderConfig for a cross-provider router tier.

    Credentials come from ``[llm_profiles.<provider_id>]`` when present,
    falling back to the registry env key; the base URL falls back to the
    registry default. Returns None (with a warning) when the provider is
    unknown or a required key cannot be resolved — the caller keeps the
    active provider, never guesses secrets.

    Key resolution order: explicit ``api_key``, then ``api_key_env_pool``
    (session-pinned rotation over env-var names; a pool whose names all
    resolve to nothing degrades to the next step), then ``api_key_env`` or
    the registry env key. A profile without a pool takes exactly the
    pre-pool single-key path. When a pool credential is used, its non-secret
    identifiers are recorded in ``turn_metadata['credential_pool']`` so the
    provider-failure path can park the key on 429/credits/auth failures.
    """
    from opensquilla.provider.deployment import resolve_provider_deployment

    resolution = resolve_provider_deployment(
        config,
        provider_id,
        model,
        session_key=session_key,
        turn_metadata=turn_metadata,
        # A router tier always crosses from the active selector when this
        # helper is called; provider-bound state must never follow it.
        replay_provider_state=False,
        credential_pool_acquirer=acquire_profile_credential,
    )
    if turn_metadata is not None:
        turn_metadata["routed_provider_resolution"] = {
            "provider": resolution.provider,
            "model": resolution.model,
            "ready": resolution.ready,
            "reason": resolution.reason,
            "credential_source": resolution.credential_source,
            "endpoint_source": resolution.endpoint_source,
        }
    if resolution.ready:
        return resolution.provider_config
    event_by_reason = {
        "unknown_provider": "cross_provider_tier.unknown_provider",
        "runtime_unsupported": "cross_provider_tier.no_runtime_support",
        "credential_pool_exhausted": "cross_provider_tier.credential_pool_exhausted",
        "missing_credential": "cross_provider_tier.credentials_unresolved",
        "missing_base_url": "cross_provider_tier.base_url_unresolved",
    }
    log.warning(
        event_by_reason.get(
            resolution.reason,
            "cross_provider_tier.deployment_unresolved",
        ),
        provider=resolution.provider,
        reason=resolution.reason,
    )
    return None


def cross_provider_tier_config(
    config: Any,
    turn_metadata: dict[str, Any],
    model: str,
    *,
    active_provider_id: str,
    session_key: str = "",
) -> Any | None:
    """Return the ProviderConfig for an executable cross-provider tier, or None.

    Execution requires ALL of:
    - ``squilla_router.cross_provider_tiers`` enabled (preview flag, default off)
    - routing applied this turn with a tier provider differing from the active one
    - the provider-state continuity diagnostic did not report unrecoverable
      provider-bound state (``discard_provider_state``) — with only
      provider-bound native state and no portable fallback, switching would
      silently degrade the session
    - resolvable credentials (profile or env), never guessed
    """
    if turn_metadata.get("routing_applied") is not True:
        return None
    routed_provider = str(turn_metadata.get("routed_provider") or "").strip().lower()
    active_provider = (active_provider_id or "").strip().lower()
    continuity = turn_metadata.get("provider_state_continuity")
    decision = str(continuity.get("decision") or "") if isinstance(continuity, dict) else ""
    active_state_provider = (
        str(continuity.get("active_state_provider") or "").strip().lower()
        if isinstance(continuity, dict)
        else ""
    )
    if routed_provider and active_state_provider and active_state_provider != routed_provider:
        # This also covers a B -> configured-primary-A transition, where the
        # selector's active provider id already equals the routed target and
        # no cross-provider ProviderConfig needs to be resolved.
        turn_metadata["provider_state_replay_disabled"] = "provider_transition"
    if not routed_provider or routed_provider == active_provider:
        return None
    router_cfg = getattr(config, "squilla_router", None)
    if not bool(getattr(router_cfg, "cross_provider_tiers", False)):
        mismatch_policy = (
            str(getattr(router_cfg, "tier_provider_mismatch", "route") or "route")
            .strip()
            .lower()
        )
        if mismatch_policy == "veto":
            # Veto operators opted out of the historical misroute entirely.
            # Reaching this point means the upstream tier rebind abstained
            # (no same-provider rebind target), so fail closed: the blocked
            # marker makes apply_model_override keep the primary provider
            # *and its model* — a foreign model id is never sent with the
            # primary provider's credentials in veto mode.
            turn_metadata["routed_provider_blocked"] = "cross_provider_tiers_disabled"
            turn_metadata["routed_provider_fallback_reason"] = (
                "cross_provider_tiers_disabled"
            )
            return None
        # Default 'route' policy: the documented (and loudly flagged)
        # historical contract runs the tier's model id on the active
        # provider's deployment — aggregator-style endpoints serve foreign
        # model ids and hand-authored ladders depend on it.  Returning None
        # without the blocked marker lets apply_model_override apply the
        # routed model to the primary provider.
        return None
    if decision == "discard_provider_state":
        log.warning(
            "cross_provider_tier.blocked_by_continuity",
            provider=routed_provider,
            decision=decision,
        )
        turn_metadata["routed_provider_blocked"] = "provider_state_continuity"
        turn_metadata["routed_provider_fallback_reason"] = "provider_state_continuity"
        return None
    resolved = resolve_tier_provider_config(
        config,
        routed_provider,
        model,
        session_key=session_key,
        turn_metadata=turn_metadata,
    )
    if resolved is None:
        # The apply boundary uses this marker to keep the selector's original
        # provider *and model*.  Without it, the foreign model id would be
        # applied to the primary provider after resolution failed.
        resolution = turn_metadata.get("routed_provider_resolution")
        reason = (
            str(resolution.get("reason") or "deployment_unresolved")
            if isinstance(resolution, dict)
            else "deployment_unresolved"
        )
        turn_metadata["routed_provider_blocked"] = reason
        turn_metadata["routed_provider_fallback_reason"] = reason
    return resolved


def _resolve_and_record_execution(
    selector: Any,
    turn_metadata: dict[str, Any],
) -> Any:
    """Resolve the selector and stamp the exact provider/model chain head."""
    provider = selector.resolve()
    current_config = getattr(selector, "current_config", None)
    turn_metadata["executed_provider"] = str(
        getattr(current_config, "provider", "") or ""
    )
    turn_metadata["executed_model"] = str(
        getattr(current_config, "model", "") or ""
    )
    return provider


def _disable_selector_provider_state_replay(
    selector: Any,
    turn_metadata: dict[str, Any],
) -> None:
    if not turn_metadata.get("provider_state_replay_disabled"):
        return
    disable = getattr(selector, "disable_provider_state_replay", None)
    if callable(disable):
        disable()


def apply_model_override(
    selector: Any,
    model: str,
    *,
    turn_metadata: dict[str, Any],
    realign_routed_model: bool,
    tier_provider_config: Any | None = None,
) -> Any:
    """Apply ``model`` to the cloned selector and resolve the provider.

    ``realign_routed_model`` is True only for the explicit-override site: an
    explicit model replaces the routed choice, so ``routed_model`` (read by
    RouterDecisionEvent and comprehensive-savings pricing) must follow and the
    route-savings figures no longer apply. The routed-model site must NOT
    realign — in observe rollout phase the baseline model runs while
    ``routed_model`` intentionally records the would-be routed choice.

    ``tier_provider_config`` switches the turn to a cross-provider tier's
    full ProviderConfig; the router fallback chain is skipped in that case
    (its entries are same-provider models of the provider being left).
    """
    if tier_provider_config is not None and hasattr(selector, "override_provider_config"):
        selector.override_provider_config(tier_provider_config)
        turn_metadata["routed_provider_applied"] = tier_provider_config.provider
        turn_metadata["provider_state_replay_disabled"] = "cross_provider_route"
        _disable_selector_provider_state_replay(selector, turn_metadata)
        return _resolve_and_record_execution(selector, turn_metadata)

    restore_primary = getattr(selector, "override_original_primary_model", None)
    if (
        realign_routed_model
        and turn_metadata.get("routed_provider_applied")
        and callable(restore_primary)
    ):
        routed_provider = str(turn_metadata.get("routed_provider_applied") or "")
        restore_primary(model)
        _disable_selector_provider_state_replay(selector, turn_metadata)
        current_config = getattr(selector, "current_config", None)
        executed_provider = str(getattr(current_config, "provider", "") or "")
        turn_metadata["routed_provider_explicit_override_from"] = routed_provider
        turn_metadata["routed_provider_fallback_reason"] = "explicit_model_override"
        turn_metadata["routed_provider_fallback_provider"] = executed_provider
        turn_metadata["routed_provider_fallback_model"] = str(
            getattr(current_config, "model", "") or ""
        )
        provider = _resolve_and_record_execution(selector, turn_metadata)
        if turn_metadata.get("routed_model") not in (None, model):
            turn_metadata["routed_model"] = model
            for savings_key in _ROUTE_SAVINGS_KEYS:
                if savings_key in turn_metadata:
                    turn_metadata[savings_key] = 0.0
        return provider

    _disable_selector_provider_state_replay(selector, turn_metadata)
    routed_provider = str(turn_metadata.get("routed_provider") or "").strip().lower()
    active_provider = str(getattr(selector, "active_provider_id", "") or "").strip().lower()
    routed_model = str(turn_metadata.get("routed_model") or "").strip()
    blocked_choice_is_still_requested = (
        not realign_routed_model or not routed_model or model == routed_model
    )
    if (
        turn_metadata.get("routed_provider_blocked")
        and blocked_choice_is_still_requested
        and routed_provider
        and active_provider
        and routed_provider != active_provider
    ):
        current_config = getattr(selector, "current_config", None)
        turn_metadata["routed_provider_fallback_provider"] = active_provider
        turn_metadata["routed_provider_fallback_model"] = str(
            getattr(current_config, "model", "") or ""
        )
        return _resolve_and_record_execution(selector, turn_metadata)

    router_fallback_chain = (
        turn_metadata.get("router_fallback_chain")
        if turn_metadata.get("routing_applied") is True
        else None
    )
    override_with_fallback_chain = getattr(
        selector,
        "override_model_with_fallback_chain",
        None,
    )
    if callable(override_with_fallback_chain) and isinstance(router_fallback_chain, list):
        override_with_fallback_chain(model, router_fallback_chain)
    else:
        selector.override_model(model)
    provider = _resolve_and_record_execution(selector, turn_metadata)

    if realign_routed_model and turn_metadata.get("routed_model") not in (None, model):
        turn_metadata["routed_model"] = model
        for savings_key in _ROUTE_SAVINGS_KEYS:
            if savings_key in turn_metadata:
                turn_metadata[savings_key] = 0.0
    return provider
