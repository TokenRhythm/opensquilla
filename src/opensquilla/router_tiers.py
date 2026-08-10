"""Canonical router tier identifiers, legacy aliases, and the typed tier view."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

TEXT_TIERS: tuple[str, str, str, str] = ("c0", "c1", "c2", "c3")
DEFAULT_TEXT_TIER = "c1"
HIGHEST_TEXT_TIER = "c3"
IMAGE_TIER = "image_model"
ROUTER_TIER_ENSEMBLE_SELECTION_MODE_KEY = "ensemble_selection_mode"
ROUTER_TIER_ENSEMBLE_ENABLED_KEY = "ensemble_enabled"
ROUTER_TIER_ENSEMBLE_SELECTION_MODES = frozenset(
    {
        "static_openrouter_b5",
        "static_tokenrhythm_b5",
        "custom_b5",
        "router_dynamic",
    }
)

TierProviderRole = Literal[
    "direct",
    "dormant_draft",
    "dynamic_member",
    "blocked",
]

LEGACY_TEXT_TIER_ALIASES: dict[str, str] = {
    "t0": "c0",
    "t1": "c1",
    "t2": "c2",
    "t3": "c3",
}

ROUTE_CLASS_TO_TIER: dict[str, str] = {
    "R0": "c0",
    "R1": "c1",
    "R2": "c2",
    "R3": "c3",
}
TIER_TO_ROUTE_CLASS: dict[str, str] = {tier: route for route, tier in ROUTE_CLASS_TO_TIER.items()}


def normalize_text_tier(value: object) -> str | None:
    """Return the canonical text tier id for *value*, accepting legacy t0-t3."""

    if value is None:
        return None
    tier = str(value).strip().lower()
    if not tier:
        return None
    if tier in TEXT_TIERS:
        return tier
    return LEGACY_TEXT_TIER_ALIASES.get(tier)


def normalize_tier_id(value: object) -> str | None:
    """Normalize any known tier id, preserving the image tier."""

    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw == IMAGE_TIER:
        return IMAGE_TIER
    return normalize_text_tier(raw)


def normalize_target_id(value: object) -> str:
    """Normalize router-control target ids such as tier:t3 -> tier:c3."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("tier:"):
        tier = normalize_text_tier(raw.removeprefix("tier:"))
        return f"tier:{tier}" if tier else raw
    return raw


def normalize_tier_mapping(mapping: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a copy of a tier mapping with legacy text tier keys canonicalized."""

    if not isinstance(mapping, Mapping):
        return {}
    normalized: dict[str, Any] = {}
    for key, value in mapping.items():
        tier = normalize_tier_id(key)
        out_key = tier or str(key)
        if out_key in normalized and str(key).strip().lower() not in TEXT_TIERS:
            continue
        normalized[out_key] = value
    return normalized


def tier_index(value: object) -> int:
    """Return 0-3 for known text tiers; -1 for unknown values."""

    tier = normalize_text_tier(value)
    if tier is None:
        return -1
    try:
        return TEXT_TIERS.index(tier)
    except ValueError:
        return -1


@dataclass(frozen=True)
class TierConfig:
    """Typed view over one router tier entry.

    Tier entries travel as plain dicts through config/TOML/RPC (and some
    tests pass objects); this is the one place that knows the field names
    and their normalization, so consumers stop re-implementing
    ``.get("model")``-style plumbing with divergent defaults.
    """

    provider: str = ""
    model: str = ""
    description: str = ""
    thinking_level: str | None = None
    supports_image: bool = False
    image_only: bool = False
    # New configurations only decide whether this tier uses the one shared
    # ``llm_ensemble`` plan. ``None`` preserves pre-field configs, whose
    # explicit ``ensemble_selection_mode`` remains a legacy override.
    ensemble_enabled: bool | None = None
    # Optional execution override for a router tier. A non-empty value asks
    # the runtime to wrap that tier in an already-configured Ensemble profile.
    # ``model`` remains the reversible tier draft/lineup anchor; all Ensemble
    # failures use the fixed provider and model from ``[llm]``.
    ensemble_selection_mode: str = ""

    @classmethod
    def from_value(cls, value: object) -> TierConfig:
        """Build from a tier dict or attribute-style object; tolerant of None."""

        def _get(key: str, default: object = None) -> object:
            if isinstance(value, Mapping):
                return value.get(key, default)
            return getattr(value, key, default)

        thinking = _get("thinking_level")
        ensemble_enabled = _get(ROUTER_TIER_ENSEMBLE_ENABLED_KEY)
        if ensemble_enabled is None:
            ensemble_enabled = _get("ensembleEnabled")
        ensemble_selection_mode = _get(ROUTER_TIER_ENSEMBLE_SELECTION_MODE_KEY)
        if ensemble_selection_mode in (None, ""):
            ensemble_selection_mode = _get("ensembleSelectionMode")
        return cls(
            provider=str(_get("provider") or "").strip(),
            model=str(_get("model") or "").strip(),
            description=str(_get("description") or ""),
            thinking_level=(str(thinking).strip() if thinking not in (None, "") else None),
            supports_image=bool(_get("supports_image", False)),
            image_only=bool(_get("image_only", False)),
            ensemble_enabled=(
                ensemble_enabled if isinstance(ensemble_enabled, bool) else None
            ),
            ensemble_selection_mode=str(ensemble_selection_mode or "").strip(),
        )


def tier_provider_role(
    tier: object,
    value: object,
    *,
    shared_selection_mode: str,
    router_dynamic_members_active: bool = False,
    ensemble_globally_enabled: bool = False,
) -> TierProviderRole:
    """Return the execution role owned by one stored tier provider.

    C3's shared-plan flag does not by itself make the retained provider/model
    dormant. Static and custom shared plans own their complete lineup, so the
    C3 deployment is only a single-model draft. A globally enabled static or
    custom plan likewise owns physical execution for every routed request;
    Router rows remain logical decisions, but their stored provider/model
    values are drafts until the global plan is disabled. An active
    ``router_dynamic`` plan instead derives members from every text Router
    tier, so those rows are execution dependencies. Legacy tier-local plans
    and ordinary tiers remain direct dependencies. An absent or unknown
    shared plan is blocked; treating it as dormant would hide the configuration
    error, while treating it as direct would accidentally execute a draft deployment.
    """

    normalized_tier = normalize_text_tier(tier)
    config = TierConfig.from_value(value)
    selection_mode = str(shared_selection_mode or "").strip()
    if normalized_tier is None:
        # Image and extension tiers are selected directly for their capability;
        # they never become members or drafts of a text Ensemble plan.
        return "direct"
    if router_dynamic_members_active and normalized_tier in TEXT_TIERS:
        return "dynamic_member"
    if config.ensemble_enabled is None and config.ensemble_selection_mode:
        # A retained pre-boolean mode still owns its routed turn. Dynamic
        # membership was handled above; fixed legacy profiles keep this tier's
        # deployment as a direct plan anchor/credential source.
        return "direct"
    if (
        ensemble_globally_enabled
        and selection_mode
        in {
            "static_openrouter_b5",
            "static_tokenrhythm_b5",
            "custom_b5",
        }
    ):
        return "dormant_draft"
    if (
        normalized_tier != HIGHEST_TEXT_TIER
        or config.ensemble_enabled is not True
    ):
        return "direct"

    if selection_mode == "router_dynamic":
        return "dynamic_member"
    if selection_mode in {
        "static_openrouter_b5",
        "static_tokenrhythm_b5",
        "custom_b5",
    }:
        return "dormant_draft"
    return "blocked"


def router_tier_provider_roles(
    tiers: Mapping[str, Any] | None,
    *,
    shared_selection_mode: str,
    ensemble_globally_enabled: bool = False,
) -> dict[str, TierProviderRole]:
    """Return canonical tier ids mapped to their provider ownership roles."""

    normalized = normalize_tier_mapping(tiers)
    dynamic_members_active = router_dynamic_tier_members_active(
        normalized,
        shared_selection_mode=shared_selection_mode,
        ensemble_globally_enabled=ensemble_globally_enabled,
    )
    return {
        tier: tier_provider_role(
            tier,
            value,
            shared_selection_mode=shared_selection_mode,
            router_dynamic_members_active=dynamic_members_active,
            ensemble_globally_enabled=ensemble_globally_enabled,
        )
        for tier, value in normalized.items()
    }


def router_dynamic_tier_members_active(
    tiers: Mapping[str, Any] | None,
    *,
    shared_selection_mode: str,
    ensemble_globally_enabled: bool = False,
) -> bool:
    """Whether the current effective plan consumes Router text tiers as members."""

    normalized = normalize_tier_mapping(tiers)
    c3 = TierConfig.from_value(normalized.get(HIGHEST_TEXT_TIER))
    selection_mode = str(shared_selection_mode or "").strip()
    for tier in TEXT_TIERS:
        config = TierConfig.from_value(normalized.get(tier))
        if (
            config.ensemble_enabled is None
            and config.ensemble_selection_mode == "router_dynamic"
        ):
            return True
    if ensemble_globally_enabled:
        return selection_mode == "router_dynamic"
    if selection_mode == "router_dynamic" and c3.ensemble_enabled is True:
        return True
    return False


def tier_provider_is_dormant(
    tier: object,
    value: object,
    *,
    shared_selection_mode: str = "",
) -> bool:
    """Whether a stored provider is only the inactive C3 single-model draft.

    ``shared_selection_mode`` is optional for source compatibility, but an
    omitted mode is intentionally treated as blocked rather than dormant.
    Callers deciding execution readiness must pass the effective shared plan.
    """

    return (
        tier_provider_role(
            tier,
            value,
            shared_selection_mode=shared_selection_mode,
        )
        == "dormant_draft"
    )


def tier_ensemble_selection_mode(
    tiers: Mapping[str, Any] | None,
    tier: object,
) -> str:
    """Return the configured Ensemble profile for one canonical text tier."""

    if not isinstance(tiers, Mapping):
        return ""
    tier_name = normalize_text_tier(tier)
    if tier_name is None:
        return ""
    return TierConfig.from_value(tiers.get(tier_name)).ensemble_selection_mode


def configured_tier_ensemble_selection_modes(
    tiers: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Return text tiers that explicitly select an Ensemble profile."""

    return {
        tier: selection_mode
        for tier in TEXT_TIERS
        if (selection_mode := tier_ensemble_selection_mode(tiers, tier))
    }


def tier_ensemble_execution(
    tiers: Mapping[str, Any] | None,
    tier: object,
    *,
    shared_selection_mode: str,
) -> tuple[str, str]:
    """Resolve one tier to ``(selection_mode, binding)``.

    ``binding`` is ``shared`` for the new boolean contract, ``legacy`` for a
    pre-field explicit mode, and ``single`` when no tier-scoped fusion should
    run. An explicit false wins over a retained legacy value so switching back
    to one model cannot be undone by preset merging or downgrade metadata.
    """

    if not isinstance(tiers, Mapping):
        return "", "single"
    tier_name = normalize_text_tier(tier)
    if tier_name is None:
        return "", "single"
    config = TierConfig.from_value(tiers.get(tier_name))
    if config.ensemble_enabled is True:
        return str(shared_selection_mode or "").strip(), "shared"
    if config.ensemble_enabled is False:
        return "", "single"
    if config.ensemble_selection_mode:
        return config.ensemble_selection_mode, "legacy"
    return "", "single"


def tier_ensemble_active(
    tiers: Mapping[str, Any] | None,
    tier: object,
) -> bool:
    """Whether one tier is configured to use any multi-model plan.

    This intentionally answers the configuration question without resolving
    the shared plan. It is used by capability routing before the provider
    wrapper is built, so C3 cannot advertise image support while its fusion
    choice is active.
    """

    if not isinstance(tiers, Mapping):
        return False
    tier_name = normalize_text_tier(tier)
    if tier_name is None:
        return False
    config = TierConfig.from_value(tiers.get(tier_name))
    if config.ensemble_enabled is not None:
        return config.ensemble_enabled
    return bool(config.ensemble_selection_mode)


def effective_tier_ensemble_selection_modes(
    tiers: Mapping[str, Any] | None,
    *,
    shared_selection_mode: str,
) -> dict[str, str]:
    """Return every tier that effectively activates an Ensemble plan."""

    resolved: dict[str, str] = {}
    for tier in TEXT_TIERS:
        selection_mode, _binding = tier_ensemble_execution(
            tiers,
            tier,
            shared_selection_mode=shared_selection_mode,
        )
        if selection_mode:
            resolved[tier] = selection_mode
    return resolved
