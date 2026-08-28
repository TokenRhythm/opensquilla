"""Single implementation of applying a per-turn model to a cloned selector.

Two turn-path sites apply a model override — the pipeline tail applies the
*routed* model, PromptAssemblerStage applies an *explicit* per-turn model on
top of it. They previously carried textually near-identical blocks that had
already drifted once (the routed_model telemetry realignment existed only in
the stage copy). The mechanics live here exactly once, including the
cross-provider tier path (credential resolution + continuity gate).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_ROUTE_SAVINGS_KEYS = (
    "savings_pct",
    "savings_max_price_per_m",
    "savings_routed_price_per_m",
)


def _metadata_nonnegative_int(turn_metadata: dict[str, Any], key: str) -> int:
    value = turn_metadata.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    return max(0, value)


def _large_context_capacity_required(turn_metadata: dict[str, Any]) -> bool:
    complete_request = bool(turn_metadata.get("large_context_capacity_required")) and (
        _metadata_nonnegative_int(
            turn_metadata,
            "large_context_request_input_tokens",
        )
        > 0
    )
    legacy_floor = bool(turn_metadata.get("large_context_floor_min_tier")) and (
        _metadata_nonnegative_int(
            turn_metadata,
            "large_context_material_tokens",
        )
        > 0
    )
    return complete_request or legacy_floor


def _bounded_fallback_chain_required(turn_metadata: dict[str, Any]) -> bool:
    # A bare floor marker historically selected the bounded selector API even
    # in light test/compatibility adapters that carried no admission metrics.
    return _large_context_capacity_required(turn_metadata) or bool(
        turn_metadata.get("large_context_floor_min_tier")
    )


def _provider_config_has_request_capacity(
    config: Any,
    turn_metadata: dict[str, Any],
    *,
    provider: str = "",
    model: str = "",
) -> bool:
    """Validate one final physical deployment against the routed material."""

    if not _large_context_capacity_required(turn_metadata):
        return True
    from opensquilla.engine.capacity_admission import (
        MAX_THINKING_BUDGET_TOKENS,
        model_has_request_capacity,
    )

    thinking_budget = turn_metadata.get("large_context_thinking_budget_tokens")
    if not isinstance(thinking_budget, int) or isinstance(thinking_budget, bool):
        thinking_budget = MAX_THINKING_BUDGET_TOKENS
    return model_has_request_capacity(
        provider=(
            str(getattr(config, "provider", "") or "").strip()
            or str(provider or "").strip()
        ),
        model=(
            str(getattr(config, "model", "") or "").strip()
            or str(model or "").strip()
        ),
        material_tokens=_metadata_nonnegative_int(
            turn_metadata,
            "large_context_material_tokens",
        ),
        request_input_tokens=_metadata_nonnegative_int(
            turn_metadata,
            "large_context_request_input_tokens",
        ),
        thinking_budget_tokens=max(0, thinking_budget),
        context_window_override_tokens=_metadata_nonnegative_int(
            turn_metadata,
            "large_context_context_window_override_tokens",
        ),
        max_output_override_tokens=_metadata_nonnegative_int(
            turn_metadata,
            "large_context_max_output_override_tokens",
        ),
        provider_request_proof_max_chars=_metadata_nonnegative_int(
            turn_metadata,
            "large_context_provider_request_proof_max_chars",
        ),
        api_key=str(getattr(config, "api_key", "") or ""),
        base_url=str(getattr(config, "base_url", "") or ""),
        proxy=str(getattr(config, "proxy", "") or ""),
    )


def _require_provider_config_capacity(
    config: Any,
    turn_metadata: dict[str, Any],
    *,
    reason: str,
    provider: str = "",
    model: str = "",
) -> None:
    if _provider_config_has_request_capacity(
        config,
        turn_metadata,
        provider=provider,
        model=model,
    ):
        return
    from opensquilla.engine.capacity_admission import (
        CAPACITY_CONFIGURATION_HINT,
        LargeContextCapacityError,
    )

    turn_metadata["large_context_capacity_blocked"] = True
    actionable_reason = f"{reason} {CAPACITY_CONFIGURATION_HINT}"
    turn_metadata["large_context_capacity_block_reason"] = actionable_reason
    raise LargeContextCapacityError(actionable_reason)


def _materialize_fallback_configs(
    selector: Any,
    entries: Sequence[object],
) -> list[Any]:
    """Resolve router identities to the exact configured deployment objects."""

    current = getattr(selector, "current_config", None)
    if current is None:
        return []
    remaining_chain = getattr(selector, "remaining_chain", None)
    try:
        configured_tail = (
            list(remaining_chain())[1:] if callable(remaining_chain) else []
        )
    except Exception:  # noqa: BLE001 - opaque selectors fail closed
        configured_tail = []

    materialized: list[Any] = []
    for entry in entries:
        if not isinstance(entry, dict):
            if getattr(entry, "provider", None) and getattr(entry, "model", None):
                materialized.append(entry)
            continue
        provider = str(
            entry.get("provider") or getattr(current, "provider", "") or ""
        ).strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue
        matches = [
            cfg
            for cfg in configured_tail
            if str(getattr(cfg, "provider", "") or "").strip() == provider
            and str(getattr(cfg, "model", "") or "").strip() == model
        ]
        if matches:
            materialized.extend(matches)
            continue
        if provider != str(getattr(current, "provider", "") or "").strip():
            continue
        try:
            materialized.append(
                replace(
                    current,
                    model=model,
                    provider_routing=dict(
                        getattr(current, "provider_routing", {}) or {}
                    ),
                )
            )
        except (TypeError, ValueError):
            continue
    return materialized


def _capacity_approved_configured_fallbacks(
    selector: Any,
    turn_metadata: dict[str, Any],
) -> list[Any]:
    """Keep only configured fallbacks with definite request-budget capacity."""

    remaining_chain = getattr(selector, "remaining_chain", None)
    if not callable(remaining_chain):
        return []
    try:
        # The current deployment becomes the first fallback after either a
        # same-provider model override or a cross-provider head replacement.
        # Admit it here when its exact endpoint is safe; the new head is
        # validated independently before execution.
        configured_tail = list(remaining_chain())
    except Exception:  # noqa: BLE001 - an opaque selector fails closed
        return []

    return _capacity_approved_fallback_entries(
        selector,
        configured_tail,
        turn_metadata,
    )


def _capacity_approved_fallback_entries(
    selector: Any,
    entries: Sequence[object],
    turn_metadata: dict[str, Any],
) -> list[Any]:
    """Admit fallbacks only when catalog data proves the full input budget fits."""

    if not _large_context_capacity_required(turn_metadata):
        return []
    return [
        config
        for config in _materialize_fallback_configs(selector, entries)
        if _provider_config_has_request_capacity(config, turn_metadata)
    ]


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


def report_profile_credential_lease_failure(
    provider_id: str,
    session_key: str,
    lease_token: str,
    failure_kind: Any,
) -> bool:
    """Report a paid-media failure only for its exact credential lease.

    This deliberately does not fall back to the session-only compatibility
    API: a late media subprocess must not park the key acquired by a newer
    run of the same session.
    """

    try:
        from opensquilla.gateway.llm_runtime import profile_credential_pools

        return profile_credential_pools().report_failure_for_lease(
            provider_id,
            session_key,
            lease_token,
            failure_kind,
        )
    except Exception:  # noqa: BLE001 - credential bookkeeping only
        log.debug("credential_pool.lease_report_failed", provider=provider_id)
        return False


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
    remaining_chain = getattr(selector, "remaining_chain", None)
    if callable(remaining_chain):
        candidates: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for candidate in remaining_chain():
            candidate_provider = str(
                getattr(candidate, "provider", "") or ""
            ).strip()
            candidate_model = str(getattr(candidate, "model", "") or "").strip()
            identity = (candidate_provider, candidate_model)
            if not candidate_provider or not candidate_model or identity in seen:
                continue
            seen.add(identity)
            candidates.append(
                {
                    "provider": candidate_provider,
                    "model": candidate_model,
                }
            )
        turn_metadata["selector_execution_chain"] = candidates
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
    if turn_metadata.get("large_context_capacity_blocked") is True:
        from opensquilla.engine.capacity_admission import LargeContextCapacityError

        reason = str(
            turn_metadata.get("large_context_capacity_block_reason")
            or "No deployment has proven capacity for this attachment request."
        )
        raise LargeContextCapacityError(reason)

    if tier_provider_config is not None and hasattr(selector, "override_provider_config"):
        _require_provider_config_capacity(
            tier_provider_config,
            turn_metadata,
            reason=(
                "The resolved cross-provider deployment does not have proven "
                "capacity for this attachment request."
            ),
        )
        if turn_metadata.get("router_fallback_strict") is True:
            try:
                selector.override_provider_config(
                    tier_provider_config,
                    preserve_existing_tail=False,
                )
            except TypeError as exc:
                raise RuntimeError(
                    "selector does not support strict artifact fallback isolation"
                ) from exc
        else:
            bounded_provider_override = getattr(
                selector,
                "override_provider_config_with_bounded_fallbacks",
                None,
            )
            if (
                _bounded_fallback_chain_required(turn_metadata)
                and callable(bounded_provider_override)
            ):
                bounded_provider_override(
                    tier_provider_config,
                    _capacity_approved_configured_fallbacks(selector, turn_metadata),
                )
            else:
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
        _require_provider_config_capacity(
            current_config,
            turn_metadata,
            reason=(
                "The explicit model override does not have proven capacity "
                "for this attachment request."
            ),
        )
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
        _require_provider_config_capacity(
            current_config,
            turn_metadata,
            reason=(
                "The configured primary deployment does not have proven capacity "
                "after the routed provider was blocked."
            ),
        )
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
    override_with_bounded_fallback_chain = getattr(
        selector,
        "override_model_with_bounded_fallback_chain",
        None,
    )
    bounded_fallbacks_required = _bounded_fallback_chain_required(turn_metadata)
    if (
        turn_metadata.get("router_fallback_strict") is True
        and callable(override_with_fallback_chain)
        and isinstance(router_fallback_chain, list)
    ):
        try:
            override_with_fallback_chain(
                model,
                router_fallback_chain,
                preserve_existing_tail=False,
            )
        except TypeError:
            # Compatibility for third-party selector shims that implement
            # the older two-argument hook. A strict Artifact turn must not
            # silently retain their unknown fallback tail.
            selector.override_model(model)
    elif bounded_fallbacks_required and callable(override_with_bounded_fallback_chain):
        approved_router_fallbacks = _capacity_approved_fallback_entries(
            selector,
            router_fallback_chain if isinstance(router_fallback_chain, list) else [],
            turn_metadata,
        )
        approved_configured_fallbacks = _capacity_approved_configured_fallbacks(
            selector,
            turn_metadata,
        )
        override_with_bounded_fallback_chain(
            model,
            approved_router_fallbacks,
            approved_configured_fallbacks,
        )
    elif bounded_fallbacks_required:
        # Compatibility selectors may predate the bounded-chain API. Keep the
        # proven head executable, but never hand an opaque selector fallbacks
        # whose physical deployment capacities cannot be audited.
        selector.override_model(model)
    elif callable(override_with_fallback_chain) and isinstance(router_fallback_chain, list):
        override_with_fallback_chain(model, router_fallback_chain)
    else:
        selector.override_model(model)
    _require_provider_config_capacity(
        getattr(selector, "current_config", None),
        turn_metadata,
        provider=active_provider or routed_provider,
        model=model,
        reason=(
            "The final model deployment does not have proven capacity for this "
            "attachment request."
        ),
    )
    provider = _resolve_and_record_execution(selector, turn_metadata)

    if realign_routed_model and turn_metadata.get("routed_model") not in (None, model):
        turn_metadata["routed_model"] = model
        for savings_key in _ROUTE_SAVINGS_KEYS:
            if savings_key in turn_metadata:
                turn_metadata[savings_key] = 0.0
    return provider
