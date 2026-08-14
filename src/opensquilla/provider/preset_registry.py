"""SquillaRouter tier presets: packaged data + synthesized coverage.

The nine legacy router-tier profiles (the "packaged" presets) ship as one
TOML file per provider under ``opensquilla/provider/presets/<id>.toml`` and are
transcribed byte-for-byte from the historical ``_router_tier_profile_defaults``
dict literals. A small set of *curated-inline* presets
(``CURATED_INLINE_PRESET_IDS``) also ship packaged TOML ladders but stay
outside the persistable set: their tiers are applied inline, never as a
``tier_profile`` id. Every other runtime-supported provider gets a
*synthesized* preset built from its onboarding default model. Synthesized
presets are registry-only view objects: they are never persisted into a
config file and are never accepted as a ``squilla_router.tier_profile`` value
(see the gateway config validator — rc1 configs brick on unknown tier_profile
ids, so the accepted set stays pinned to the legacy nine).

Loading is lazy via ``importlib.resources`` (same pattern as
``catalog_overrides.toml``); a missing/corrupt file degrades to an empty
packaged set rather than crashing import.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources

import structlog

log = structlog.get_logger(__name__)

# The nine profile ids that existed as hardcoded dict literals in
# gateway/config.py. This is the ONLY set the gateway config validator accepts
# as a persisted ``squilla_router.tier_profile``; it is a downgrade contract
# (an rc1 gateway rejects any tier_profile it does not recognize). Keep it as a
# literal so the registry's packaged set can be equality-checked against it.
LEGACY_PROVIDER_PRESET_IDS: frozenset[str] = frozenset(
    {
        "openrouter",
        "dashscope",
        "deepseek",
        "gemini",
        "volcengine",
        "byteplus",
        "openai",
        "zhipu",
        "moonshot",
    }
)

# Curated presets that ship packaged tier data but must never persist as a
# ``squilla_router.tier_profile`` id: the accepted set stays pinned to the
# legacy nine (downgrade contract), so provider saves and boot defaults apply
# these ladders as inline tiers instead.
CURATED_INLINE_PRESET_IDS: frozenset[str] = frozenset(
    {"qwen_token_plan", "tokenrhythm"}
)

_PRESETS_SUBDIR = "presets"


@dataclass(frozen=True)
class ProviderPreset:
    """One router-tier preset (packaged or synthesized).

    ``tiers`` maps a tier id (``c0``-``c3`` and optionally ``image_model``) to
    its field dict (provider/model/description/thinking_level/supports_image/
    image_only, as present). ``synthesized`` presets are registry-only: never
    persisted, never a valid ``tier_profile`` id.
    """

    preset_id: str
    provider_id: str
    label: str
    description: str
    default_model: str
    tiers: Mapping[str, dict]
    synthesized: bool = False

    @property
    def persistable(self) -> bool:
        """True when this id may persist as ``squilla_router.tier_profile``.

        Only the legacy nine qualify; curated-inline and synthesized presets
        apply as inline tiers (a persisted unknown id bricks rc1 loaders on
        downgrade).
        """

        return not self.synthesized and self.preset_id in LEGACY_PROVIDER_PRESET_IDS

    def tier_defaults(self) -> dict[str, dict]:
        """Return a deep-enough copy of the tier mapping for merging.

        Mirrors the historical ``_router_tier_profile_defaults`` contract:
        each tier is a fresh ``dict`` so callers can mutate/merge without
        touching the cached registry object.
        """

        return {name: dict(value) for name, value in self.tiers.items()}


def _tier(
    provider_id: str,
    model: str,
    description: str,
    *,
    thinking_level: str = "",
    supports_image: bool = False,
    image_only: bool = False,
) -> dict:
    entry: dict[str, object] = {
        "provider": provider_id,
        "model": model,
        "description": description,
        "supports_image": supports_image,
    }
    if thinking_level:
        entry["thinking_level"] = thinking_level
    if image_only:
        entry["image_only"] = True
    return entry


def _text_ladder(
    provider_id: str,
    c0: str,
    c1: str,
    c2: str,
    c3: str,
    *,
    subject: str,
) -> dict[str, dict]:
    return {
        "c0": _tier(provider_id, c0, f"{subject} fast route.", thinking_level="off"),
        "c1": _tier(provider_id, c1, f"{subject} balanced route.", thinking_level="low"),
        "c2": _tier(provider_id, c2, f"{subject} strong route.", thinking_level="medium"),
        "c3": _tier(provider_id, c3, f"{subject} highest route.", thinking_level="high"),
    }


def _minimax_ladder(provider_id: str) -> tuple[str, dict[str, dict]]:
    return (
        "MiniMax-M2.7",
        _text_ladder(
            provider_id,
            "MiniMax-M2.7",
            "MiniMax-M2.7",
            "MiniMax-M3",
            "MiniMax-M3",
            subject="MiniMax",
        ),
    )


_CURATED_SYNTHESIZED_PRESETS: Mapping[str, tuple[str, dict[str, dict]]] = {
    "qianfan": (
        "ernie-4.5-turbo-128k",
        {
            **_text_ladder(
                "qianfan",
                "ernie-4.5-turbo-128k",
                "ernie-4.5-turbo-128k",
                "ernie-4.5-turbo-128k",
                "ernie-4.5-turbo-128k",
                subject="Qianfan ERNIE 4.5 Turbo",
            ),
            "image_model": _tier(
                "qianfan",
                "ernie-4.5-turbo-vl-32k",
                "Qianfan vision route.",
                thinking_level="medium",
                supports_image=True,
                image_only=True,
            ),
        },
    ),
    "minimax": _minimax_ladder("minimax"),
    "minimax_coding_openai": _minimax_ladder("minimax_coding_openai"),
    "minimax_coding_anthropic": _minimax_ladder("minimax_coding_anthropic"),
    "minimax_cn": _minimax_ladder("minimax_cn"),
    "minimax_global": _minimax_ladder("minimax_global"),
    "minimax_openai": _minimax_ladder("minimax_openai"),
    "kimi_coding_openai": (
        "kimi-for-coding",
        _text_ladder(
            "kimi_coding_openai",
            "kimi-for-coding",
            "kimi-for-coding",
            "kimi-for-coding",
            "kimi-for-coding",
            subject="Kimi Coding",
        ),
    ),
    "kimi_coding_anthropic": (
        "kimi-for-coding",
        _text_ladder(
            "kimi_coding_anthropic",
            "kimi-for-coding",
            "kimi-for-coding",
            "kimi-for-coding",
            "kimi-for-coding",
            subject="Kimi Coding Anthropic-compatible",
        ),
    ),
    "mimo_openai": (
        "mimo-v2.5",
        _text_ladder(
            "mimo_openai",
            "mimo-v2.5",
            "mimo-v2.5",
            "mimo-v2.5-pro",
            "mimo-v2.5-pro",
            subject="MiMo",
        ),
    ),
    "mimo_anthropic": (
        "mimo-v2.5",
        _text_ladder(
            "mimo_anthropic",
            "mimo-v2.5",
            "mimo-v2.5",
            "mimo-v2.5-pro",
            "mimo-v2.5-pro",
            subject="MiMo Anthropic-compatible",
        ),
    ),
    "volcengine_coding_plan": (
        "doubao-seed-2.0-pro",
        _text_ladder(
            "volcengine_coding_plan",
            "doubao-seed-2.0-lite",
            "doubao-seed-2.0-pro",
            "doubao-seed-2.0-code",
            "doubao-seed-2.0-code",
            subject="Volcengine Coding Plan",
        ),
    ),
}


def _load_packaged_preset(preset_id: str) -> ProviderPreset | None:
    import tomllib

    try:
        path = resources.files("opensquilla.provider").joinpath(
            _PRESETS_SUBDIR, f"{preset_id}.toml"
        )
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a missing/corrupt preset degrades, never crashes
        log.warning("preset_registry.packaged_unavailable", preset_id=preset_id)
        return None
    meta = payload.get("preset")
    tiers = payload.get("tiers")
    if not isinstance(meta, Mapping) or not isinstance(tiers, Mapping):
        log.warning("preset_registry.packaged_malformed", preset_id=preset_id)
        return None
    normalized_tiers: dict[str, dict] = {}
    for name, fields in tiers.items():
        if isinstance(fields, Mapping):
            normalized_tiers[str(name)] = dict(fields)
    return ProviderPreset(
        preset_id=str(meta.get("id") or preset_id),
        provider_id=str(meta.get("provider") or preset_id),
        label=str(meta.get("label") or preset_id),
        description=str(meta.get("description") or ""),
        default_model=str(meta.get("default_model") or ""),
        tiers=normalized_tiers,
        synthesized=False,
    )


@cache
def _packaged_presets() -> dict[str, ProviderPreset]:
    """Lazily load the packaged presets (legacy nine + curated), keyed by id."""

    presets: dict[str, ProviderPreset] = {}
    for preset_id in sorted(LEGACY_PROVIDER_PRESET_IDS | CURATED_INLINE_PRESET_IDS):
        preset = _load_packaged_preset(preset_id)
        if preset is not None:
            presets[preset.preset_id] = preset
    return presets


def _synthesized_tiers(provider_id: str, default_model: str) -> dict[str, dict]:
    """Bind all four text tiers to ``(provider_id, default_model)``.

    Generic descriptions, no image tier — synthesized presets carry no
    curated per-tier ladder, only provider-uniform routing coverage.
    """

    ladder = {
        "c0": "fast",
        "c1": "balanced",
        "c2": "strong",
        "c3": "highest",
    }
    tiers: dict[str, dict] = {}
    for tier, role in ladder.items():
        tiers[tier] = {
            "provider": provider_id,
            "model": default_model,
            "description": (
                f"{provider_id} {role} route (synthesized default; no curated "
                f"per-tier model ladder)."
            ),
            "supports_image": False,
        }
    return tiers


def _curated_synthesized_preset(provider_id: str) -> ProviderPreset | None:
    data = _CURATED_SYNTHESIZED_PRESETS.get(provider_id)
    if data is None:
        return None
    default_model, tiers = data
    return ProviderPreset(
        preset_id=provider_id,
        provider_id=provider_id,
        label=provider_id,
        description=f"Curated inline router preset for {provider_id}.",
        default_model=default_model,
        tiers=tiers,
        synthesized=True,
    )


@cache
def _synthesized_presets() -> dict[str, ProviderPreset]:
    """Build synthesized presets for every non-legacy runtime provider."""

    from opensquilla.provider.registry import list_provider_specs

    packaged = _packaged_presets()
    presets: dict[str, ProviderPreset] = {}
    for spec in list_provider_specs():
        provider_id = spec.provider_id
        if provider_id in packaged or provider_id in LEGACY_PROVIDER_PRESET_IDS:
            continue
        if not spec.runtime_supported:
            continue
        curated = _curated_synthesized_preset(provider_id)
        if curated is not None:
            presets[provider_id] = curated
            continue
        # Onboarding's default-direct-model semantics, inlined (provider must
        # not import onboarding — architecture import contract): only curated
        # legacy profiles carry a known default model; every synthesized
        # provider's onboarding default is empty, so tiers bind to
        # (provider, "") until the operator supplies a model.
        default_model = ""
        presets[provider_id] = ProviderPreset(
            preset_id=provider_id,
            provider_id=provider_id,
            label=provider_id,
            description=f"Synthesized router preset for {provider_id}.",
            default_model=default_model,
            tiers=_synthesized_tiers(provider_id, default_model),
            synthesized=True,
        )
    return presets


def legacy_profile_ids() -> frozenset[str]:
    """Return the frozenset of accepted (persistable) tier_profile ids.

    Derived from the packaged (non-synthesized) presets and asserted equal to
    the literal legacy nine, so a packaging drift is caught rather than
    silently widening the accepted set.
    """

    packaged_ids = frozenset(
        preset_id
        for preset_id in _packaged_presets()
        if preset_id not in CURATED_INLINE_PRESET_IDS
    )
    if packaged_ids != LEGACY_PROVIDER_PRESET_IDS:
        # Packaged data drifted from the pinned legacy set. Fall back to the
        # literal so validation never widens/narrows silently.
        log.warning(
            "preset_registry.legacy_id_drift",
            packaged=sorted(packaged_ids),
            expected=sorted(LEGACY_PROVIDER_PRESET_IDS),
        )
        return LEGACY_PROVIDER_PRESET_IDS
    return packaged_ids


def get_preset(preset_id: str) -> ProviderPreset | None:
    """Return the preset for ``preset_id`` (packaged first, then synthesized)."""

    key = str(preset_id or "").strip().lower()
    if not key:
        return None
    packaged = _packaged_presets()
    if key in packaged:
        return packaged[key]
    return _synthesized_presets().get(key)


def list_presets(*, include_synthesized: bool = True) -> list[ProviderPreset]:
    """Return presets sorted by id; packaged first, synthesized optional."""

    presets = dict(_packaged_presets())
    if include_synthesized:
        for preset_id, preset in _synthesized_presets().items():
            presets.setdefault(preset_id, preset)
    return sorted(presets.values(), key=lambda p: (p.synthesized, p.preset_id))
