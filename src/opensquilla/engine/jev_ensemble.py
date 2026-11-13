"""Local preparation of the configured multi-model execution offered by JEV."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from opensquilla.provider.selector import ProviderConfig
from opensquilla.router_tiers import (
    effective_ensemble_selection_mode,
    ensemble_selection_configured,
)

JEV_ENSEMBLE_CANDIDATE_ID = "ensemble:configured"


@dataclass(frozen=True)
class PreparedJevEnsemble:
    provider: Any
    deployment: ProviderConfig
    descriptor: dict[str, Any]
    supports_tools: bool
    context_window_tokens: int


def jev_ensemble_descriptor(
    config: Any, *, inherited: ProviderConfig, session_key: str = "",
) -> dict[str, Any] | None:
    """Return a non-secret projection of a ready saved lineup."""
    prepared = prepare_jev_ensemble(config, inherited=inherited, session_key=session_key)
    return prepared.descriptor if prepared is not None else None


def prepare_jev_ensemble(
    config: Any, *, inherited: ProviderConfig, fallback_provider: Any = None,
    catalog: Any = None, session_key: str = "", acquire: bool = False,
    expected_fingerprint: str | None = None,
    fallback_selector: Any = None,
) -> PreparedJevEnsemble | None:
    """Prepare a ready saved lineup without sending requests or activating it."""
    from opensquilla.engine.selector_override import (
        acquire_profile_credential,
        peek_profile_credential,
        report_profile_credential_failure,
    )
    from opensquilla.provider.ensemble import (
        _member_tools_capability_is_verified,
        build_ensemble_provider_from_config,
    )
    from opensquilla.provider.model_catalog import shared_catalog

    if not getattr(getattr(config, "jev_router", None), "ensemble_enabled", True):
        return None
    if not ensemble_selection_configured(config):
        return None
    ensemble_config = getattr(config, "llm_ensemble", None)
    if ensemble_config is None or not inherited.provider or not inherited.model:
        return None
    selection = effective_ensemble_selection_mode(config)
    material = {
        "ensemble": ensemble_config.model_dump(mode="json", exclude={"enabled"}),
        "fallback": {"provider": inherited.provider, "model": inherited.model},
        "strategy": selection,
    }
    fingerprint = hashlib.sha256(json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    if expected_fingerprint is not None and expected_fingerprint != fingerprint:
        return None
    model_catalog = catalog if catalog is not None else shared_catalog()
    try:
        provider = build_ensemble_provider_from_config(
            config=config, inherited_provider_config=inherited,
            fallback_provider=fallback_provider,
            _enable_member_request_budget_rebinding=True,
            _model_catalog=model_catalog,
            _credential_pool_acquirer=(
                acquire_profile_credential if acquire else peek_profile_credential
            ),
            _credential_pool_failure_reporter=report_profile_credential_failure,
            _session_key=session_key,
            _fallback_selector=fallback_selector,
            _selection_mode_override=selection,
            _defer_provider_state_replay_activation=True,
        )
        if len(provider.proposers) < 2 or not all(
            member.ready for member in [*provider.proposers, provider.aggregator]
        ):
            return None
        members = [*provider.proposers, provider.aggregator]
        windows = [model_catalog.resolve_deployment_limits(
            member.provider_config.model, provider=member.provider_config.provider,
            api_key=member.provider_config.api_key, base_url=member.provider_config.base_url,
            proxy=member.provider_config.proxy,
        ) for member in members]
        known_window = min(
            (int(window.context_window) for window in windows if window.context_window_known),
            default=0,
        ) if all(window.context_window_known for window in windows) else 0
        tools = _member_tools_capability_is_verified(
            provider.aggregator, model_catalog=model_catalog,
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return None

    def identity(member: Any) -> dict[str, str]:
        return {"provider": member.provider_config.provider, "model": member.provider_config.model}

    descriptor = {
        "tier": "c4", "label": "模型融合 · 多路分析与综合验证", "strategy": selection,
        "proposers": [identity(member) for member in provider.proposers],
        "aggregator": identity(provider.aggregator), "configurationFingerprint": fingerprint,
    }
    deployment = inherited
    if provider.selection_plan.get("provider_state_replay") == "disabled_cross_provider":
        deployment = replace(
            inherited, provider_routing=dict(inherited.provider_routing),
            replay_provider_state=False,
        )
    return PreparedJevEnsemble(
        provider=provider, deployment=deployment, descriptor=descriptor,
        supports_tools=tools, context_window_tokens=known_window,
    )
