"""Effective-LLM provenance resolution: which layer decided each value.

:func:`resolve_effective_llm` walks the high-value LLM routing fields of a
gateway config and reports, per dotted config path, the effective value plus
the layer that decided it (:class:`ResolvedField`). The vocabulary is:

* ``default`` — a built-in default (pydantic field default, provider-spec
  default base URL, or an engine constant such as ``DEFAULT_MAX_TOKENS``).
* ``catalog`` — model metadata (live provider catalog, models.dev snapshot,
  or the packaged static fallback table).
* ``preset`` — a router tier-profile preset value the operator did not
  override.
* ``config`` — an operator-supplied value (config file, RPC write, env).
* ``session`` — reserved for per-session overrides; no field reports it in
  this pass.

Honesty notes (documented limitations, by design):

* ``llm.provider`` / ``llm.model`` / ``llm.base_url`` use value-vs-baseline
  attribution because gateway boot materializes these fields back into the
  live model (``resolve_llm_runtime_config`` writes them), which poisons
  pydantic's ``model_fields_set``. Consequence: a value explicitly set to
  its own default is indistinguishable from the default and reports
  ``default``; an env-sourced value that matches a baseline is attributed
  to that baseline's layer.
* ``llm_ensemble.enabled`` / ``llm_ensemble.selection_mode`` use
  ``model_fields_set``: from raw persisted state a materialized default is
  indistinguishable from an explicitly written one, so any field present in
  the live model (including every field after a full config round-trip
  through ``config.set``/``config.apply``) reports ``config`` even when its
  value equals the default. Only a never-touched section reports
  ``default``.
* ``llm.max_tokens`` / ``llm.context_window`` delegate to the ModelCatalog
  ``*_with_source`` variants, which are the single implementation behind
  ``resolve_max_tokens`` / ``resolve_context_window`` — attribution
  therefore cannot drift from the real resolvers. Context-window clamping
  may adjust the max_tokens number without changing its attribution.
  ``llm.context_window`` additionally applies the global
  ``llm.context_window_tokens`` config value below the per-model
  ``[models.*]`` override via ``resolve_effective_context_window``, the
  shared implementation of the engine budgeting precedence.

Secrets never appear here by construction: the emitted paths form a literal
allowlist of non-secret field names (the only dynamic path segment is the
router tier name, and the only values emitted under it are the tier's
``provider``/``model`` ids). The ``config.effective`` RPC layer additionally
re-checks every path segment against the public redaction sets before
anything reaches the wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from opensquilla.provider.model_catalog import (
    ModelCatalog,
    resolve_effective_context_window,
)
from opensquilla.provider.preset_registry import get_preset
from opensquilla.provider.registry import UnknownProviderError, get_provider_spec

FieldSource = Literal["default", "catalog", "preset", "config", "session"]

# ModelCatalog *_with_source labels -> provenance vocabulary.
_CATALOG_SOURCE_MAP: dict[str, FieldSource] = {
    "override": "config",
    "config": "config",
    "catalog": "catalog",
    "default": "default",
}


@dataclass(frozen=True)
class ResolvedField:
    """One effective value plus the layer that decided it."""

    value: Any
    source: FieldSource


def _field_default(model: Any, name: str) -> Any:
    """Return the pydantic field default for ``name`` on ``model``'s class."""
    try:
        return type(model).model_fields[name].default
    except (AttributeError, KeyError, TypeError):
        return None


def _spec_default_base_url(provider: str) -> str:
    try:
        return get_provider_spec(provider).default_base_url
    except UnknownProviderError:
        return ""


def _tier_preset_baseline(router_cfg: Any) -> dict[str, Any]:
    """Preset tier table for the active tier profile.

    The provider-layer preset registry module (``provider/preset_registry.py``)
    has not landed on this base, so the baseline is the gateway's
    ``_router_tier_profile_defaults`` table — reached by re-running
    ``SquillaRouterConfig``'s own ``tier_profile`` validator (constructing a
    bare instance of the live config's class with only ``tier_profile`` set)
    instead of importing gateway code. That is the exact code path that
    materialized the live ``tiers`` table, so the baseline cannot drift from
    it, and the provider layer keeps its no-gateway-import invariant. Switch
    the baseline source to the preset registry when it merges.

    Returns ``{}`` when no baseline can be derived (unknown profile, or a
    non-pydantic stand-in config); callers then attribute every tier field
    to ``config``.
    """
    profile = getattr(router_cfg, "tier_profile", None)
    try:
        if profile:
            baseline = type(router_cfg)(tier_profile=profile)
        else:
            baseline = type(router_cfg)()
    except Exception:
        return {}
    tiers = getattr(baseline, "tiers", None)
    return tiers if isinstance(tiers, dict) else {}


def _tier_inline_preset_baseline(provider: str, model: str) -> dict[str, Any]:
    """Inline-applied preset ladder for ``provider``, empty models completed.

    Providers without a persistable tier_profile (e.g. tokenrhythm) get their
    preset — a curated-inline packaged ladder or a synthesized one — expanded
    inline by the gateway's default-tiers validator at load time and by
    provider saves, with empty tier model slots completed from the effective
    ``llm.model``. A live tier table matching that shape was derived, not
    operator-chosen, so it counts as a ``preset`` baseline alongside the
    tier-profile table.
    """
    if not provider:
        return {}
    try:
        preset = get_preset(provider)
    except Exception:
        return {}
    if preset is None or preset.persistable:
        return {}
    tiers = preset.tier_defaults()
    for tier in tiers.values():
        if not str(tier.get("model") or "").strip():
            tier["model"] = model
    return tiers


def _value_vs_baseline(value: Any, baselines: set[Any]) -> FieldSource:
    return "default" if value in baselines else "config"


def resolve_effective_llm(config: Any, catalog: ModelCatalog) -> dict[str, ResolvedField]:
    """Resolve the effective high-value LLM fields of ``config`` with provenance.

    See the module docstring for the source vocabulary and the documented
    attribution limitations. ``config`` is duck-typed (a ``GatewayConfig``
    in production); missing sections are skipped rather than raised on.
    """
    fields: dict[str, ResolvedField] = {}

    llm = getattr(config, "llm", None)
    provider = str(getattr(llm, "provider", "") or "").strip().lower()
    model = str(getattr(llm, "model", "") or "")

    if llm is not None:
        provider_default = str(_field_default(llm, "provider") or "").strip().lower()
        fields["llm.provider"] = ResolvedField(
            provider, _value_vs_baseline(provider, {provider_default})
        )

        fields["llm.model"] = ResolvedField(
            model, _value_vs_baseline(model, {_field_default(llm, "model")})
        )

        # Spec-default provenance: boot fills an unset base_url from the
        # provider spec (and the class default is itself the default
        # provider's spec URL), so anything matching either baseline was not
        # operator-chosen.
        base_url = str(getattr(llm, "base_url", "") or "")
        base_url_baselines = {
            baseline
            for baseline in (_field_default(llm, "base_url"), _spec_default_base_url(provider))
            if baseline
        }
        fields["llm.base_url"] = ResolvedField(
            base_url,
            "default" if not base_url else _value_vs_baseline(base_url, base_url_baselines),
        )

        # Delegated to the real resolvers (single implementation, no drift).
        try:
            user_max_tokens = int(getattr(llm, "max_tokens", 0) or 0)
        except (TypeError, ValueError):
            user_max_tokens = 0
        max_tokens, max_tokens_source = catalog.resolve_max_tokens_with_source(
            model, user_max_tokens, provider
        )
        fields["llm.max_tokens"] = ResolvedField(
            max_tokens, _CATALOG_SOURCE_MAP[max_tokens_source]
        )
        # Precedence: per-model [models.*] override > global config window >
        # catalog > default — the single shared implementation in
        # resolve_effective_context_window, mirroring the engine budgeting path.
        context_window, context_window_source = resolve_effective_context_window(
            catalog,
            model,
            provider=provider,
            global_override=getattr(llm, "context_window_tokens", 0) or 0,
        )
        fields["llm.context_window"] = ResolvedField(
            context_window, _CATALOG_SOURCE_MAP[context_window_source]
        )

    router_cfg = getattr(config, "squilla_router", None)
    tiers = getattr(router_cfg, "tiers", None)
    if isinstance(tiers, dict):
        baseline_tiers = _tier_preset_baseline(router_cfg)
        inline_preset_tiers = (
            {}
            if getattr(router_cfg, "tier_profile", None)
            else _tier_inline_preset_baseline(provider, model)
        )
        for tier_name in sorted(tiers):
            tier = tiers[tier_name]
            if not isinstance(tier, dict):
                continue
            tier_baseline = baseline_tiers.get(tier_name)
            if not isinstance(tier_baseline, dict):
                tier_baseline = {}
            tier_synth = inline_preset_tiers.get(tier_name)
            if not isinstance(tier_synth, dict):
                tier_synth = {}
            for key in ("provider", "model"):
                if key not in tier:
                    continue
                value = tier[key]
                source: FieldSource = (
                    "preset"
                    if (tier_baseline and tier_baseline.get(key) == value)
                    or (tier_synth and tier_synth.get(key) == value)
                    else "config"
                )
                fields[f"squilla_router.tiers.{tier_name}.{key}"] = ResolvedField(value, source)

    ensemble = getattr(config, "llm_ensemble", None)
    if ensemble is not None:
        explicitly_set = getattr(ensemble, "model_fields_set", None) or frozenset()
        for name in ("enabled", "selection_mode"):
            fields[f"llm_ensemble.{name}"] = ResolvedField(
                getattr(ensemble, name, None),
                "config" if name in explicitly_set else "default",
            )

    return fields
