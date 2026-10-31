"""Explicit adoption of remote recommendations into operator-owned config.

The catalogue is data, not active configuration. Only first-time onboarding
and the confirmed apply command may call these mutations.
"""

from __future__ import annotations

import logging
from typing import Any

from opensquilla.router_tiers import (
    CUSTOM_B5_SELECTION_MODE,
    TEXT_TIERS,
    TierConfig,
    effective_ensemble_selection_mode,
    recommended_ensemble_selection_mode_for_provider,
    static_b5_profile,
)

SUPPORTED_PROVIDERS = frozenset({"openrouter", "tokenrhythm"})
_THINKING_LEVELS = {None, "off", "minimal", "low", "medium", "high", "xhigh", "adaptive"}
_log = logging.getLogger(__name__)


def _row(role: str, provider: str, model: str, thinking: str | None) -> dict[str, Any]:
    return {
        "role": role, "provider": provider, "model": model,
        "thinkingLevel": thinking or None,
    }


def current_recommendation_rows(
    config: Any, provider_id: str, kind: str,
) -> list[dict[str, Any]]:
    """Expose actual saved values, never resolving a current remote preset."""
    if provider_id not in SUPPORTED_PROVIDERS or kind not in {"router", "ensemble"}:
        raise ValueError("unsupported recommendation")
    if kind == "router":
        rows = []
        for role in TEXT_TIERS:
            tier = TierConfig.from_value(config.squilla_router.tiers.get(role))
            rows.append(_row(
                role, tier.provider or config.llm.provider, tier.model, tier.thinking_level,
            ))
        return rows
    mode = effective_ensemble_selection_mode(config)
    profile = static_b5_profile(mode, config)
    if profile is not None:
        rows = [
            _row(
                f"proposer_{index + 1}", profile.provider_id, model,
                profile.proposer_thinking_levels[index]
                if profile.proposer_thinking_levels else profile.thinking_level,
            )
            for index, model in enumerate(profile.proposer_models)
        ]
        rows.append(_row(
            "aggregator", profile.provider_id, profile.aggregator_model,
            profile.aggregator_thinking_level
            if profile.proposer_thinking_levels else profile.thinking_level,
        ))
        return rows
    rows = []
    if mode == CUSTOM_B5_SELECTION_MODE:
        for candidate in config.llm_ensemble.candidates:
            if candidate.enabled and candidate.role != "aggregator":
                rows.append(_row(
                    f"proposer_{len(rows) + 1}", candidate.provider,
                    candidate.model, candidate.thinking_level,
                ))
        aggregator = next((
            candidate for candidate in config.llm_ensemble.candidates
            if candidate.enabled and candidate.role == "aggregator"
        ), None)
        rows.append(_row(
            "aggregator", aggregator.provider if aggregator else config.llm.provider,
            aggregator.model if aggregator else config.llm.model,
            aggregator.thinking_level if aggregator else getattr(config.llm, "thinking", None),
        ))
    return rows


def apply_recommendation(config: Any, plan: dict[str, Any]) -> list[str]:
    """Materialize a complete approved plan on a detached config candidate.

    Does not fetch, persist, switch routing mode, or change another scheme.
    The caller owns the transaction and optimistic revision check.
    """
    from opensquilla.gateway.config import (
        LlmEnsembleConfig,
        SquillaRouterConfig,
    )
    from opensquilla.onboarding.router_policy import PrimaryProviderChangedError

    provider = plan.get("providerId")
    kind = plan.get("kind")
    revision = plan.get("revision")
    if provider not in SUPPORTED_PROVIDERS or kind not in {"router", "ensemble"}:
        raise ValueError("unsupported recommendation")
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("recommendation revision is required")
    rows = plan.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("recommendation rows are required")
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("provider") != provider
            or not isinstance(row.get("model"), str)
            or not row["model"].strip()
            or len(row["model"]) > 512
            or row.get("thinkingLevel") not in _THINKING_LEVELS
        ):
            raise ValueError("invalid recommendation model")
    if kind == "router":
        if provider != str(config.llm.provider).strip().lower():
            raise PrimaryProviderChangedError("The primary provider changed; review again")
        if [row.get("role") for row in rows] != list(TEXT_TIERS):
            raise ValueError("recommendation must contain exactly C0 through C3")
        payload = config.squilla_router.model_dump(mode="python")
        tiers = dict(payload["tiers"])
        for row in rows:
            previous = tiers.get(row["role"])
            tier = dict(previous) if isinstance(previous, dict) else {}
            tier.update(provider=provider, model=row["model"])
            tier.pop("thinking_level", None)
            tier.pop("thinking", None)
            tier.pop("supports_thinking", None)
            if row["thinkingLevel"] is not None:
                tier["thinking_level"] = row["thinkingLevel"]
            tiers[row["role"]] = tier
        payload.update(
            tiers=tiers, tier_profile=None, preset_binding="follow_primary",
            recommendation_revision=revision,
        )
        config.squilla_router = SquillaRouterConfig(**payload)
        # Persist a default-valued primary too: a provider-less legacy file
        # can otherwise infer OpenRouter and retarget these saved tiers.
        config.mark_force_persist("llm.provider")
        paths = [
            "squilla_router.tiers", "squilla_router.tier_profile",
            "squilla_router.preset_binding", "squilla_router.recommendation_revision",
        ]
    else:
        if not 3 <= len(rows) <= 7 or [row.get("role") for row in rows] != [
            *(f"proposer_{index + 1}" for index in range(len(rows) - 1)), "aggregator",
        ]:
            raise ValueError("recommendation requires 2–6 proposers and one aggregator")
        if len({row["model"] for row in rows[:-1]}) != len(rows) - 1:
            raise ValueError("recommendation proposer models must be distinct")
        if config.llm_ensemble.min_successful_proposers > len(rows) - 1:
            raise ValueError("recommendation has fewer proposers than the configured minimum")
        payload = config.llm_ensemble.model_dump(mode="python")
        profiles = dict(payload["recommended_profiles"])
        members = [
            {"model": row["model"], "thinking_level": row["thinkingLevel"]} for row in rows
        ]
        profiles[provider] = {
            "revision": revision, "proposers": members[:-1], "aggregator": members[-1],
        }
        payload.update(
            recommended_profiles=profiles,
            selection_mode=recommended_ensemble_selection_mode_for_provider(provider),
        )
        explicit_fields = config.llm_ensemble.model_fields_set | {
            "recommended_profiles", "selection_mode",
        }
        updated_ensemble = LlmEnsembleConfig(**payload)
        # C3 retry defaults depend on whether the retry field was explicitly set.
        object.__setattr__(updated_ensemble, "__pydantic_fields_set__", explicit_fields)
        config.llm_ensemble = updated_ensemble
        paths = ["llm_ensemble.recommended_profiles", "llm_ensemble.selection_mode"]
    for path in paths:
        config.mark_force_persist(path)
    return paths


def initialize_new_recommendations(config: Any) -> None:
    """Seed a new config from already available data without waiting for network."""
    from opensquilla.provider.recommendations import get_recommendation

    provider = str(config.llm.provider).strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        return
    router = config.squilla_router
    ensemble = config.llm_ensemble
    eligible = []
    if (
        not router.recommendation_revision
        and router.preset_binding != "custom"
        and not {"tiers", "tier_profile"}.intersection(router.model_fields_set)
    ):
        eligible.append("router")
    if (
        not ensemble.recommended_profiles
        and not {"selection_mode", "candidates", "model_options"}.intersection(
            ensemble.model_fields_set,
        )
    ):
        eligible.append("ensemble")
    for kind in eligible:
        try:
            apply_recommendation(config, get_recommendation(config, provider, kind))
        except ValueError:
            # A recommendation incompatible with explicit local constraints
            # must never prevent startup. Keep the already resolved defaults.
            _log.debug("Initial recommendation incompatible with local %s settings", kind)
